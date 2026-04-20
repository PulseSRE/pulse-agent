# Topology Perspectives Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `get_topology_graph()` with agent-composed filtering, metrics enrichment, and layout hints so the agent can generate 5 purpose-built topology perspectives (Physical, Logical, Network, Multi-Tenant, Helm).

**Architecture:** The existing `DependencyGraph` singleton remains the single data source. `get_topology_graph()` gains `kinds`, `relationships`, `layout_hint`, `include_metrics`, and `group_by` parameters. The frontend `GraphRenderer` gains a `layoutHint` prop selecting among 3 layout strategies (`top-down`, `left-to-right`, `grouped`). Metrics come from the K8s metrics-server API with a 30s TTL cache.

**Tech Stack:** Python 3.11, kubernetes client, FastAPI, React/TypeScript, SVG rendering

---

## File Structure

| File | Responsibility |
|---|---|
| `sre_agent/dependency_graph.py` | `_fetch_metrics()` helper with 30s TTL cache |
| `sre_agent/view_tools.py` | Extended `get_topology_graph()` — validation, filtering, grouping, metrics enrichment |
| `sre_agent/component_registry.py` | Updated topology component entry with new optional fields and prompt hint |
| `tests/test_topology_and_live_table.py` | 15 new backend tests for filtering, metrics, validation, caching |
| `sre_agent/evals/scenarios_data/release.json` | 2 new scenarios (physical_topology, network_topology) |
| `OpenshiftPulse/src/kubeview/engine/agentComponents.ts` | Extended TypeScript interfaces (TopologySpec, TopoNode, NodeMetrics) |
| `OpenshiftPulse/src/kubeview/components/topology/GraphRenderer.tsx` | Layout strategies, metric bars, grouped containers |
| `OpenshiftPulse/src/kubeview/components/agent/AgentTopology.tsx` | Prop forwarding, perspective quick-launch pills |
| `OpenshiftPulse/src/kubeview/components/topology/__tests__/GraphRenderer.test.ts` | Frontend layout, metrics, and pill tests |

---

### Task 1: Backend — Parameter Validation Constants

**Files:**
- Modify: `sre_agent/view_tools.py:1269-1354`
- Test: `tests/test_topology_and_live_table.py`

- [ ] **Step 1: Write failing tests for validation**

Add these tests to `tests/test_topology_and_live_table.py` after the existing `TestGetTopologyGraph` class:

```python
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

    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_topology_validation_invalid_kinds(self, mock_get_graph):
        from sre_agent.view_tools import get_topology_graph
        mock_get_graph.return_value = self._make_full_graph()
        result = get_topology_graph(namespace="production", kinds="FooBar,Pod")
        assert isinstance(result, str)
        assert "FooBar" in result
        assert "Valid kinds:" in result

    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_topology_validation_invalid_relationships(self, mock_get_graph):
        from sre_agent.view_tools import get_topology_graph
        mock_get_graph.return_value = self._make_full_graph()
        result = get_topology_graph(namespace="production", relationships="badrel")
        assert isinstance(result, str)
        assert "badrel" in result
        assert "Valid relationships:" in result

    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_topology_validation_invalid_layout_hint(self, mock_get_graph):
        from sre_agent.view_tools import get_topology_graph
        mock_get_graph.return_value = self._make_full_graph()
        result = get_topology_graph(namespace="production", layout_hint="badlayout")
        assert isinstance(result, str)
        assert "badlayout" in result
        assert "Valid layout hints:" in result

    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_topology_backward_compat(self, mock_get_graph):
        from sre_agent.view_tools import get_topology_graph
        mock_get_graph.return_value = self._make_full_graph()
        result = get_topology_graph(namespace="production")
        assert isinstance(result, tuple)
        _, component = result
        assert len(component["nodes"]) == 7  # all production nodes (excludes Node which has ns="")

    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_topology_backward_compat_all_ns(self, mock_get_graph):
        from sre_agent.view_tools import get_topology_graph
        mock_get_graph.return_value = self._make_full_graph()
        result = get_topology_graph(namespace="")
        assert isinstance(result, tuple)
        _, component = result
        assert len(component["nodes"]) == 8  # all nodes including Node//worker-1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_topology_and_live_table.py::TestTopologyFiltering -v`
Expected: FAIL — `get_topology_graph()` doesn't accept `kinds`, `relationships`, or `layout_hint` params yet

- [ ] **Step 3: Add validation constants and parameter signature**

In `sre_agent/view_tools.py`, add these constants before the `get_topology_graph` function:

```python
VALID_TOPOLOGY_KINDS = frozenset({
    "Node", "Pod", "Deployment", "ReplicaSet", "StatefulSet", "DaemonSet",
    "Job", "CronJob", "Service", "Ingress", "Route", "ConfigMap", "Secret",
    "PVC", "ServiceAccount", "NetworkPolicy", "HelmRelease",
})

VALID_TOPOLOGY_RELATIONSHIPS = frozenset({
    "owns", "selects", "mounts", "references", "uses",
    "schedules", "routes_to", "applies_to", "scales", "manages",
})

VALID_LAYOUT_HINTS = frozenset({"top-down", "left-to-right", "grouped"})
```

Update the function signature and add validation at the top:

