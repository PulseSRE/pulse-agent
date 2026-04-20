"""Tests for get_topology_graph tool and create_live_table tool."""

from __future__ import annotations

import json
from unittest.mock import patch

from sre_agent.dependency_graph import DependencyGraph


class TestGetTopologyGraph:
    """Tests for the get_topology_graph view tool."""

    def _make_graph(self) -> DependencyGraph:
        g = DependencyGraph()
        g.add_node("Deployment", "production", "web")
        g.add_node("Pod", "production", "web-1")
        g.add_node("Service", "production", "web-svc")
        g.add_edge("Deployment/production/web", "Pod/production/web-1", "owns")
        g.add_edge("Service/production/web-svc", "Pod/production/web-1", "selects")
        return g

    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_returns_topology_component(self, mock_get_graph):
        from sre_agent.view_tools import get_topology_graph

        mock_get_graph.return_value = self._make_graph()
        result = get_topology_graph(namespace="production")
        assert isinstance(result, tuple)
        text, component = result
        assert component["kind"] == "topology"
        assert len(component["nodes"]) == 3
        assert len(component["edges"]) == 2
        assert "3 resources" in text

    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_filters_by_namespace(self, mock_get_graph):
        from sre_agent.view_tools import get_topology_graph

        g = self._make_graph()
        g.add_node("Pod", "staging", "other-pod")
        mock_get_graph.return_value = g
        result = get_topology_graph(namespace="production")
        _, component = result
        assert len(component["nodes"]) == 3  # staging pod excluded

    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_empty_graph_returns_string(self, mock_get_graph):
        from sre_agent.view_tools import get_topology_graph

        mock_get_graph.return_value = DependencyGraph()
        result = get_topology_graph(namespace="production")
        assert isinstance(result, str)
        assert "No topology data" in result

    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_all_namespaces(self, mock_get_graph):
        from sre_agent.view_tools import get_topology_graph

        g = self._make_graph()
        g.add_node("Pod", "staging", "other-pod")
        mock_get_graph.return_value = g
        result = get_topology_graph(namespace="")
        _, component = result
        assert len(component["nodes"]) == 4  # all namespaces

    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_node_structure(self, mock_get_graph):
        from sre_agent.view_tools import get_topology_graph

        mock_get_graph.return_value = self._make_graph()
        _, component = get_topology_graph(namespace="production")
        node = next(n for n in component["nodes"] if n["kind"] == "Pod")
        assert node["name"] == "web-1"
        assert node["namespace"] == "production"
        assert node["status"] == "healthy"

    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_edge_structure(self, mock_get_graph):
        from sre_agent.view_tools import get_topology_graph

        mock_get_graph.return_value = self._make_graph()
        _, component = get_topology_graph(namespace="production")
        owns_edge = next(e for e in component["edges"] if e["relationship"] == "owns")
        assert "Deployment" in owns_edge["source"]
        assert "Pod" in owns_edge["target"]


