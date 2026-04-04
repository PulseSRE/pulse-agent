"""Layout template registry — mirrors frontend layoutTemplates.ts."""

TEMPLATES: dict[str, list[dict]] = {
    "sre_dashboard": [
        {"kinds": ["grid", "info_card_grid"], "x": 0, "y": 0, "w": 4, "h": 2},
        {"kinds": ["metric_card"], "x": 0, "y": 0, "w": 1, "h": 2},
        {"kinds": ["metric_card"], "x": 1, "y": 0, "w": 1, "h": 2},
        {"kinds": ["metric_card"], "x": 2, "y": 0, "w": 1, "h": 2},
        {"kinds": ["metric_card"], "x": 3, "y": 0, "w": 1, "h": 2, "optional": True},
        {"kinds": ["chart"], "x": 0, "y": 2, "w": 2, "h": 5},
        {"kinds": ["chart"], "x": 2, "y": 2, "w": 2, "h": 5, "optional": True},
        {"kinds": ["data_table", "status_list"], "x": 0, "y": 7, "w": 4, "h": 6},
    ],
    "namespace_overview": [
        {"kinds": ["info_card_grid", "grid"], "x": 0, "y": 0, "w": 4, "h": 2},
        {"kinds": ["chart"], "x": 0, "y": 2, "w": 2, "h": 5, "optional": True},
        {"kinds": ["chart"], "x": 2, "y": 2, "w": 2, "h": 5, "optional": True},
        {"kinds": ["data_table"], "x": 0, "y": 7, "w": 4, "h": 6},
        {"kinds": ["log_viewer", "data_table"], "x": 0, "y": 13, "w": 4, "h": 5, "optional": True},
    ],
    "incident_report": [
        {"kinds": ["status_list", "badge_list"], "x": 0, "y": 0, "w": 4, "h": 3},
        {"kinds": ["log_viewer"], "x": 0, "y": 3, "w": 2, "h": 6},
        {"kinds": ["key_value", "yaml_viewer"], "x": 2, "y": 3, "w": 2, "h": 6},
        {"kinds": ["data_table"], "x": 0, "y": 9, "w": 4, "h": 5, "optional": True},
    ],
    "monitoring_panel": [
        {"kinds": ["grid", "info_card_grid"], "x": 0, "y": 0, "w": 4, "h": 2},
        {"kinds": ["metric_card"], "x": 0, "y": 0, "w": 1, "h": 2},
        {"kinds": ["metric_card"], "x": 1, "y": 0, "w": 1, "h": 2},
        {"kinds": ["metric_card"], "x": 2, "y": 0, "w": 1, "h": 2},
        {"kinds": ["metric_card"], "x": 3, "y": 0, "w": 1, "h": 2, "optional": True},
        {"kinds": ["chart"], "x": 0, "y": 2, "w": 2, "h": 5},
        {"kinds": ["chart"], "x": 2, "y": 2, "w": 2, "h": 5, "optional": True},
        {"kinds": ["status_list", "data_table"], "x": 0, "y": 7, "w": 4, "h": 5},
    ],
    "resource_detail": [
        {"kinds": ["key_value"], "x": 0, "y": 0, "w": 2, "h": 5},
        {"kinds": ["relationship_tree", "status_list"], "x": 2, "y": 0, "w": 2, "h": 5},
        {"kinds": ["yaml_viewer", "log_viewer"], "x": 0, "y": 5, "w": 4, "h": 5},
        {"kinds": ["data_table"], "x": 0, "y": 10, "w": 4, "h": 5, "optional": True},
    ],
}


def apply_template(template_id: str, components: list[dict]) -> dict | None:
    """Apply a layout template to components. Returns positions dict or None."""
    slots = TEMPLATES.get(template_id)
    if not slots:
        return None

    positions: dict[int, dict] = {}
    used: set[int] = set()

    for slot in slots:
        for i, comp in enumerate(components):
            if i in used:
                continue
            if comp.get("kind") in slot["kinds"]:
                used.add(i)
                pos = len(positions)
                positions[pos] = {"x": slot["x"], "y": slot["y"], "w": slot["w"], "h": slot["h"]}
                break

    # Append unmatched components full-width at the bottom
    max_y = max((p["y"] + p["h"] for p in positions.values()), default=0)
    for i in range(len(components)):
        if i not in used:
            pos = len(positions)
            positions[pos] = {"x": 0, "y": max_y, "w": 4, "h": 5}
            max_y += 5

    return positions
