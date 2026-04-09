"""Tests for security scanning tools."""

from __future__ import annotations

from types import SimpleNamespace

from kubernetes.client.rest import ApiException

from sre_agent.k8s_tools import WRITE_TOOLS
from sre_agent.security_agent import ALL_TOOLS as SEC_ALL_TOOLS
from sre_agent.security_tools import (
    get_security_summary,
    list_service_account_secrets,
    scan_images,
    scan_network_policies,
    scan_pod_security,
    scan_rbac_risks,
    scan_scc_usage,
    scan_sccs,
    scan_secrets,
)
from tests.conftest import _list_result, _make_namespace, _make_pod, _ts


class TestScanPodSecurity:
    def test_detects_privileged(self, mock_security_k8s):
        pod = _make_pod("bad-pod", privileged=True, run_as_non_root=False)
        mock_security_k8s["core"].list_pod_for_all_namespaces.return_value = _list_result([pod])
        result = scan_pod_security.call({"namespace": "ALL"})
        assert "PRIVILEGED" in result
        assert "bad-pod" in result

    def test_no_issues(self, mock_security_k8s):
        pod = _make_pod("good-pod", privileged=False, run_as_non_root=True)
        # Ensure security context has everything set properly
        sc = pod.spec.containers[0].security_context
        sc.allow_privilege_escalation = False
        sc.read_only_root_filesystem = True
        sc.capabilities = SimpleNamespace(add=None, drop=["ALL"])
        sc.run_as_non_root = True
        mock_security_k8s["core"].list_pod_for_all_namespaces.return_value = _list_result([pod])
        result = scan_pod_security.call({"namespace": "ALL"})
        assert "No pod security issues" in result

    def test_detects_host_network(self, mock_security_k8s):
        pod = _make_pod("net-pod", host_network=True)
        mock_security_k8s["core"].list_pod_for_all_namespaces.return_value = _list_result([pod])
        result = scan_pod_security.call({"namespace": "ALL"})
        assert "hostNetwork" in result

    def test_api_error(self, mock_security_k8s):
        mock_security_k8s["core"].list_pod_for_all_namespaces.side_effect = ApiException(status=403, reason="Forbidden")
        result = scan_pod_security.call({"namespace": "ALL"})
        assert "Error (403)" in result


class TestScanImages:
    def test_flags_latest_tag(self, mock_security_k8s):
        pod = _make_pod("img-pod", image="docker.io/nginx:latest")
        mock_security_k8s["core"].list_pod_for_all_namespaces.return_value = _list_result([pod])
        result = scan_images.call({"namespace": "ALL"})
        assert "latest" in result
        assert "untrusted registry" in result

    def test_trusted_registry_no_latest(self, mock_security_k8s):
        pod = _make_pod("good-img", image="registry.redhat.io/ubi9@sha256:abc123")
        mock_security_k8s["core"].list_pod_for_all_namespaces.return_value = _list_result([pod])
        result = scan_images.call({"namespace": "ALL"})
        assert "No image policy violations" in result


class TestScanRbacRisks:
    def test_detects_cluster_admin(self, mock_security_k8s):
        crb = SimpleNamespace(
            metadata=SimpleNamespace(name="admin-binding"),
            role_ref=SimpleNamespace(name="cluster-admin"),
            subjects=[SimpleNamespace(kind="User", name="bad-user", namespace=None)],
        )
        mock_security_k8s["rbac"].list_cluster_role_binding.return_value = _list_result([crb])
        # Need cluster roles too
        cr = SimpleNamespace(
            metadata=SimpleNamespace(name="basic-role"),
            rules=[SimpleNamespace(verbs=["get"], resources=["pods"], api_groups=[""])],
        )
        mock_security_k8s["rbac"].list_cluster_role.return_value = _list_result([cr])
        result = scan_rbac_risks.call({})
        assert "cluster-admin" in result
        assert "bad-user" in result

    def test_skips_system_accounts(self, mock_security_k8s):
        crb = SimpleNamespace(
            metadata=SimpleNamespace(name="system-binding"),
            role_ref=SimpleNamespace(name="cluster-admin"),
            subjects=[SimpleNamespace(kind="User", name="system:admin", namespace=None)],
        )
        mock_security_k8s["rbac"].list_cluster_role_binding.return_value = _list_result([crb])
        mock_security_k8s["rbac"].list_cluster_role.return_value = _list_result([])
        result = scan_rbac_risks.call({})
        assert "system:admin" not in result

    def test_detects_wildcard_permissions(self, mock_security_k8s):
        mock_security_k8s["rbac"].list_cluster_role_binding.return_value = _list_result([])
        cr = SimpleNamespace(
            metadata=SimpleNamespace(name="wildcard-role"),
            rules=[SimpleNamespace(verbs=["*"], resources=["*"], api_groups=["*"])],
        )
        mock_security_k8s["rbac"].list_cluster_role.return_value = _list_result([cr])
        result = scan_rbac_risks.call({})
        assert "wildcard" in result.lower()


class TestScanNetworkPolicies:
    def test_finds_unprotected_namespaces(self, mock_security_k8s):
        mock_security_k8s["core"].list_namespace.return_value = _list_result(
            [
                _make_namespace("my-app"),
                _make_namespace("default"),
            ]
        )
        mock_security_k8s["networking"].list_network_policy_for_all_namespaces.return_value = _list_result([])
        result = scan_network_policies.call({"namespace": "ALL"})
        assert "my-app" in result
        assert "NO network policies" in result


