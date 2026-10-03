# Semantic Layout Engine — Design Spec

> Historical design/implementation record. This preserves the proposal and tasks at the date in the filename; unchecked tasks, paths, commands, and security claims are not evidence of the current implementation. Use [current documentation](../../README.md) and verify behavior against code before applying a procedure.


**Goal:** Replace the 5 fixed layout templates with a smart auto-layout engine that computes grid positions from component roles. No template selection needed — the engine handles any combination of components automatically.

**Problem:** The current template system uses greedy first-match slot assignment. Components must arrive in the right order. A `grid` containing metric_cards can match a w=1 metric_card slot, getting squished. Only 5 templates exist — anything outside them gets ugly full-width stacking. The agent has to pick a template ID, adding another decision point to an already complex prompt.

**Approach:** Role-based row packing. Each component gets a role from its `kind`. Roles are sorted into hierarchy (KPIs → Charts → Details). Components are packed into rows with pairing rules for side-by-side placement.

---

## 1. Layout Engine (`sre_agent/layout_engine.py`)

New module replacing `layout_templates.py`.

### Public API

```python
def compute_layout(components: list[dict]) -> dict[int, dict]:
    """Compute grid positions for a list of dashboard components.
    
    Returns {component_index: {"x": int, "y": int, "w": int, "h": int}}.
    Uses a 4-column grid. Components are sorted by role (KPIs first, then
    charts, then details) and packed into rows with smart pairing.
    """
```

### Role Classification

| Kind | Role | Default w | Default h |
|------|------|-----------|-----------|
| `metric_card` | `kpi` | 1 | 2 |
| `grid` (with metric_card items) | `kpi_group` | 4 | 2 |
| `info_card_grid` | `kpi_group` | 4 | 2 |
| `chart` | `chart` | 2 | 5 |
| `node_map` | `chart` | 4 | 6 |
| `data_table` | `table` | 4 | 6 |
| `status_list` | `status` | 4 | 3 |
| `badge_list` | `status` | 4 | 3 |
| `log_viewer` | `detail` | 2 | 6 |
| `key_value` | `detail` | 2 | 5 |
| `yaml_viewer` | `detail` | 2 | 5 |
| `relationship_tree` | `detail` | 2 | 5 |
| `tabs` | `container` | 4 | 8 |
| `section` | `container` | 4 | 5 |

### Row Ordering

Components are sorted into groups by role priority:
1. `kpi_group` (full-width metric grids)
2. `kpi` (individual metric cards)
3. `status` (status lists, badge lists)
4. `chart` (charts, node maps)
5. `detail` (log viewers, key-value, yaml, trees)
6. `table` (data tables)
7. `container` (tabs, sections)

### Packing Rules

**KPI group (`kpi_group`):**
- Full-width (w=4, h=2), one per row

**KPI individual (`kpi`):**
- Pack side-by-side up to 4 per row (w=1 each)
- If 1-3 cards, they share the row starting from x=0

**Charts (`chart`):**
- First 2 charts: side-by-side (w=2 each, same y)
- `node_map` always full-width (w=4, h=6)
- 3rd+ chart: full-width (w=4)

**Details (`detail`):**
- Pair complementary kinds side-by-side (w=2 each):
  - `log_viewer` + `key_value`
  - `log_viewer` + `yaml_viewer`
  - `key_value` + `relationship_tree`
- Unpaired details: full-width (w=4)

**Tables (`table`):**
- Always full-width (w=4, h=6)

**Status (`status`):**
- Always full-width (w=4, h=3)

**Containers (`container`):**
- Always full-width (w=4)

### Algorithm

```python
def compute_layout(components):
    # 1. Classify each component into a role
    classified = [(i, comp, _classify(comp)) for i, comp in enumerate(components)]
    
    # 2. Sort by role priority
    ROLE_ORDER = ["kpi_group", "kpi", "status", "chart", "detail", "table", "container"]
    classified.sort(key=lambda x: ROLE_ORDER.index(x[2].role))
    
    # 3. Pack into rows
    positions = {}
    y = 0
    
    # Pack each role group
    for role_group in group_by_role(classified):
        y = _pack_group(role_group, positions, y)
    
    return positions
```

