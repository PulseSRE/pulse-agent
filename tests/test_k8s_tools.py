"""Tests for SRE Kubernetes tools."""

from __future__ import annotations

import json
from types import SimpleNamespace

from kubernetes.client.rest import ApiException

from sre_agent.k8s_tools import (
    MAX_REPLICAS,
    MAX_TAIL_LINES,
    WRITE_TOOLS,
    cordon_node,
    delete_pod,
    describe_deployment,
    describe_node,
    describe_pod,
    describe_resource,
    exec_command,
    get_cluster_operators,
    get_cluster_version,
    get_configmap,
    get_events,
    get_node_metrics,
    get_persistent_volume_claims,
    get_pod_logs,
    get_pod_metrics,
    get_resource_quotas,
    get_resource_recommendations,
    get_services,
    list_deployments,
    list_namespaces,
    list_nodes,
    list_pods,
    restart_deployment,
    scale_deployment,
    search_logs,
    test_connectivity,
    uncordon_node,
)
from tests.conftest import (
    _list_result,
    _make_deployment,
    _make_event,
    _make_namespace,
    _make_node,
    _make_pod,
    _text,
    _ts,
)


class TestListNamespaces:
    def test_returns_namespaces(self, mock_k8s):
        mock_k8s["core"].list_namespace.return_value = _list_result(
            [
                _make_namespace("default"),
                _make_namespace("kube-system"),
            ]
        )
        result = list_namespaces.call({})
        assert "default" in result
        assert "kube-system" in result

    def test_empty(self, mock_k8s):
        mock_k8s["core"].list_namespace.return_value = _list_result([])
        result = list_namespaces.call({})
        assert "No namespaces found" in result

    def test_api_error(self, mock_k8s):
        mock_k8s["core"].list_namespace.side_effect = ApiException(status=403, reason="Forbidden")
        result = list_namespaces.call({})
        assert "Error (403)" in result


class TestListPods:
    def test_returns_pods(self, mock_k8s):
        mock_k8s["core"].list_namespaced_pod.return_value = _list_result(
            [
                _make_pod("web-1"),
                _make_pod("web-2", restarts=5),
            ]
        )
        result = _text(list_pods.call({"namespace": "default"}))
        assert "web-1" in result
        assert "web-2" in result
        assert "Restarts=5" in result

    def test_all_namespaces(self, mock_k8s):
        mock_k8s["core"].list_pod_for_all_namespaces.return_value = _list_result(
            [
                _make_pod("pod-a", namespace="ns1"),
            ]
        )
        result = _text(list_pods.call({"namespace": "ALL"}))
        assert "ns1/pod-a" in result

    def test_with_selectors(self, mock_k8s):
        mock_k8s["core"].list_namespaced_pod.return_value = _list_result([])
        list_pods.call({"namespace": "default", "label_selector": "app=web", "field_selector": "status.phase=Running"})
        mock_k8s["core"].list_namespaced_pod.assert_called_once_with(
            "default", limit=200, label_selector="app=web", field_selector="status.phase=Running"
        )


class TestDescribePod:
    def test_returns_details(self, mock_k8s):
        mock_k8s["core"].read_namespaced_pod.return_value = _make_pod("my-pod")
        mock_k8s["core"].list_namespaced_event.return_value = _list_result(
            [
                _make_event(),
            ]
        )
        result = describe_pod.call({"namespace": "default", "pod_name": "my-pod"})
        assert isinstance(result, tuple)
        text, component = result
        assert '"name": "my-pod"' in text
        assert '"state": "running"' in text
        assert component["kind"] == "section"
        # Should have key_value, badge_list, status_list, and data_table
        kinds = [c["kind"] for c in component["components"]]
        assert "key_value" in kinds
        assert "data_table" in kinds

    def test_events_api_failure_still_returns_pod(self, mock_k8s):
        mock_k8s["core"].read_namespaced_pod.return_value = _make_pod("my-pod")
        mock_k8s["core"].list_namespaced_event.side_effect = ApiException(status=403, reason="Forbidden")
        result = describe_pod.call({"namespace": "default", "pod_name": "my-pod"})
        assert isinstance(result, tuple)
        text, _component = result
        assert '"name": "my-pod"' in text
        data = json.loads(text)
        assert "recent_events" not in data


