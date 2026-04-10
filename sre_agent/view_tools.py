"""Tools for creating custom dashboard views from conversation context."""

from __future__ import annotations

import contextvars
import json
import uuid

from anthropic import beta_tool

from .tool_registry import register_tool

# Structured signal prefix — tools return this instead of magic string markers.
# The API layer checks tool results for this prefix and extracts the JSON signal.
SIGNAL_PREFIX = "__SIGNAL__"


def _signal(signal_type: str, message: str, **kwargs) -> str:
    """Return a structured signal that the API layer can process.

    The returned string contains both a human-readable message (for Claude)
    and a JSON signal (for the API layer) separated by the SIGNAL_PREFIX.
    """
    payload = {"type": signal_type, **kwargs}
    return f"{message}\n{SIGNAL_PREFIX}{json.dumps(payload)}"


# Context variable for current user identity — async-safe and propagates
# across asyncio.to_thread boundaries (unlike threading.local or globals).
_current_user_var: contextvars.ContextVar[str] = contextvars.ContextVar("current_user", default="anonymous")


def set_current_user(owner: str) -> None:
    """Set the current user for view tools (called by API layer per-request)."""
    _current_user_var.set(owner)


def get_current_user() -> str:
    """Get the current user identity."""
    return _current_user_var.get()


@beta_tool
def create_dashboard(title: str, description: str = "", template: str = "") -> str:
    """Create a custom dashboard view. ALWAYS call critique_view(view_id) immediately after this to check quality.

    Layout is computed automatically based on component types — no template needed.

    Args:
        title: Name for the dashboard (e.g. "SRE Overview", "Node Health").
        description: Brief description of what the dashboard shows.
        template: Deprecated — layout is now automatic. Ignored if provided.
    """
    view_id = f"cv-{uuid.uuid4().hex[:12]}"
    kwargs = {"view_id": view_id, "title": title, "description": description}
    if template:
        kwargs["template"] = template
    return _signal(
        "view_spec",
        f"Created view '{title}' with ID {view_id}. "
        f"The dashboard is now saved and visible to the user. "
        f"Tell the user: 'Here is your dashboard. Would you like any changes?'",
        **kwargs,
    )