---

## 2. Integration Points

### `sre_agent/api.py`

Replace template-based layout with auto-layout. In the `view_spec` signal handler:

**Before (current):**
```python
positions = None
if view_template:
    from .layout_templates import apply_template as _apply_tpl
    positions = _apply_tpl(view_template, session_components)
```

**After:**
```python
from .layout_engine import compute_layout
positions = compute_layout(session_components)
```

Always compute positions — no conditional on template ID.

### `sre_agent/view_tools.py`

Keep `template` parameter on `create_dashboard` for backward compatibility but make it a no-op. The docstring should note that layout is now automatic.

### `sre_agent/view_planner.py`

Keep `template` parameter on `plan_dashboard` for backward compatibility but make it optional with default `""`. Update docstring.

### `sre_agent/view_designer.py`

- Remove Rule 4: "ALWAYS use a layout template"
- Remove template selection from Design Patterns section
- Remove template from worked example
- Update `create_dashboard` references to not require template
- Update `plan_dashboard` references to not require template
- Add note: "Layout is computed automatically — you do not need to specify a template."

### `sre_agent/layout_templates.py`

Keep the file as-is. It's still imported by existing views in the database. The frontend `layoutTemplates.ts` references it for display. Mark the Python module as deprecated with a module-level comment.

### Frontend

**No changes needed.** The frontend already reads `positions` from the view spec. The only change is that every view now has positions (before, views without a template had no positions). This is strictly an improvement.

---

## 3. Test Framework (`tests/test_layout_engine.py`)

~20 tests:

**Single component tests:**
- `test_single_chart_full_width` — 1 chart → w=4, y=0
- `test_single_table_full_width` — 1 data_table → w=4
- `test_single_metric_card` — 1 metric_card → w=1, x=0

**KPI packing:**
- `test_four_metric_cards_in_row` — 4 cards → all y=0, x=0,1,2,3
- `test_grid_kpi_group_full_width` — grid with metric_cards → w=4
- `test_info_card_grid_full_width` — info_card_grid → w=4
- `test_kpi_before_charts` — KPIs always at top

**Chart packing:**
- `test_two_charts_side_by_side` — w=2 each, same y
- `test_three_charts` — first 2 side-by-side, third full-width
- `test_node_map_full_width` — node_map always w=4

**Detail pairing:**
- `test_log_viewer_key_value_paired` — side-by-side, w=2 each
- `test_key_value_relationship_tree_paired` — side-by-side
- `test_unpaired_detail_full_width` — single log_viewer → w=4

**Full dashboards:**
- `test_full_sre_dashboard` — grid + 2 charts + table → 3 rows, proper hierarchy
- `test_full_incident_report` — status_list + log_viewer + key_value + table
- `test_full_namespace_overview` — info_card_grid + 2 charts + table + log_viewer

**Edge cases:**
- `test_empty_list` — [] → {}
- `test_unknown_kind_at_bottom` — unknown kind → full-width at end
- `test_no_overlapping_positions` — verify no two components share x,y space
- `test_y_coordinates_monotonic` — y values never decrease within role groups

---

## 4. Files Created/Modified

| File | Action | Purpose |
|------|--------|---------|
| `sre_agent/layout_engine.py` | CREATE | Role-based auto-layout engine |
| `sre_agent/layout_templates.py` | MODIFY | Add deprecation comment |
| `sre_agent/api.py` | MODIFY | Replace template layout with compute_layout |
| `sre_agent/view_designer.py` | MODIFY | Remove template requirements from prompt |
| `sre_agent/view_tools.py` | MODIFY | Make template param optional/no-op |
| `sre_agent/view_planner.py` | MODIFY | Make template param optional |
| `tests/test_layout_engine.py` | CREATE | ~20 tests |
| `tests/test_harness.py` | MODIFY | Update any template-dependent tests |

---

## Out of Scope
- Frontend layout changes (positions already flow through)
- Agent-described layout hints (Phase 5)
- Responsive layout for different screen sizes
- Drag-and-drop repositioning
