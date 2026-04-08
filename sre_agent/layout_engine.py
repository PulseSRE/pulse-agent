"""Semantic layout engine — role-based auto-layout on a 4-column grid.

Replaces fixed template slot matching (layout_templates.py) with intelligent
component classification and packing.
"""

from __future__ import annotations

# Role priority order for packing
_ROLE_ORDER = ["kpi_group", "kpi", "status", "chart", "detail", "table", "container"]

# kind -> (role, default_w, default_h)
# kind -> (role, default_w, default_h)
# Heights use rowHeight=30px grid units (1 unit = ~46px with 16px margin)
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
}

# Detail pairing: kind -> set of compatible partner kinds
_DETAIL_PAIRS: dict[str, set[str]] = {
    "log_viewer": {"key_value", "yaml_viewer"},
    "key_value": {"relationship_tree", "log_viewer"},
    "yaml_viewer": {"log_viewer"},
    "relationship_tree": {"key_value"},
}


def _classify(component: dict) -> tuple[str, int, int]:
    """Return (role, default_w, default_h) for a component."""
    kind = component.get("kind", "")

    if kind == "grid":
        items = component.get("items", [])
        cols = component.get("columns", 2)
        rows = max(1, -(-len(items) // cols))  # ceil division
        if any(item.get("kind") == "metric_card" for item in items):
            return "kpi_group", 4, 3 + rows * 4
        return "container", 4, 4 + rows * 6

    if kind in _KIND_MAP:
        return _KIND_MAP[kind]

    return "container", 4, 5


def compute_layout(components: list[dict]) -> dict[int, dict]:
    """Compute grid positions for a list of components.

    Returns ``{original_index: {"x": int, "y": int, "w": int, "h": int}}``.
    Uses a 4-column grid with role-based priority packing.
    """
    if not components:
        return {}

    # Classify each component, keeping original index
    classified: list[tuple[int, str, str, int, int]] = []
    for i, comp in enumerate(components):
        role, default_w, default_h = _classify(comp)
        kind = comp.get("kind", "")
        classified.append((i, role, kind, default_w, default_h))

    # Group by role: (orig_idx, kind, default_w, default_h)
    groups: dict[str, list[tuple[int, str, int, int]]] = {r: [] for r in _ROLE_ORDER}
    for orig_idx, role, kind, dw, dh in classified:
        groups[role].append((orig_idx, kind, dw, dh))

    positions: dict[int, dict] = {}
    y = 0

    for role in _ROLE_ORDER:
        items = groups[role]
        if not items:
            continue

        if role == "kpi":
            _pack_kpi(items, positions, y)
            row_count = (len(items) + 3) // 4
            default_h = items[0][3]  # all kpi cards share same height
            y += row_count * default_h

        elif role == "chart":
            y = _pack_charts(items, positions, y)

        elif role == "detail":
            y = _pack_details(items, positions, y)

        else:
            # kpi_group, status, table, container: full-width, one per row
            for orig_idx, _kind, _dw, dh in items:
                positions[orig_idx] = {"x": 0, "y": y, "w": 4, "h": dh}
                y += dh

    return positions


def _pack_kpi(
    items: list[tuple[int, str, int, int]],
    positions: dict[int, dict],
    start_y: int,
) -> None:
    """Pack KPI metric cards side-by-side, up to 4 per row.

    When fewer than 4 cards exist, distribute width evenly:
    1 card -> w=4, 2 cards -> w=2, 3 cards -> w=1, 4+ -> w=1.
    """
    count = len(items)
    if count == 1:
        card_w = 4
    elif count == 2:
        card_w = 2
    else:
        card_w = 1

    x = 0
    y = start_y
    for orig_idx, _kind, _dw, dh in items:
        if x >= 4:
            x = 0
            y += dh
        positions[orig_idx] = {"x": x, "y": y, "w": card_w, "h": dh}
        x += card_w


def _pack_charts(
    items: list[tuple[int, str, int, int]],
    positions: dict[int, dict],
    start_y: int,
) -> int:
    """Pack charts: first 2 half-width side-by-side (if not node_map), rest full-width."""
    y = start_y

    if len(items) == 1:
        orig_idx, _kind, _dw, dh = items[0]
        positions[orig_idx] = {"x": 0, "y": y, "w": 4, "h": dh}
        return y + dh

    # Separate half-width and full-width (node_map) charts
    half_width_indices: list[int] = []
    full_width_indices: list[int] = []

    for i, (_orig_idx, _kind, dw, _dh) in enumerate(items):
        if dw >= 4:
            full_width_indices.append(i)
        else:
            half_width_indices.append(i)

    # Pair first 2 half-width charts side-by-side
    if len(half_width_indices) >= 2:
        i0, i1 = half_width_indices[0], half_width_indices[1]
        orig0, _, _, dh0 = items[i0]
        orig1, _, _, dh1 = items[i1]
        row_h = max(dh0, dh1)
        positions[orig0] = {"x": 0, "y": y, "w": 2, "h": row_h}
        positions[orig1] = {"x": 2, "y": y, "w": 2, "h": row_h}
        y += row_h
        remaining_half = half_width_indices[2:]
    else:
        remaining_half = half_width_indices

    # Full-width node_maps
    for fi in full_width_indices:
        orig_idx, _kind, _dw, dh = items[fi]
        positions[orig_idx] = {"x": 0, "y": y, "w": 4, "h": dh}
        y += dh

    # Remaining half-width charts go full-width
    for hi in remaining_half:
        orig_idx, _kind, _dw, dh = items[hi]
        positions[orig_idx] = {"x": 0, "y": y, "w": 4, "h": dh}
        y += dh

    return y


def _pack_details(
    items: list[tuple[int, str, int, int]],
    positions: dict[int, dict],
    start_y: int,
) -> int:
    """Pack detail components, pairing complementary kinds side-by-side."""
    y = start_y

    if len(items) == 1:
        orig_idx, _kind, _dw, dh = items[0]
        positions[orig_idx] = {"x": 0, "y": y, "w": 4, "h": dh}
        return y + dh

    used: set[int] = set()

    for i, (orig_idx, kind, _dw, dh) in enumerate(items):
        if i in used:
            continue

        # Try to find a compatible partner
        partner = None
        compatible = _DETAIL_PAIRS.get(kind, set())
        for j, (_orig_j, kind_j, _dw_j, _dh_j) in enumerate(items):
            if j in used or j == i:
                continue
            if kind_j in compatible:
                partner = j
                break

        if partner is not None:
            used.add(i)
            used.add(partner)
            orig_j, _kind_j, _dw_j, dh_j = items[partner]
            row_h = max(dh, dh_j)
            positions[orig_idx] = {"x": 0, "y": y, "w": 2, "h": row_h}
            positions[orig_j] = {"x": 2, "y": y, "w": 2, "h": row_h}
            y += row_h
        else:
            used.add(i)
            positions[orig_idx] = {"x": 0, "y": y, "w": 4, "h": dh}
            y += dh

    return y