```python
@beta_tool
def get_topology_graph(
    namespace: str = "",
    kinds: str = "",
    relationships: str = "",
    layout_hint: str = "",
    include_metrics: bool = False,
    group_by: str = "",
):
    """Build an interactive dependency topology graph showing resource relationships, health status, and risk levels.

    Returns a visual network graph filtered by resource kinds and relationships.
    Each node shows health status (healthy/warning/error).

    Perspective reference — use these parameter patterns:
    - Hardware/capacity: kinds="Node,Pod" relationships="schedules" layout_hint="grouped" include_metrics=true group_by="node"
    - App structure: kinds="Deployment,ReplicaSet,Pod,ConfigMap,Secret,PVC,ServiceAccount" relationships="owns,references,mounts,uses" layout_hint="top-down"
    - Network flow: kinds="Route,Ingress,Service,Pod,NetworkPolicy" relationships="routes_to,selects,applies_to" layout_hint="left-to-right"
    - Tenant usage: kinds="Namespace,Pod,Node" relationships="schedules" layout_hint="grouped" include_metrics=true group_by="namespace"
    - Helm releases: kinds="HelmRelease,Deployment,StatefulSet,Service,ConfigMap,Secret" relationships="manages,owns" layout_hint="grouped"

    Args:
        namespace: Kubernetes namespace to graph. Leave empty for all namespaces.
        kinds: Comma-separated resource types to include (e.g. "Node,Pod,Service"). Empty = all types.
        relationships: Comma-separated relationship types to include (e.g. "owns,selects"). Empty = auto-infer from kinds.
        layout_hint: Layout strategy: "top-down", "left-to-right", or "grouped". Empty = "top-down".
        include_metrics: Fetch CPU/memory metrics from metrics-server for Node/Pod resources.
        group_by: Group nodes by "namespace" or a label key (e.g. "team"). Requires layout_hint="grouped".
    """
    # Validate kinds
    kind_set: set[str] | None = None
    if kinds:
        kind_set = {k.strip() for k in kinds.split(",") if k.strip()}
        invalid = kind_set - VALID_TOPOLOGY_KINDS
        if invalid:
            return f"Invalid kinds: {', '.join(sorted(invalid))}. Valid kinds: {', '.join(sorted(VALID_TOPOLOGY_KINDS))}"

    # Validate relationships
    rel_set: set[str] | None = None
    if relationships:
        rel_set = {r.strip() for r in relationships.split(",") if r.strip()}
        invalid = rel_set - VALID_TOPOLOGY_RELATIONSHIPS
        if invalid:
            return f"Invalid relationships: {', '.join(sorted(invalid))}. Valid relationships: {', '.join(sorted(VALID_TOPOLOGY_RELATIONSHIPS))}"

    # Validate layout_hint
    if layout_hint and layout_hint not in VALID_LAYOUT_HINTS:
        return f"Invalid layout hint: {layout_hint}. Valid layout hints: {', '.join(sorted(VALID_LAYOUT_HINTS))}"

    from .dependency_graph import get_dependency_graph

    graph = get_dependency_graph()
    nodes: list[dict] = []
    edges: list[dict] = []

    # Health status from active findings
    finding_status: dict[str, str] = {}
    try:
        from .db import get_database
        db = get_database()
        rows = db.fetchall("SELECT severity, resources FROM findings WHERE resolved = 0")
        for f in rows or []:
            sev = f.get("severity", "")
            for res_str in (f.get("resources") or "").split(","):
                res_str = res_str.strip()
                if res_str:
                    finding_status[res_str] = "error" if sev in ("critical", "warning") else "warning"
    except Exception:
        pass

    for key, node in graph.get_nodes().items():
        if namespace and node.namespace != namespace:
            continue
        resource_key = f"{node.kind}:{node.namespace}:{node.name}"
        status = finding_status.get(resource_key, "healthy")
        nodes.append(
            {
                "id": key,
                "kind": node.kind,
                "name": node.name,
                "namespace": node.namespace,
                "status": status,
            }
        )

    node_ids = {n["id"] for n in nodes}
    for edge in graph.get_edges():
        if edge.source in node_ids and edge.target in node_ids:
            edges.append(
                {
                    "source": edge.source,
                    "target": edge.target,
                    "relationship": edge.relationship,
                }
            )

    if not nodes:
        ns_label = f" in {namespace}" if namespace else ""
        return f"No topology data available{ns_label}. The dependency graph is built during monitor scans."

    ns_label = f" — {namespace}" if namespace else ""
    kind_counts: dict[str, int] = {}
    for n in nodes:
        kind_counts[n["kind"]] = kind_counts.get(n["kind"], 0) + 1
    summary_parts = [f"{c} {k}s" for k, c in sorted(kind_counts.items(), key=lambda x: -x[1])]

    text = (
        f"Topology graph{ns_label}: {len(nodes)} resources, {len(edges)} relationships. "
        f"Resources: {', '.join(summary_parts)}."
    )

    component = {
        "kind": "topology",
        "title": f"Topology{ns_label}",
        "description": f"{len(nodes)} resources, {len(edges)} relationships",
        "nodes": nodes,
        "edges": edges,
    }

    return (text, component)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_topology_and_live_table.py::TestTopologyFiltering -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Run full test suite to check for regressions**

Run: `python3 -m pytest tests/ -x -q`
Expected: All tests pass (existing topology tests unchanged)

- [ ] **Step 6: Commit**

```bash
git add sre_agent/view_tools.py tests/test_topology_and_live_table.py
git commit -m "feat(topology): add parameter validation for perspective filtering"
```

---

### Task 2: Backend — Kind and Relationship Filtering

**Files:**
- Modify: `sre_agent/view_tools.py:1269-1354`
- Test: `tests/test_topology_and_live_table.py`

- [ ] **Step 1: Write failing tests for filtering**

Add to `TestTopologyFiltering` in `tests/test_topology_and_live_table.py`:

```python
    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_topology_kind_filtering(self, mock_get_graph):
        from sre_agent.view_tools import get_topology_graph
        mock_get_graph.return_value = self._make_full_graph()
        result = get_topology_graph(namespace="production", kinds="Node,Pod")
        assert isinstance(result, tuple)
        _, component = result
        node_kinds = {n["kind"] for n in component["nodes"]}
        assert node_kinds == {"Pod"}  # Node has namespace="" so excluded by ns filter
        for edge in component["edges"]:
            assert edge["relationship"] in ("schedules",)

    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_topology_kind_filtering_all_ns(self, mock_get_graph):
        from sre_agent.view_tools import get_topology_graph
        mock_get_graph.return_value = self._make_full_graph()
        result = get_topology_graph(namespace="", kinds="Node,Pod")
        assert isinstance(result, tuple)
        _, component = result
        node_kinds = {n["kind"] for n in component["nodes"]}
        assert node_kinds == {"Node", "Pod"}

    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_topology_relationship_filtering(self, mock_get_graph):
        from sre_agent.view_tools import get_topology_graph
        mock_get_graph.return_value = self._make_full_graph()
        result = get_topology_graph(namespace="production", relationships="owns")
        assert isinstance(result, tuple)
        _, component = result
        for edge in component["edges"]:
            assert edge["relationship"] == "owns"

    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_topology_auto_relationship_inference(self, mock_get_graph):
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
    def test_topology_conflicting_filters(self, mock_get_graph):
        from sre_agent.view_tools import get_topology_graph
        mock_get_graph.return_value = self._make_full_graph()
        result = get_topology_graph(namespace="production", kinds="Node,Pod", relationships="owns")
        assert isinstance(result, str)
        assert "no edges" in result.lower() or "no relationship" in result.lower()

    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_topology_component_output_fields(self, mock_get_graph):
        from sre_agent.view_tools import get_topology_graph
        mock_get_graph.return_value = self._make_full_graph()
        result = get_topology_graph(namespace="production", layout_hint="grouped", group_by="namespace")
        assert isinstance(result, tuple)
        _, component = result
        assert component["layout_hint"] == "grouped"
        assert component["include_metrics"] is False
        assert component["group_by"] == "namespace"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_topology_and_live_table.py::TestTopologyFiltering::test_topology_kind_filtering -v`
Expected: FAIL — filtering logic not implemented yet

- [ ] **Step 3: Implement filtering logic**

In `sre_agent/view_tools.py`, update the node building loop and edge building loop inside `get_topology_graph()` to apply kind and relationship filters. After the validation block:

```python
    from .dependency_graph import get_dependency_graph

    graph = get_dependency_graph()
    nodes: list[dict] = []
    edges: list[dict] = []

    # Health status from active findings
    finding_status: dict[str, str] = {}
    try:
        from .db import get_database
        db = get_database()
        rows = db.fetchall("SELECT severity, resources FROM findings WHERE resolved = 0")
        for f in rows or []:
            sev = f.get("severity", "")
            for res_str in (f.get("resources") or "").split(","):
                res_str = res_str.strip()
                if res_str:
                    finding_status[res_str] = "error" if sev in ("critical", "warning") else "warning"
    except Exception:
        pass

    for key, node in graph.get_nodes().items():
        if namespace and node.namespace != namespace:
            continue
        if kind_set and node.kind not in kind_set:
            continue
        resource_key = f"{node.kind}:{node.namespace}:{node.name}"
        status = finding_status.get(resource_key, "healthy")
        node_dict: dict = {
            "id": key,
            "kind": node.kind,
            "name": node.name,
            "namespace": node.namespace,
            "status": status,
        }
        if group_by:
            if group_by == "namespace":
                node_dict["group"] = node.namespace
            else:
                node_dict["group"] = node.labels.get(group_by, "unlabeled")
        nodes.append(node_dict)

    node_ids = {n["id"] for n in nodes}
    node_kinds = {n["kind"] for n in nodes}

    for edge in graph.get_edges():
        if edge.source not in node_ids or edge.target not in node_ids:
            continue
        if rel_set and edge.relationship not in rel_set:
            continue
        if kind_set and not rel_set:
            src_node = graph.get_node(edge.source)
            tgt_node = graph.get_node(edge.target)
            if src_node and tgt_node:
                if src_node.kind not in node_kinds or tgt_node.kind not in node_kinds:
                    continue
        edges.append({
            "source": edge.source,
            "target": edge.target,
            "relationship": edge.relationship,
        })

    # Cross-validation: if both kinds and relationships are set, check edges exist
    if kind_set and rel_set and not edges and nodes:
        return (
            f"No edges possible: relationship types {', '.join(sorted(rel_set))} do not connect "
            f"the given kinds {', '.join(sorted(kind_set))}. Try removing the relationships filter "
            f"or adding the missing kinds."
        )

    if not nodes:
        ns_label = f" in {namespace}" if namespace else ""
        return f"No topology data available{ns_label}. The dependency graph is built during monitor scans."

    ns_label = f" — {namespace}" if namespace else ""
    kind_counts: dict[str, int] = {}
    for n in nodes:
        kind_counts[n["kind"]] = kind_counts.get(n["kind"], 0) + 1
    summary_parts = [f"{c} {k}s" for k, c in sorted(kind_counts.items(), key=lambda x: -x[1])]

    text = (
        f"Topology graph{ns_label}: {len(nodes)} resources, {len(edges)} relationships. "
        f"Resources: {', '.join(summary_parts)}."
    )

    component: dict = {
        "kind": "topology",
        "title": f"Topology{ns_label}",
        "description": f"{len(nodes)} resources, {len(edges)} relationships",
        "layout_hint": layout_hint or "top-down",
        "include_metrics": include_metrics,
        "group_by": group_by,
        "nodes": nodes,
        "edges": edges,
    }

    return (text, component)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_topology_and_live_table.py::TestTopologyFiltering -v`
Expected: All 11 tests PASS (5 validation + 6 filtering)

- [ ] **Step 5: Run full test suite**

Run: `python3 -m pytest tests/ -x -q`
Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
git add sre_agent/view_tools.py tests/test_topology_and_live_table.py
git commit -m "feat(topology): add kind/relationship filtering with auto-inference and cross-validation"
```

