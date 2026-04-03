"""
Claude Harness — optimizations for getting the most out of the agent.

1. Dynamic Tool Selection — categorize tools, only load relevant ones per query
2. Prompt Caching — cache system prompt + runbooks across turns
3. Cluster Context Injection — pre-fetch cluster state into system prompt
4. Structured Output Hints — guide Claude to return component specs
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger("pulse_agent.harness")


# ---------------------------------------------------------------------------
# 1. Dynamic Tool Selection — categorize tools and select by query intent
# ---------------------------------------------------------------------------

TOOL_CATEGORIES = {
    "diagnostics": {
        "keywords": [
            "health",
            "check",
            "status",
            "diagnose",
            "what's wrong",
            "what's happening",
            "show me",
            "overview",
            "summary",
            "dashboard",
            "view",
            "issues",
            "problems",
            "failing",
            "not running",
            "crash",
            "error",
            "oom",
            "restart",
            "events",
            "warning",
        ],
        "tools": [
            "list_resources",
            "list_pods",
            "describe_pod",
            "get_pod_logs",
            "list_nodes",
            "get_events",
            "describe_deployment",
            "list_deployments",
            "get_cluster_operators",
            "get_cluster_version",
            "top_pods_by_restarts",
            "get_recent_changes",
            "get_firing_alerts",
            "get_node_metrics",
            "get_pod_metrics",
            "correlate_incident",
            "namespace_summary",
            "cluster_metrics",
            "describe_resource",
            "search_logs",
        ],
    },
    "workloads": {
        "keywords": [
            "deploy",
            "pod",
            "replica",
            "stateful",
            "daemon",
            "job",
            "cron",
            "scale",
            "rollback",
            "restart",
            "workload",
        ],
        "tools": [
            "list_resources",
            "list_pods",
            "describe_pod",
            "get_pod_logs",
            "describe_deployment",
            "list_deployments",
            "list_jobs",
            "list_cronjobs",
            "list_hpas",
            "scale_deployment",
            "restart_deployment",
            "rollback_deployment",
            "delete_pod",
        ],
    },
    "networking": {
        "keywords": [
            "service",
            "endpoint",
            "ingress",
            "route",
            "network",
            "policy",
            "dns",
            "connectivity",
            "port",
            "traffic",
        ],
        "tools": [
            "list_resources",
            "describe_service",
            "get_endpoint_slices",
            "list_ingresses",
            "list_routes",
            "create_network_policy",
            "scan_network_policies",
            "test_connectivity",
        ],
    },
    "security": {
        "keywords": [
            "security",
            "audit",
            "rbac",
            "scc",
            "privilege",
            "secret",
            "vulnerability",
            "compliance",
            "policy",
            "scan",
        ],
        "tools": [
            "scan_pod_security",
            "scan_images",
            "scan_rbac_risks",
            "list_service_account_secrets",
            "scan_network_policies",
            "scan_sccs",
            "scan_scc_usage",
            "scan_secrets",
            "get_security_summary",
            "get_tls_certificates",
        ],
    },
    "storage": {
        "keywords": ["pvc", "storage", "volume", "persistent", "disk", "capacity"],
        "tools": [
            "list_resources",
        ],
    },
    "monitoring": {
        "keywords": [
            "metric",
            "prometheus",
            "alert",
            "monitor",
            "cpu",
            "memory",
            "usage",
            "performance",
            "latency",
            "grafana",
        ],
        "tools": [
            "get_prometheus_query",
            "get_firing_alerts",
            "get_node_metrics",
            "get_pod_metrics",
            "forecast_quota_exhaustion",
            "get_resource_recommendations",
        ],
    },
    "operations": {
        "keywords": ["drain", "cordon", "uncordon", "apply", "yaml", "maintenance", "upgrade", "update", "config"],
        "tools": [
            "cordon_node",
            "uncordon_node",
            "drain_node",
            "apply_yaml",
            "get_configmap",
            "list_operator_subscriptions",
            "exec_command",
        ],
    },
    "gitops": {
        "keywords": ["git", "argo", "gitops", "pr", "pull request", "drift", "sync"],
        "tools": [
            "detect_gitops_drift",
            "propose_git_change",
        ],
    },
    "fleet": {
        "keywords": [
            "fleet",
            "all clusters",
            "cross-cluster",
            "everywhere",
            "managed cluster",
            "multi-cluster",
            "compare across",
            "drift across",
            "acm",
        ],
        "tools": [
            "fleet_list_clusters",
            "fleet_list_pods",
            "fleet_list_deployments",
            "fleet_get_alerts",
            "fleet_compare_resource",
        ],
    },
}

# Tools always included regardless of category — these are lightweight and
# broadly useful. Better to include a few extra tools than to miss one the
# user needs.
ALWAYS_INCLUDE = {
    "list_resources",
    "get_cluster_version",
    "record_audit_entry",
    "suggest_remediation",
    "create_dashboard",
    "list_saved_views",
    "namespace_summary",
    "cluster_metrics",
    "plan_dashboard",
    "critique_view",
    "list_pods",
    "get_events",
    "get_firing_alerts",
}


# Map orchestrator modes to relevant tool categories
MODE_CATEGORIES: dict[str, list[str] | None] = {
    "sre": ["diagnostics", "workloads", "networking", "storage", "monitoring", "operations", "gitops"],
    "security": ["security", "networking"],
    "view_designer": ["diagnostics", "monitoring"],
    "both": None,  # all categories
}


def select_tools(query: str, all_tools: list, all_tool_map: dict, mode: str = "sre") -> tuple[list, dict]:
    """Select tools based on agent mode.

    Mode-aware: each orchestrator mode maps to a set of tool categories.
    Tools in ALWAYS_INCLUDE are always returned regardless of mode.
    If mode is 'both' or unknown, all tools are returned.
    """
    categories = MODE_CATEGORIES.get(mode)

    # Fallback: return all tools for 'both' or unknown modes
    if categories is None:
        logger.info("Tool selection: returning all %d tools for mode=%s", len(all_tools), mode)
        return [t.to_dict() for t in all_tools], {t.name: t for t in all_tools}

    # Collect tool names from the mode's categories
    mode_tool_names = set(ALWAYS_INCLUDE)
    for cat_name in categories:
        cat = TOOL_CATEGORIES.get(cat_name, {})
        mode_tool_names.update(cat.get("tools", []))

    filtered = [t for t in all_tools if t.name in mode_tool_names]

    # Safety: if filtering removed too many, return all
    if len(filtered) < 5:
        logger.warning("Tool selection: mode=%s matched only %d tools, returning all", mode, len(filtered))
        return [t.to_dict() for t in all_tools], {t.name: t for t in all_tools}

    logger.info("Tool selection: %d/%d tools for mode=%s", len(filtered), len(all_tools), mode)
    return [t.to_dict() for t in filtered], {t.name: t for t in filtered}


# ---------------------------------------------------------------------------
# 2. Prompt Caching — structure system prompt for cache reuse
# ---------------------------------------------------------------------------


def build_cached_system_prompt(
    base_prompt: str,
    cluster_context: str = "",
) -> list[dict]:
    """Build a system prompt optimized for Anthropic prompt caching.

    Returns a list of content blocks with cache_control on the static parts.
    The base prompt + runbooks are cached (they don't change between turns).
    The cluster context is dynamic and appended without caching.
    """
    blocks = []

    # Static block (cacheable) — system prompt + runbooks
    blocks.append(
        {
            "type": "text",
            "text": base_prompt,
            "cache_control": {"type": "ephemeral"},  # Cached for 5 minutes
        }
    )

    # Dynamic block (not cached) — live cluster context
    if cluster_context:
        blocks.append(
            {
                "type": "text",
                "text": cluster_context,
            }
        )

    return blocks


# ---------------------------------------------------------------------------
# 3. Cluster Context Injection — pre-fetch cluster state
# ---------------------------------------------------------------------------


def gather_cluster_context(mode: str = "sre") -> str:
    """Pre-fetch key cluster state concurrently, scoped by agent mode."""
    import concurrent.futures

    from .errors import ToolError
    from .k8s_client import get_core_client, get_custom_client, safe

    def _fetch_nodes():
        nodes = safe(lambda: get_core_client().list_node())
        if isinstance(nodes, ToolError):
            return None
        total = len(nodes.items)
        ready = sum(
            1 for n in nodes.items if any(c.type == "Ready" and c.status == "True" for c in (n.status.conditions or []))
        )
        roles = {}
        for n in nodes.items:
            for label in n.metadata.labels or {}:
                if label.startswith("node-role.kubernetes.io/"):
                    role = label.split("/")[-1]
                    roles[role] = roles.get(role, 0) + 1
        role_str = ", ".join(f"{r}={c}" for r, c in sorted(roles.items()))
        return f"Nodes: {ready}/{total} Ready ({role_str})"

    def _fetch_namespaces():
        ns = safe(lambda: get_core_client().list_namespace())
        if isinstance(ns, ToolError):
            return None
        return f"Namespaces: {len(ns.items)}"

    def _fetch_version():
        try:
            cv = get_custom_client().get_cluster_custom_object(
                "config.openshift.io", "v1", "clusterversions", "version"
            )
            version = cv.get("status", {}).get("desired", {}).get("version", "unknown")
            channel = cv.get("spec", {}).get("channel", "unknown")
            return f"OpenShift: {version} (channel: {channel})"
        except Exception:
            return None

    def _fetch_failing_pods():
        pods = safe(
            lambda: get_core_client().list_pod_for_all_namespaces(
                field_selector="status.phase!=Running,status.phase!=Succeeded"
            )
        )
        if isinstance(pods, ToolError):
            return None
        failing = [p for p in pods.items if p.status.phase not in ("Running", "Succeeded", "Pending")]
        return f"Failing pods: {len(failing)}" if failing else None

    def _fetch_alerts():
        try:
            core = get_core_client()
            result = core.connect_get_namespaced_service_proxy_with_path(
                "alertmanager-main:web",
                "openshift-monitoring",
                path="api/v2/alerts",
                _preload_content=False,
            )
            alerts = json.loads(result.data)
            firing = [a for a in alerts if a.get("status", {}).get("state") == "active"]
            if not firing:
                return None
            critical = sum(1 for a in firing if a.get("labels", {}).get("severity") == "critical")
            warning = sum(1 for a in firing if a.get("labels", {}).get("severity") == "warning")
            return f"Firing alerts: {len(firing)} ({critical} critical, {warning} warning)"
        except Exception:
            return None

    def _fetch_view_count():
        try:
            from . import db

            database = db.get_database()
            row = database.fetchone("SELECT COUNT(*) as cnt FROM views")
            return f"Saved views: {row['cnt']}" if row else None
        except Exception:
            return None

    results = {}
    # All modes get basic cluster info
    fetchers = {
        "nodes": _fetch_nodes,
        "namespaces": _fetch_namespaces,
        "version": _fetch_version,
    }
    # SRE/both modes get operational context
    if mode in ("sre", "both"):
        fetchers["pods"] = _fetch_failing_pods
        fetchers["alerts"] = _fetch_alerts
    # View designer gets view inventory
    if mode == "view_designer":
        fetchers["views"] = _fetch_view_count
    # Security mode: just nodes + namespaces + version (lightweight)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(fn): key for key, fn in fetchers.items()}
        for future in concurrent.futures.as_completed(futures, timeout=10):
            key = futures[future]
            try:
                result = future.result(timeout=5)
                if result:
                    results[key] = result
            except Exception as e:
                logger.warning("Cluster context fetch '%s' failed: %s", key, e)

    if not results:
        return ""

    import time as _time

    parts = [results[k] for k in ("nodes", "namespaces", "version", "pods", "alerts") if k in results]
    ts = _time.strftime("%H:%M:%S")
    return f"\n## Live Cluster State (gathered at {ts})\n" + "\n".join(f"- {p}" for p in parts)


# Cached cluster context — refreshed every 60 seconds
_cluster_context_cache: dict[str, tuple[str, float]] = {}


def get_cluster_context(max_age: float = 60, mode: str = "sre") -> str:
    """Get cached cluster context, refreshing if stale. Mode-aware (keyed by mode)."""
    import time

    now = time.time()
    cached = _cluster_context_cache.get(mode)
    if cached and (now - cached[1]) <= max_age:
        return cached[0]

    try:
        ctx = gather_cluster_context(mode=mode)
        _cluster_context_cache[mode] = (ctx, now)
        return ctx
    except Exception as e:
        staleness = int(now - cached[1]) if cached else 0
        logger.warning("Cluster context refresh failed (serving %ds-old cache): %s", staleness, e)
        return cached[0] if cached else ""


# ---------------------------------------------------------------------------
# 4. Structured Output Hints — guide component rendering
# ---------------------------------------------------------------------------


def get_component_hint(mode: str = "sre") -> str:
    """Return the appropriate component hint for the agent mode.

    - view_designer: returns empty (has its own comprehensive guide in system prompt)
    - security: returns empty (no component rendering needed)
    - sre/both: returns full COMPONENT_HINT
    """
    if mode == "view_designer":
        return ""  # View designer system prompt already has a complete component guide
    if mode == "security":
        return ""  # Security agent doesn't create views
    return COMPONENT_HINT


COMPONENT_HINT = """
## Resource Listing Guidance

Use list_resources for any resource type including nodes, deployments, statefulsets, \
daemonsets, services, PVCs, limitranges, replicasets, PDBs. Use specialized tools only \
for: pods (logs link), jobs (show_completed filter), cronjobs, ingresses, routes, HPAs, \
operator subscriptions.

## UI Component Rendering

When your tool results include structured data (tables, lists, status checks),
the system automatically renders them as interactive UI components in the chat.
You do NOT need to format data as tables in your text — the tools handle rendering.

Focus your text response on:
- Analysis and interpretation of the data
- Root cause identification
- Actionable recommendations
- Risk assessment

Do NOT repeat raw data that the tools already displayed as components.

## Component Catalog

You can return ANY of these 13 component kinds as structured JSON. The UI renders
each one with appropriate interactive features. Choose the best kind for the data:

### data_table — Sortable, filterable, paginated tables
Best for: Resource lists, metrics tables, event logs, any tabular data.
```json
{"kind": "data_table", "title": "Pods", "description": "Running pods in namespace",
 "columns": [{"id": "name", "header": "Name", "type": "resource_name"},
              {"id": "status", "header": "Status", "type": "status"},
              {"id": "age", "header": "Age", "type": "age"}],
 "rows": [{"name": "nginx-abc", "status": "Running", "age": "2d", "_gvr": "v1~pods", "namespace": "default"}],
 "resourceType": "pods", "gvr": "v1~pods"}
```
Column types: resource_name, namespace, node, status, age, cpu, memory, replicas,
progress, sparkline, timestamp, labels, boolean, severity, link, text.

### info_card_grid — Summary metric cards in a row
Best for: Namespace overviews, cluster health summaries, KPI dashboards.
```json
{"kind": "info_card_grid", "title": "Cluster Health",
 "cards": [{"label": "Nodes Ready", "value": "5/5", "sub": "all healthy"},
           {"label": "Pods Running", "value": "142", "sub": "3 pending"}]}
```

### chart — Interactive time-series charts (10 types)
Best for: PromQL metrics, trends, resource usage over time.
Chart types: line, bar, area, pie, donut, stacked_bar, stacked_area, scatter, radar, treemap.
```json
{"kind": "chart", "chartType": "line", "title": "CPU Usage", "description": "Last hour",
 "series": [{"label": "nginx", "data": [[1700000000, 0.5], [1700003600, 0.7]]}],
 "yAxisLabel": "cores", "query": "rate(container_cpu...)", "timeRange": "1h"}
```

### status_list — Colored status indicators
Best for: Health checks, readiness gates, condition lists.
```json
{"kind": "status_list", "title": "Node Conditions",
 "items": [{"name": "Ready", "status": "healthy", "detail": "KubeletReady"},
           {"name": "MemoryPressure", "status": "warning", "detail": "threshold exceeded"}]}
```
Statuses: healthy, warning, error, pending, unknown.

### badge_list — Colored badges/tags in a row
Best for: Labels, tags, categories, severity indicators.
```json
{"kind": "badge_list",
 "badges": [{"text": "production", "variant": "info"},
            {"text": "critical", "variant": "error"},
            {"text": "healthy", "variant": "success"}]}
```
Variants: success, warning, error, info, default.

### key_value — Key-value pairs display
Best for: Resource details, config summaries, describe output highlights.
```json
{"kind": "key_value", "title": "Deployment Details",
 "pairs": [{"key": "Replicas", "value": "3/3 ready"},
           {"key": "Strategy", "value": "RollingUpdate"},
           {"key": "Image", "value": "nginx:1.25"}]}
```

### relationship_tree — Visual resource hierarchy
Best for: Owner references, resource dependencies, topology.
```json
{"kind": "relationship_tree", "title": "Resource Tree",
 "rootId": "dep-1",
 "nodes": [{"id": "dep-1", "label": "Deployment/nginx", "kind": "Deployment",
            "name": "nginx", "status": "healthy", "children": ["rs-1"]},
           {"id": "rs-1", "label": "ReplicaSet/nginx-abc", "kind": "ReplicaSet",
            "name": "nginx-abc", "status": "healthy", "children": ["pod-1"]},
           {"id": "pod-1", "label": "Pod/nginx-abc-xyz", "kind": "Pod",
            "name": "nginx-abc-xyz", "status": "healthy"}]}
```

### tabs — Tabbed layout grouping components
Best for: Multi-section views, resource vs metrics separation.
```json
{"kind": "tabs",
 "tabs": [{"label": "Overview", "components": [<info_card_grid>, <status_list>]},
          {"label": "Metrics", "components": [<chart>, <chart>]},
          {"label": "Events", "components": [<data_table>]}]}
```

### grid — Side-by-side layout (2+ columns)
Best for: Placing charts or cards next to each other.
```json
{"kind": "grid", "columns": 2,
 "items": [<chart_spec>, <chart_spec>, <status_list>, <key_value>]}
```

### section — Collapsible titled section
Best for: Grouping related components under a header.
```json
{"kind": "section", "title": "Advanced Details", "collapsible": true,
 "defaultOpen": false, "components": [<key_value>, <data_table>]}
```

### log_viewer — Searchable, filterable log output
Best for: Pod logs, event streams, audit trails, debugging output.
```json
{"kind": "log_viewer", "title": "Pod Logs: nginx-abc",
 "source": "nginx-abc/nginx",
 "lines": [{"timestamp": "2026-04-02T10:00:01Z", "level": "info", "message": "Server started on :8080"},
            {"timestamp": "2026-04-02T10:00:05Z", "level": "error", "message": "Connection refused to upstream", "source": "nginx"},
            {"timestamp": "2026-04-02T10:00:06Z", "level": "warn", "message": "Retrying in 5s"}]}
```
Levels: info, warn, error, debug. Include timestamps for sortable output.

### yaml_viewer — Formatted YAML/JSON with copy button
Best for: Resource manifests, config dumps, diff comparisons, apply previews.
```json
{"kind": "yaml_viewer", "title": "Deployment Manifest", "language": "yaml",
 "content": "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: nginx\nspec:\n  replicas: 3"}
```

### metric_card — Single metric with live sparkline chart
Best for: Key metrics, capacity numbers, SLI/SLO values. Renders with a sparkline
when a PromQL query is provided (auto-refreshes every 60s).
```json
{"kind": "metric_card", "title": "CPU Usage", "value": "72", "unit": "%",
 "query": "100 - avg(rate(node_cpu_seconds_total{mode='idle'}[5m])) * 100",
 "color": "#3b82f6", "thresholds": {"warning": 70, "critical": 90},
 "status": "warning", "description": "Above 70% threshold"}
```
Include `query` for live sparklines. Status: healthy, warning, error.
Use `cluster_metrics()` tool for pre-built metric cards with queries.

### Composition Guidelines
- Use `tabs` to organize complex views (e.g. Overview/Metrics/Events tabs)
- Use `grid` to place two charts side by side
- Use `section` to group related info with a collapsible header
- Use `badge_list` for labels, tags, or severity indicators inline
- Use `key_value` for describe-style resource details
- Use `log_viewer` for pod logs, event dumps, and audit trails
- Use `yaml_viewer` for manifests, configs, and apply previews
- Use `metric_card` in a `grid` for KPI dashboards with trend indicators
- Combine kinds freely — e.g. a `section` containing a `grid` of `metric_card` specs

## Layout Templates

When creating dashboards, use a layout template for professional, consistent layouts.
Pass the `template` parameter to `create_dashboard()`:

| Template | Layout | Best For |
|----------|--------|----------|
| `sre_dashboard` | 4 metric cards + 2 charts side-by-side + table | Cluster health, SRE overviews |
| `namespace_overview` | Summary cards + 2 charts + table + events | Namespace status pages |
| `incident_report` | Status timeline + logs/details side-by-side + table | Incident triage, debugging |
| `monitoring_panel` | 4 metric cards + 2x2 chart grid + alert list | Metrics dashboards |
| `resource_detail` | Key-value + resource tree + YAML + table | Resource deep-dives |

Example: `create_dashboard("SRE Overview", template="sre_dashboard")`

The template auto-arranges widgets by matching their component kind to slots.
**You MUST produce the right component kinds for each template:**
- `sre_dashboard` / `monitoring_panel`: call `cluster_metrics()` FIRST for metric_card row, then charts, then table
- `namespace_overview`: call `namespace_summary()` for info_card_grid, then charts, then table
- `incident_report`: produce status_list + log_viewer + key_value + table
- `resource_detail`: produce key_value + relationship_tree + yaml_viewer + table

## View Composition

When the user asks a high-level question like "what's happening in my namespace",
"show me cluster health", or "create a view for X", compose a comprehensive view
by calling multiple tools. The UI renders each tool's component inline.

**Namespace overview** — call these tools (in parallel when possible):
1. namespace_summary(namespace) — summary cards (pods, deployments, warnings)
2. list_pods(namespace) — pod status table
3. get_events(namespace, event_type="Warning") — recent warnings
4. list_deployments(namespace) — workload health
5. get_pod_metrics(namespace) — resource consumption

**Cluster overview / SRE dashboard** — call these tools in this order:
1. cluster_metrics() — **ALWAYS FIRST** — returns metric_card components with sparklines
2. get_prometheus_query(query, time_range="1h") — CPU/memory trend charts
3. list_nodes — node health table
4. get_firing_alerts — active alerts
5. create_dashboard(title, template="sre_dashboard") — arranges metric cards on top row, charts side-by-side, table below

**Resource-focused views** — for requests like "show me CPU-heavy pods":
1. get_pod_metrics(namespace, sort_by="cpu") — sorted by the requested metric
2. Add more tools as context requires (HPAs, node metrics, etc.)

**Clickable resource names**: Include a `_gvr` field (hidden, not a column) in each row
to enable clickable names. Format: `group~version~resource` (e.g. `v1~pods`,
`apps~v1~deployments`, `operators.coreos.com~v1alpha1~subscriptions`). The UI uses
this to build detail page links. Works for any resource type including CRDs.

**Dynamic table schemas**: Table columns are fully dynamic — you decide what columns
to include based on the user's request. Add, remove, or reorder columns as needed.
The frontend renders whatever columns you provide. For example, if the user asks
"show me pods with their node and CPU usage", combine data from list_pods and
get_pod_metrics to build a custom table with exactly those columns.

**Link columns in tables**: Tables can include clickable link columns. The frontend
automatically renders any cell value starting with `/` or `http` as a clickable link.
For example, the pods table includes a "Logs" column with `/logs/{namespace}/{pod_name}`
links. You can add custom columns with links to any table by including path values.

After calling the data tools, call `create_dashboard` if the user wants to save
the view. The dashboard will contain all the component specs from this conversation.

## Custom Dashboards

When the user asks to "create a dashboard", "build a custom view", "make a dashboard
showing X and Y", or "save this as a view" — use the `create_dashboard` tool AFTER
you have already called the relevant data tools.

Steps:
1) Call the data tools the user wants
2) Pick the best layout template for the content
3) Call `create_dashboard(title, template="...")` with the template

