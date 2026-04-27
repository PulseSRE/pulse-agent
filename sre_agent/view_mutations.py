"""View mutation tools for modifying dashboards and widgets."""

from __future__ import annotations

import json

from .decorators import beta_tool
from .tool_registry import register_tool


def _signal(signal_type: str, message: str, **kwargs) -> str:
    """Return a structured signal that the API layer can process.

    The returned string contains both a human-readable message (for Claude)
    and a JSON signal (for the API layer) separated by the SIGNAL_PREFIX.
    """
    from .view_tools import SIGNAL_PREFIX

    payload = {"type": signal_type, **kwargs}
    return f"{message}\n{SIGNAL_PREFIX}{json.dumps(payload)}"


def get_current_user() -> str:
    """Get the current user identity."""
    from .view_tools import get_current_user as _get_current_user

    return _get_current_user()


def _resolve_view(view_id: str) -> tuple[dict | None, str]:
    """Look up view scoped to the current user. No ownerless fallback (IDOR prevention)."""
    from . import db

    owner = get_current_user()
    view = db.get_view(view_id, owner)
    return view, owner


@beta_tool
def update_view_widgets(
    view_id: str,
    action: str,
    widget_index: int = -1,
    new_title: str = "",
    new_description: str = "",
    params_json: str = "",
) -> str:
    """Modify an existing view — rename widgets, change chart types, update columns, sort, filter, convert widget types. The UI auto-refreshes.

    Args:
        view_id: The view ID (e.g. 'cv-abc123').
        action: One of: 'rename_widget', 'update_widget_description', 'change_chart_type',
                'remove_widget', 'move_widget', 'rename', 'update_description',
                'update_columns', 'sort_by', 'filter_by', 'change_kind', 'update_query',
                'set_render_override'.
        widget_index: Widget index for widget actions. Use get_view_details to see indices.
        new_title: New title for rename/rename_widget, or chart type for change_chart_type.
        new_description: New description for update_description/update_widget_description.
        params_json: JSON string with action-specific parameters. Used by:
                     update_columns: {"columns": ["name", "status", "age"]}
                     sort_by: {"column": "restarts", "direction": "desc"}
                     filter_by: {"column": "status", "operator": "!=", "value": "Running"}
                     change_kind: {"new_kind": "chart"}
                     update_query: {"query": "sum(rate(...))"}
                     set_render_override: {"render_as": "bar_list", "render_options": {"label_column": "name"}}
    """
    from .mutations.base import MutationContext
    from .mutations.registry import get_all_actions, get_mutation

    view, actual_owner = _resolve_view(view_id)
    if not view:
        return f"View '{view_id}' not found."

    mutation = get_mutation(action)
    if not mutation:
        return f"Unknown action '{action}'. Use: {', '.join(get_all_actions())}."

    try:
        params = json.loads(params_json) if params_json else {}
    except json.JSONDecodeError:
        return "Error: params_json must be valid JSON."

    ctx = MutationContext(
        view_id=view_id,
        view=view,
        owner=actual_owner,
        widget_index=widget_index,
        new_title=new_title,
        new_description=new_description,
        params=params,
    )

    error = mutation.validate(ctx)
    if error:
        return error

    result = mutation.apply(ctx)
    if result.success:
        return _signal("view_updated", result.message, view_id=view_id)
    return result.message


@beta_tool
def remove_widget_from_view(view_id: str, widget_title: str):
    """Remove a widget from a view by its title. Case-insensitive partial match. The UI will auto-refresh.

    Args:
        view_id: The view ID (e.g. 'cv-abc123').
        widget_title: Title (or substring) of the widget to remove.
    """
    from . import db

    view, actual_owner = _resolve_view(view_id)
    if not view:
        return f"View '{view_id}' not found."

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
    updated = db.update_view(view_id, actual_owner, _snapshot=True, _action="remove_widget", layout=new_layout)
    if not updated:
        return f"Failed to remove '{removed_title}' — permission denied or view not found."
    return _signal(
        "view_updated",
        f"Removed '{removed_title}' from view. {len(new_layout)} widgets remaining.",
        view_id=view_id,
    )


@beta_tool
def undo_view_change(view_id: str, version: int = -1):
    """Undo the last change to a view, or restore a specific version. Every view change is automatically versioned.

    Args:
        view_id: The view ID (e.g. 'cv-abc123').
        version: Specific version number to restore. Use -1 (default) to undo the last change. Use get_view_versions to see available versions.
    """
    from . import db

    view, actual_owner = _resolve_view(view_id)
    if not view:
        return f"View '{view_id}' not found."
    if version == -1:
        versions = db.list_view_versions(view_id, limit=1)
        if not versions:
            return "No version history available for this view."
        version = versions[0]["version"]

    result = db.restore_view_version(view_id, actual_owner, version)
    if not result:
        return f"Could not restore version {version}. View not found."
    return _signal("view_updated", f"Restored view to version {version}.", view_id=view_id)