---

### Task 3: Backend — Group-by with Max Size

**Files:**
- Modify: `sre_agent/view_tools.py`
- Test: `tests/test_topology_and_live_table.py`

- [ ] **Step 1: Write failing tests for grouping**

Add to `TestTopologyFiltering` in `tests/test_topology_and_live_table.py`:

```python
    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_topology_group_by_namespace(self, mock_get_graph):
        from sre_agent.view_tools import get_topology_graph
        mock_get_graph.return_value = self._make_full_graph()
        result = get_topology_graph(namespace="", group_by="namespace")
        assert isinstance(result, tuple)
        _, component = result
        for node in component["nodes"]:
            assert "group" in node
            if node["kind"] == "Node":
                assert node["group"] == ""
            else:
                assert node["group"] == "production"

    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_topology_group_by_label(self, mock_get_graph):
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
    def test_topology_group_max_size(self, mock_get_graph):
        from sre_agent.view_tools import get_topology_graph
        g = DependencyGraph()
        for i in range(25):
            g.add_node("Pod", "production", f"pod-{i}", {"team": "big"})
        mock_get_graph.return_value = g
        result = get_topology_graph(namespace="production", group_by="team")
        assert isinstance(result, tuple)
        _, component = result
        big_group = [n for n in component["nodes"] if n.get("group") == "big"]
        summary = [n for n in component["nodes"] if n.get("id", "").startswith("_summary/")]
        assert len(big_group) == 20
        assert len(summary) == 1
        assert "5 more" in summary[0]["name"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_topology_and_live_table.py::TestTopologyFiltering::test_topology_group_max_size -v`
Expected: FAIL — max group size not implemented

- [ ] **Step 3: Implement max group size logic**

In `sre_agent/view_tools.py`, after building the `nodes` list and before building `edges`, add the group size cap:

