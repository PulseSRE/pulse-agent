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
    """Create a custom dashboard view that the user can save and access from the sidebar. Use this when the user asks to create a dashboard, custom view, or persistent display of data. The dashboard will contain the component specs from the current conversation.

    If a layout template is specified, widgets are automatically arranged in a
    professional grid layout instead of stacking vertically.

    Args:
        title: Name for the dashboard (e.g. "SRE Overview", "Node Health").
        description: Brief description of what the dashboard shows.
        template: Optional layout template ID. Available templates:
                  'sre_dashboard' — 4 metric cards + 2 charts side-by-side + table
                  'namespace_overview' — summary cards + 2 charts + table + events
                  'incident_report' — status timeline + logs/details side-by-side + table
                  'monitoring_panel' — 4 metric cards + 2x2 chart grid + alerts
                  'resource_detail' — key-value + resource tree + yaml + table
    """
    view_id = f"cv-{uuid.uuid4().hex[:12]}"
    kwargs = {"view_id": view_id, "title": title, "description": description}
    if template:
        kwargs["template"] = template
    return _signal("view_spec", f"Created view '{title}' with ID {view_id}.", **kwargs)


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
        f"  Warning events: {warning_count}"
    )

    # Build info_card_grid component
    cards = [
        {"label": "Pods Running", "value": str(running), "sub": f"of {total_pods} total"},
        {"label": "Pods Failing", "value": str(failed + crashloop), "sub": f"{crashloop} crashlooping"},
        {"label": "Deployments", "value": f"{healthy_deps}/{total_deps}", "sub": "healthy"},
        {"label": "Warnings", "value": str(warning_count), "sub": "active events"},
    ]
    component = {
        "kind": "info_card_grid",
        "title": f"Namespace Summary — {namespace}",
        "cards": cards,
    }
    return (text, component)


@beta_tool
def cluster_metrics() -> str:
    """Get key cluster metrics as metric cards: node count, pod count, CPU usage, memory usage. Returns metric_card components ideal for dashboard headers.

    Use this when the user wants metric cards, KPIs, or summary numbers at the top of a dashboard.
    """
    from .errors import ToolError
    from .k8s_client import get_core_client, safe

    core = get_core_client()

    # Node count
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

    # Pod count
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
            "kind": "metric_card",
            "title": "Nodes Ready",
            "value": f"{nodes_ready}/{node_count}",
            "status": "healthy" if nodes_ready == node_count else "warning",
            "description": f"{node_count} total",
        },
        {
            "kind": "metric_card",
            "title": "Pods Running",
            "value": str(pods_running),
            "status": "healthy" if pods_failing == 0 else "warning",
            "description": f"{pods_failing} failing" if pods_failing else f"{pod_count} total",
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
    ]

    # CPU/memory cards use PromQL queries for live sparklines — no metrics API needed

    text = f"Cluster metrics: {node_count} nodes ({nodes_ready} ready), {pods_running} pods running"
    # Return as a grid of metric_cards
    component = {"kind": "grid", "columns": len(cards), "items": cards}
    return (text, component)


@beta_tool
def list_saved_views() -> str:
    """List all custom dashboard views saved by the current user. Returns view titles, descriptions, widget counts, and direct links. Use this when the user asks to see their dashboards, saved views, or custom views."""
    from . import db

    owner = get_current_user()
    views = db.list_views(owner)
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
        return f"View '{view_id}' not found or you don't have access to it."

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
        return f"View '{view_id}' not found or you don't have access to it."

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
        return f"Could not restore version {version}. View not found or access denied."
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


register_tool(create_dashboard)
register_tool(namespace_summary)
register_tool(cluster_metrics)
register_tool(list_saved_views)
register_tool(get_view_details)
register_tool(update_view_widgets)
register_tool(add_widget_to_view)
register_tool(undo_view_change)
register_tool(get_view_versions)

# Exported list for view_designer agent
VIEW_TOOLS = [
    create_dashboard,
    namespace_summary,
    cluster_metrics,
    list_saved_views,
    get_view_details,
    update_view_widgets,
    add_widget_to_view,
    undo_view_change,
    get_view_versions,
]