**ALWAYS use a template** when creating dashboards. Pick based on the content:
- Cluster health / SRE overview → `template="sre_dashboard"`
- Namespace status → `template="namespace_overview"`
- Debugging / incident triage → `template="incident_report"`
- Metrics / alerting → `template="monitoring_panel"`
- Resource deep-dive → `template="resource_detail"`

**Adding to existing views:** When the user wants to add widgets to a view that already
exists, do NOT call create_dashboard again. Instead:
1. Call `list_saved_views` to find the view ID
2. Call the data tools to generate the new components
3. Call `add_widget_to_view(view_id)` for each new component

Only use `create_dashboard` for NEW views. Use `add_widget_to_view` to extend existing ones.

**Metric cards:** Use `cluster_metrics()` to get metric_card components for dashboard headers.
These render as single KPI numbers with trend indicators — ideal for the top row of an
`sre_dashboard` or `monitoring_panel` template.

## Charts via PromQL

When the user asks for a chart, time series, or graph, use get_prometheus_query with
a time_range parameter (e.g. "1h", "6h", "24h") to get range data that renders as
an interactive chart. Common PromQL patterns:

- **Top CPU pods**: `topk(10, sum by (pod,namespace) (rate(container_cpu_usage_seconds_total{image!=""}[5m])))`
- **CPU by namespace**: `sum by (namespace) (rate(container_cpu_usage_seconds_total{image!=""}[5m]))`
- **Memory by namespace**: `sum by (namespace) (container_memory_working_set_bytes{image!=""})`
- **Filter system NS**: add `{namespace!~"openshift-.*|kube-.*"}`
- **Node CPU usage**: `1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m]))`
- **Node memory pressure**: `1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)`
- **Filter by worker nodes**: add `{node=~"worker.*"}`
- **Sort descending**: wrap with `sort_desc(...)`