```python
    # Cap group sizes at 20 to prevent SVG overload
    MAX_GROUP_SIZE = 20
    if group_by:
        groups: dict[str, list[dict]] = {}
        for n in nodes:
            g = n.get("group", "")
            if g not in groups:
                groups[g] = []
            groups[g].append(n)

        capped_nodes: list[dict] = []
        for g, members in groups.items():
            if len(members) <= MAX_GROUP_SIZE:
                capped_nodes.extend(members)
            else:
                capped_nodes.extend(members[:MAX_GROUP_SIZE])
                overflow = len(members) - MAX_GROUP_SIZE
                capped_nodes.append({
                    "id": f"_summary/{g}",
                    "kind": "Summary",
                    "name": f"+ {overflow} more",
                    "namespace": members[0]["namespace"],
                    "status": "healthy",
                    "group": g,
                })
        nodes = capped_nodes
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_topology_and_live_table.py::TestTopologyFiltering -v`
Expected: All 14 tests PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/view_tools.py tests/test_topology_and_live_table.py
git commit -m "feat(topology): add group_by with max 20 nodes per group and summary overflow"
```

---

### Task 4: Backend — Metrics Enrichment with TTL Cache

**Files:**
- Modify: `sre_agent/dependency_graph.py`
- Modify: `sre_agent/view_tools.py`
- Test: `tests/test_topology_and_live_table.py`

- [ ] **Step 1: Write failing tests for metrics**

Add to `TestTopologyFiltering` in `tests/test_topology_and_live_table.py`:

```python
    @patch("sre_agent.dependency_graph._fetch_metrics")
    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_topology_metrics_enrichment(self, mock_get_graph, mock_fetch):
        from sre_agent.view_tools import get_topology_graph
        mock_get_graph.return_value = self._make_full_graph()
        mock_fetch.return_value = (
            {"worker-1": {"cpu_usage": "1200m", "cpu_capacity": "4000m", "memory_usage": "4294967296", "memory_capacity": "17179869184", "cpu_usage_m": 1200, "cpu_capacity_m": 4000, "memory_usage_b": 4294967296, "memory_capacity_b": 17179869184}},
            {"production/web-1": {"cpu_usage": "100m", "memory_usage": "256Mi", "cpu_usage_m": 100, "memory_usage_b": 268435456}},
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
    def test_topology_metrics_graceful_degradation(self, mock_get_graph, mock_fetch):
        from sre_agent.view_tools import get_topology_graph
        mock_get_graph.return_value = self._make_full_graph()
        mock_fetch.return_value = ({}, {})
        result = get_topology_graph(namespace="", kinds="Node,Pod", include_metrics=True)
        assert isinstance(result, tuple)
        _, component = result
        for node in component["nodes"]:
            assert "metrics" not in node

    @patch("sre_agent.dependency_graph._fetch_metrics")
    @patch("sre_agent.dependency_graph.get_dependency_graph")
    def test_topology_metrics_cache(self, mock_get_graph, mock_fetch):
        from sre_agent.view_tools import get_topology_graph
        import sre_agent.dependency_graph as dg
        dg._metrics_cache.clear()
        mock_get_graph.return_value = self._make_full_graph()
        mock_fetch.return_value = (
            {"worker-1": {"cpu_usage": "1000m", "cpu_capacity": "4000m", "memory_usage": "1Gi", "memory_capacity": "8Gi", "cpu_usage_m": 1000, "cpu_capacity_m": 4000, "memory_usage_b": 1073741824, "memory_capacity_b": 8589934592}},
            {},
        )
        get_topology_graph(namespace="", kinds="Node", include_metrics=True)
        get_topology_graph(namespace="", kinds="Node", include_metrics=True)
        assert mock_fetch.call_count == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_topology_and_live_table.py::TestTopologyFiltering::test_topology_metrics_enrichment -v`
Expected: FAIL — `_fetch_metrics` doesn't exist yet

- [ ] **Step 3: Implement `_fetch_metrics()` with TTL cache in `dependency_graph.py`**

Add at the bottom of `sre_agent/dependency_graph.py`, before the singleton section:

```python
_metrics_cache: dict[str, tuple[float, tuple[dict, dict]]] = {}
_METRICS_TTL = 30


def _fetch_metrics(namespace: str = "") -> tuple[dict[str, dict], dict[str, dict]]:
    """Fetch CPU/memory metrics from metrics-server with 30s TTL cache.

    Returns (node_metrics_by_name, pod_metrics_by_key) where key is "namespace/name".
    Returns ({}, {}) if metrics-server is unavailable.
    """
    cache_key = namespace or "__all__"
    now = time.time()
    cached = _metrics_cache.get(cache_key)
    if cached and now - cached[0] < _METRICS_TTL:
        return cached[1]

    node_metrics: dict[str, dict] = {}
    pod_metrics: dict[str, dict] = {}

    try:
        from .k8s_client import get_core_client, get_custom_client, safe
        from .errors import ToolError
        from .units import parse_cpu_millicores, parse_memory_bytes, format_cpu, format_memory

        custom = get_custom_client()

        raw_nodes = safe(lambda: custom.list_cluster_custom_object("metrics.k8s.io", "v1beta1", "nodes"))
        if not isinstance(raw_nodes, ToolError):
            core = get_core_client()
            node_list = safe(lambda: core.list_node())
            capacity_map: dict[str, dict] = {}
            if not isinstance(node_list, ToolError):
                for n in node_list.items:
                    cap = n.status.capacity or {}
                    capacity_map[n.metadata.name] = {
                        "cpu": str(cap.get("cpu", "0")),
                        "memory": str(cap.get("memory", "0")),
                    }
            for item in raw_nodes.get("items", []):
                name = item["metadata"]["name"]
                usage = item.get("usage", {})
                cap = capacity_map.get(name, {})
                cpu_usage_m = parse_cpu_millicores(usage.get("cpu", "0"))
                cpu_cap_m = parse_cpu_millicores(cap.get("cpu", "0"))
                mem_usage_b = parse_memory_bytes(usage.get("memory", "0"))
                mem_cap_b = parse_memory_bytes(cap.get("memory", "0"))
                node_metrics[name] = {
                    "cpu_usage": format_cpu(cpu_usage_m),
                    "cpu_capacity": format_cpu(cpu_cap_m),
                    "memory_usage": format_memory(mem_usage_b),
                    "memory_capacity": format_memory(mem_cap_b),
                    "cpu_usage_m": cpu_usage_m,
                    "cpu_capacity_m": cpu_cap_m,
                    "memory_usage_b": mem_usage_b,
                    "memory_capacity_b": mem_cap_b,
                }

        if namespace:
            raw_pods = safe(lambda: custom.list_namespaced_custom_object(
                "metrics.k8s.io", "v1beta1", namespace, "pods"
            ))
        else:
            raw_pods = safe(lambda: custom.list_cluster_custom_object("metrics.k8s.io", "v1beta1", "pods"))
        if not isinstance(raw_pods, ToolError):
            for item in raw_pods.get("items", []):
                ns = item["metadata"]["namespace"]
                name = item["metadata"]["name"]
                containers = item.get("containers", [])
                total_cpu_m = sum(parse_cpu_millicores(c.get("usage", {}).get("cpu", "0")) for c in containers)
                total_mem_b = sum(parse_memory_bytes(c.get("usage", {}).get("memory", "0")) for c in containers)
                pod_metrics[f"{ns}/{name}"] = {
                    "cpu_usage": format_cpu(total_cpu_m),
                    "memory_usage": format_memory(total_mem_b),
                    "cpu_usage_m": total_cpu_m,
                    "memory_usage_b": total_mem_b,
                }

    except Exception:
        logger.debug("Metrics-server unavailable for topology enrichment", exc_info=True)

    result = (node_metrics, pod_metrics)
    _metrics_cache[cache_key] = (now, result)
    return result
```

- [ ] **Step 4: Wire metrics into `get_topology_graph()` in `view_tools.py`**

In `sre_agent/view_tools.py`, after building the `nodes` list and after group capping, before building edges, add metrics enrichment:

```python
    if include_metrics:
        from .dependency_graph import _fetch_metrics
        node_met, pod_met = _fetch_metrics(namespace)
        for n in nodes:
            if n["kind"] == "Node":
                m = node_met.get(n["name"])
                if m:
                    cpu_pct = round(m["cpu_usage_m"] * 100 / m["cpu_capacity_m"]) if m["cpu_capacity_m"] else 0
                    mem_pct = round(m["memory_usage_b"] * 100 / m["memory_capacity_b"]) if m["memory_capacity_b"] else 0
                    n["metrics"] = {
                        "cpu_usage": m["cpu_usage"],
                        "cpu_capacity": m["cpu_capacity"],
                        "cpu_percent": cpu_pct,
                        "memory_usage": m["memory_usage"],
                        "memory_capacity": m["memory_capacity"],
                        "memory_percent": mem_pct,
                    }
            elif n["kind"] == "Pod":
                key = f"{n['namespace']}/{n['name']}"
                m = pod_met.get(key)
                if m:
                    n["metrics"] = {
                        "cpu_usage": m["cpu_usage"],
                        "memory_usage": m["memory_usage"],
                        "cpu_percent": 0,
                        "memory_percent": 0,
                    }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_topology_and_live_table.py::TestTopologyFiltering -v`
Expected: All 17 tests PASS

- [ ] **Step 6: Run full test suite**

Run: `python3 -m pytest tests/ -x -q`
Expected: All tests pass

- [ ] **Step 7: Commit**

```bash
git add sre_agent/dependency_graph.py sre_agent/view_tools.py tests/test_topology_and_live_table.py
git commit -m "feat(topology): add metrics enrichment from metrics-server with 30s TTL cache"
```

---

### Task 5: Backend — Component Registry + Prompt Guidance

**Files:**
- Modify: `sre_agent/component_registry.py:422-438`

- [ ] **Step 1: Update the topology component registration**

In `sre_agent/component_registry.py`, replace the existing topology `register_component` call:

```python
register_component(
    ComponentKind(
        name="topology",
        description="Interactive resource dependency graph with health status and perspective filtering",
        category="visualization",
        required_fields=["nodes", "edges"],
        optional_fields=["title", "description", "layout_hint", "include_metrics", "group_by"],
        title_required=False,
        example={
            "kind": "topology",
            "title": "Physical Topology — production",
            "layout_hint": "grouped",
            "include_metrics": True,
            "group_by": "node",
            "nodes": [{"id": "1", "kind": "Node", "name": "worker-1", "namespace": "", "status": "healthy", "group": "worker-1"}],
            "edges": [{"source": "1", "target": "2", "relationship": "schedules"}],
        },
        prompt_hint=(
            "topology — Interactive dependency graph. Use get_topology_graph() to create.\n"
            "Perspective patterns:\n"
            "  Physical: kinds='Node,Pod' layout_hint='grouped' include_metrics=true group_by='node'\n"
            "  Logical: kinds='Deployment,ReplicaSet,Pod,ConfigMap,Secret,PVC,ServiceAccount' layout_hint='top-down'\n"
            "  Network: kinds='Route,Ingress,Service,Pod,NetworkPolicy' layout_hint='left-to-right'\n"
            "  Multi-Tenant: kinds='Namespace,Pod,Node' layout_hint='grouped' include_metrics=true group_by='namespace'\n"
            "  Helm: kinds='HelmRelease,Deployment,StatefulSet,Service,ConfigMap,Secret' layout_hint='grouped'"
        ),
    )
)
```

- [ ] **Step 2: Run tests to verify no regressions**

Run: `python3 -m pytest tests/ -x -q`
Expected: All tests pass

- [ ] **Step 3: Commit**

```bash
git add sre_agent/component_registry.py
git commit -m "feat(topology): update component registry with perspective fields and prompt hints"
```

---

### Task 6: Backend — Add Topology Perspective Scenarios to Release Suite

**Files:**
- Modify: `sre_agent/evals/scenarios_data/release.json`

- [ ] **Step 1: Add 2 new scenarios to the release suite**

Add these two scenario objects to the `scenarios` array in `sre_agent/evals/scenarios_data/release.json`:

```json
    {
      "scenario_id": "release_physical_topology",
      "category": "sre",
      "description": "Generate physical topology showing hardware utilization with metrics.",
      "tool_calls": [
        "get_topology_graph"
      ],
      "rejected_tools": 0,
      "duration_seconds": 15,
      "user_confirmed_resolution": true,
      "final_response": "Physical topology shows 3 nodes with 12 pods. worker-1 is at 78% CPU and 65% memory, worker-2 at 42% CPU. No hardware overload detected but worker-1 is approaching capacity.",
      "verification_passed": true,
      "rollback_available": false,
      "retry_attempts": 0,
      "transient_failures": 0
    },
    {
      "scenario_id": "release_network_topology",
      "category": "sre",
      "description": "Generate network flow topology for connectivity debugging.",
      "tool_calls": [
        "get_topology_graph"
      ],
      "rejected_tools": 0,
      "duration_seconds": 12,
      "user_confirmed_resolution": true,
      "final_response": "Network topology for production: Route web-route routes to Service web-svc which selects 3 pods. NetworkPolicy deny-all applies to all pods. No connectivity issues.",
      "verification_passed": true,
      "rollback_available": false,
      "retry_attempts": 0,
      "transient_failures": 0
    }
```

- [ ] **Step 2: Verify the JSON is valid**

Run: `python3 -c "import json; json.load(open('sre_agent/evals/scenarios_data/release.json')); print('Valid JSON')"`
Expected: `Valid JSON`

- [ ] **Step 3: Verify scenarios load correctly**

Run: `python3 -c "from sre_agent.evals.scenarios import load_suite; s = load_suite('release'); print(f'{len(s)} scenarios loaded')"`
Expected: Previous count + 2

- [ ] **Step 4: Commit**

```bash
git add sre_agent/evals/scenarios_data/release.json
git commit -m "feat(topology): add physical and network topology scenarios to release suite"
```

---

### Task 7: Frontend — TypeScript Interfaces

**Files:**
- Modify: `/Users/amobrem/ali/OpenshiftPulse/src/kubeview/engine/agentComponents.ts:353-372`

- [ ] **Step 1: Extend TopologySpec interface**

In `/Users/amobrem/ali/OpenshiftPulse/src/kubeview/engine/agentComponents.ts`, replace the existing `TopologySpec` interface:

```typescript
export type LayoutHint = 'top-down' | 'left-to-right' | 'grouped';

export interface NodeMetrics {
  cpu_usage: string;
  cpu_capacity: string;
  cpu_percent: number;
  memory_usage: string;
  memory_capacity: string;
  memory_percent: number;
}

export interface TopologySpec {
  kind: 'topology';
  title?: string;
  description?: string;
  layout_hint?: LayoutHint;
  include_metrics?: boolean;
  group_by?: string;
  nodes: Array<{
    id: string;
    kind: string;
    name: string;
    namespace: string;
    status?: 'healthy' | 'warning' | 'error';
    risk?: number;
    riskLevel?: 'critical' | 'high' | 'medium' | 'low';
    recentlyChanged?: boolean;
    group?: string;
    metrics?: NodeMetrics;
  }>;
  edges: Array<{
    source: string;
    target: string;
    relationship: string;
  }>;
}
```

- [ ] **Step 2: Run type check**

Run: `cd /Users/amobrem/ali/OpenshiftPulse && npx tsc --noEmit`
Expected: 0 errors

- [ ] **Step 3: Commit**

```bash
cd /Users/amobrem/ali/OpenshiftPulse && git add src/kubeview/engine/agentComponents.ts && git commit -m "feat(topology): extend TopologySpec with layout hints, metrics, and grouping"
```

---

### Task 8: Frontend — GraphRenderer Layout Strategies

**Files:**
- Modify: `/Users/amobrem/ali/OpenshiftPulse/src/kubeview/components/topology/GraphRenderer.tsx`
- Create: `/Users/amobrem/ali/OpenshiftPulse/src/kubeview/components/topology/__tests__/GraphRenderer.test.ts`

- [ ] **Step 1: Write failing tests for layout strategies**

Create `/Users/amobrem/ali/OpenshiftPulse/src/kubeview/components/topology/__tests__/GraphRenderer.test.ts`:

```typescript
import { describe, it, expect } from 'vitest';
import { layoutGraph, layoutLeftToRight, layoutGrouped } from '../GraphRenderer';
import type { TopoNode, TopoEdge } from '../GraphRenderer';

const baseNodes: TopoNode[] = [
  { id: 'Route/prod/web', kind: 'Route', name: 'web', namespace: 'prod' },
  { id: 'Service/prod/web', kind: 'Service', name: 'web', namespace: 'prod' },
  { id: 'Pod/prod/web-1', kind: 'Pod', name: 'web-1', namespace: 'prod' },
];

const baseEdges: TopoEdge[] = [
  { source: 'Route/prod/web', target: 'Service/prod/web', relationship: 'routes_to' },
  { source: 'Service/prod/web', target: 'Pod/prod/web-1', relationship: 'selects' },
];

describe('layoutGraph (top-down)', () => {
  it('assigns increasing y for deeper layers', () => {
    const result = layoutGraph(baseNodes, baseEdges);
    const route = result.find(n => n.kind === 'Route')!;
    const svc = result.find(n => n.kind === 'Service')!;
    const pod = result.find(n => n.kind === 'Pod')!;
    expect(route.y).toBeLessThan(svc.y);
    expect(svc.y).toBeLessThan(pod.y);
  });
});

describe('layoutLeftToRight', () => {
  it('assigns increasing x for deeper layers', () => {
    const result = layoutLeftToRight(baseNodes, baseEdges);
    const route = result.find(n => n.kind === 'Route')!;
    const svc = result.find(n => n.kind === 'Service')!;
    const pod = result.find(n => n.kind === 'Pod')!;
    expect(route.x).toBeLessThan(svc.x);
    expect(svc.x).toBeLessThan(pod.x);
  });

  it('keeps same-layer nodes at same x', () => {
    const nodes: TopoNode[] = [
      ...baseNodes,
      { id: 'Pod/prod/web-2', kind: 'Pod', name: 'web-2', namespace: 'prod' },
    ];
    const edges: TopoEdge[] = [
      ...baseEdges,
      { source: 'Service/prod/web', target: 'Pod/prod/web-2', relationship: 'selects' },
    ];
    const result = layoutLeftToRight(nodes, edges);
    const pod1 = result.find(n => n.name === 'web-1')!;
    const pod2 = result.find(n => n.name === 'web-2')!;
    expect(pod1.x).toBe(pod2.x);
  });
});

describe('layoutGrouped', () => {
  it('groups nodes by group field', () => {
    const nodes: TopoNode[] = [
      { id: 'Node//w1', kind: 'Node', name: 'w1', namespace: '', group: 'w1' } as TopoNode & { group: string },
      { id: 'Pod/p/a', kind: 'Pod', name: 'a', namespace: 'p', group: 'w1' } as TopoNode & { group: string },
      { id: 'Node//w2', kind: 'Node', name: 'w2', namespace: '', group: 'w2' } as TopoNode & { group: string },
      { id: 'Pod/p/b', kind: 'Pod', name: 'b', namespace: 'p', group: 'w2' } as TopoNode & { group: string },
    ];
    const result = layoutGrouped(nodes, []);
    expect(result.length).toBe(4);
  });

  it('returns empty array for empty input', () => {
    expect(layoutGrouped([], [])).toEqual([]);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/amobrem/ali/OpenshiftPulse && npx vitest run src/kubeview/components/topology/__tests__/GraphRenderer.test.ts`
Expected: FAIL — `layoutLeftToRight` and `layoutGrouped` not exported yet

- [ ] **Step 3: Refactor `layoutGraph` and add new layout functions**

In `/Users/amobrem/ali/OpenshiftPulse/src/kubeview/components/topology/GraphRenderer.tsx`:

1. Add `group` to the `TopoNode` interface:

```typescript
export interface TopoNode {
  id: string;
  kind: string;
  name: string;
  namespace: string;
  status?: 'healthy' | 'warning' | 'error';
  risk?: number;
  riskLevel?: 'critical' | 'high' | 'medium' | 'low';
  recentlyChanged?: boolean;
  group?: string;
  metrics?: {
    cpu_usage: string;
    cpu_capacity: string;
    cpu_percent: number;
    memory_usage: string;
    memory_capacity: string;
    memory_percent: number;
  };
}
```

2. Rename `layoutGraph` to `layoutTopDown` and keep `layoutGraph` as alias:

```typescript
export function layoutTopDown(nodes: TopoNode[], edges: TopoEdge[]): LayoutNode[] {
  // ... existing layoutGraph body unchanged ...
}

export const layoutGraph = layoutTopDown;
```

3. Add `layoutLeftToRight`:

```typescript
export function layoutLeftToRight(nodes: TopoNode[], edges: TopoEdge[]): LayoutNode[] {
  if (nodes.length === 0) return [];

  const nodeIds = new Set(nodes.map(n => n.id));
  const children = new Map<string, string[]>();
  const parents = new Map<string, string[]>();
  for (const e of edges) {
    if (!nodeIds.has(e.source) || !nodeIds.has(e.target)) continue;
    if (!children.has(e.source)) children.set(e.source, []);
    children.get(e.source)!.push(e.target);
    if (!parents.has(e.target)) parents.set(e.target, []);
    parents.get(e.target)!.push(e.source);
  }

  const roots = nodes.filter(n => !parents.has(n.id) || parents.get(n.id)!.length === 0);
  if (roots.length === 0) {
    const kindPriority: Record<string, number> = {
      HelmRelease: 0, Route: 1, Ingress: 1, HPA: 1, NetworkPolicy: 1,
      Node: 0, Service: 2, Deployment: 3, StatefulSet: 3, DaemonSet: 3,
      CronJob: 3, Job: 4, ReplicaSet: 4, Pod: 5,
      ServiceAccount: 6, ConfigMap: 6, Secret: 6, PVC: 6,
    };
    roots.push(...nodes.filter(n => (kindPriority[n.kind] ?? 3) <= 2));
    if (roots.length === 0) roots.push(nodes[0]);
  }

  const layers = new Map<string, number>();
  const queue: string[] = [];
  for (const r of roots) {
    if (!layers.has(r.id)) { layers.set(r.id, 0); queue.push(r.id); }
  }
  while (queue.length > 0) {
    const curr = queue.shift()!;
    const currLayer = layers.get(curr)!;
    for (const child of children.get(curr) ?? []) {
      const existing = layers.get(child);
      if (existing === undefined || existing < currLayer + 1) {
        layers.set(child, currLayer + 1);
        queue.push(child);
      }
    }
  }
  for (const n of nodes) {
    if (!layers.has(n.id)) layers.set(n.id, 0);
  }

  const byLayer = new Map<number, TopoNode[]>();
  for (const n of nodes) {
    const layer = layers.get(n.id) ?? 0;
    if (!byLayer.has(layer)) byLayer.set(layer, []);
    byLayer.get(layer)!.push(n);
  }

  const colWidth = 260;
  const rowHeight = 64;
  const paddingX = 30;
  const paddingY = 30;

  const result: LayoutNode[] = [];
  let globalXOffset = 0;

  for (const layer of [...byLayer.keys()].sort((a, b) => a - b)) {
    const group = byLayer.get(layer)!;
    group.sort((a, b) => a.kind.localeCompare(b.kind) || a.name.localeCompare(b.name));
    group.forEach((node, idx) => {
      result.push({ ...node, x: paddingX + globalXOffset, y: paddingY + idx * rowHeight });
    });
    globalXOffset += colWidth;
  }
  return result;
}
```

4. Add `layoutGrouped`:

```typescript
export function layoutGrouped(nodes: TopoNode[], _edges: TopoEdge[]): LayoutNode[] {
  if (nodes.length === 0) return [];

  const groups = new Map<string, TopoNode[]>();
  for (const n of nodes) {
    const g = n.group ?? 'default';
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g)!.push(n);
  }

  const nodeWidth = 160;
  const rowHeight = 50;
  const groupGap = 30;
  const headerHeight = 28;
  const internalPadding = 16;
  const paddingX = 30;
  const paddingY = 30;
  const maxGroupsPerRow = 3;
  const nodesPerRow = 2;

  const result: LayoutNode[] = [];
  let groupIdx = 0;

  const groupHeights: number[] = [];
  for (const [, members] of groups) {
    const rows = Math.ceil(members.length / nodesPerRow);
    groupHeights.push(headerHeight + internalPadding * 2 + rows * rowHeight);
  }

  const maxGroupWidth = nodeWidth * nodesPerRow + internalPadding * 2;

  let gIdx = 0;
  for (const [, members] of groups) {
    const groupCol = gIdx % maxGroupsPerRow;
    const groupRow = Math.floor(gIdx / maxGroupsPerRow);

    let yOffset = 0;
    for (let r = 0; r < groupRow; r++) {
      const rowStart = r * maxGroupsPerRow;
      const rowEnd = Math.min(rowStart + maxGroupsPerRow, groupHeights.length);
      yOffset += Math.max(...groupHeights.slice(rowStart, rowEnd)) + groupGap;
    }

    const groupX = paddingX + groupCol * (maxGroupWidth + groupGap);
    const groupY = paddingY + yOffset;

    members.forEach((node, idx) => {
      const col = idx % nodesPerRow;
      const row = Math.floor(idx / nodesPerRow);
      result.push({
        ...node,
        x: groupX + internalPadding + col * nodeWidth,
        y: groupY + headerHeight + internalPadding + row * rowHeight,
      });
    });
    gIdx++;
  }
  return result;
}
```

- [ ] **Step 4: Update the `GraphRenderer` component to accept `layoutHint` and `includeMetrics` props**

Update the props interface:

```typescript
interface GraphRendererProps {
  nodes: TopoNode[];
  edges: TopoEdge[];
  hoveredNode: string | null;
  setHoveredNode: Dispatch<SetStateAction<string | null>>;
  selectedNode: string | null;
  setSelectedNode: Dispatch<SetStateAction<string | null>>;
  layoutHint?: 'top-down' | 'left-to-right' | 'grouped';
  includeMetrics?: boolean;
}
```

Update the destructuring and layout memo:

```typescript
export default function GraphRenderer({
  nodes, edges, hoveredNode, setHoveredNode, selectedNode, setSelectedNode,
  layoutHint, includeMetrics,
}: GraphRendererProps) {
  // ...
  const layout = useMemo(() => {
    if (layoutHint === 'left-to-right') return layoutLeftToRight(nodes, edges);
    if (layoutHint === 'grouped') return layoutGrouped(nodes, edges);
    return layoutTopDown(nodes, edges);
  }, [nodes, edges, layoutHint]);
```

- [ ] **Step 5: Run tests**

Run: `cd /Users/amobrem/ali/OpenshiftPulse && npx vitest run src/kubeview/components/topology/__tests__/GraphRenderer.test.ts`
Expected: All tests PASS

- [ ] **Step 6: Run type check**

Run: `cd /Users/amobrem/ali/OpenshiftPulse && npx tsc --noEmit`
Expected: 0 errors

- [ ] **Step 7: Commit**

```bash
cd /Users/amobrem/ali/OpenshiftPulse && git add src/kubeview/components/topology/GraphRenderer.tsx src/kubeview/components/topology/__tests__/GraphRenderer.test.ts && git commit -m "feat(topology): add left-to-right and grouped layout strategies with tests"
```

---

### Task 9: Frontend — Metric Bars + Grouped Containers + Edge Direction

**Files:**
- Modify: `/Users/amobrem/ali/OpenshiftPulse/src/kubeview/components/topology/GraphRenderer.tsx`

- [ ] **Step 1: Add metric bar rendering**

In the node `<g>` element in `GraphRenderer.tsx`, after the risk badge, add:

```typescript
              {includeMetrics && (node as any).metrics && (() => {
                const m = (node as any).metrics as { cpu_percent: number; memory_percent: number; cpu_usage: string; cpu_capacity: string; memory_usage: string; memory_capacity: string };
                const barWidth = 130;
                const cpuColor = m.cpu_percent >= 80 ? '#ef4444' : m.cpu_percent >= 60 ? '#eab308' : '#3b82f6';
                const memColor = m.memory_percent >= 80 ? '#ef4444' : m.memory_percent >= 60 ? '#eab308' : '#22c55e';
                return (
                  <g data-testid="metric-bar">
                    <title>{`CPU: ${m.cpu_usage}/${m.cpu_capacity} (${m.cpu_percent}%) | Memory: ${m.memory_usage}/${m.memory_capacity} (${m.memory_percent}%)`}</title>
                    <rect x={14} y={32} width={barWidth} height={3} rx={1} fill="#1e293b" />
                    <rect x={14} y={32} width={barWidth * m.cpu_percent / 100} height={3} rx={1} fill={cpuColor} />
                    <rect x={14} y={37} width={barWidth} height={3} rx={1} fill="#1e293b" />
                    <rect x={14} y={37} width={barWidth * m.memory_percent / 100} height={3} rx={1} fill={memColor} />
                  </g>
                );
              })()}
```

Increase node body height from 36 to 44 when metrics are present. Update the `<rect>` for the node body and kind color bar to use dynamic height:

```typescript
              const nodeH = includeMetrics && (node as any).metrics ? 44 : 36;
```

Use `nodeH` for the rect heights.

- [ ] **Step 2: Add grouped container rendering**

In the SVG, before the edges, add group containers when `layoutHint === 'grouped'`:

```typescript
        {layoutHint === 'grouped' && (() => {
          const groupMap = new Map<string, LayoutNode[]>();
          for (const n of layout) {
            const g = (n as any).group ?? 'default';
            if (!groupMap.has(g)) groupMap.set(g, []);
            groupMap.get(g)!.push(n);
          }
          return [...groupMap.entries()].map(([groupName, members]) => {
            const nodeH = includeMetrics ? 52 : 44;
            const minX = Math.min(...members.map(m => m.x)) - 16;
            const minY = Math.min(...members.map(m => m.y)) - 28;
            const maxX = Math.max(...members.map(m => m.x)) + 176;
            const maxY = Math.max(...members.map(m => m.y)) + nodeH;
            return (
              <g key={`group-${groupName}`}>
                <rect
                  x={minX} y={minY}
                  width={maxX - minX} height={maxY - minY}
                  rx={8} fill="#0f172a" fillOpacity={0.5}
                  stroke="#334155" strokeWidth={1} strokeDasharray="4 2"
                />
                <text x={minX + 8} y={minY + 16} fill="#94a3b8" fontSize={11} fontWeight={600}>
                  {groupName}
                </text>
              </g>
            );
          });
        })()}
```

- [ ] **Step 3: Update edge rendering for left-to-right layout**

Update the edge path generation to handle horizontal flow:

```typescript
          const nodeH = includeMetrics && (nodeMap.get(edge.source) as any)?.metrics ? 44 : 36;

          let d: string;
          let labelX: number;
          let labelY: number;
          if (layoutHint === 'left-to-right') {
            const x1 = from.x + 160;
            const y1 = from.y + nodeH / 2;
            const x2 = to.x;
            const y2 = to.y + nodeH / 2;
            const midX = (x1 + x2) / 2;
            d = `M ${x1} ${y1} C ${midX} ${y1}, ${midX} ${y2}, ${x2} ${y2}`;
            labelX = midX;
            labelY = (y1 + y2) / 2 - 4;
          } else {
            const x1 = from.x + 80;
            const y1 = from.y + nodeH;
            const x2 = to.x + 80;
            const y2 = to.y;
            const midY = (y1 + y2) / 2;
            d = `M ${x1} ${y1} C ${x1} ${midY}, ${x2} ${midY}, ${x2} ${y2}`;
            labelX = (x1 + x2) / 2;
            labelY = midY - 4;
          }
```

Use `d`, `labelX`, `labelY` in the existing path and text elements.

- [ ] **Step 4: Run type check and tests**

Run: `cd /Users/amobrem/ali/OpenshiftPulse && npx tsc --noEmit && npx vitest run src/kubeview/components/topology/__tests__/GraphRenderer.test.ts`
Expected: 0 type errors, all tests pass

- [ ] **Step 5: Commit**

```bash
cd /Users/amobrem/ali/OpenshiftPulse && git add src/kubeview/components/topology/GraphRenderer.tsx && git commit -m "feat(topology): add metric bars, grouped containers, and directional edges"
```

---

### Task 10: Frontend — Perspective Quick-Launch Pills in AgentTopology

**Files:**
- Modify: `/Users/amobrem/ali/OpenshiftPulse/src/kubeview/components/agent/AgentTopology.tsx`

- [ ] **Step 1: Add perspective pills and forward layout props**

In `/Users/amobrem/ali/OpenshiftPulse/src/kubeview/components/agent/AgentTopology.tsx`:

1. Add imports:

```typescript
import { PromptPill } from './AIBranding';
import { useAgentStore } from '../../store/agentStore';
```

2. Add perspective definitions:

```typescript
const PERSPECTIVES = [
  { label: 'Physical', prompt: 'Show physical topology with hardware metrics' },
  { label: 'Logical', prompt: 'Show logical app structure topology' },
  { label: 'Network', prompt: 'Show network flow topology' },
  { label: 'Multi-Tenant', prompt: 'Show multi-tenant topology grouped by namespace' },
  { label: 'Helm', prompt: 'Show Helm release topology' },
] as const;
```

3. Inside the component function, add:

```typescript
  const sendMessage = useAgentStore((s) => s.sendMessage);
```

4. Add the pill row between the legend and the graph `<div>`:

```typescript
      {/* Perspective pills */}
      <div className="px-3 py-1.5 border-b border-slate-800 flex flex-wrap items-center gap-1.5">
        <span className="text-[10px] text-slate-600 mr-1">View:</span>
        {PERSPECTIVES.map((p) => {
          const ns = spec.nodes[0]?.namespace;
          const prompt = ns ? `${p.prompt} for namespace ${ns}` : p.prompt;
          return (
            <PromptPill key={p.label} onClick={() => sendMessage(prompt)}>
              {p.label}
            </PromptPill>
          );
        })}
      </div>
```

5. Forward props to `GraphRenderer`:

```typescript
          <GraphRenderer
            nodes={spec.nodes}
            edges={spec.edges}
            hoveredNode={hoveredNode}
            setHoveredNode={setHoveredNode}
            selectedNode={selectedNode}
            setSelectedNode={setSelectedNode}
            layoutHint={spec.layout_hint}
            includeMetrics={spec.include_metrics}
          />
```

- [ ] **Step 2: Run type check**

Run: `cd /Users/amobrem/ali/OpenshiftPulse && npx tsc --noEmit`
Expected: 0 errors

- [ ] **Step 3: Run full frontend test suite**

Run: `cd /Users/amobrem/ali/OpenshiftPulse && npx vitest run`
Expected: All tests pass

- [ ] **Step 4: Commit**

```bash
cd /Users/amobrem/ali/OpenshiftPulse && git add src/kubeview/components/agent/AgentTopology.tsx && git commit -m "feat(topology): add perspective quick-launch pills and forward layout props"
```

---

### Task 11: Docs — Update CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update CLAUDE.md**

Add to the relevant sections:
- In the project summary line, update tool count if needed
- In the Key Files or Tools section, note: `get_topology_graph` now supports `kinds`, `relationships`, `layout_hint`, `include_metrics`, `group_by` params for 5 topology perspectives (Physical, Logical, Network, Multi-Tenant, Helm) with metrics-server enrichment (30s TTL cache) and grouped layout containers

- [ ] **Step 2: Run backend tests**

Run: `python3 -m pytest tests/ -x -q`
Expected: All tests pass

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md with topology perspective filtering"
```

---

### Task 12: Full Integration Test

- [ ] **Step 1: Run backend test suite**

Run: `python3 -m pytest tests/ -v`
Expected: All tests pass (1800+)

- [ ] **Step 2: Run frontend test suite**

Run: `cd /Users/amobrem/ali/OpenshiftPulse && npx vitest run`
Expected: All tests pass (1940+)

- [ ] **Step 3: Run type check**

Run: `cd /Users/amobrem/ali/OpenshiftPulse && npx tsc --noEmit`
Expected: 0 errors

- [ ] **Step 4: Run selector suite**

Run: `python3 -c "
import sre_agent.skill_loader as sl
sl._skills = {}; sl._keyword_index = []; sl._selector = None; sl._HARD_PRE_ROUTE.clear()
from sre_agent.evals.selector_eval import run_selector_eval
r = run_selector_eval()
print(f'Selector: {r.passed}/{r.total_scenarios} ({r.passed/r.total_scenarios:.0%})')
"`
Expected: 55/55 (100%)

- [ ] **Step 5: Verify scenario loading**

Run: `python3 -c "from sre_agent.evals.scenarios import load_suite; print(f'Release: {len(load_suite(\"release\"))} scenarios')"`
Expected: Includes new physical_topology and network_topology scenarios