@beta_tool
def namespace_summary(namespace: str) -> str:
    """Get a high-level summary of a namespace: pod counts by status, deployment health, warning events, and resource usage. Use this as the first tool when the user asks for an overview of a namespace.

    Args:
        namespace: Kubernetes namespace to summarize.
    """
    from .errors import ToolError
    from .k8s_client import get_apps_client, get_core_client, safe

    core = get_core_client()

    # Pod counts
    pods_result = safe(lambda: core.list_namespaced_pod(namespace, limit=500))
    if isinstance(pods_result, ToolError):
        return str(pods_result)

    total_pods = len(pods_result.items)
    running = sum(1 for p in pods_result.items if p.status.phase == "Running")
    failed = sum(1 for p in pods_result.items if p.status.phase == "Failed")
    pending = sum(1 for p in pods_result.items if p.status.phase == "Pending")
    crashloop = sum(
        1
        for p in pods_result.items
        for cs in (p.status.container_statuses or [])
        if cs.state and cs.state.waiting and cs.state.waiting.reason == "CrashLoopBackOff"
    )

    # Deployment counts
    apps = get_apps_client()
    deps_result = safe(lambda: apps.list_namespaced_deployment(namespace, limit=500))
    total_deps = 0
    healthy_deps = 0
    degraded_deps = 0
    if not isinstance(deps_result, ToolError):
        total_deps = len(deps_result.items)
        for dep in deps_result.items:
            ready = dep.status.ready_replicas or 0
            desired = dep.status.replicas or 0
            if ready == desired and desired > 0:
                healthy_deps += 1
            elif ready < desired:
                degraded_deps += 1

    # Additional resource counts
    sts_result = safe(lambda: apps.list_namespaced_stateful_set(namespace, limit=500))
    sts_count = 0 if isinstance(sts_result, ToolError) else len(sts_result.items)

    ds_result = safe(lambda: apps.list_namespaced_daemon_set(namespace, limit=500))
    ds_count = 0 if isinstance(ds_result, ToolError) else len(ds_result.items)

    svc_result = safe(lambda: core.list_namespaced_service(namespace, limit=500))
    svc_count = 0 if isinstance(svc_result, ToolError) else len(svc_result.items)

    cm_result = safe(lambda: core.list_namespaced_config_map(namespace, limit=500))
    cm_count = 0 if isinstance(cm_result, ToolError) else len(cm_result.items)

    # Warning events (last hour)
    events_result = safe(lambda: core.list_namespaced_event(namespace, field_selector="type=Warning"))
    warning_count = 0
    if not isinstance(events_result, ToolError):
        warning_count = len(events_result.items)

    # Build text summary
    text = (
        f"Namespace '{namespace}' summary:\n"
        f"  Pods: {total_pods} total — {running} running, {pending} pending, "
        f"{failed} failed, {crashloop} crashlooping\n"
        f"  Deployments: {total_deps} total — {healthy_deps} healthy, "
        f"{degraded_deps} degraded\n"
        f"  StatefulSets: {sts_count} | DaemonSets: {ds_count} | "
        f"Services: {svc_count} | ConfigMaps: {cm_count}\n"
        f"  Warning events: {warning_count}"
    )

    # Build resource counts component (clickable summary cards)
    resource_counts = {
        "kind": "resource_counts",
        "title": f"{namespace} Resources",
        "namespace": namespace,
        "items": [
            {"resource": "pods", "count": total_pods, "gvr": "v1~pods", "status": "error" if failed > 0 else "healthy"},
            {
                "resource": "deployments",
                "count": total_deps,
                "gvr": "apps~v1~deployments",
                "status": "warning" if degraded_deps > 0 else "healthy",
            },
            {"resource": "statefulsets", "count": sts_count, "gvr": "apps~v1~statefulsets"},
            {"resource": "daemonsets", "count": ds_count, "gvr": "apps~v1~daemonsets"},
            {"resource": "services", "count": svc_count, "gvr": "v1~services"},
            {"resource": "configmaps", "count": cm_count, "gvr": "v1~configmaps"},
            {
                "resource": "events",
                "count": warning_count,
                "gvr": "v1~events",
                "status": "warning" if warning_count > 0 else "healthy",
            },
        ],
    }

    # Build metric cards with PromQL sparklines (clickable)
    cards = [
        {
            "kind": "metric_card",
            "title": "Pods Running",
            "value": str(running),
            "status": "healthy" if failed + crashloop == 0 else "warning",
            "description": f"of {total_pods} total",
            "query": f"count(kube_pod_status_phase{{namespace=\"{namespace}\",phase='Running'}})",
            "color": "#10b981",
            "link": f"/r/v1~pods?ns={namespace}",
        },
        {
            "kind": "metric_card",
            "title": "Pod Restarts",
            "value": str(crashloop),
            "status": "healthy" if crashloop == 0 else "error",
            "description": f"{failed} failed",
            "query": f'sum(rate(kube_pod_container_status_restarts_total{{namespace="{namespace}"}}[5m]))',
            "color": "#ef4444" if crashloop > 0 else "#10b981",
            "link": f"/r/v1~pods?ns={namespace}",
        },
        {
            "kind": "metric_card",
            "title": "Deployments",
            "value": f"{healthy_deps}/{total_deps}",
            "status": "healthy" if degraded_deps == 0 else "warning",
            "description": "healthy",
            "link": f"/r/apps~v1~deployments?ns={namespace}",
        },
        {
            "kind": "metric_card",
            "title": "Warnings",
            "value": str(warning_count),
            "status": "healthy" if warning_count == 0 else "warning",
            "description": "active events",
            "query": f"count(kube_event_count{{namespace=\"{namespace}\",type='Warning'}}) or vector(0)",
            "color": "#f59e0b" if warning_count > 0 else "#10b981",
            "link": f"/r/v1~events?ns={namespace}",
        },
    ]
    component = {
        "kind": "grid",
        "title": f"{namespace} Overview",
        "description": f"Key health indicators for the {namespace} namespace",
        "columns": 2,
        "items": [resource_counts] + cards,
    }
    return (text, component)


