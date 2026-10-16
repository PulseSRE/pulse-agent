"""Unified layout engine — backend-authoritative layout on a 4-column grid.

Single source of truth for dashboard layout. Frontend renders positions
verbatim. Claude provides optional layout hints (w, h, group, priority)
which the engine respects when valid and ignores when not.
"""

from __future__ import annotations

import math

# Role priority order for packing
_ROLE_ORDER = ["kpi_group", "kpi", "status", "chart", "detail", "table", "container"]

# kind -> (role, default_w, default_h)
_KIND_MAP: dict[str, tuple[str, int, int]] = {
    "metric_card": ("kpi", 1, 3),
    "info_card_grid": ("kpi_group", 4, 5),
    "chart": ("chart", 2, 10),
    "node_map": ("chart", 4, 12),
    "data_table": ("table", 4, 12),
    "status_list": ("status", 4, 6),
    "badge_list": ("status", 4, 3),
    "log_viewer": ("detail", 2, 12),
    "key_value": ("detail", 2, 10),
    "yaml_viewer": ("detail", 2, 10),
    "relationship_tree": ("detail", 2, 10),
    "tabs": ("container", 4, 16),
    "section": ("container", 4, 10),
    "bar_list": ("detail", 2, 8),
    "progress_list": ("detail", 2, 8),
    "stat_card": ("kpi", 1, 4),
    "timeline": ("chart", 4, 10),
    "donut_chart": ("chart", 2, 10),
    "resource_counts": ("kpi_group", 4, 4),
    "summary_bar": ("kpi_group", 4, 3),
    "topology": ("chart", 4, 24),
    "status_pipeline": ("kpi_group", 4, 3),
    "confidence_badge": ("kpi", 1, 2),
    "resolution_tracker": ("detail", 4, 10),
    "blast_radius": ("detail", 4, 8),
    "action_button": ("detail", 1, 3),
}

# Width hint vocabulary
_WIDTH_MAP: dict[str, int] = {
    "quarter": 1,
    "half": 2,
    "three_quarter": 3,
    "full": 4,
}

# Detail pairing: kind -> set of compatible partner kinds
_DETAIL_PAIRS: dict[str, set[str]] = {
    "log_viewer": {"key_value", "yaml_viewer"},
    "key_value": {"relationship_tree", "log_viewer"},
    "yaml_viewer": {"log_viewer"},
    "relationship_tree": {"key_value"},
}


def _resolve_width(hint: str | None, default: int) -> int:
    """Resolve a width hint to a grid column count (1-4)."""
    if not hint or hint == "auto":
        return default
    return _WIDTH_MAP.get(hint, default)


def _estimate_nested_height(component: dict, _depth: int = 0) -> int:
    """Recursively estimate grid-unit height a component needs.

    For leaf components, returns the default from _KIND_MAP.
    For containers, sums children heights plus overhead.
    """
    if _depth > 5:
        return _KIND_MAP.get(component.get("kind", ""), ("container", 4, 5))[2]

    kind = component.get("kind", "")

    if kind == "section":
        children = component.get("components", [])
        if not children:
            return 6
        overhead = 3
        return overhead + sum(_estimate_nested_height(c, _depth + 1) for c in children)

    if kind == "tabs":
        tabs = component.get("tabs", [])
        if not tabs:
            return 6
        overhead = 3
        tallest = max(
            (sum(_estimate_nested_height(c, _depth + 1) for c in tab.get("components", [])) for tab in tabs),
            default=4,
        )
        return overhead + tallest

    if kind == "grid":
        items = component.get("items", [])
        cols = component.get("columns", 4)
        if not items:
            return 5
        overhead = 1
        row_heights: list[int] = []
        for row_start in range(0, len(items), cols):
            row_items = items[row_start : row_start + cols]
            row_heights.append(max(_estimate_nested_height(item, _depth + 1) for item in row_items))
        return overhead + sum(row_heights)

    _, _, default_h = _KIND_MAP.get(kind, ("container", 4, 5))
    return _resolve_height(None, default_h, component)