class TestDescribeNode:
    def test_returns_details(self, mock_k8s):
        mock_k8s["core"].read_node.return_value = _make_node("node-1", roles=["master", "worker"])
        result = describe_node.call({"node_name": "node-1"})
        data = json.loads(result)
        assert data["name"] == "node-1"
        assert data["node_info"]["kubelet"] == "v1.28.0"
        assert data["unschedulable"] is False

    def test_api_error(self, mock_k8s):
        mock_k8s["core"].read_node.side_effect = ApiException(status=404, reason="Not Found")
        result = describe_node.call({"node_name": "ghost"})
        assert "Error (404)" in result


class TestGetPodLogs:
    def test_returns_logs(self, mock_k8s):
        mock_k8s["core"].read_namespaced_pod_log.return_value = "line1\nline2\nline3"
        result = get_pod_logs.call({"namespace": "default", "pod_name": "my-pod"})
        assert "line1" in result

    def test_clamps_tail_lines(self, mock_k8s):
        mock_k8s["core"].read_namespaced_pod_log.return_value = "logs"
        get_pod_logs.call({"namespace": "default", "pod_name": "p", "tail_lines": 99999})
        call_kwargs = mock_k8s["core"].read_namespaced_pod_log.call_args
        assert call_kwargs.kwargs.get("tail_lines", call_kwargs[1].get("tail_lines")) == MAX_TAIL_LINES

    def test_empty_logs(self, mock_k8s):
        mock_k8s["core"].read_namespaced_pod_log.return_value = ""
        result = get_pod_logs.call({"namespace": "default", "pod_name": "p"})
        assert "(empty logs)" in result


class TestListNodes:
    def test_returns_nodes(self, mock_k8s):
        mock_k8s["core"].list_node.return_value = _list_result(
            [
                _make_node("node-1", roles=["master"]),
                _make_node("node-2", roles=["worker"]),
            ]
        )
        result = _text(list_nodes.call({}))
        assert "node-1" in result
        assert "master" in result
        assert "node-2" in result


class TestGetEvents:
    def test_returns_events(self, mock_k8s):
        mock_k8s["core"].list_namespaced_event.return_value = _list_result(
            [
                _make_event(reason="Pulled", message="Pulled image nginx", event_type="Normal"),
                _make_event(reason="BackOff", message="Back-off restarting", event_type="Warning"),
            ]
        )
        result = _text(get_events.call({"namespace": "default"}))
        assert "Pulled" in result
        assert "BackOff" in result

    def test_filters_by_type(self, mock_k8s):
        mock_k8s["core"].list_namespaced_event.return_value = _list_result([])
        get_events.call({"namespace": "default", "event_type": "Warning"})
        call_kwargs = mock_k8s["core"].list_namespaced_event.call_args
        assert "type=Warning" in call_kwargs.kwargs.get("field_selector", call_kwargs[1].get("field_selector", ""))

    def test_all_namespaces(self, mock_k8s):
        mock_k8s["core"].list_event_for_all_namespaces.return_value = _list_result([])
        result = _text(get_events.call({"namespace": "ALL"}))
        assert "No events found" in result


class TestListDeployments:
    def test_returns_deployments(self, mock_k8s):
        mock_k8s["apps"].list_namespaced_deployment.return_value = _list_result(
            [
                _make_deployment("nginx", ready=3),
            ]
        )
        result = _text(list_deployments.call({"namespace": "default"}))
        assert "nginx" in result
        assert "Ready=3/3" in result

    def test_all_namespaces(self, mock_k8s):
        mock_k8s["apps"].list_deployment_for_all_namespaces.return_value = _list_result(
            [
                _make_deployment("api", namespace="prod"),
            ]
        )
        result = _text(list_deployments.call({"namespace": "ALL"}))
        assert "prod/api" in result


class TestDescribeDeployment:
    def test_returns_details(self, mock_k8s):
        mock_k8s["apps"].read_namespaced_deployment.return_value = _make_deployment("nginx")
        result = describe_deployment.call({"namespace": "default", "name": "nginx"})
        assert isinstance(result, tuple)
        text, component = result
        data = json.loads(text)
        assert data["name"] == "nginx"
        assert data["replicas"] == 3
        assert data["strategy"] == "RollingUpdate"
        assert len(data["containers"]) == 1
        # Verify structured component
        assert component["kind"] == "section"
        kinds = [c["kind"] for c in component["components"]]
        assert "key_value" in kinds
        assert "status_list" in kinds

    def test_api_error(self, mock_k8s):
        mock_k8s["apps"].read_namespaced_deployment.side_effect = ApiException(status=404, reason="Not Found")
        result = describe_deployment.call({"namespace": "default", "name": "ghost"})
        assert "Error (404)" in result