@beta_tool
def cluster_metrics(category: str = "overview") -> str:
    """Get key cluster metrics as metric cards for dashboard headers.

    Returns different KPI cards based on the category — pick the one that
    matches the dashboard topic, not always the generic overview.

    Args:
        category: One of: 'overview' (nodes, pods, CPU%, memory%),
                  'network' (traffic in/out, errors, dropped packets),
                  'storage' (PVC count, disk usage, PV available),
                  'security' (firing alerts, degraded operators, RBAC risks),
                  'workloads' (deployments, replicas, restarts, HPA),
                  'control_plane' (API latency, etcd leader, scheduler).
    """
    from .errors import ToolError
    from .k8s_client import get_core_client, safe

    # Category-specific metric card definitions
    _CATEGORY_CARDS: dict[str, list[dict]] = {
        "overview": [
            {
                "kind": "metric_card",
                "title": "Nodes Ready",
                "value": "",
                "query": "sum(kube_node_status_condition{condition='Ready',status='true'})",
                "description": "Healthy cluster nodes",
                "color": "#10b981",
                "thresholds": {"warning": 2, "critical": 1},
                "link": "/compute",
            },
            {
                "kind": "metric_card",
                "title": "Pods Running",
                "value": "",
                "query": "count(kube_pod_status_phase{phase='Running'})",
                "description": "Active workload pods",
                "color": "#3b82f6",
                "link": "/workloads",
            },
            {
                "kind": "metric_card",
                "title": "Cluster CPU",
                "value": "",
                "unit": "%",
                "query": "100 - avg(rate(node_cpu_seconds_total{mode='idle'}[5m])) * 100",
                "color": "#3b82f6",
                "thresholds": {"warning": 70, "critical": 90},
            },
            {
                "kind": "metric_card",
                "title": "Cluster Memory",
                "value": "",
                "unit": "%",
                "query": "100 - (sum(node_memory_MemAvailable_bytes) / sum(node_memory_MemTotal_bytes)) * 100",
                "color": "#8b5cf6",
                "thresholds": {"warning": 80, "critical": 95},
            },
        ],
        "network": [
            {
                "kind": "metric_card",
                "title": "Network Receive",
                "value": "",
                "unit": "B/s",
                "query": "sum(rate(container_network_receive_bytes_total[5m]))",
                "description": "Cluster inbound traffic",
                "color": "#3b82f6",
            },
            {
                "kind": "metric_card",
                "title": "Network Transmit",
                "value": "",
                "unit": "B/s",
                "query": "sum(rate(container_network_transmit_bytes_total[5m]))",
                "description": "Cluster outbound traffic",
                "color": "#8b5cf6",
            },
            {
                "kind": "metric_card",
                "title": "Packet Drops",
                "value": "",
                "query": "sum(rate(container_network_receive_packets_dropped_total[5m])) + sum(rate(container_network_transmit_packets_dropped_total[5m]))",
                "description": "Total dropped packets/s",
                "color": "#f59e0b",
                "thresholds": {"warning": 10, "critical": 100},
            },
            {
                "kind": "metric_card",
                "title": "Network Errors",
                "value": "",
                "query": "sum(rate(container_network_receive_errors_total[5m])) + sum(rate(container_network_transmit_errors_total[5m]))",
                "description": "Total network errors/s",
                "color": "#ef4444",
                "thresholds": {"warning": 1, "critical": 10},
            },
        ],
        "storage": [
            {
                "kind": "metric_card",
                "title": "PVCs Bound",
                "value": "",
                "query": "count(kube_persistentvolumeclaim_status_phase{phase='Bound'})",
                "description": "Bound persistent volume claims",
                "color": "#10b981",
            },
            {
                "kind": "metric_card",
                "title": "PVCs Pending",
                "value": "",
                "query": "count(kube_persistentvolumeclaim_status_phase{phase='Pending'}) or vector(0)",
                "description": "Waiting for provisioning",
                "color": "#f59e0b",
                "thresholds": {"warning": 1, "critical": 5},
            },
            {
                "kind": "metric_card",
                "title": "Disk Usage",
                "value": "",
                "unit": "%",
                "query": "100 - (sum(node_filesystem_avail_bytes{device=~'/.*'}) / sum(node_filesystem_size_bytes{device=~'/.*'})) * 100",
                "description": "Cluster filesystem usage",
                "color": "#8b5cf6",
                "thresholds": {"warning": 80, "critical": 90},
            },
            {
                "kind": "metric_card",
                "title": "PVs Available",
                "value": "",
                "query": "count(kube_persistentvolume_status_phase{phase='Available'}) or vector(0)",
                "description": "Unbound persistent volumes",
                "color": "#3b82f6",
            },
        ],
        "security": [
            {
                "kind": "metric_card",
                "title": "Firing Alerts",
                "value": "",
                "query": "count(ALERTS{alertstate='firing'}) or vector(0)",
                "description": "Active alert count",
                "color": "#ef4444",
                "thresholds": {"warning": 1, "critical": 5},
            },
            {
                "kind": "metric_card",
                "title": "Operators Available",
                "value": "",
                "unit": "%",
                "query": "sum(cluster_operator_up == 1) / count(cluster_operator_up) * 100",
                "description": "Cluster operator health",
                "color": "#10b981",
                "thresholds": {"warning": 95, "critical": 90},
            },
            {
                "kind": "metric_card",
                "title": "Targets Down",
                "value": "",
                "unit": "%",
                "query": "100 * (1 - sum(up) / count(up))",
                "description": "Unreachable Prometheus targets",
                "color": "#f59e0b",
                "thresholds": {"warning": 5, "critical": 15},
            },
            {
                "kind": "metric_card",
                "title": "API Error Rate",
                "value": "",
                "unit": "%",
                "query": "sum(rate(apiserver_request_total{code=~'5..'}[5m])) / sum(rate(apiserver_request_total[5m])) * 100",
                "description": "API server 5xx rate",
                "color": "#ef4444",
                "thresholds": {"warning": 1, "critical": 5},
            },
        ],
        "workloads": [
            {
                "kind": "metric_card",
                "title": "Deployments",
                "value": "",
                "query": "count(kube_deployment_status_replicas)",
                "description": "Total deployments",
                "color": "#3b82f6",
            },
            {
                "kind": "metric_card",
                "title": "Unavailable Replicas",
                "value": "",
                "query": "sum(kube_deployment_status_replicas_unavailable) or vector(0)",
                "description": "Replicas not ready",
                "color": "#ef4444",
                "thresholds": {"warning": 1, "critical": 5},
            },
            {
                "kind": "metric_card",
                "title": "Pod Restarts",
                "value": "",
                "query": "sum(increase(kube_pod_container_status_restarts_total[1h]))",
                "description": "Restarts in last hour",
                "color": "#f59e0b",
                "thresholds": {"warning": 10, "critical": 50},
            },
            {
                "kind": "metric_card",
                "title": "HPA Active",
                "value": "",
                "query": "count(kube_horizontalpodautoscaler_status_current_replicas)",
                "description": "Active autoscalers",
                "color": "#8b5cf6",
            },
        ],
        "control_plane": [
            {
                "kind": "metric_card",
                "title": "API Latency p99",
                "value": "",
                "unit": "s",
                "query": "histogram_quantile(0.99, sum(rate(apiserver_request_duration_seconds_bucket{verb!~'WATCH|CONNECT'}[5m])) by (le))",
                "description": "99th percentile latency",
                "color": "#3b82f6",
                "thresholds": {"warning": 1, "critical": 5},
            },
            {
                "kind": "metric_card",
                "title": "etcd Leader",
                "value": "",
                "query": "max(etcd_server_has_leader)",
                "description": "1 = has leader",
                "color": "#10b981",
            },
            {
                "kind": "metric_card",
                "title": "API Request Rate",
                "value": "",
                "unit": "/s",
                "query": "sum(rate(apiserver_request_total[5m]))",
                "description": "Requests per second",
                "color": "#8b5cf6",
            },
            {
                "kind": "metric_card",
                "title": "Scheduler Latency",
                "value": "",
                "unit": "s",
                "query": "histogram_quantile(0.99, sum(rate(scheduler_e2e_scheduling_duration_seconds_bucket[5m])) by (le))",
                "description": "p99 scheduling latency",
                "color": "#f59e0b",
                "thresholds": {"warning": 1, "critical": 10},
            },
        ],
    }

    if category not in _CATEGORY_CARDS:
        category = "overview"

    cards = _CATEGORY_CARDS[category]

    # For overview, enrich with live K8s data
    if category == "overview":
        core = get_core_client()
        nodes_result = safe(lambda: core.list_node())
        node_count = 0
        nodes_ready = 0
        if not isinstance(nodes_result, ToolError):
            node_count = len(nodes_result.items)
            nodes_ready = sum(
                1
                for n in nodes_result.items
                for c in (n.status.conditions or [])
                if c.type == "Ready" and c.status == "True"
            )

        pods_result = safe(lambda: core.list_pod_for_all_namespaces(limit=1000))
        pod_count = 0
        pods_running = 0
        pods_failing = 0
        if not isinstance(pods_result, ToolError):
            pod_count = len(pods_result.items)
            pods_running = sum(1 for p in pods_result.items if p.status.phase == "Running")
            pods_failing = sum(1 for p in pods_result.items if p.status.phase in ("Failed", "Unknown"))

        cards = [
            {
                **cards[0],
                "value": f"{nodes_ready}/{node_count}",
                "status": "healthy" if nodes_ready == node_count else "warning",
            },
            {
                **cards[1],
                "value": str(pods_running),
                "status": "healthy" if pods_failing == 0 else "warning",
                "description": f"{pods_failing} failing" if pods_failing else f"{pod_count} total",
            },
            cards[2],
            cards[3],
        ]

    # Add stat_card summary for overview (big numbers without sparklines)
    if category == "overview":
        try:
            ns_result = safe(lambda: core.list_namespace())
            if not isinstance(ns_result, ToolError):
                active_ns = sum(1 for n in ns_result.items if n.status.phase == "Active")
                cards.append(
                    {
                        "kind": "stat_card",
                        "title": "Namespaces",
                        "value": str(active_ns),
                        "description": "Active namespaces",
                    }
                )
        except Exception:
            pass

    text = f"Cluster metrics ({category}): {len(cards)} KPI cards"
    component = {
        "kind": "grid",
        "title": f"Cluster {category.replace('_', ' ').title()}",
        "columns": min(len(cards), 4),
        "items": cards,
    }
    return (text, component)