def _resolve_height(hint: str | None, default: int, component: dict) -> int:
    """Resolve a height hint, applying content-aware sizing."""
    kind = component.get("kind", "")

    # Content-aware heights
    if kind == "data_table":
        rows = len(component.get("rows", []))
        if rows:
            default = 3 + min(rows, 15)  # header + footer + 1 grid unit per row, capped at pageSize
        elif component.get("datasources"):
            default = 10  # live table with no initial snapshot — moderate default
        else:
            default = 5
    elif kind == "status_list":
        items = len(component.get("items", []))
        default = 2 + min(math.ceil(items * 0.8), 8) if items else 4
    elif kind == "key_value":
        pairs = len(component.get("pairs", component.get("items", [])))
        default = 3 + min(pairs, 5) if pairs else 4
    elif kind == "chart":
        series = component.get("series", [])
        if not series and not component.get("query"):
            default = 4
        else:
            # Rendered chart card (header + 240px plot + legend) is ~360px;
            # 10 units = 392px at rowHeight 32 / margin 8. 12 left ~120px empty.
            default = 10
    elif kind in ("metric_card", "stat_card"):
        # Without a sparkline the card is ~92px; 3 units = 112px (4 left ~60px empty).
        default = 6 if component.get("query") else 3
    elif kind == "info_card_grid":
        cards = len(component.get("cards", []))
        default = max(4, 2 + min(math.ceil(cards * 1.5), 6))
    elif kind in ("section", "tabs", "grid"):
        default = _estimate_nested_height(component)
    elif kind == "bar_list":
        items = len(component.get("items", []))
        default = 2 + min(items, 8) if items else 4
    elif kind == "progress_list":
        items = len(component.get("items", []))
        default = 2 + min(math.ceil(items * 1.2), 8) if items else 4

    if not hint:
        return default
    if hint == "compact":
        return max(3, int(default * 0.7))
    if hint == "tall":
        return int(default * 1.5)
    return default


def _classify(component: dict) -> tuple[str, int, int]:
    """Return (role, width, height) for a component, applying layout hints."""
    kind = component.get("kind", "")
    hints = component.get("layout", {})

    # Grid components need special classification
    if kind == "grid":
        role = (
            "kpi_group"
            if any(item.get("kind") == "metric_card" for item in component.get("items", []))
            else "container"
        )
        default_w = 4
        default_h = 5
    elif kind in _KIND_MAP:
        role, default_w, default_h = _KIND_MAP[kind]
    else:
        role, default_w, default_h = "container", 4, 5

    # Apply hints
    w = _resolve_width(hints.get("w"), default_w)
    h = _resolve_height(hints.get("h"), default_h, component)

    # Priority hint promotes role
    if hints.get("priority") == "top":
        role = "kpi"  # Force to top

    return role, w, h


def parse_rows(rows: str | list | None) -> list[list[str]]:
    """Parse planned rows: one row per line, widget titles separated by ``|``.

    Also accepts an already-parsed list of lists. Blank entries are dropped.
    """
    if not rows:
        return []
    if isinstance(rows, list):
        parsed = [[str(t).strip() for t in row if str(t).strip()] for row in rows if isinstance(row, list)]
    else:
        parsed = [[t.strip() for t in line.split("|") if t.strip()] for line in str(rows).splitlines()]
    return [row for row in parsed if row]