@beta_tool
def get_view_versions(view_id: str):
    """Show the version history for a view — every change is tracked.

    Args:
        view_id: The view ID (e.g. 'cv-abc123').
    """
    from . import db

    view, _actual_owner = _resolve_view(view_id)
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
def optimize_view(view_id: str, strategy: str = "group") -> str:
    """Analyze and reorganize a dashboard's widgets for better layout. Groups related widgets into sections, reorders by priority, and re-computes positions.

    Args:
        view_id: The view ID (e.g. 'cv-abc123').
        strategy: One of:
            'group' — Group widgets by topic (compute, memory, workloads, alerts, etc.) and wrap in sections.
            'reflow' — Re-run the layout engine on current widgets without grouping.
            'compact' — Remove empty space, pack widgets tightly.
            'fit' — Recalculate widget heights based on actual content (fixes clipped sections).
    """
    from . import db
    from .layout_engine import compute_layout

    def _apply_positions(widgets: list[dict]) -> tuple[list[dict], dict]:
        """Run layout engine and merge positions back into widget dicts.

        Returns (updated_widgets, positions_map) — both the merged layout
        and the separate positions dict for the frontend.
        """
        positions = compute_layout(widgets)
        result = []
        for i, w in enumerate(widgets):
            updated = dict(w)
            if i in positions:
                updated.update(positions[i])
            result.append(updated)
        return result, positions

    view, actual_owner = _resolve_view(view_id)
    if not view:
        return f"View '{view_id}' not found."

    layout = view.get("layout", [])
    if not layout:
        return f"View '{view_id}' has no widgets to optimize."

    if strategy == "reflow":
        # Just re-run layout engine on existing widgets
        positioned, positions = _apply_positions(layout)
        db.update_view(view_id, actual_owner, layout=positioned, positions=positions)
        return _signal(
            "view_updated",
            f"Re-flowed {len(positioned)} widgets with semantic layout engine.",
            view_id=view_id,
        )

    if strategy == "compact":
        # Strip positions and let the engine repack from scratch
        stripped = [{k: v for k, v in w.items() if k not in ("x", "y", "w", "h")} for w in layout]
        positioned, positions = _apply_positions(stripped)
        db.update_view(view_id, actual_owner, layout=positioned, positions=positions)
        return _signal(
            "view_updated",
            f"Compacted {len(positioned)} widgets — removed gaps and re-packed.",
            view_id=view_id,
        )

    # strategy == "group": analyze widgets and group into sections
    groups: dict[str, list[dict]] = {}
    _TOPIC_KEYWORDS: dict[str, list[str]] = {
        "Compute": ["cpu", "node", "compute", "core", "processor"],
        "Memory": ["memory", "mem", "oom", "rss", "heap"],
        "Network": ["network", "traffic", "ingress", "route", "dns", "bandwidth", "http"],
        "Storage": ["storage", "pvc", "disk", "volume", "iops"],
        "Workloads": ["pod", "deploy", "replica", "container", "restart", "crash", "workload"],
        "Alerts": ["alert", "firing", "critical", "warning", "incident"],
        "Security": ["security", "rbac", "scc", "vulnerability", "scan", "compliance"],
    }

    for widget in layout:
        title = (widget.get("title", "") or "").lower()
        query = json.dumps(widget).lower()
        assigned = False
        for group_name, keywords in _TOPIC_KEYWORDS.items():
            if any(kw in title or kw in query for kw in keywords):
                groups.setdefault(group_name, []).append(widget)
                assigned = True
                break
        if not assigned:
            groups.setdefault("Overview", []).append(widget)

    # Reorder widgets flat — KPIs first, then grouped by topic.
    # No section wrappers: the grid layout system expects flat components.
    reordered: list[dict] = []
    kpi_kinds = {"metric_card", "info_card_grid", "stat_card"}
    kpis = [w for ws in groups.values() for w in ws if w.get("kind") in kpi_kinds]
    non_kpis = {id(w) for w in kpis}

    # KPIs pinned to top row
    reordered.extend(kpis)

    # Then each topic group in order (charts before tables within each group)
    chart_kinds = {"chart", "donut_chart", "node_map"}
    for group_name in ["Overview", "Compute", "Memory", "Workloads", "Network", "Storage", "Alerts", "Security"]:
        group_widgets = [w for w in groups.get(group_name, []) if id(w) not in non_kpis]
        if not group_widgets:
            continue
        # Sort: charts first, then status/detail, then tables
        group_widgets.sort(
            key=lambda w: 0 if w.get("kind") in chart_kinds else 2 if w.get("kind") == "data_table" else 1
        )
        reordered.extend(group_widgets)

    positioned, positions = _apply_positions(reordered)
    db.update_view(view_id, actual_owner, layout=positioned, positions=positions)

    group_summary = ", ".join(f"{name} ({len(ws)})" for name, ws in groups.items() if ws)
    return _signal(
        "view_updated",
        f"Reorganized {len(layout)} widgets into {len(groups)} groups: {group_summary}. "
        f"KPIs pinned to top, charts before tables within each group.",
        view_id=view_id,
    )


register_tool(update_view_widgets)
register_tool(remove_widget_from_view)
register_tool(undo_view_change)
register_tool(get_view_versions)
register_tool(optimize_view)