@beta_tool
def list_saved_views() -> str:
    """List all custom dashboard views saved by the current user. Returns view titles, descriptions, widget counts, and direct links. Use this when the user asks to see their dashboards, saved views, or custom views."""
    from . import db

    owner = get_current_user()
    views = db.list_views(owner)
    if not views:
        # Fallback: query all views (user identity may differ across sessions/redeploys)
        try:
            _db = db.get_database()
            rows = _db.fetchall(
                "SELECT id, owner, title, description, icon, layout, positions, created_at, updated_at "
                "FROM views ORDER BY updated_at DESC LIMIT 50"
            )
            views = [db._deserialize_view_row(r) for r in rows] if rows else []
        except Exception:
            pass
        if not views:
            return "No saved views found. You can create one by asking me to build a dashboard."

    lines = []
    rows = []
    for v in views:
        widget_count = len(v.get("layout", []))
        lines.append(f"  {v['title']} — {widget_count} widgets — /custom/{v['id']}")
        rows.append(
            {
                "title": v["title"],
                "description": v.get("description", ""),
                "widgets": widget_count,
                "link": f"/custom/{v['id']}",
                "updated": v.get("updated_at", ""),
            }
        )

    text = f"Your saved dashboards ({len(views)}):\n" + "\n".join(lines)
    component = {
        "kind": "data_table",
        "title": f"Your Dashboards ({len(rows)})",
        "columns": [
            {"id": "title", "header": "Title"},
            {"id": "description", "header": "Description"},
            {"id": "widgets", "header": "Widgets"},
            {"id": "link", "header": "Link"},
            {"id": "updated", "header": "Updated"},
        ],
        "rows": rows,
    }
    return (text, component)