class TestScanSccs:
    def test_detects_privileged_scc(self, mock_security_k8s):
        mock_security_k8s["custom"].list_cluster_custom_object.return_value = {
            "items": [
                {
                    "metadata": {"name": "privileged"},
                    "allowPrivilegedContainer": True,
                    "allowHostNetwork": True,
                    "allowHostPID": False,
                    "allowHostIPC": False,
                    "allowHostPorts": False,
                    "allowHostDirVolumePlugin": False,
                    "runAsUser": {"type": "RunAsAny"},
                    "seLinuxContext": {"type": "RunAsAny"},
                    "volumes": ["*"],
                    "users": ["system:admin"],
                    "groups": [],
                }
            ],
        }
        result = scan_sccs.call({})
        assert "PRIVILEGED" in result
        assert "hostNetwork" in result
        assert "[HIGH]" in result


class TestScanSccUsage:
    def test_detects_risky_scc(self, mock_security_k8s):
        pod = _make_pod("risky-pod")
        pod.metadata.annotations = {"openshift.io/scc": "privileged"}
        mock_security_k8s["core"].list_pod_for_all_namespaces.return_value = _list_result([pod])
        result = scan_scc_usage.call({"namespace": "ALL"})
        assert "privileged" in result
        assert "risky" in result.lower()


class TestScanSecrets:
    def test_detects_old_secrets(self, mock_security_k8s):
        old_secret = SimpleNamespace(
            metadata=SimpleNamespace(
                name="old-key",
                namespace="default",
                creation_timestamp=_ts(60 * 24 * 120),  # 120 days ago
            ),
            type="Opaque",
            data={},
        )
        mock_security_k8s["core"].list_secret_for_all_namespaces.return_value = _list_result([old_secret])
        mock_security_k8s["core"].list_pod_for_all_namespaces.return_value = _list_result([])
        result = scan_secrets.call({"namespace": "ALL"})
        assert "old-key" in result
        assert "90 days" in result


class TestListServiceAccountSecrets:
    def test_returns_accounts(self, mock_security_k8s):
        sa = SimpleNamespace(
            metadata=SimpleNamespace(name="default", namespace="myns"),
            automount_service_account_token=None,
            secrets=[SimpleNamespace(name="default-token-abc")],
        )
        mock_security_k8s["core"].list_namespaced_service_account.return_value = _list_result([sa])
        result = list_service_account_secrets.call({"namespace": "myns"})
        assert "myns/default" in result
        assert "secrets=1" in result

    def test_all_namespaces(self, mock_security_k8s):
        sa = SimpleNamespace(
            metadata=SimpleNamespace(name="runner", namespace="ci"),
            automount_service_account_token=True,
            secrets=[],
        )
        mock_security_k8s["core"].list_service_account_for_all_namespaces.return_value = _list_result([sa])
        result = list_service_account_secrets.call({"namespace": "ALL"})
        assert "ci/runner" in result
        assert "automountToken=True" in result


class TestScanImagesCustomRegistries:
    def test_custom_trusted_registries(self, mock_security_k8s, monkeypatch):
        monkeypatch.setenv("PULSE_AGENT_TRUSTED_REGISTRIES", "gcr.io/my-project/,docker.io/library/")
        from sre_agent.config import _reset_settings

        _reset_settings()
        pod = _make_pod("gcr-pod", image="gcr.io/my-project/app:v1.0")
        mock_security_k8s["core"].list_pod_for_all_namespaces.return_value = _list_result([pod])
        result = scan_images.call({"namespace": "ALL"})
        assert "untrusted registry" not in result


class TestGetSecuritySummary:
    def test_returns_summary(self, mock_security_k8s):
        pod = _make_pod("test", privileged=True)
        pod.spec.containers[0].security_context = None
        mock_security_k8s["core"].list_pod_for_all_namespaces.return_value = _list_result([pod])
        mock_security_k8s["core"].list_namespace.return_value = _list_result([_make_namespace("app")])
        mock_security_k8s["networking"].list_network_policy_for_all_namespaces.return_value = _list_result([])
        mock_security_k8s["rbac"].list_cluster_role_binding.return_value = _list_result([])
        result = get_security_summary.call({})
        assert "total_pods" in result
        assert "containers_no_security_context" in result


class TestSecurityAgentToolList:
    def test_excludes_write_tools(self):
        tool_names = {t.name for t in SEC_ALL_TOOLS}
        assert tool_names & WRITE_TOOLS == set(), (
            f"Security agent should not have write tools: {tool_names & WRITE_TOOLS}"
        )

    def test_includes_security_tools(self):
        tool_names = {t.name for t in SEC_ALL_TOOLS}
        expected = {
            "scan_pod_security",
            "scan_images",
            "scan_rbac_risks",
            "scan_network_policies",
            "scan_sccs",
            "scan_scc_usage",
            "scan_secrets",
            "get_security_summary",
            "list_service_account_secrets",
        }
        assert expected <= tool_names, f"Missing security tools: {expected - tool_names}"

    def test_includes_sre_read_tools(self):
        tool_names = {t.name for t in SEC_ALL_TOOLS}
        expected_reads = {"list_pods", "describe_pod", "get_pod_logs", "get_events"}
        assert expected_reads <= tool_names, f"Missing SRE read tools: {expected_reads - tool_names}"