def _even_widths(count: int) -> list[int]:
    """Column widths that fill a 4-column row with ``count`` items (count <= 4)."""
    if count <= 0:
        return []
    if 4 % count == 0:
        return [4 // count] * count
    return [1] * (count - 1) + [4 - (count - 1)]


def _layout_planned_rows(components: list[dict], rows: list[list[str]]) -> dict[int, dict]:
    """Place widgets in the rows the agent planned, in reading order.

    Each planned row shares the width evenly (up to 4 per line). A widget that
    is full width by default (tables, topology, tabs, ...) takes its own line
    at its place in the row. Widgets not named in any row are laid out after
    the planned rows with the usual role-based packing. Titles match
    case-insensitively; unknown titles are ignored.
    """
    by_title: dict[str, int] = {}
    for i, comp in enumerate(components):
        title = str(comp.get("title") or "").strip().lower()
        if title and title not in by_title:
            by_title[title] = i

    positions: dict[int, dict] = {}
    placed: set[int] = set()
    y = 0

    def flush(line: list[int]) -> None:
        nonlocal y
        for start in range(0, len(line), 4):
            chunk = line[start : start + 4]
            widths = _even_widths(len(chunk))
            heights = [_classify(components[i])[2] for i in chunk]
            x = 0
            for idx, w, h in zip(chunk, widths, heights, strict=True):
                # Own height: a metric card beside a chart stays card-sized.
                positions[idx] = {"x": x, "y": y, "w": w, "h": h}
                x += w
            y += max(heights)

    for row in rows:
        line: list[int] = []
        for title in row:
            idx = by_title.get(title.strip().lower())
            if idx is None or idx in placed:
                continue
            placed.add(idx)
            _role, w, h = _classify(components[idx])
            if w >= 4:
                flush(line)
                line = []
                positions[idx] = {"x": 0, "y": y, "w": 4, "h": h}
                y += h
            else:
                line.append(idx)
        flush(line)

    remaining = [i for i in range(len(components)) if i not in placed]
    if remaining:
        rest = compute_layout([components[i] for i in remaining])
        for sub_idx, pos in rest.items():
            positions[remaining[sub_idx]] = {**pos, "y": pos["y"] + y}
    return positions


def compute_layout(components: list[dict], rows: str | list | None = None) -> dict[int, dict]:
    """Compute grid positions for a list of components.

    Returns ``{original_index: {"x": int, "y": int, "w": int, "h": int}}``.
    Uses a 4-column grid with role-based priority packing.
    Respects layout hints (w, h, group, priority) from components.

    ``rows`` is the order the agent planned (see ``parse_rows``). When given,
    planned widgets are placed in that order instead of being regrouped by
    widget type, which used to discard the plan's narrative.
    """
    if not components:
        return {}
    planned = parse_rows(rows)
    if planned:
        return _layout_planned_rows(components, planned)

    # Classify each component
    classified: list[tuple[int, str, str, int, int, str]] = []
    for i, comp in enumerate(components):
        role, w, h = _classify(comp)
        kind = comp.get("kind", "")
        group = comp.get("layout", {}).get("group", "")
        classified.append((i, role, kind, w, h, group))

    # Separate grouped and ungrouped items
    grouped: dict[str, list[tuple[int, str, int, int]]] = {}
    ungrouped: list[tuple[int, str, str, int, int]] = []

    for orig_idx, role, kind, w, h, group in classified:
        if group:
            if group not in grouped:
                grouped[group] = []
            grouped[group].append((orig_idx, kind, w, h))
        else:
            ungrouped.append((orig_idx, role, kind, w, h))

    # Sort ungrouped by role order
    role_rank = {r: i for i, r in enumerate(_ROLE_ORDER)}
    ungrouped.sort(key=lambda x: role_rank.get(x[1], 99))

    positions: dict[int, dict] = {}
    y = 0

    # Pack ungrouped items by role
    role_groups: dict[str, list[tuple[int, str, int, int]]] = {}
    for orig_idx, role, kind, w, h in ungrouped:
        if role not in role_groups:
            role_groups[role] = []
        role_groups[role].append((orig_idx, kind, w, h))

    for role in _ROLE_ORDER:
        # Pack any groups that naturally belong here (first item's role matches)
        for group_name, group_items in list(grouped.items()):
            # Determine group's natural role from first item
            first_kind = group_items[0][1]
            first_role = _KIND_MAP.get(first_kind, ("container", 4, 5))[0]
            if first_role == role:
                y = _pack_group(group_items, positions, y)
                del grouped[group_name]

        items = role_groups.get(role, [])
        if not items:
            continue

        if role == "kpi":
            _pack_kpi(items, positions, y)
            row_count = math.ceil(len(items) / 4)
            default_h = items[0][3]
            y += row_count * default_h
        elif role == "chart":
            y = _pack_charts(items, positions, y)
        elif role == "detail":
            y = _pack_details(items, positions, y)
        else:
            for orig_idx, _kind, w, h in items:
                positions[orig_idx] = {"x": 0, "y": y, "w": w, "h": h}
                y += h

    # Pack remaining groups at the end
    for group_items in grouped.values():
        y = _pack_group(group_items, positions, y)

    return positions


def _pack_group(
    items: list[tuple[int, str, int, int]],
    positions: dict[int, dict],
    start_y: int,
) -> int:
    """Pack a group of items into consecutive rows, respecting widths."""
    y = start_y
    x = 0
    row_h = 0

    # Sort by width descending for better bin-packing
    sorted_items = sorted(items, key=lambda t: -t[2])

    for orig_idx, _kind, w, h in sorted_items:
        if x + w > 4:
            y += row_h
            x = 0
            row_h = 0
        positions[orig_idx] = {"x": x, "y": y, "w": w, "h": h}
        row_h = max(row_h, h)
        x += w

    if row_h > 0:
        y += row_h

    return y


def _fill_last_row(positions: dict[int, dict], row: list[int]) -> None:
    """Widen a partially filled final row so it spans all 4 columns.

    A 5th KPI card used to sit alone at 1/4 width, and a 3rd chart at 1/2,
    leaving a visible hole in the grid. Split the row evenly when the item
    count divides 4; otherwise give the remainder to the last item.
    """
    if not row:
        return
    used = sum(positions[i]["w"] for i in row)
    if used >= 4:
        return
    if 4 % len(row) == 0:
        w = 4 // len(row)
        for n, i in enumerate(row):
            positions[i]["x"] = n * w
            positions[i]["w"] = w
    else:
        positions[row[-1]]["w"] += 4 - used


def _pack_kpi(
    items: list[tuple[int, str, int, int]],
    positions: dict[int, dict],
    start_y: int,
) -> None:
    """Pack KPI metric cards side-by-side, up to 4 per row.

    Distributes width evenly: 1 card -> w=4, 2 -> w=2, 3+ -> w=1.
    Respects explicit width hints from _classify.
    """
    count = len(items)
    # Check if any items have explicit width hints (not default 1)
    has_hints = any(w != 1 for _, _, w, _ in items)

    if not has_hints:
        if count == 1:
            card_w = 4
        elif count == 2:
            card_w = 2
        else:
            card_w = 1
    else:
        card_w = None  # Use per-item widths

    x = 0
    y = start_y
    row_h = items[0][3] if items else 4
    row: list[int] = []
    for orig_idx, _kind, w, h in items:
        actual_w = card_w if card_w is not None else w
        if x + actual_w > 4:
            x = 0
            y += row_h
            row = []
        positions[orig_idx] = {"x": x, "y": y, "w": actual_w, "h": h}
        row.append(orig_idx)
        row_h = max(row_h, h)
        x += actual_w
    if card_w is not None:  # never override explicit width hints
        _fill_last_row(positions, row)


def _pack_charts(
    items: list[tuple[int, str, int, int]],
    positions: dict[int, dict],
    start_y: int,
) -> int:
    """Pack charts using bin-packing on 4-column grid."""
    y = start_y
    x = 0
    row_h = 0

    row: list[int] = []
    for orig_idx, _kind, w, h in items:
        if x + w > 4:
            y += row_h
            x = 0
            row_h = 0
            row = []
        positions[orig_idx] = {"x": x, "y": y, "w": w, "h": h}
        row.append(orig_idx)
        row_h = max(row_h, h)
        x += w
    # Only default half-width charts; explicit width hints are respected.
    if all(positions[i]["w"] == 2 for i in row):
        _fill_last_row(positions, row)

    if row_h > 0:
        y += row_h

    return y


def _pack_details(
    items: list[tuple[int, str, int, int]],
    positions: dict[int, dict],
    start_y: int,
) -> int:
    """Pack detail components, pairing complementary kinds side-by-side."""
    y = start_y

    if len(items) == 1:
        orig_idx, _kind, _w, h = items[0]
        positions[orig_idx] = {"x": 0, "y": y, "w": 4, "h": h}
        return y + h

    used: set[int] = set()

    for i, (orig_idx, kind, _w, h) in enumerate(items):
        if i in used:
            continue

        # Try to find a compatible partner
        partner = None
        compatible = _DETAIL_PAIRS.get(kind, set())
        for j, (_orig_j, kind_j, _w_j, _h_j) in enumerate(items):
            if j in used or j == i:
                continue
            if kind_j in compatible:
                partner = j
                break

        if partner is not None:
            used.add(i)
            used.add(partner)
            orig_j, _kind_j, _w_j, h_j = items[partner]
            row_h = max(h, h_j)
            positions[orig_idx] = {"x": 0, "y": y, "w": 2, "h": row_h}
            positions[orig_j] = {"x": 2, "y": y, "w": 2, "h": row_h}
            y += row_h
        else:
            used.add(i)
            positions[orig_idx] = {"x": 0, "y": y, "w": 4, "h": h}
            y += h

    return y


# ---------------------------------------------------------------------------
# View-type layout templates — hero header + tabs for agent views
# ---------------------------------------------------------------------------

_INCIDENT_STEPS = ["Detected", "Investigated", "Action Taken", "Verifying", "Resolved"]
_PLAN_STEPS = ["Analyzing", "Ready", "Executing", "Completed"]
_ASSESSMENT_STEPS = ["Analyzing", "Ready", "Acknowledged"]

_STATUS_TO_STEP: dict[str, dict[str, int]] = {
    "incident": {
        "investigating": 1,
        "action_taken": 2,
        "verifying": 3,
        "resolved": 4,
    },
    "plan": {
        "analyzing": 0,
        "ready": 1,
        "executing": 2,
        "completed": 3,
    },
    "assessment": {
        "analyzing": 0,
        "ready": 1,
        "acknowledged": 2,
    },
}

_TAB_TEMPLATES: dict[str, list[str]] = {
    "incident": ["Resolution", "Analysis", "Impact", "Timeline"],
    "plan": ["Prerequisites", "Steps", "Impact", "Current State"],
    "assessment": ["Trend", "Recommendations", "Impact"],
}

_VIEW_STEPS: dict[str, list[str]] = {
    "incident": _INCIDENT_STEPS,
    "plan": _PLAN_STEPS,
    "assessment": _ASSESSMENT_STEPS,
}


def build_view_layout(
    components: list[dict],
    view_type: str,
    status: str = "",
) -> list[dict]:
    """Wrap components in a hero-header + tabs layout for agent view types.

    Returns a new layout list: [section(hero), tabs(...)].
    Custom views pass through unchanged.
    """
    if view_type not in _TAB_TEMPLATES:
        return components

    steps_list = _VIEW_STEPS[view_type]

    step_map = _STATUS_TO_STEP.get(view_type, {})
    current_step = step_map.get(status, 0)

    hero_children: list[dict] = []
    tab_buckets: dict[str, list[dict]] = {t: [] for t in _TAB_TEMPLATES[view_type]}
    tab_names = _TAB_TEMPLATES[view_type]

    confidence_badge = None
    metric_cards: list[dict] = []

    for comp in components:
        kind = comp.get("kind", "")
        if kind == "confidence_badge":
            confidence_badge = comp
        elif kind == "status_pipeline":
            pass
        elif kind == "metric_card":
            metric_cards.append(comp)
        elif kind in ("resolution_tracker", "action_button"):
            tab_buckets[tab_names[0]].append(comp)
        elif kind == "blast_radius":
            if "Impact" in tab_buckets:
                tab_buckets["Impact"].append(comp)
            else:
                tab_buckets[tab_names[-1]].append(comp)
        elif kind == "timeline":
            if view_type == "incident" and "Timeline" in tab_buckets:
                tab_buckets["Timeline"].append(comp)
            else:
                tab_buckets[tab_names[-1]].append(comp)
        elif kind in ("key_value", "data_table"):
            if view_type == "incident":
                tab_buckets["Analysis"].append(comp)
            elif view_type == "plan":
                tab_buckets["Current State"].append(comp)
            else:
                tab_buckets[tab_names[0]].append(comp)
        else:
            tab_buckets[tab_names[0]].append(comp)

    if confidence_badge:
        hero_children.append(confidence_badge)

    hero_children.append(
        {
            "kind": "status_pipeline",
            "steps": steps_list,
            "current": current_step,
        }
    )

    if metric_cards:
        hero_children.append(
            {
                "kind": "grid",
                "columns": min(len(metric_cards), 6),
                "items": metric_cards,
            }
        )

    tabs_content = []
    for tab_name in tab_names:
        bucket = tab_buckets[tab_name]
        tabs_content.append({"label": tab_name, "components": bucket})

    return [
        {
            "kind": "section",
            "title": "",
            "collapsible": False,
            "defaultOpen": True,
            "components": hero_children,
        },
        {
            "kind": "tabs",
            "tabs": tabs_content,
        },
    ]