@beta_tool
def get_view_details(view_id: str) -> str:
    """Get details of a saved view including its widget list. Use this before modifying a view to understand its current state.

    Args:
        view_id: The view ID (e.g. 'cv-abc123'). Use list_saved_views first to find IDs.
    """
    from . import db

    owner = get_current_user()
    view = db.get_view(view_id, owner)
    if not view:
        view = db.get_view(view_id)  # Fallback without owner filter
    if not view:
        return f"View '{view_id}' not found."

    widgets = view.get("layout", [])
    lines = [f"View: {view['title']}", f"Description: {view.get('description', '')}", f"Widgets ({len(widgets)}):"]
    for i, w in enumerate(widgets):
        kind = w.get("kind", "unknown")
        title = w.get("title", w.get("kind", "untitled"))
        lines.append(f"  [{i}] {kind}: {title}")

    return "\n".join(lines)


@beta_tool
def update_view_widgets(
    view_id: str,
    action: str,
    widget_index: int = -1,
    new_title: str = "",
    new_description: str = "",
) -> str:
    """Modify an existing view — rename widgets, change chart types, remove/reorder widgets, rename view. The UI auto-refreshes.

    Args:
        view_id: The view ID (e.g. 'cv-abc123').
        action: One of: 'rename_widget', 'update_widget_description', 'change_chart_type', 'remove_widget', 'move_widget', 'rename', 'update_description'.
        widget_index: Widget index for widget actions. Use get_view_details to see indices.
        new_title: New title for rename/rename_widget, or new position for move_widget, or chart type for change_chart_type ('line', 'bar', 'area').
        new_description: New description for update_description/update_widget_description.
    """
    from . import db

    owner = get_current_user()
    view = db.get_view(view_id, owner)
    if not view:
        view = db.get_view(view_id)  # Fallback without owner filter
    if not view:
        return f"View '{view_id}' not found."
    # Use the view's actual owner for updates (identity may differ across sessions)
    owner = view.get("owner", owner)

    if action == "remove_widget":
        layout = view.get("layout", [])
        if widget_index < 0 or widget_index >= len(layout):
            return f"Invalid widget index {widget_index}. View has {len(layout)} widgets (0-{len(layout) - 1})."
        removed = layout[widget_index]
        removed_title = removed.get("title", removed.get("kind", "widget"))
        new_layout = [w for i, w in enumerate(layout) if i != widget_index]
        db.update_view(view_id, owner, layout=new_layout)
        # Return a marker so the API layer can emit a view_updated event
        return _signal(
            "view_updated",
            f"Removed widget [{widget_index}]: {removed_title}. View now has {len(new_layout)} widgets.",
            view_id=view_id,
        )

    elif action == "move_widget":
        layout = view.get("layout", [])
        if widget_index < 0 or widget_index >= len(layout):
            return f"Invalid widget index {widget_index}."
        try:
            new_pos = int(new_title)  # reuse new_title param for target position
        except (ValueError, TypeError):
            return "Error: provide target position as new_title (e.g. '0' for top)."
        new_pos = max(0, min(new_pos, len(layout) - 1))
        widget = layout.pop(widget_index)
        layout.insert(new_pos, widget)
        db.update_view(view_id, owner, layout=layout)
        moved_title = widget.get("title", widget.get("kind", "widget"))
        return _signal(
            "view_updated", f"Moved widget '{moved_title}' from position {widget_index} to {new_pos}.", view_id=view_id
        )

    elif action == "rename_widget":
        layout = view.get("layout", [])
        if widget_index < 0 or widget_index >= len(layout):
            return f"Invalid widget index {widget_index}."
        if not new_title:
            return "Error: new_title is required."
        layout[widget_index]["title"] = new_title
        db.update_view(view_id, owner, layout=layout)
        return _signal("view_updated", f"Renamed widget [{widget_index}] to '{new_title}'.", view_id=view_id)

    elif action == "update_widget_description":
        layout = view.get("layout", [])
        if widget_index < 0 or widget_index >= len(layout):
            return f"Invalid widget index {widget_index}."
        layout[widget_index]["description"] = new_description
        db.update_view(view_id, owner, layout=layout)
        return _signal("view_updated", f"Updated widget [{widget_index}] description.", view_id=view_id)

    elif action == "change_chart_type":
        layout = view.get("layout", [])
        if widget_index < 0 or widget_index >= len(layout):
            return f"Invalid widget index {widget_index}."
        if layout[widget_index].get("kind") != "chart":
            return f"Widget [{widget_index}] is not a chart (it's a {layout[widget_index].get('kind')})."
        chart_type = new_title  # reuse param: 'line', 'bar', 'area'
        if chart_type not in ("line", "bar", "area"):
            return f"Invalid chart type '{chart_type}'. Use: line, bar, area."
        layout[widget_index]["chartType"] = chart_type
        db.update_view(view_id, owner, layout=layout)
        return _signal("view_updated", f"Changed widget [{widget_index}] to {chart_type} chart.", view_id=view_id)

    elif action == "rename":
        if not new_title:
            return "Error: new_title is required for rename action."
        db.update_view(view_id, owner, title=new_title)
        return _signal("view_updated", f"Renamed view to '{new_title}'.", view_id=view_id)

    elif action == "update_description":
        db.update_view(view_id, owner, description=new_description)
        return _signal("view_updated", "Updated view description.", view_id=view_id)

    else:
        return f"Unknown action '{action}'. Use: rename_widget, update_widget_description, change_chart_type, remove_widget, move_widget, rename, update_description."