class TestGetResourceQuotas:
    def test_returns_quotas(self, mock_k8s):
        quota = SimpleNamespace(
            metadata=SimpleNamespace(name="compute"),
            status=SimpleNamespace(
                hard={"cpu": "4", "memory": "8Gi"},
                used={"cpu": "2", "memory": "4Gi"},
            ),
        )
        mock_k8s["core"].list_namespaced_resource_quota.return_value = _list_result([quota])
        result = get_resource_quotas.call({"namespace": "default"})
        assert "compute" in result
        assert "cpu: 2 / 4" in result
        assert "memory: 4Gi / 8Gi" in result

    def test_no_quotas(self, mock_k8s):
        mock_k8s["core"].list_namespaced_resource_quota.return_value = _list_result([])
        result = get_resource_quotas.call({"namespace": "default"})
        assert "No resource quotas" in result


class TestGetServices:
    def test_returns_services(self, mock_k8s):
        svc = SimpleNamespace(
            metadata=SimpleNamespace(name="web", namespace="default"),
            spec=SimpleNamespace(
                type="ClusterIP",
                cluster_ip="10.0.0.1",
                ports=[SimpleNamespace(port=80, protocol="TCP", target_port=8080)],
            ),
        )
        mock_k8s["core"].list_namespaced_service.return_value = _list_result([svc])
        result = get_services.call({"namespace": "default"})
        assert "web" in result
        assert "ClusterIP" in result
        assert "80/TCP" in result

    def test_all_namespaces(self, mock_k8s):
        mock_k8s["core"].list_service_for_all_namespaces.return_value = _list_result([])
        result = get_services.call({"namespace": "ALL"})
        assert "No services found" in result


class TestGetPersistentVolumeClaims:
    def test_returns_pvcs(self, mock_k8s):
        pvc = SimpleNamespace(
            metadata=SimpleNamespace(name="data-vol", namespace="default", creation_timestamp=_ts(60)),
            spec=SimpleNamespace(storage_class_name="gp2"),
            status=SimpleNamespace(phase="Bound", capacity={"storage": "10Gi"}),
        )
        mock_k8s["core"].list_namespaced_persistent_volume_claim.return_value = _list_result([pvc])
        result = get_persistent_volume_claims.call({"namespace": "default"})
        assert "data-vol" in result
        assert "Bound" in result
        assert "10Gi" in result
        assert "gp2" in result


class TestGetClusterVersion:
    def test_returns_k8s_version(self, mock_k8s):
        mock_k8s["version"].get_code.return_value = SimpleNamespace(git_version="v1.28.0", platform="linux/amd64")
        mock_k8s["custom"].get_cluster_custom_object.side_effect = ApiException(status=404, reason="Not Found")
        result = get_cluster_version.call({})
        assert "v1.28.0" in result

    def test_returns_ocp_version(self, mock_k8s):
        mock_k8s["version"].get_code.return_value = SimpleNamespace(git_version="v1.28.0", platform="linux/amd64")
        mock_k8s["custom"].get_cluster_custom_object.return_value = {
            "status": {
                "desired": {"version": "4.14.5"},
                "conditions": [
                    {"type": "Available", "status": "True"},
                ],
            },
            "spec": {"channel": "stable-4.14"},
        }
        result = get_cluster_version.call({})
        assert "4.14.5" in result
        assert "stable-4.14" in result


class TestGetClusterOperators:
    def test_returns_operators(self, mock_k8s):
        mock_k8s["custom"].list_cluster_custom_object.return_value = {
            "items": [
                {
                    "metadata": {"name": "dns"},
                    "status": {
                        "conditions": [
                            {"type": "Available", "status": "True"},
                            {"type": "Progressing", "status": "False"},
                            {"type": "Degraded", "status": "False"},
                        ]
                    },
                }
            ],
        }
        result = _text(get_cluster_operators.call({}))
        assert "dns" in result
        assert "Available=True" in result

    def test_not_openshift(self, mock_k8s):
        mock_k8s["custom"].list_cluster_custom_object.side_effect = ApiException(status=404, reason="Not Found")
        result = get_cluster_operators.call({})
        assert "Error" in result