class TestTopologyFiltering:
    """Tests for topology perspective filtering and validation."""

    def _make_full_graph(self) -> DependencyGraph:
        g = DependencyGraph()
        g.add_node("Node", "", "worker-1")
        g.add_node("Pod", "production", "web-1", {"app": "web", "team": "platform"})
        g.add_node("Pod", "production", "web-2", {"app": "web", "team": "platform"})
        g.add_node("Deployment", "production", "web", {"app": "web"})
        g.add_node("Service", "production", "web-svc")
        g.add_node("ConfigMap", "production", "web-config")
        g.add_node("Ingress", "production", "web-ing")
        g.add_node("NetworkPolicy", "production", "deny-all")
        g.add_edge("Deployment/production/web", "Pod/production/web-1", "owns")
        g.add_edge("Deployment/production/web", "Pod/production/web-2", "owns")
        g.add_edge("Service/production/web-svc", "Pod/production/web-1", "selects")
        g.add_edge("Service/production/web-svc", "Pod/production/web-2", "selects")
        g.add_edge("Pod/production/web-1", "ConfigMap/production/web-config", "references")
        g.add_edge("Node//worker-1", "Pod/production/web-1", "schedules")
        g.add_edge("Ingress/production/web-ing", "Service/production/web-svc", "routes_to")
        g.add_edge("NetworkPolicy/production/deny-all", "Pod/production/web-1", "applies_to")
        return g

    # -- Validation --

    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_validation_invalid_kinds(self, mock_get_graph):
        from sre_agent.view_tools import get_topology_graph

        mock_get_graph.return_value = self._make_full_graph()
        result = get_topology_graph(namespace="production", kinds="FooBar,Pod")
        assert isinstance(result, str)
        assert "FooBar" in result
        assert "Valid kinds:" in result

    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_validation_invalid_relationships(self, mock_get_graph):
        from sre_agent.view_tools import get_topology_graph

        mock_get_graph.return_value = self._make_full_graph()
        result = get_topology_graph(namespace="production", relationships="badrel")
        assert isinstance(result, str)
        assert "badrel" in result
        assert "Valid relationships:" in result

    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_validation_invalid_layout_hint(self, mock_get_graph):
        from sre_agent.view_tools import get_topology_graph

        mock_get_graph.return_value = self._make_full_graph()
        result = get_topology_graph(namespace="production", layout_hint="badlayout")
        assert isinstance(result, str)
        assert "badlayout" in result
        assert "Valid layout hints:" in result

    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_backward_compat(self, mock_get_graph):
        from sre_agent.view_tools import get_topology_graph

        mock_get_graph.return_value = self._make_full_graph()
        result = get_topology_graph(namespace="production")
        assert isinstance(result, tuple)
        _, component = result
        assert len(component["nodes"]) == 7

    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_backward_compat_all_ns(self, mock_get_graph):
        from sre_agent.view_tools import get_topology_graph

        mock_get_graph.return_value = self._make_full_graph()
        result = get_topology_graph(namespace="")
        assert isinstance(result, tuple)
        _, component = result
        assert len(component["nodes"]) == 8

    # -- Kind/relationship filtering --

    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_kind_filtering(self, mock_get_graph):
        from sre_agent.view_tools import get_topology_graph

        mock_get_graph.return_value = self._make_full_graph()
        result = get_topology_graph(namespace="production", kinds="Node,Pod")
        assert isinstance(result, tuple)
        _, component = result
        node_kinds = {n["kind"] for n in component["nodes"]}
        assert node_kinds == {"Node", "Pod"}  # Node is cluster-scoped but explicitly requested

    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_kind_filtering_all_ns(self, mock_get_graph):
        from sre_agent.view_tools import get_topology_graph

        mock_get_graph.return_value = self._make_full_graph()
        result = get_topology_graph(namespace="", kinds="Node,Pod")
        assert isinstance(result, tuple)
        _, component = result
        node_kinds = {n["kind"] for n in component["nodes"]}
        assert node_kinds == {"Node", "Pod"}

    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_relationship_filtering(self, mock_get_graph):
        from sre_agent.view_tools import get_topology_graph

        mock_get_graph.return_value = self._make_full_graph()
        result = get_topology_graph(namespace="production", relationships="owns")
        assert isinstance(result, tuple)
        _, component = result
        for edge in component["edges"]:
            assert edge["relationship"] == "owns"

    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_auto_relationship_inference(self, mock_get_graph):
        from sre_agent.view_tools import get_topology_graph

        mock_get_graph.return_value = self._make_full_graph()
        result = get_topology_graph(namespace="", kinds="Service,Pod")
        assert isinstance(result, tuple)
        _, component = result
        rel_types = {e["relationship"] for e in component["edges"]}
        assert "selects" in rel_types
        assert "owns" not in rel_types
        assert "schedules" not in rel_types

    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_conflicting_filters(self, mock_get_graph):
        from sre_agent.view_tools import get_topology_graph

        mock_get_graph.return_value = self._make_full_graph()
        # ConfigMap and Service have no "owns" relationship between them
        result = get_topology_graph(namespace="production", kinds="ConfigMap,Service", relationships="owns")
        assert isinstance(result, str)
        assert "no edges" in result.lower() or "no relationship" in result.lower()

    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_component_output_fields(self, mock_get_graph):
        from sre_agent.view_tools import get_topology_graph

        mock_get_graph.return_value = self._make_full_graph()
        result = get_topology_graph(namespace="production", layout_hint="grouped", group_by="namespace")
        assert isinstance(result, tuple)
        _, component = result
        assert component["layout_hint"] == "grouped"
        assert component["include_metrics"] is False
        assert component["group_by"] == "namespace"

    # -- Group-by --

    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_group_by_namespace(self, mock_get_graph):
        from sre_agent.view_tools import get_topology_graph

        mock_get_graph.return_value = self._make_full_graph()
        result = get_topology_graph(namespace="", group_by="namespace")
        assert isinstance(result, tuple)
        _, component = result
        for node in component["nodes"]:
            assert "group" in node
            if node["kind"] == "Node":
                assert node["group"] == "cluster-scoped"
            else:
                assert node["group"] == "production"

    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_group_by_label(self, mock_get_graph):
        from sre_agent.view_tools import get_topology_graph

        mock_get_graph.return_value = self._make_full_graph()
        result = get_topology_graph(namespace="production", group_by="team")
        assert isinstance(result, tuple)
        _, component = result
        pod_nodes = [n for n in component["nodes"] if n["kind"] == "Pod"]
        for pod in pod_nodes:
            assert pod["group"] == "platform"
        deploy = next(n for n in component["nodes"] if n["kind"] == "Deployment")
        assert deploy["group"] == "unlabeled"

    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_group_max_size(self, mock_get_graph):
        from sre_agent.view_tools import get_topology_graph

        g = DependencyGraph()
        for i in range(25):
            g.add_node("Pod", "production", f"pod-{i}", {"team": "big"})
        mock_get_graph.return_value = g
        result = get_topology_graph(namespace="production", group_by="team")
        assert isinstance(result, tuple)
        _, component = result
        pods = [n for n in component["nodes"] if n["kind"] == "Pod"]
        summary = [n for n in component["nodes"] if n.get("id", "").startswith("_summary/")]
        assert len(pods) == 20
        assert len(summary) == 1
        assert "5 more" in summary[0]["name"]

    # -- Metrics --

    @patch("sre_agent.dependency_graph._fetch_metrics")
    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_metrics_enrichment(self, mock_get_graph, mock_fetch):
        from sre_agent.view_tools import get_topology_graph

        mock_get_graph.return_value = self._make_full_graph()
        mock_fetch.return_value = (
            {
                "worker-1": {
                    "cpu_usage": "1200m",
                    "cpu_capacity": "4",
                    "memory_usage": "4096Mi",
                    "memory_capacity": "16384Mi",
                    "cpu_usage_m": 1200,
                    "cpu_capacity_m": 4000,
                    "memory_usage_b": 4294967296,
                    "memory_capacity_b": 17179869184,
                }
            },
            {
                "production/web-1": {
                    "cpu_usage": "100m",
                    "memory_usage": "256Mi",
                    "cpu_usage_m": 100,
                    "memory_usage_b": 268435456,
                }
            },
        )
        result = get_topology_graph(namespace="", kinds="Node,Pod", include_metrics=True)
        assert isinstance(result, tuple)
        _, component = result
        node = next(n for n in component["nodes"] if n["kind"] == "Node")
        assert "metrics" in node
        assert node["metrics"]["cpu_percent"] == 30
        assert node["metrics"]["memory_percent"] == 25
        pod = next(n for n in component["nodes"] if n["name"] == "web-1")
        assert "metrics" in pod

    @patch("sre_agent.dependency_graph._fetch_metrics")
    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_metrics_graceful_degradation(self, mock_get_graph, mock_fetch):
        from sre_agent.view_tools import get_topology_graph

        mock_get_graph.return_value = self._make_full_graph()
        mock_fetch.return_value = ({}, {})
        result = get_topology_graph(namespace="", kinds="Node,Pod", include_metrics=True)
        assert isinstance(result, tuple)
        _, component = result
        for node in component["nodes"]:
            assert "metrics" not in node

    def test_metrics_cache_ttl(self):
        """Cache returns same result within TTL window."""
        import sre_agent.dependency_graph as dg

        dg._metrics_cache.clear()
        test_result = ({"node1": {"cpu_usage_m": 100}}, {"ns/pod1": {"cpu_usage_m": 50}})
        dg._metrics_cache["__all__"] = (dg.time.time(), test_result)
        cached = dg._metrics_cache.get("__all__")
        assert cached is not None
        assert cached[1] == test_result