@beta_tool
def add_widget_to_view(view_id: str) -> str:
    """Add the most recent component from this conversation to an existing view. Call this AFTER calling a data tool (like get_prometheus_query, list_pods, etc.) that generated a component. The UI will auto-refresh.

    Args:
        view_id: The view ID to add the widget to (e.g. 'cv-abc123').
    """
    return _signal("add_widget", f"Adding latest component to view {view_id}.", view_id=view_id)


@beta_tool
def remove_widget_from_view(view_id: str, widget_title: str) -> str:
    """Remove a widget from a view by its title. Case-insensitive partial match. The UI will auto-refresh.

    Args:
        view_id: The view ID (e.g. 'cv-abc123').
        widget_title: Title (or substring) of the widget to remove.
    """
    from . import db

    owner = get_current_user()
    view = db.get_view(view_id, owner)
    if not view:
        view = db.get_view(view_id)
    if not view:
        return f"View '{view_id}' not found."

    owner = view.get("owner", owner)
    layout = view.get("layout", [])
    search = widget_title.lower()

    matches = [(i, w) for i, w in enumerate(layout) if search in (w.get("title") or "").lower()]

    if not matches:
        titles = [w.get("title", w.get("kind", "?")) for w in layout]
        return f"No widget matching '{widget_title}'. Widgets: {titles}"

    if len(matches) > 1:
        names = [w.get("title", w.get("kind", "?")) for _, w in matches]
        return f"Multiple matches for '{widget_title}': {names}. Be more specific."

    idx, removed = matches[0]
    removed_title = removed.get("title", removed.get("kind", "widget"))
    new_layout = [w for i, w in enumerate(layout) if i != idx]
    db.update_view(view_id, owner, _snapshot=True, _action="remove_widget", layout=new_layout)
    return _signal(
        "view_updated",
        f"Removed '{removed_title}' from view. {len(new_layout)} widgets remaining.",
        view_id=view_id,
    )


