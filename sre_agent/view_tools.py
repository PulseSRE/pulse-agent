"""Tools for creating custom dashboard views from conversation context."""

from __future__ import annotations

import uuid

from anthropic import beta_tool

from .tool_registry import register_tool

# Module-level current user identity (set by API layer before each agent turn).
# Safe because agent turns are serialized per-WebSocket and set_current_user
# is called before asyncio.to_thread spawns the tool execution thread.
_current_user_id: str = "anonymous"


def set_current_user(owner: str) -> None:
    """Set the current user for view tools (called by API layer per-request)."""
    global _current_user_id
    _current_user_id = owner


def get_current_user() -> str:
    """Get the current user identity."""
    return _current_user_id


@beta_tool
def create_dashboard(title: str, description: str = "") -> str:
    """Create a custom dashboard view that the user can save and access from the sidebar. Use this when the user asks to create a dashboard, custom view, or persistent display of data. The dashboard will contain the component specs from the current conversation.

    Args:
        title: Name for the dashboard (e.g. "SRE Overview", "Node Health").
        description: Brief description of what the dashboard shows.
    """
    view_id = f"cv-{uuid.uuid4().hex[:12]}"
    # Return a marker that the API layer will intercept and convert to a view_spec event
    return f"__VIEW_SPEC__{view_id}|{title}|{description}"


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
    """Modify an existing view — remove/reorder widgets, rename, or update description. The UI auto-refreshes.

    Args:
        view_id: The view ID (e.g. 'cv-abc123').
        action: One of: 'remove_widget', 'move_widget', 'rename', 'update_description'.
        widget_index: Widget index for remove/move actions. Use get_view_details to see indices.
        new_title: New title (for 'rename') or new position as string (for 'move_widget', e.g. '0' for top).
        new_description: New description (only for 'update_description').
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
        return f"__VIEW_UPDATED__{view_id}|Removed widget [{widget_index}]: {removed_title}. View now has {len(new_layout)} widgets."

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
        return f"__VIEW_UPDATED__{view_id}|Moved widget '{moved_title}' from position {widget_index} to {new_pos}."

    elif action == "rename":
        if not new_title:
            return "Error: new_title is required for rename action."
        db.update_view(view_id, owner, title=new_title)
        return f"__VIEW_UPDATED__{view_id}|Renamed view to '{new_title}'."

    elif action == "update_description":
        db.update_view(view_id, owner, description=new_description)
        return f"__VIEW_UPDATED__{view_id}|Updated view description."

    else:
        return f"Unknown action '{action}'. Use: remove_widget, move_widget, rename, update_description."


@beta_tool
def add_widget_to_view(view_id: str) -> str:
    """Add the most recent component from this conversation to an existing view. Call this AFTER calling a data tool (like get_prometheus_query, list_pods, etc.) that generated a component. The UI will auto-refresh.

    Args:
        view_id: The view ID to add the widget to (e.g. 'cv-abc123').
    """
    # Return a marker — the API layer will intercept this and add the latest
    # session component to the view
    return f"__ADD_WIDGET__{view_id}"


register_tool(create_dashboard)
register_tool(namespace_summary)
register_tool(list_saved_views)
register_tool(get_view_details)
register_tool(update_view_widgets)
register_tool(add_widget_to_view)