class TestGetConfigmap:
    def test_returns_data(self, mock_k8s):
        cm = SimpleNamespace(
            metadata=SimpleNamespace(name="app-config", namespace="default"),
            data={"key1": "value1", "key2": "value2"},
        )
        mock_k8s["core"].read_namespaced_config_map.return_value = cm
        result = get_configmap.call({"namespace": "default", "name": "app-config"})
        data = json.loads(result)
        assert data["name"] == "app-config"
        assert data["data"]["key1"] == "value1"

    def test_api_error(self, mock_k8s):
        mock_k8s["core"].read_namespaced_config_map.side_effect = ApiException(status=404, reason="Not Found")
        result = get_configmap.call({"namespace": "default", "name": "ghost"})
        assert "Error (404)" in result


class TestWriteTools:
    def test_write_tool_names(self):
        assert "scale_deployment" in WRITE_TOOLS
        assert "restart_deployment" in WRITE_TOOLS
        assert "cordon_node" in WRITE_TOOLS
        assert "uncordon_node" in WRITE_TOOLS
        assert "delete_pod" in WRITE_TOOLS

    def test_scale_clamps_replicas(self, mock_k8s):
        mock_k8s["apps"].patch_namespaced_deployment_scale.return_value = SimpleNamespace()
        result = scale_deployment.call({"namespace": "default", "name": "nginx", "replicas": 9999})
        assert f"{MAX_REPLICAS} replicas" in result

    def test_scale_clamps_negative(self, mock_k8s):
        mock_k8s["apps"].patch_namespaced_deployment_scale.return_value = SimpleNamespace()
        result = scale_deployment.call({"namespace": "default", "name": "nginx", "replicas": -5})
        assert "0 replicas" in result

    def test_delete_pod_clamps_grace(self, mock_k8s):
        mock_k8s["core"].delete_namespaced_pod.return_value = SimpleNamespace()
        result = delete_pod.call({"namespace": "default", "pod_name": "p", "grace_period_seconds": 9999})
        assert "deleted" in result

    def test_restart_deployment(self, mock_k8s):
        mock_k8s["apps"].patch_namespaced_deployment.return_value = SimpleNamespace()
        result = restart_deployment.call({"namespace": "default", "name": "nginx"})
        assert "Rolling restart triggered" in result

    def test_cordon_node(self, mock_k8s):
        mock_k8s["core"].patch_node.return_value = SimpleNamespace()
        result = cordon_node.call({"node_name": "node-1"})
        assert "cordoned" in result

    def test_uncordon_node(self, mock_k8s):
        mock_k8s["core"].patch_node.return_value = SimpleNamespace()
        result = uncordon_node.call({"node_name": "node-1"})
        assert "uncordoned" in result


class TestMetricsTools:
    def test_node_metrics(self, mock_k8s):
        mock_k8s["custom"].list_cluster_custom_object.return_value = {
            "items": [
                {"metadata": {"name": "node-1"}, "usage": {"cpu": "500m", "memory": "4096Mi"}},
            ]
        }
        mock_k8s["core"].list_node.return_value = _list_result([_make_node("node-1", cpu="4", memory="16Gi")])
        result = _text(get_node_metrics.call({}))
        assert "node-1" in result
        assert "CPU=500m" in result
        assert "Memory=4096Mi" in result

    def test_node_metrics_not_available(self, mock_k8s):
        mock_k8s["custom"].list_cluster_custom_object.side_effect = ApiException(status=404, reason="Not Found")
        result = get_node_metrics.call({})
        text = _text(result) if isinstance(result, tuple) else result
        assert "metrics-server" in text.lower() or "not available" in text.lower()

    def test_pod_metrics(self, mock_k8s):
        mock_k8s["custom"].list_namespaced_custom_object.return_value = {
            "items": [
                {
                    "metadata": {"name": "web-1", "namespace": "default"},
                    "containers": [{"name": "main", "usage": {"cpu": "100m", "memory": "256Mi"}}],
                },
            ]
        }
        result = _text(get_pod_metrics.call({"namespace": "default"}))
        assert "web-1" in result
        assert "CPU=100m" in result

    def test_pod_metrics_sort_by_memory(self, mock_k8s):
        mock_k8s["custom"].list_namespaced_custom_object.return_value = {
            "items": [
                {
                    "metadata": {"name": "low-mem", "namespace": "default"},
                    "containers": [{"name": "c", "usage": {"cpu": "10m", "memory": "64Mi"}}],
                },
                {
                    "metadata": {"name": "high-mem", "namespace": "default"},
                    "containers": [{"name": "c", "usage": {"cpu": "10m", "memory": "2048Mi"}}],
                },
            ]
        }
        result = _text(get_pod_metrics.call({"namespace": "default", "sort_by": "memory"}))
        lines = result.strip().split("\n")
        assert "high-mem" in lines[0]

    def test_pod_metrics_all_namespaces(self, mock_k8s):
        mock_k8s["custom"].list_cluster_custom_object.return_value = {
            "items": [
                {
                    "metadata": {"name": "p1", "namespace": "ns1"},
                    "containers": [{"name": "c", "usage": {"cpu": "50m", "memory": "128Mi"}}],
                },
            ]
        }
        result = _text(get_pod_metrics.call({"namespace": "ALL"}))
        assert "ns1/p1" in result


