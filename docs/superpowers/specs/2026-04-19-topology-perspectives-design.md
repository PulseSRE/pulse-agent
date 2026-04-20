# Customizable Topology Perspectives — Design Spec

## Goal

Extend `get_topology_graph()` with agent-composed filtering so the agent can generate purpose-built topology views — Physical, Logical, Network, Multi-Tenant, Helm — each answering a different operator question. The agent infers the right perspective from the query, but the UI provides perspective quick-launch pills for one-click override.

## Architecture

The existing `DependencyGraph` singleton (17 resource types, 10 relationships) remains the single data source. `get_topology_graph()` gains filter parameters that the agent composes per query. The frontend `GraphRenderer` gains a `layoutHint` prop that selects among 3 layout strategies. Metrics enrichment is optional via the metrics-server API with a TTL cache.

No predefined view registry. The agent's system prompt includes a reference table mapping common question patterns to recommended parameter combinations.

## Tool API Changes

### `get_topology_graph()` — Extended Signature

```python
def get_topology_graph(
    namespace: str = "",
    kinds: str = "",           # comma-separated: "Node,Pod,Service"
    relationships: str = "",   # comma-separated: "schedules,selects"
    layout_hint: str = "",     # enum: "top-down" | "left-to-right" | "grouped"
    include_metrics: bool = False,
    group_by: str = "",        # "namespace" | label key like "team"
) -> Union[str, Tuple[str, dict]]
```

**Backward compatible:** All new params default to empty/false. No params = all types, all relationships (current behavior).

### Parameter Validation

- `kinds`: validated against the 17 known resource types: `Node, Pod, Deployment, ReplicaSet, StatefulSet, DaemonSet, Job, CronJob, Service, Ingress, Route, ConfigMap, Secret, PVC, ServiceAccount, NetworkPolicy, HelmRelease`. Invalid values return an error string listing valid options.
- `relationships`: validated against 10 known types: `owns, selects, mounts, references, uses, schedules, routes_to, applies_to, scales, manages`. Invalid values return an error listing valid options.
- `layout_hint`: validated against enum: `top-down, left-to-right, grouped`. Invalid values return an error listing valid options.
- `group_by`: `"namespace"` is always valid. Any other string is treated as a label key lookup.
- **Cross-validation:** If both `kinds` and `relationships` are provided, verify at least one relationship type can connect the given kinds. If no edges are possible, return an error suggesting either removing the `relationships` filter or adding the missing kinds.

### Filtering Logic

1. **Kind filtering:** If `kinds` is provided, keep only nodes whose `kind` is in the set. Prune edges where either source or target was removed.
2. **Relationship filtering:** If `relationships` is provided, further filter edges to only those relationship types. Applied after kind filtering.
3. **Auto-relationship inference:** When kinds are filtered but relationships are empty, include only edge types where both endpoint kinds exist in the filtered kinds set. E.g., kinds=`["Node","Pod"]` → only `schedules` edges since other relationship types have at least one endpoint kind not in the filter.
4. **Group-by:** When `group_by` is set, add a `group` field to each node:
   - `group_by="namespace"` → `node["group"] = node["namespace"]`
   - Any other value → look up that label key on the original K8s resource. If the label doesn't exist on a resource, `group = "unlabeled"`.
   - **Max group size:** Groups with more than 20 nodes are collapsed — only the first 20 are rendered, with an additional summary node showing "+ N more". This prevents large namespaces (200+ pods) from overwhelming the SVG.

## Metrics Enrichment

When `include_metrics=True`, fetch from the K8s metrics-server API:

- **Node metrics:** `GET /apis/metrics.k8s.io/v1beta1/nodes`
- **Pod metrics:** `GET /apis/metrics.k8s.io/v1beta1/pods` — always scoped to `namespace` when provided

Each Node and Pod node gets an optional `metrics` field:

```python
{
    "id": "Node//worker-1",
    "kind": "Node",
    "name": "worker-1",
    "status": "healthy",
    "metrics": {
        "cpu_usage": "1.2",        # cores (string for display)
        "cpu_capacity": "4.0",     # from node.status.capacity
        "cpu_percent": 30,         # integer 0-100
        "memory_usage": "4.1Gi",   # human-readable
        "memory_capacity": "16Gi",
        "memory_percent": 26
    }
}
```

**Capacity source:** Node capacity from `node.status.capacity`. Pod capacity from container resource requests/limits (use limits if set, else requests, else omit `cpu_capacity`/`memory_capacity` and `cpu_percent`/`memory_percent`).

**Graceful degradation:** If metrics-server is unavailable (API returns 404 or connection error), log a warning and return the topology without metrics. The `metrics` field is absent from nodes. No error returned to the user.

**Caching:** `_fetch_metrics()` results are cached with a 30-second TTL using a module-level cache dict keyed by namespace. Subsequent calls within the TTL window return cached data. This prevents repeated API server hits when the agent calls `get_topology_graph` multiple times (e.g., user switching perspectives via quick-launch pills).