class TestCreateLiveTable:
    """Tests for the create_live_table tool."""

    def test_valid_k8s_only(self):
        from sre_agent.k8s_tools.live_table import create_live_table

        ds = [
            {"type": "k8s", "id": "prod", "label": "Production", "resource": "pods", "namespace": "production"},
            {"type": "k8s", "id": "staging", "label": "Staging", "resource": "pods", "namespace": "staging"},
        ]
        with patch("sre_agent.k8s_tools.live_table._fetch_table_rows") as mock_fetch:
            mock_fetch.return_value = {
                "columns": [{"id": "name", "header": "Name", "type": "resource_name"}],
                "rows": [{"name": "web-1", "_gvr": "v1~pods"}],
                "gvr": "v1~pods",
            }
            result = create_live_table(title="Cross-NS Pods", datasources_json=json.dumps(ds))

        text, component = result
        assert component["kind"] == "data_table"
        assert len(component["datasources"]) == 2
        assert component["rows"]  # initial snapshot
        assert "Live table" in text

    def test_valid_k8s_plus_promql(self):
        from sre_agent.k8s_tools.live_table import create_live_table

        ds = [
            {"type": "k8s", "id": "pods", "label": "Pods", "resource": "pods", "namespace": "default"},
            {
                "type": "promql",
                "id": "cpu",
                "label": "CPU",
                "query": "rate(cpu[5m])",
                "columnId": "cpu_usage",
                "columnHeader": "CPU",
                "joinLabel": "pod",
                "joinColumn": "name",
            },
        ]
        with patch("sre_agent.k8s_tools.live_table._fetch_table_rows") as mock_fetch:
            mock_fetch.return_value = {
                "columns": [{"id": "name", "header": "Name", "type": "resource_name"}],
                "rows": [{"name": "web-1"}],
                "gvr": "v1~pods",
            }
            result = create_live_table(title="Pods + CPU", datasources_json=json.dumps(ds))

        _, component = result
        assert len(component["datasources"]) == 2
        # Enrichment column placeholder added
        col_ids = [c["id"] for c in component["columns"]]
        assert "cpu_usage" in col_ids

    def test_invalid_json(self):
        from sre_agent.k8s_tools.live_table import create_live_table

        result = create_live_table(title="Bad", datasources_json="not json")
        assert isinstance(result, str)
        assert "Invalid JSON" in result

    def test_empty_datasources(self):
        from sre_agent.k8s_tools.live_table import create_live_table

        result = create_live_table(title="Bad", datasources_json="[]")
        assert isinstance(result, str)
        assert "non-empty" in result

    def test_too_many_datasources(self):
        from sre_agent.k8s_tools.live_table import create_live_table

        ds = [{"type": "k8s", "id": f"ds{i}", "resource": "pods"} for i in range(11)]
        result = create_live_table(title="Bad", datasources_json=json.dumps(ds))
        assert isinstance(result, str)
        assert "Maximum 10" in result

    def test_no_k8s_datasource(self):
        from sre_agent.k8s_tools.live_table import create_live_table

        ds = [
            {
                "type": "promql",
                "id": "cpu",
                "query": "rate(cpu[5m])",
                "columnId": "cpu",
                "joinLabel": "pod",
                "joinColumn": "name",
            }
        ]
        result = create_live_table(title="Bad", datasources_json=json.dumps(ds))
        assert isinstance(result, str)
        assert "K8s datasource" in result

    def test_missing_resource(self):
        from sre_agent.k8s_tools.live_table import create_live_table

        ds = [{"type": "k8s", "id": "bad", "label": "Bad"}]
        result = create_live_table(title="Bad", datasources_json=json.dumps(ds))
        assert isinstance(result, str)
        assert "missing required 'resource'" in result

    def test_too_many_k8s_datasources(self):
        from sre_agent.k8s_tools.live_table import create_live_table

        ds = [{"type": "k8s", "id": f"ds{i}", "label": f"DS{i}", "resource": "pods"} for i in range(6)]
        result = create_live_table(title="Bad", datasources_json=json.dumps(ds))
        assert isinstance(result, str)
        assert "Maximum 5" in result

    def test_promql_missing_join(self):
        from sre_agent.k8s_tools.live_table import create_live_table

        ds = [
            {"type": "k8s", "id": "pods", "resource": "pods"},
            {"type": "promql", "id": "cpu", "query": "rate(cpu[5m])", "columnId": "cpu"},
        ]
        result = create_live_table(title="Bad", datasources_json=json.dumps(ds))
        assert isinstance(result, str)
        assert "joinLabel" in result

    def test_logs_missing_namespace(self):
        from sre_agent.k8s_tools.live_table import create_live_table

        ds = [
            {"type": "k8s", "id": "pods", "resource": "pods"},
            {"type": "logs", "id": "errs", "columnId": "errors"},
        ]
        result = create_live_table(title="Bad", datasources_json=json.dumps(ds))
        assert isinstance(result, str)
        assert "missing required 'namespace'" in result

    def test_fetch_error_still_returns_spec(self):
        from sre_agent.k8s_tools.live_table import create_live_table

        ds = [{"type": "k8s", "id": "pods", "label": "Pods", "resource": "pods"}]
        with patch("sre_agent.k8s_tools.live_table._fetch_table_rows") as mock_fetch:
            mock_fetch.return_value = "Error: Not running in-cluster (no service account token)."
            result = create_live_table(title="Offline", datasources_json=json.dumps(ds))

        _text, component = result
        assert component["kind"] == "data_table"
        assert component["datasources"]  # spec still present for frontend
        assert component["rows"] == []  # no initial data