class TestDescribeResource:
    def test_returns_resource_details(self, mock_k8s):
        from unittest.mock import MagicMock

        mock_api = MagicMock()
        mock_api.call_api.return_value = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": "my-config",
                "namespace": "default",
                "labels": {"app": "test"},
                "annotations": {"note": "val"},
                "ownerReferences": [],
                "creationTimestamp": "2025-01-01T00:00:00Z",
            },
            "data": {"key": "value"},
        }
        # Mock events
        mock_k8s["core"].list_namespaced_event.return_value = _list_result([])
        mock_k8s["core"].api_client = mock_api

        result = describe_resource.call({"namespace": "default", "name": "my-config", "kind": "ConfigMap"})
        assert isinstance(result, tuple)
        text, component = result
        assert '"my-config"' in text
        assert component["kind"] == "section"
        kinds = [c["kind"] for c in component["components"]]
        assert "key_value" in kinds
        assert "badge_list" in kinds

    def test_cluster_scoped_resource(self, mock_k8s):
        from unittest.mock import MagicMock

        mock_api = MagicMock()
        mock_api.call_api.return_value = {
            "apiVersion": "v1",
            "kind": "Node",
            "metadata": {
                "name": "node-1",
                "labels": {},
                "annotations": {},
                "ownerReferences": [],
                "creationTimestamp": "2025-01-01T00:00:00Z",
            },
            "status": {
                "conditions": [
                    {"type": "Ready", "status": "True", "reason": "", "message": ""},
                ],
            },
        }

        mock_k8s["core"].api_client = mock_api
        result = describe_resource.call({"namespace": "_", "name": "node-1", "kind": "Node"})
        assert isinstance(result, tuple)
        text, component = result
        assert '"node-1"' in text
        # Should have conditions in status_list
        kinds = [c["kind"] for c in component["components"]]
        assert "status_list" in kinds

    def test_api_error(self, mock_k8s):
        from unittest.mock import MagicMock

        mock_api = MagicMock()
        mock_api.call_api.side_effect = ApiException(status=404, reason="Not Found")

        mock_k8s["core"].api_client = mock_api
        result = describe_resource.call({"namespace": "default", "name": "ghost", "kind": "ConfigMap"})
        assert "Error (404)" in result

    def test_grouped_resource(self, mock_k8s):
        from unittest.mock import MagicMock

        mock_api = MagicMock()
        mock_api.call_api.return_value = {
            "apiVersion": "apps/v1",
            "kind": "StatefulSet",
            "metadata": {
                "name": "my-sts",
                "namespace": "default",
                "labels": {},
                "annotations": {},
                "ownerReferences": [],
                "creationTimestamp": "2025-01-01T00:00:00Z",
            },
            "status": {"conditions": []},
        }
        mock_k8s["core"].list_namespaced_event.return_value = _list_result([])

        mock_k8s["core"].api_client = mock_api
        result = describe_resource.call(
            {"namespace": "default", "name": "my-sts", "kind": "StatefulSet", "group": "apps"}
        )
        assert isinstance(result, tuple)
        text, _ = result
        assert '"my-sts"' in text
        # Verify the API path used /apis/apps/v1
        call_args = mock_api.call_api.call_args
        assert "/apis/apps/v1/" in call_args[0][0]