**Implementation:** New `_fetch_metrics(namespace: str) -> tuple[dict, dict]` helper in `dependency_graph.py` returning `(node_metrics_by_name, pod_metrics_by_key)`. Uses `safe()` wrapper on the custom objects API (`/apis/metrics.k8s.io/v1beta1/...`). Merges into topology nodes by matching node name or `namespace/pod_name`.

## Component Output

The topology component spec gains new optional fields:

```python
{
    "kind": "topology",
    "title": "Physical Topology — production",
    "description": "12 resources, 8 relationships",
    "layout_hint": "grouped",             # NEW — "top-down" | "left-to-right" | "grouped"
    "include_metrics": True,               # NEW
    "group_by": "node",                    # NEW — what the groups represent
    "nodes": [
        {
            "id": "Node//worker-1",
            "kind": "Node",
            "name": "worker-1",
            "namespace": "",
            "status": "healthy",
            "group": "worker-1",          # NEW — for grouped layouts
            "metrics": {                   # NEW — optional
                "cpu_usage": "1.2",
                "cpu_capacity": "4.0",
                "cpu_percent": 30,
                "memory_usage": "4.1Gi",
                "memory_capacity": "16Gi",
                "memory_percent": 26
            }
        }
    ],
    "edges": [
        {"source": "Node//worker-1", "target": "Pod/prod/nginx-abc", "relationship": "schedules"}
    ]
}
```

## Frontend Changes

### TypeScript Interfaces

```typescript
type LayoutHint = 'top-down' | 'left-to-right' | 'grouped';

interface TopologySpec {
  kind: 'topology';
  title?: string;
  description?: string;
  layout_hint?: LayoutHint;
  include_metrics?: boolean;
  group_by?: string;
  nodes: TopoNode[];
  edges: TopoEdge[];
}

interface TopoNode {
  id: string;
  kind: string;
  name: string;
  namespace: string;
  status?: 'healthy' | 'warning' | 'error';
  risk?: number;
  riskLevel?: 'critical' | 'high' | 'medium' | 'low';
  recentlyChanged?: boolean;
  group?: string;                    // NEW
  metrics?: NodeMetrics;             // NEW
}

interface NodeMetrics {
  cpu_usage: string;
  cpu_capacity: string;
  cpu_percent: number;
  memory_usage: string;
  memory_capacity: string;
  memory_percent: number;
}
```

### GraphRenderer Layout Strategies

Extract current layout into named functions. Select based on `layoutHint` prop:

| layout_hint | Function | Behavior |
|---|---|---|
| `top-down` (default) | `layoutTopDown()` | Current hierarchical BFS from roots, vertical bezier curves |
| `left-to-right` | `layoutLeftToRight()` | Same BFS but horizontal flow, horizontal bezier curves |
| `grouped` | `layoutGrouped()` | Partition nodes by `group` field, render each group as a labeled container box, lay out nodes within each container top-down |

The `grouped` layout partitions nodes by `node.group`, renders each partition inside a rounded container with the group name as header, then lays out nodes within each container using top-down ordering. Max nesting depth: 1 — no nested groups.

### Metric Bars

When `include_metrics` is true and a node has a `metrics` field, render below the node name:
- Two thin horizontal bars (CPU blue `#3b82f6`, memory green `#22c55e`)
- Width proportional to percent (0-100)
- Tooltip on hover showing exact values: "CPU: 1.2/4.0 cores (30%) | Memory: 4.1/16Gi (26%)"
- Color shifts: 0-60% normal color, 60-80% amber `#eab308`, 80-100% red `#ef4444`
- No text on the bars themselves — exact values shown only in tooltip

### Perspective Quick-Launch Pills

Rendered below the graph title in `AgentTopology.tsx` as a row of small pill buttons:

```
[Physical] [Logical] [Network] [Multi-Tenant] [Helm]
```