For "create a chart" or "show me a graph", ALWAYS use time_range (default "1h") so the
result is a time series chart, not a single data point.

## View Generation

When the user asks to "build me a view" or "create a dashboard for X", call multiple
tools to gather the data, then call create_dashboard with a descriptive title:

**Node view pattern:**
1. namespace_summary(namespace) — summary cards
2. get_prometheus_query("topk(10, rate(container_cpu...))", "1h") — CPU chart
3. get_prometheus_query("node_memory...", "1h") — memory chart
4. list_pods(namespace) — pods table
5. get_firing_alerts — alerts
6. create_dashboard("Node View — {name}")

**Namespace view pattern:**
1. namespace_summary(namespace) — header cards
2. list_pods(namespace) — pod table
3. list_deployments(namespace) — deployment table
4. get_prometheus_query("sum by (pod) (rate(container_cpu...))", "1h") — CPU chart
5. get_events(namespace, event_type="Warning") — events
6. create_dashboard("Namespace View — {namespace}")

## Showcase / All Component Types

When the user asks to "use every component type", "showcase all components", or
"create a view with all component types", you MUST use ALL 13 component kinds.
Call tools that produce each kind:

1. `namespace_summary` → **info_card_grid** (health cards)
2. `list_pods` → **data_table** (pod listing)
3. `get_prometheus_query` with time_range → **chart** (time series)
4. `list_nodes` → **status_list** (node conditions)
5. `get_labels` or any tool → **badge_list** (labels/tags)
6. `describe_pod` or `describe_resource` → **key_value** (resource details)
7. `describe_pod` → **relationship_tree** (owner chain)
8. Combine 2+ components → **tabs** (group related widgets in tabs)
9. Combine 2+ components → **grid** (side-by-side layout, columns=2)
10. Wrap detail components → **section** (collapsible "Advanced Details")