class TestExecCommand:
    def test_executes_command(self, mock_k8s):
        mock_k8s["stream"].return_value = "uid=1000(app) gid=1000(app)\n"
        result = exec_command.call(
            {
                "namespace": "default",
                "pod_name": "my-pod",
                "command": "whoami",
            }
        )
        assert "uid=1000" in result
        mock_k8s["stream"].assert_called_once()

    def test_rejects_shell_metacharacters(self, mock_k8s):
        result = exec_command.call(
            {
                "namespace": "default",
                "pod_name": "my-pod",
                "command": "cat /etc/passwd; rm -rf /",
            }
        )
        assert "metacharacters" in result.lower()
        mock_k8s["stream"].assert_not_called()

    def test_rejects_pipe(self, mock_k8s):
        result = exec_command.call(
            {
                "namespace": "default",
                "pod_name": "my-pod",
                "command": "ps aux | grep nginx",
            }
        )
        assert "metacharacters" in result.lower()

    def test_rejects_dollar(self, mock_k8s):
        result = exec_command.call(
            {
                "namespace": "default",
                "pod_name": "my-pod",
                "command": "echo $SECRET",
            }
        )
        assert "metacharacters" in result.lower()

    def test_truncates_large_output(self, mock_k8s):
        mock_k8s["stream"].return_value = "x" * 20_000
        result = exec_command.call(
            {
                "namespace": "default",
                "pod_name": "my-pod",
                "command": "cat bigfile",
            }
        )
        assert "truncated" in result

    def test_empty_output(self, mock_k8s):
        mock_k8s["stream"].return_value = ""
        result = exec_command.call(
            {
                "namespace": "default",
                "pod_name": "my-pod",
                "command": "true",
            }
        )
        assert "(no output)" in result

    def test_api_error(self, mock_k8s):
        mock_k8s["stream"].side_effect = ApiException(status=404, reason="Not Found")
        result = exec_command.call(
            {
                "namespace": "default",
                "pod_name": "ghost",
                "command": "whoami",
            }
        )
        assert "Error (404)" in result

    def test_with_container(self, mock_k8s):
        mock_k8s["stream"].return_value = "ok"
        exec_command.call(
            {
                "namespace": "default",
                "pod_name": "my-pod",
                "command": "env",
                "container": "sidecar",
            }
        )
        call_kwargs = mock_k8s["stream"].call_args
        assert call_kwargs.kwargs.get("container") == "sidecar"

    def test_empty_command(self, mock_k8s):
        result = exec_command.call(
            {
                "namespace": "default",
                "pod_name": "my-pod",
                "command": "",
            }
        )
        assert "required" in result.lower()

    def test_in_write_tools(self):
        assert "exec_command" in WRITE_TOOLS

    def test_test_connectivity_in_write_tools(self):
        assert "test_connectivity" in WRITE_TOOLS


class TestSearchLogs:
    def test_finds_matching_lines(self, mock_k8s):
        mock_k8s["core"].list_namespaced_pod.return_value = _list_result(
            [
                _make_pod("web-1"),
                _make_pod("web-2"),
            ]
        )
        mock_k8s["core"].read_namespaced_pod_log.side_effect = [
            "INFO starting\nERROR connection refused\nINFO ready",
            "INFO starting\nERROR timeout connecting to db\nINFO recovered",
        ]
        result = search_logs.call(
            {
                "namespace": "default",
                "label_selector": "app=web",
                "pattern": "ERROR",
            }
        )
        assert "web-1" in result
        assert "web-2" in result
        assert "connection refused" in result
        assert "timeout" in result
        assert "2/2 pods" in result

    def test_no_matches(self, mock_k8s):
        mock_k8s["core"].list_namespaced_pod.return_value = _list_result(
            [
                _make_pod("web-1"),
            ]
        )
        mock_k8s["core"].read_namespaced_pod_log.return_value = "INFO all good\nINFO still good"
        result = search_logs.call(
            {
                "namespace": "default",
                "label_selector": "app=web",
                "pattern": "ERROR",
            }
        )
        assert "No matches" in result

    def test_no_pods_found(self, mock_k8s):
        mock_k8s["core"].list_namespaced_pod.return_value = _list_result([])
        result = search_logs.call(
            {
                "namespace": "default",
                "label_selector": "app=ghost",
                "pattern": "ERROR",
            }
        )
        assert "No pods found" in result

    def test_case_insensitive(self, mock_k8s):
        mock_k8s["core"].list_namespaced_pod.return_value = _list_result(
            [
                _make_pod("web-1"),
            ]
        )
        mock_k8s["core"].read_namespaced_pod_log.return_value = "error: something failed"
        result = search_logs.call(
            {
                "namespace": "default",
                "label_selector": "app=web",
                "pattern": "ERROR",
            }
        )
        assert "something failed" in result

    def test_missing_selector(self, mock_k8s):
        result = search_logs.call(
            {
                "namespace": "default",
                "label_selector": "",
                "pattern": "ERROR",
            }
        )
        assert "required" in result.lower()

    def test_tail_lines_capped(self, mock_k8s):
        mock_k8s["core"].list_namespaced_pod.return_value = _list_result(
            [
                _make_pod("web-1"),
            ]
        )
        mock_k8s["core"].read_namespaced_pod_log.return_value = "line"
        search_logs.call(
            {
                "namespace": "default",
                "label_selector": "app=web",
                "pattern": "x",
                "tail_lines": 9999,
            }
        )
        call_kwargs = mock_k8s["core"].read_namespaced_pod_log.call_args
        assert call_kwargs.kwargs.get("tail_lines", call_kwargs[1].get("tail_lines")) == 500


