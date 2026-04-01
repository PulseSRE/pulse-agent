"""Tests for audit log scanners."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from sre_agent.audit_scanner import (
    scan_config_changes,
    scan_rbac_changes,
    scan_recent_deployments,
    scan_warning_events,
)


def _ts(minutes_ago: int = 5) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes_ago)


class TestScanConfigChanges:
    def test_no_findings_when_no_recent_changes(self):
        cm = SimpleNamespace(
            metadata=SimpleNamespace(
                name="old-config",
                namespace="prod",
                managed_fields=[
                    SimpleNamespace(time=_ts(120), operation="Update", manager="kubectl"),
                ],
            ),
        )
        with (
            patch("sre_agent.audit_scanner.get_core_client") as core,
            patch("sre_agent.audit_scanner.get_apps_client"),
        ):
            core.return_value.list_config_map_for_all_namespaces.return_value = SimpleNamespace(items=[cm])
            findings = scan_config_changes()
        assert len(findings) == 0

    def test_finding_when_config_change_precedes_crash(self):
        recent_time = _ts(5)
        cm = SimpleNamespace(
            metadata=SimpleNamespace(
                name="app-config",
                namespace="prod",
                managed_fields=[
                    SimpleNamespace(time=recent_time, operation="Update", manager="helm"),
                ],
            ),
        )
        event = SimpleNamespace(
            reason="CrashLoopBackOff",
            last_timestamp=_ts(3),
            metadata=SimpleNamespace(creation_timestamp=_ts(3), namespace="prod"),
            involved_object=SimpleNamespace(name="web-pod-abc", kind="Pod"),
        )
        with (
            patch("sre_agent.audit_scanner.get_core_client") as core,
            patch("sre_agent.audit_scanner.get_apps_client"),
        ):
            core.return_value.list_config_map_for_all_namespaces.return_value = SimpleNamespace(items=[cm])
            core.return_value.list_event_for_all_namespaces.return_value = SimpleNamespace(items=[event])
            findings = scan_config_changes()
        assert len(findings) == 1
        assert findings[0]["category"] == "audit_config"
        assert "app-config" in findings[0]["title"]

    def test_skips_system_namespaces(self):
        cm = SimpleNamespace(
            metadata=SimpleNamespace(
                name="kube-config",
                namespace="kube-system",
                managed_fields=[
                    SimpleNamespace(time=_ts(2), operation="Update", manager="system"),
                ],
            ),
        )
        with (
            patch("sre_agent.audit_scanner.get_core_client") as core,
            patch("sre_agent.audit_scanner.get_apps_client"),
        ):
            core.return_value.list_config_map_for_all_namespaces.return_value = SimpleNamespace(items=[cm])
            findings = scan_config_changes()
        assert len(findings) == 0


class TestScanRbacChanges:
    def test_detects_new_cluster_admin_binding(self):
        crb = SimpleNamespace(
            metadata=SimpleNamespace(
                name="suspicious-admin",
                creation_timestamp=_ts(2),
                managed_fields=[SimpleNamespace(manager="kubectl", operation="Update", time=_ts(2))],
            ),
            role_ref=SimpleNamespace(name="cluster-admin", kind="ClusterRole"),
            subjects=[SimpleNamespace(kind="ServiceAccount", name="jenkins")],
        )
        with patch("sre_agent.audit_scanner.get_rbac_client") as rbac:
            rbac.return_value.list_cluster_role_binding.return_value = SimpleNamespace(items=[crb])
            rbac.return_value.list_role_binding_for_all_namespaces.return_value = SimpleNamespace(items=[])
            findings = scan_rbac_changes()
        assert len(findings) == 1
        assert findings[0]["severity"] == "critical"
        assert "cluster-admin" in findings[0]["title"]

    def test_ignores_system_bindings(self):
        crb = SimpleNamespace(
            metadata=SimpleNamespace(
                name="system:admin",
                creation_timestamp=_ts(2),
            ),
            role_ref=SimpleNamespace(name="cluster-admin", kind="ClusterRole"),
            subjects=[],
        )
        with patch("sre_agent.audit_scanner.get_rbac_client") as rbac:
            rbac.return_value.list_cluster_role_binding.return_value = SimpleNamespace(items=[crb])
            rbac.return_value.list_role_binding_for_all_namespaces.return_value = SimpleNamespace(items=[])
            findings = scan_rbac_changes()
        assert len(findings) == 0

    def test_ignores_old_bindings(self):
        crb = SimpleNamespace(
            metadata=SimpleNamespace(
                name="old-admin",
                creation_timestamp=_ts(60 * 48),  # 2 days ago
            ),
            role_ref=SimpleNamespace(name="cluster-admin", kind="ClusterRole"),
            subjects=[],
        )
        with patch("sre_agent.audit_scanner.get_rbac_client") as rbac:
            rbac.return_value.list_cluster_role_binding.return_value = SimpleNamespace(items=[crb])
            rbac.return_value.list_role_binding_for_all_namespaces.return_value = SimpleNamespace(items=[])
            findings = scan_rbac_changes()
        assert len(findings) == 0


class TestScanRecentDeployments:
    def test_detects_failing_rollout(self):
        dep = SimpleNamespace(
            metadata=SimpleNamespace(
                name="api-server",
                namespace="prod",
                annotations={"deployment.kubernetes.io/revision": "5"},
            ),
            spec=SimpleNamespace(
                replicas=3,
                template=SimpleNamespace(
                    metadata=SimpleNamespace(annotations={}),
                ),
            ),
            status=SimpleNamespace(
                available_replicas=1,
                unavailable_replicas=2,
                conditions=[
                    SimpleNamespace(
                        type="Progressing",
                        status="True",
                        last_transition_time=_ts(5),
                    ),
                ],
            ),
        )
        with (
            patch("sre_agent.audit_scanner.get_apps_client") as apps,
            patch("sre_agent.audit_scanner.get_core_client") as core,
        ):
            apps.return_value.list_deployment_for_all_namespaces.return_value = SimpleNamespace(items=[dep])
            core.return_value.list_event_for_all_namespaces.return_value = SimpleNamespace(items=[])
            findings = scan_recent_deployments()
        assert len(findings) == 1
        assert findings[0]["category"] == "audit_deployment"
        assert "api-server" in findings[0]["title"]


class TestScanWarningEvents:
    def test_detects_high_frequency_events(self):
        events = [
            SimpleNamespace(
                metadata=SimpleNamespace(namespace="prod"),
                reason="BackOff",
                count=20,
                involved_object=SimpleNamespace(kind="Pod", name=f"web-{i}"),
            )
            for i in range(3)
        ]
        with patch("sre_agent.audit_scanner.get_core_client") as core:
            core.return_value.list_event_for_all_namespaces.return_value = SimpleNamespace(items=events)
            findings = scan_warning_events()
        assert len(findings) == 1
        assert "BackOff" in findings[0]["title"]
        assert "60x" in findings[0]["title"]

    def test_no_findings_below_threshold(self):
        events = [
            SimpleNamespace(
                metadata=SimpleNamespace(namespace="prod"),
                reason="Pulled",
                count=2,
                involved_object=SimpleNamespace(kind="Pod", name="web-1"),
            ),
        ]
        with patch("sre_agent.audit_scanner.get_core_client") as core:
            core.return_value.list_event_for_all_namespaces.return_value = SimpleNamespace(items=events)
            findings = scan_warning_events()
        assert len(findings) == 0

    def test_skips_system_namespaces(self):
        events = [
            SimpleNamespace(
                metadata=SimpleNamespace(namespace="openshift-monitoring"),
                reason="BackOff",
                count=100,
                involved_object=SimpleNamespace(kind="Pod", name="prom-1"),
            ),
        ]
        with patch("sre_agent.audit_scanner.get_core_client") as core:
            core.return_value.list_event_for_all_namespaces.return_value = SimpleNamespace(items=events)
            findings = scan_warning_events()
        assert len(findings) == 0


class TestScanAuthEvents:
    def test_detects_kubeadmin(self):
        from sre_agent.audit_scanner import scan_auth_events

        with (
            patch("sre_agent.audit_scanner.get_core_client") as core,
            patch("sre_agent.audit_scanner.get_custom_client") as custom,
        ):
            custom.return_value.list_cluster_custom_object.return_value = {
                "items": [{"metadata": {"name": "kubeadmin"}}]
            }
            core.return_value.list_namespaced_event.return_value = SimpleNamespace(items=[])
            core.return_value.list_secret_for_all_namespaces.return_value = SimpleNamespace(items=[])
            findings = scan_auth_events()

        kubeadmin_findings = [f for f in findings if "kubeadmin" in f["title"]]
        assert len(kubeadmin_findings) == 1
        assert kubeadmin_findings[0]["severity"] == "warning"

    def test_no_kubeadmin_no_finding(self):
        from sre_agent.audit_scanner import scan_auth_events

        with (
            patch("sre_agent.audit_scanner.get_core_client") as core,
            patch("sre_agent.audit_scanner.get_custom_client") as custom,
        ):
            custom.return_value.list_cluster_custom_object.return_value = {
                "items": [{"metadata": {"name": "admin-user"}}]
            }
            core.return_value.list_namespaced_event.return_value = SimpleNamespace(items=[])
            core.return_value.list_secret_for_all_namespaces.return_value = SimpleNamespace(items=[])
            findings = scan_auth_events()

        kubeadmin_findings = [f for f in findings if "kubeadmin" in f.get("title", "")]
        assert len(kubeadmin_findings) == 0

    def test_detects_auth_failures(self):
        from sre_agent.audit_scanner import scan_auth_events

        events = [
            SimpleNamespace(reason="AuthFailed", message="login denied for user X", metadata=SimpleNamespace())
            for _ in range(6)
        ]
        with (
            patch("sre_agent.audit_scanner.get_core_client") as core,
            patch("sre_agent.audit_scanner.get_custom_client") as custom,
        ):
            custom.return_value.list_cluster_custom_object.return_value = {"items": []}
            core.return_value.list_namespaced_event.return_value = SimpleNamespace(items=events)
            core.return_value.list_secret_for_all_namespaces.return_value = SimpleNamespace(items=[])
            findings = scan_auth_events()

        auth_findings = [f for f in findings if "Authentication failures" in f.get("title", "")]
        assert len(auth_findings) == 1

    def test_detects_namespace_role_binding(self):
        from sre_agent.audit_scanner import scan_rbac_changes

        rb = SimpleNamespace(
            metadata=SimpleNamespace(
                name="dev-admin-binding",
                namespace="staging",
                creation_timestamp=_ts(2),
            ),
            role_ref=SimpleNamespace(name="admin", kind="ClusterRole"),
            subjects=[SimpleNamespace(kind="User", name="dev-user")],
        )
        with patch("sre_agent.audit_scanner.get_rbac_client") as rbac:
            rbac.return_value.list_cluster_role_binding.return_value = SimpleNamespace(items=[])
            rbac.return_value.list_role_binding_for_all_namespaces.return_value = SimpleNamespace(items=[rb])
            findings = scan_rbac_changes()
        assert len(findings) == 1
        assert findings[0]["severity"] == "warning"
        assert "admin" in findings[0]["title"]
        assert "staging" in findings[0]["title"]