@beta_tool
def emit_component(kind: str, spec_json: str) -> str:
    """Emit a custom component for the current dashboard. Use for bar_list, progress_list, stat_card, or any component type.

    The component is added to the session and will be included when create_dashboard is called.

    Args:
        kind: Component kind (e.g. 'bar_list', 'progress_list', 'stat_card', 'status_list').
        spec_json: JSON string with the component spec. Must include all required fields for the kind.
            Example bar_list: {"title": "Top Pods", "items": [{"label": "nginx", "value": 42}]}
            Example progress_list: {"title": "Node CPU", "items": [{"label": "node-1", "value": 70, "max": 100, "unit": "%"}]}
            Example stat_card: {"title": "Error Rate", "value": "2.3", "unit": "%", "trend": "down", "trendValue": "12%"}
    """
    import json as _json

    try:
        spec = _json.loads(spec_json)
    except _json.JSONDecodeError as e:
        return f"Invalid JSON: {e}"

    spec["kind"] = kind

    from .quality_engine import VALID_KINDS, QualityResult, _validate_component

    if kind not in VALID_KINDS:
        return f"Invalid kind '{kind}'. Valid: {', '.join(sorted(VALID_KINDS))}"

    # Schema-only validation (no dashboard structure rules like "must have chart + table")
    result = QualityResult()
    _validate_component(spec, result)
    if result.errors:
        return f"Invalid {kind} spec: {'; '.join(result.errors)}"

    text = f"Emitted {kind} component"
    if spec.get("title"):
        text += f": {spec['title']}"

    return (text, spec)


