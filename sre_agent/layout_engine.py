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
    "metric_card": ("kpi", 1, 4),
    "info_card_grid": ("kpi_group", 4, 5),
    "chart": ("chart", 2, 12),
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
        if not series:
            default = 4  # Empty chart — compact
        elif len(series) <= 1:
            default = 8  # Single series — medium
        # else: keep default 12 for multi-series
    elif kind in ("metric_card", "stat_card"):
        default = 4  # Always compact — these are KPI cards
    elif kind == "info_card_grid":
        cards = len(component.get("cards", []))
        default = max(4, 2 + min(math.ceil(cards * 1.5), 6))
    elif kind == "section":
        items = component.get("items", [])
        default = max(6, 2 + len(items) * 4)  # Scale with child count
    elif kind == "grid":
        items = component.get("items", [])
        cols = component.get("columns", 4)
        rows = max(1, math.ceil(len(items) / cols))
        if any(item.get("kind") == "metric_card" for item in items):
            default = min(8, 1 + rows * 3)
        else:
            default = min(12, 1 + rows * 4)
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


def compute_layout(components: list[dict]) -> dict[int, dict]:
    """Compute grid positions for a list of components.

    Returns ``{original_index: {"x": int, "y": int, "w": int, "h": int}}``.
    Uses a 4-column grid with role-based priority packing.
    Respects layout hints (w, h, group, priority) from components.
    """
    if not components:
        return {}

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
    for orig_idx, _kind, w, h in items:
        actual_w = card_w if card_w is not None else w
        if x + actual_w > 4:
            x = 0
            y += row_h
        positions[orig_idx] = {"x": x, "y": y, "w": actual_w, "h": h}
        row_h = max(row_h, h)
        x += actual_w


def _pack_charts(
    items: list[tuple[int, str, int, int]],
    positions: dict[int, dict],
    start_y: int,
) -> int:
    """Pack charts using bin-packing on 4-column grid."""
    y = start_y
    x = 0
    row_h = 0

    for orig_idx, _kind, w, h in items:
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