- Styled like existing `PromptPill` components — small, muted, not visually dominant
- Clicking a pill sends a canned message through the dock (e.g., "Show physical topology for {current namespace}")
- The agent processes the message normally and returns a new topology with the appropriate filters
- The currently active perspective (if detectable from the topology's filter params) is highlighted
- This keeps the agent as the primary entry point (conversational-first) while giving users a one-click correction

### AgentTopology.tsx

- Pass `layoutHint`, `includeMetrics`, `groupBy` props to `GraphRenderer`
- Add perspective pill row below the title/description header
- Pills send canned prompts through the dock's `sendMessage()` function

## Prompt Guidance

Add to the topology tool's docstring and `component_registry.py` prompt_hint:

```
Perspective reference — use these parameter patterns:

| Question | kinds | relationships | layout_hint | include_metrics | group_by |
|---|---|---|---|---|---|
| Hardware/capacity ("is my hardware overloaded?") | Node,Pod | schedules | grouped | true | node |
| App structure ("how is this app structured?") | Deployment,ReplicaSet,Pod,ConfigMap,Secret,PVC,ServiceAccount | owns,references,mounts,uses | top-down | false | |
| Network flow ("why can't A reach B?") | Route,Ingress,Service,Pod,NetworkPolicy | routes_to,selects,applies_to | left-to-right | false | |
| Team/tenant usage ("which team uses most?") | Namespace,Pod,Node | schedules | grouped | true | namespace |
| Helm releases ("what does this release manage?") | HelmRelease,Deployment,StatefulSet,Service,ConfigMap,Secret | manages,owns | grouped | false | helm-release |
```

## Component Registry Update

In `component_registry.py`, update the topology entry:
- Add `layout_hint`, `include_metrics`, `group_by` to `optional_fields`
- Update `prompt_hint` with the perspective reference table above

## Testing

### Backend Unit Tests (dependency_graph.py / view_tools.py)

- `test_topology_kind_filtering` — provide kinds=["Node","Pod"], verify only those kinds in result nodes, edges pruned
- `test_topology_relationship_filtering` — provide relationships=["schedules"], verify only those edges
- `test_topology_auto_relationship_inference` — provide kinds only, verify only edge types where both endpoint kinds exist
- `test_topology_conflicting_filters` — kinds=["Node","Pod"] + relationships=["owns"] returns error with suggestion
- `test_topology_metrics_enrichment` — mock metrics-server, verify `metrics` field on nodes with correct percent calculations
- `test_topology_metrics_graceful_degradation` — mock metrics-server 404, verify topology returns without metrics, no error
- `test_topology_metrics_cache` — call twice within 30s, verify second call uses cache (no API hit)
- `test_topology_group_by_namespace` — verify `group` field = namespace for each node
- `test_topology_group_by_label` — mock resources with label, verify group field; resources without label get `group="unlabeled"`
- `test_topology_group_max_size` — group with 25 nodes shows 20 + summary node
- `test_topology_validation_invalid_kinds` — invalid kind returns error with valid options
- `test_topology_validation_invalid_relationships` — invalid relationship returns error with valid options
- `test_topology_validation_invalid_layout_hint` — invalid hint returns error with valid options
- `test_topology_backward_compat` — no params returns all types and relationships (existing behavior unchanged)
- `test_topology_component_output_fields` — verify `layout_hint`, `include_metrics`, `group_by` in component dict

### Eval Scenarios (2 new in release suite)

- **physical_topology**: "Is my hardware overloaded? Show me the physical topology." → agent calls `get_topology_graph` with `kinds` containing Node+Pod, `include_metrics=true`, `layout_hint="grouped"`. Score: tool called with correct params.
- **network_topology**: "Show me the network topology for namespace production" → agent calls `get_topology_graph` with `kinds` containing Route/Ingress/Service/Pod/NetworkPolicy, `layout_hint="left-to-right"`. Score: tool called with correct params.

### Frontend Tests (GraphRenderer)

- `test_layout_hint_selects_strategy` — verify `layoutHint` prop dispatches to correct layout function
- `test_metric_bars_render` — verify metric bars appear when `includeMetrics=true` and nodes have metrics
- `test_metric_bars_color_thresholds` — verify color changes at 60%/80% boundaries
- `test_grouped_layout_containers` — verify grouped layout creates container boxes with group labels
- `test_grouped_layout_max_nodes` — verify groups with >20 nodes show collapse summary
- `test_no_metrics_no_bars` — verify no metric bars when `includeMetrics=false`
- `test_perspective_pills_render` — verify 5 perspective pills render below title

## Files to Modify

| File | Changes |
|---|---|
| `sre_agent/dependency_graph.py` | Add `_fetch_metrics()` helper with 30s TTL cache |
| `sre_agent/view_tools.py` | Extend `get_topology_graph()` signature, add filtering/grouping/metrics/validation logic |
| `sre_agent/component_registry.py` | Update topology entry: optional_fields, prompt_hint |
| `tests/test_topology_and_live_table.py` | Add 15 filtering/metrics/validation/cache tests |
| `tests/test_dependency_graph.py` | Add group_by tests |
| `sre_agent/evals/scenarios/` | Add 2 perspective eval scenarios |
| `OpenshiftPulse/.../topology/GraphRenderer.tsx` | Add `layoutHint` prop, extract layout functions, add `layoutLeftToRight()` and `layoutGrouped()`, add metric bars |
| `OpenshiftPulse/.../topology/AgentTopology.tsx` | Pass `layoutHint`/`includeMetrics`/`groupBy` props, add perspective quick-launch pills |
| `OpenshiftPulse/.../engine/agentComponents.ts` | Extend `TopologySpec` and `TopoNode` interfaces |
| `OpenshiftPulse/.../topology/__tests__/GraphRenderer.test.ts` | Add layout hint, metric bar, grouped layout, and perspective pill tests |

## Deferred (v2)

These were identified during review but deferred to avoid scope creep. Agent-composed filters make all of these possible without code changes to the filtering/layout engine:

- **Storage perspective** — PVC→PV→StorageClass→CSI for "why is PVC stuck pending?" (requires adding PV, StorageClass resource types to DependencyGraph)
- **GitOps perspective** — ArgoCD Application→Deployment sync status (requires ArgoCD API integration)
- **Extended metrics** — node conditions (DiskPressure, MemoryPressure), pod restart counts, QoS class
- **ARIA accessibility** — `role="img"` + `aria-label` on SVG elements (existing gap, separate PR)
- **SVG font size audit** — existing 8/9/10px violations in GraphRenderer (separate PR)