@beta_tool
def undo_view_change(view_id: str, version: int = -1) -> str:
    """Undo the last change to a view, or restore a specific version. Every view change is automatically versioned.

    Args:
        view_id: The view ID (e.g. 'cv-abc123').
        version: Specific version number to restore. Use -1 (default) to undo the last change. Use get_view_versions to see available versions.
    """
    from . import db

    owner = get_current_user()
    if version == -1:
        versions = db.list_view_versions(view_id, limit=1)
        if not versions:
            return "No version history available for this view."
        version = versions[0]["version"]

    result = db.restore_view_version(view_id, owner, version)
    if not result:
        # Fallback: try with view's actual owner
        view = db.get_view(view_id)
        if view:
            result = db.restore_view_version(view_id, view.get("owner", owner), version)
    if not result:
        return f"Could not restore version {version}. View not found."
    return _signal("view_updated", f"Restored view to version {version}.", view_id=view_id)


@beta_tool
def get_view_versions(view_id: str) -> str:
    """Show the version history for a view — every change is tracked.

    Args:
        view_id: The view ID (e.g. 'cv-abc123').
    """
    from . import db

    owner = get_current_user()
    view = db.get_view(view_id, owner)
    if not view:
        return f"View '{view_id}' not found."

    versions = db.list_view_versions(view_id) or []
    if not versions:
        return f"No version history for view '{view['title']}'."

    lines = [f"Version history for '{view['title']}' ({len(versions)} versions):"]
    rows = []
    for v in versions:
        lines.append(f"  v{v['version']} — {v['action']} — {v['created_at']}")
        rows.append(
            {"version": v["version"], "action": v["action"], "title": v.get("title", ""), "created_at": v["created_at"]}
        )

    text = "\n".join(lines)
    component = {
        "kind": "data_table",
        "title": f"Version History — {view['title']}",
        "columns": [
            {"id": "version", "header": "Version", "type": "text"},
            {"id": "action", "header": "Action", "type": "text"},
            {"id": "created_at", "header": "When", "type": "timestamp"},
        ],
        "rows": rows,
    }
    return (text, component)


@beta_tool
def delete_dashboard(view_id: str) -> str:
    """Delete a saved dashboard view permanently. This cannot be undone.

    Args:
        view_id: The view ID to delete (e.g. 'cv-abc123'). Use list_saved_views to find IDs.
    """
    from . import db

    owner = get_current_user()
    # Try with current user first, then fallback to view's actual owner
    success = db.delete_view(view_id, owner)
    if not success:
        view = db.get_view(view_id)
        if view:
            success = db.delete_view(view_id, view.get("owner", owner))
    if not success:
        return f"View '{view_id}' not found or you don't have permission to delete it."
    return _signal("view_deleted", f"Deleted dashboard {view_id}.", view_id=view_id)


@beta_tool
def clone_dashboard(view_id: str, new_title: str = "") -> str:
    """Clone an existing dashboard to create a copy you can modify independently.

    Args:
        view_id: The view ID to clone (e.g. 'cv-abc123'). Use list_saved_views to find IDs.
        new_title: Optional new title for the cloned view. If empty, appends '(copy)' to original title.
    """
    from . import db

    owner = get_current_user()
    new_id = db.clone_view(view_id, owner)
    if not new_id:
        return f"View '{view_id}' not found or you don't have permission to clone it."

    # Rename if new_title provided
    if new_title:
        db.update_view(new_id, owner, title=new_title)

    return _signal(
        "view_cloned",
        f"Cloned dashboard {view_id} → {new_id}." + (f" Renamed to '{new_title}'." if new_title else ""),
        view_id=new_id,
        source_view_id=view_id,
    )


register_tool(delete_dashboard)
register_tool(clone_dashboard)
register_tool(create_dashboard)
register_tool(namespace_summary)
register_tool(cluster_metrics)
register_tool(list_saved_views)
register_tool(get_view_details)
register_tool(update_view_widgets)
register_tool(add_widget_to_view)
register_tool(remove_widget_from_view)
register_tool(emit_component)
register_tool(undo_view_change)
register_tool(get_view_versions)

# Exported list for view_designer agent
from .view_critic import critique_view
from .view_planner import plan_dashboard

register_tool(critique_view)
register_tool(plan_dashboard)

VIEW_TOOLS = [
    create_dashboard,
    delete_dashboard,
    clone_dashboard,
    namespace_summary,
    cluster_metrics,
    list_saved_views,
    get_view_details,
    update_view_widgets,
    add_widget_to_view,
    remove_widget_from_view,
    emit_component,
    undo_view_change,
    get_view_versions,
    critique_view,
    plan_dashboard,
]
