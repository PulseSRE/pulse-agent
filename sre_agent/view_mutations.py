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
    from . import db

    owner = get_current_user()
    view = db.get_view(view_id, owner)
    if not view:
        return f"View '{view_id}' not found."

    # Helper for params_json parsing (used by mutation actions below)
    def _parse_params() -> dict | str:
        try:
            return json.loads(params_json) if params_json else {}
        except json.JSONDecodeError:
            return "Error: params_json must be valid JSON."

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

    elif action == "update_columns":
        layout = view.get("layout", [])
        if widget_index < 0 or widget_index >= len(layout):
            return f"Invalid widget index {widget_index}."
        widget = layout[widget_index]
        if widget.get("kind") != "data_table":
            return f"Widget [{widget_index}] is not a data_table (it's a {widget.get('kind')})."
        params = _parse_params()
        if isinstance(params, str):
            return params
        columns = params.get("columns", [])
        if not columns:
            return "Error: params_json must include 'columns' list."
        # Filter existing columns to only include requested ones
        existing_cols = {c["id"]: c for c in widget.get("columns", [])}
        new_cols = [existing_cols[cid] for cid in columns if cid in existing_cols]
        if not new_cols:
            return f"No matching columns found. Available: {list(existing_cols.keys())}"
        widget["columns"] = new_cols
        # Filter rows to only include requested columns
        if widget.get("rows"):
            col_ids = {c["id"] for c in new_cols}
            widget["rows"] = [
                {k: v for k, v in row.items() if k in col_ids or k.startswith("_")} for row in widget["rows"]
            ]
        db.update_view(view_id, owner, layout=layout)
        return _signal("view_updated", f"Updated columns on widget [{widget_index}] to {columns}.", view_id=view_id)

    elif action == "sort_by":
        layout = view.get("layout", [])
        if widget_index < 0 or widget_index >= len(layout):
            return f"Invalid widget index {widget_index}."
        widget = layout[widget_index]
        if widget.get("kind") != "data_table":
            return f"Widget [{widget_index}] is not a data_table."
        params = _parse_params()
        if isinstance(params, str):
            return params
        column = params.get("column", "")
        direction = params.get("direction", "asc")
        if not column:
            return "Error: params_json must include 'column'."
        # Sort rows in place
        rows = widget.get("rows", [])
        reverse = direction.lower() == "desc"
        try:
            rows.sort(key=lambda r: r.get(column, ""), reverse=reverse)
        except TypeError:
            pass  # Mixed types — leave unsorted
        widget["rows"] = rows
        widget["_sort"] = {"column": column, "direction": direction}
        db.update_view(view_id, owner, layout=layout)
        return _signal("view_updated", f"Sorted widget [{widget_index}] by {column} {direction}.", view_id=view_id)

    elif action == "filter_by":
        layout = view.get("layout", [])
        if widget_index < 0 or widget_index >= len(layout):
            return f"Invalid widget index {widget_index}."
        widget = layout[widget_index]
        if widget.get("kind") != "data_table":
            return f"Widget [{widget_index}] is not a data_table."
        params = _parse_params()
        if isinstance(params, str):
            return params
        column = params.get("column", "")
        operator = params.get("operator", "==")
        value = params.get("value", "")
        if not column:
            return "Error: params_json must include 'column'."
        # Store filter metadata (frontend applies it)
        filters = widget.get("_filters", [])
        filters.append({"column": column, "operator": operator, "value": value})
        widget["_filters"] = filters
        db.update_view(view_id, owner, layout=layout)
        return _signal(
            "view_updated", f"Added filter on widget [{widget_index}]: {column} {operator} {value}.", view_id=view_id
        )

    elif action == "change_kind":
        layout = view.get("layout", [])
        if widget_index < 0 or widget_index >= len(layout):
            return f"Invalid widget index {widget_index}."
        params = _parse_params()
        if isinstance(params, str):
            return params
        new_kind = params.get("new_kind", "")
        if not new_kind:
            return "Error: params_json must include 'new_kind'."
        from .component_registry import get_valid_kinds

        if new_kind not in get_valid_kinds():
            return f"Invalid kind '{new_kind}'. Valid: {sorted(get_valid_kinds())}"
        widget = layout[widget_index]
        old_kind = widget.get("kind", "unknown")
        # Use component_transform for intelligent data mapping when available
        from .component_transform import can_transform, transform

        if can_transform(old_kind, new_kind):
            layout[widget_index] = transform(widget, new_kind)
        else:
            widget["kind"] = new_kind
        db.update_view(view_id, owner, layout=layout)
        return _signal(
            "view_updated", f"Changed widget [{widget_index}] from {old_kind} to {new_kind}.", view_id=view_id
        )

    elif action == "update_query":
        layout = view.get("layout", [])
        if widget_index < 0 or widget_index >= len(layout):
            return f"Invalid widget index {widget_index}."
        params = _parse_params()
        if isinstance(params, str):
            return params
        query = params.get("query", "")
        if not query:
            return "Error: params_json must include 'query'."
        widget = layout[widget_index]
        widget["query"] = query
        db.update_view(view_id, owner, layout=layout)
        return _signal("view_updated", f"Updated query on widget [{widget_index}].", view_id=view_id)

    elif action == "set_render_override":
        layout = view.get("layout", [])
        if widget_index < 0 or widget_index >= len(layout):
            return f"Invalid widget index {widget_index}."
        params = _parse_params()
        if isinstance(params, str):
            return params
        render_as = params.get("render_as", "")
        if not render_as:
            return "Error: params_json must include 'render_as'."
        from .component_registry import get_valid_kinds

        if render_as not in get_valid_kinds():
            return f"Invalid render_as '{render_as}'. Valid: {sorted(get_valid_kinds())}"
        widget = layout[widget_index]
        widget["render_as"] = render_as
        widget["render_options"] = params.get("render_options", {})
        db.update_view(view_id, owner, layout=layout)
        return _signal(
            "view_updated", f"Set render override on widget [{widget_index}] to {render_as}.", view_id=view_id
        )

    else:
        return (
            f"Unknown action '{action}'. Use: rename_widget, update_widget_description, "
            "change_chart_type, remove_widget, move_widget, rename, update_description, "
            "update_columns, sort_by, filter_by, change_kind, update_query, set_render_override."
        )