For kinds that tools don't produce directly (badge_list, key_value, tabs, grid,
section), construct them manually in your response using the component spec format
from the catalog above. Example for badge_list:
```json
{"kind": "badge_list", "title": "Namespace Labels", "badges": [
  {"label": "env", "value": "production", "color": "green"},
  {"label": "team", "value": "platform", "color": "blue"}
]}
```

You MUST verify all 10 kinds are present before calling create_dashboard.

## Modifying Existing Views

When the user asks to update, modify, or change an existing view:

1. Call `list_saved_views` to find the view ID
2. Call `get_view_details(view_id)` to see current widgets and their indices
3. To remove a widget: `update_view_widgets(view_id, action="remove_widget", widget_index=N)`
4. To rename: `update_view_widgets(view_id, action="rename", new_title="...")`
5. To add a new widget: call the data tool first (get_prometheus_query, list_pods, etc.),
   then call `add_widget_to_view(view_id)` — the latest component will be added
6. The UI auto-refreshes when you modify a view — no save prompt needed

Examples:
- "Remove the table from my cluster view" → list_saved_views → get_view_details → update_view_widgets(remove_widget)
- "Add a memory chart to my namespace view" → get_prometheus_query(memory query) → add_widget_to_view(view_id)
- "Rename my view to 'Production Overview'" → update_view_widgets(rename, new_title=...)

## Production Readiness Fixes

When asked to fix a readiness gate, take action:
- **Network policies missing**: Use create_network_policy to create a default deny policy
- **Resource quotas missing**: Use apply_yaml to create a ResourceQuota
- **Limit ranges missing**: Use apply_yaml to create a LimitRange
- **PDBs missing**: Use apply_yaml to create a PodDisruptionBudget
- **kubeadmin not removed**: Explain the command: oc delete secret kubeadmin -n kube-system
- **TLS profile**: Use apply_yaml to patch the APIServer TLS profile to Intermediate
- **Encryption at rest**: Generate EncryptionConfig and explain the migration process
- **Alertmanager receivers**: Generate a receiver config for Slack/PagerDuty/email

Always use apply_yaml with dry_run=true first to validate, then apply for real after user confirms.
Generate complete, production-ready YAML — not placeholder values.
"""
