# Investigation View Plan

## Problem

When a user claims an inbox item, `_generate_view_for_item()` builds a shallow view — 3 info_card components that echo the alert metadata (title, severity, namespace, status). The user sees what they already know from the inbox, not a diagnostic dashboard that helps them verify the diagnosis and act on it.

## Solution

Hybrid: Phase B produces a `viewPlan` (widget specs with data hints), claim time executes it (fetches live data, assembles layout). No hardcoded templates — the LLM decides what widgets to include based on its investigation findings.

## Design

### Phase B: viewPlan in Investigation Response

The investigation prompt in `monitor/investigations.py` is enhanced to also return a `viewPlan` field. The response schema becomes:

```json
{
  "summary": "...",
  "suspectedCause": "...",
  "recommendedFix": "...",
  "confidence": 0.85,
  "evidence": ["..."],
  "viewPlan": [
    {"kind": "chart", "title": "Memory Trend", "props": {"query": "container_memory_usage_bytes{pod=\"api-xxx\"}", "time_range": "6h"}},
    {"kind": "data_table", "title": "Recent Events", "tool": "get_events", "args": {"namespace": "prod", "minutes": 60}},
    {"kind": "resolution_tracker", "title": "Investigation", "props": {"steps": [...]}}
  ]
}
```

Each widget has:
- `kind` — component kind from the registry (chart, data_table, status_list, metric_card, resolution_tracker, action_button, topology, blast_radius, etc.)
- `title` — widget title
- `props` — static data to render directly (no tool call needed)
- OR `tool` + `args` — a read-only tool to call at render time for live data

The prompt addition injects the list of valid component kinds (from `component_registry.get_valid_kinds()`) and valid read-only tool names (from `TOOL_REGISTRY - WRITE_TOOLS`) so the LLM produces valid references. Cap: 4-6 widgets.

### Validation at Phase B Parse Time

After extracting the investigation response JSON in `_phase_b_investigate`:

1. Validate each widget's `kind` against `component_registry.get_valid_kinds()`
2. Validate each widget's `tool` against `TOOL_REGISTRY` and reject if in `WRITE_TOOLS`
3. Drop invalid widgets silently (log at debug level)
4. Store validated `view_plan` + `view_plan_at` timestamp in item metadata

### Claim-Time View Executor

New module: `sre_agent/view_executor.py`

```python
def execute_view_plan(view_plan: list[dict], item: dict) -> list[dict]:
    """Execute a viewPlan and return assembled component layout."""
```

For each widget in the plan:
- **Props-only widgets** (resolution_tracker, blast_radius, etc.): build component directly from `kind` + `props`
- **Tool-backed widgets** (get_events, list_pods, get_prometheus_query, etc.): call the tool's underlying Python function directly (not through SDK wrapper), extract the component from the `tuple[str, dict]` return

Key safeguards:
- **Timeout**: each tool call wrapped in `ThreadPoolExecutor` with 10s timeout
- **Security**: skip any tool in `WRITE_TOOLS` (defense in depth — also validated at parse time)
- **Staleness**: if `view_plan_at` is >30 minutes old, skip tool-backed widgets (use only props-only widgets). Tool args may reference pods/resources that no longer exist
- **Graceful degradation**: if a widget fails, skip it and continue. If all widgets fail, fall back to the existing info_card layout
- **Cap**: max 6 widgets executed

### Integration with inbox.py

`_generate_view_for_item()` becomes a thin dispatcher:

```python
def _generate_view_for_item(item_id, item):
    metadata = item.get("metadata", {})
    view_plan = metadata.get("view_plan", [])
    
    if view_plan:
        from .view_executor import execute_view_plan
        layout = execute_view_plan(view_plan, item)
    else:
        layout = _fallback_layout(item, metadata)  # existing info_card behavior
    
    if not layout:
        layout = _fallback_layout(item, metadata)
    
    save_view(layout=layout, ...)
```

### Tool Return Type Handling

Tools return `str | tuple[str, dict]`:
- If tuple: second element is the component spec — use it, override `title` from widget spec
- If str: wrap the text in the widget's `kind` as an `info_card` with the text as body

### What Changes Where

| File | Change |
|------|--------|
| `sre_agent/monitor/investigations.py` | Enhance prompt to request viewPlan, inject valid kinds + tool names |
| `sre_agent/inbox.py` | Store view_plan + view_plan_at in _phase_b_investigate. Simplify _generate_view_for_item to dispatch to view_executor |
| `sre_agent/view_executor.py` | **New module**: execute_view_plan with timeout, security, staleness, fallback |
| Tests | view_executor: tool-backed widgets, props-only widgets, timeout, write-tool rejection, staleness skip, all-fail fallback |
| Eval | Fleet eval scenario for investigation view quality |

### What Does NOT Change

- `view_tools.py` / `create_dashboard` — user-created dashboards unchanged
- `component_registry.py` — all needed widget kinds already exist
- Skill prompts — this is the monitor pipeline, not the chat agent
- Frontend — components already render all widget kinds

### Example: SearchPVCNotPresent After This Change

Phase B investigates, finds search PVC absent, no search workloads, 2 other PVCs healthy. Produces:

```json
"viewPlan": [
  {"kind": "data_table", "title": "PVC Inventory", "tool": "list_resources", "args": {"resource": "pvc", "namespace": "openshiftpulse"}},
  {"kind": "data_table", "title": "Deployments", "tool": "list_deployments", "args": {"namespace": "openshiftpulse"}},
  {"kind": "data_table", "title": "Recent Events", "tool": "get_events", "args": {"namespace": "openshiftpulse", "minutes": 60}},
  {"kind": "resolution_tracker", "title": "Investigation", "props": {"steps": [
    {"title": "Checked for search workloads", "status": "complete", "detail": "No elasticsearch or search deployments found"},
    {"title": "Verified PVC inventory", "status": "complete", "detail": "2 PVCs exist (agent-data, pg-data), none search-related"},
    {"title": "Conclusion: expected configuration", "status": "complete", "detail": "Search stack not deployed"}
  ]}}
]
```

User sees: actual PVC inventory, deployment list, events, and the agent's reasoning steps — not 4 label cards echoing the alert.

### Backward Compatibility

- Items with `view_plan` in metadata: new path (execute plan)
- Items with `investigation_summary` but no `view_plan`: old path (info_card fallback)
- Items with neither: no view generated (same as today)

No migration needed.