@beta_tool
def remove_widget_from_view(view_id: str, widget_title: str):
    """Remove a widget from a view by its title. Case-insensitive partial match. The UI will auto-refresh.

    Args:
        view_id: The view ID (e.g. 'cv-abc123').
        widget_title: Title (or substring) of the widget to remove.
    """
    from . import db

    owner = get_current_user()
    view = db.get_view(view_id, owner)
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
    db.update_view(view_id, owner, _snapshot=True, _action="remove_widget", layout=new_layout)
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

    owner = get_current_user()
    if version == -1:
        versions = db.list_view_versions(view_id, limit=1)
        if not versions:
            return "No version history available for this view."
        version = versions[0]["version"]

    result = db.restore_view_version(view_id, owner, version)
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

    owner = get_current_user()
    view = db.get_view(view_id, owner)
    if not view:
        return f"View '{view_id}' not found."

    layout = view.get("layout", [])
    if not layout:
        return f"View '{view_id}' has no widgets to optimize."

    if strategy == "reflow":
        # Just re-run layout engine on existing widgets
        positioned, positions = _apply_positions(layout)
        db.update_view(view_id, owner, layout=positioned, positions=positions)
        return _signal(
            "view_updated",
            f"Re-flowed {len(positioned)} widgets with semantic layout engine.",
            view_id=view_id,
        )

    if strategy == "compact":
        # Strip positions and let the engine repack from scratch
        stripped = [{k: v for k, v in w.items() if k not in ("x", "y", "w", "h")} for w in layout]
        positioned, positions = _apply_positions(stripped)
        db.update_view(view_id, owner, layout=positioned, positions=positions)
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
    db.update_view(view_id, owner, layout=positioned, positions=positions)

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