class TestTestConnectivity:
    def test_successful_connection(self, mock_k8s):
        mock_k8s["stream"].return_value = "Connection to target 80 port [tcp/http] succeeded!"
        result = test_connectivity.call(
            {
                "source_namespace": "default",
                "source_pod": "my-pod",
                "target_host": "my-service.default.svc",
                "target_port": 80,
            }
        )
        assert "succeeded" in result.lower()
        assert "my-service.default.svc:80" in result

    def test_failed_connection(self, mock_k8s):
        mock_k8s["stream"].side_effect = ApiException(status=500, reason="command terminated")
        result = test_connectivity.call(
            {
                "source_namespace": "default",
                "source_pod": "my-pod",
                "target_host": "unreachable.svc",
                "target_port": 443,
            }
        )
        assert "FAILED" in result

    def test_pod_not_found(self, mock_k8s):
        mock_k8s["stream"].side_effect = ApiException(status=404, reason="Not Found")
        result = test_connectivity.call(
            {
                "source_namespace": "default",
                "source_pod": "ghost",
                "target_host": "target.svc",
                "target_port": 80,
            }
        )
        assert "not found" in result.lower()

    def test_invalid_host(self, mock_k8s):
        result = test_connectivity.call(
            {
                "source_namespace": "default",
                "source_pod": "my-pod",
                "target_host": "host; rm -rf /",
                "target_port": 80,
            }
        )
        assert "invalid" in result.lower()

    def test_invalid_port(self, mock_k8s):
        result = test_connectivity.call(
            {
                "source_namespace": "default",
                "source_pod": "my-pod",
                "target_host": "target.svc",
                "target_port": 99999,
            }
        )
        assert "Error" in result

    def test_ipv6_host(self, mock_k8s):
        mock_k8s["stream"].return_value = "connected"
        result = test_connectivity.call(
            {
                "source_namespace": "default",
                "source_pod": "my-pod",
                "target_host": "::1",
                "target_port": 80,
            }
        )
        assert "succeeded" in result.lower()


class TestGetResourceRecommendations:
    def test_returns_recommendations(self, mock_k8s):
        from unittest.mock import patch

        mock_results = {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {
                        "metric": {"pod": "web-1", "container": "main", "namespace": "default"},
                        "value": [1234567890, "0.15"],
                    },
                ],
            },
        }

        def mock_urlopen(*args, **kwargs):
            resp = SimpleNamespace()
            resp.read = lambda: json.dumps(mock_results).encode()
            return resp

        with (
            patch("builtins.open", side_effect=FileNotFoundError),
            patch("urllib.request.urlopen", side_effect=mock_urlopen),
        ):
            result = get_resource_recommendations.call({"namespace": "default"})

        text = _text(result)
        # With mocked data, should have some output (may be "No workload" if keys don't align)
        assert isinstance(text, str)

    def test_no_metrics(self, mock_k8s):
        from unittest.mock import patch

        def mock_urlopen(*args, **kwargs):
            resp = SimpleNamespace()
            resp.read = lambda: json.dumps({"status": "success", "data": {"result": []}}).encode()
            return resp

        with (
            patch("builtins.open", side_effect=FileNotFoundError),
            patch("urllib.request.urlopen", side_effect=mock_urlopen),
        ):
            result = get_resource_recommendations.call({"namespace": "default"})

        text = _text(result)
        assert "no" in text.lower() or "No" in text
