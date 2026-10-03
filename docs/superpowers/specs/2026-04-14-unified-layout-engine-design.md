# Unified Layout Engine

> Historical design/implementation record. This preserves the proposal and tasks at the date in the filename; unchecked tasks, paths, commands, and security claims are not evidence of the current implementation. Use [current documentation](../../README.md) and verify behavior against code before applying a procedure.


**Date:** 2026-04-14
**Status:** Approved
**Author:** Ali + Claude

## Problem

Dashboard layout is currently split across three independent systems that can disagree:

1. **Backend `layout_engine.py`** — `compute_layout()` with role-based packing on a 4-column grid (rowHeight=30)
2. **Frontend `layoutTemplates.ts`** — `applyTemplate()` with slot-based templates (5 predefined)
3. **Frontend `CustomView.tsx`** — `generateDefaultLayout()` / `idealWidth()` / `idealHeight()` as fallback

This causes: sizing mismatches (widgets too tall/short/clipped), bad composition, poor spatial arrangement, broken grid containers, and layout jumps when editing. The three systems use different height scales (backend h=12 vs frontend template h=5) and make independent decisions.

## Design

**Single source of truth: backend-authoritative layout.** `layout_engine.py` is the only system that computes positions. The frontend renders them verbatim and only feeds back manual user adjustments (drag/resize in edit mode).

**Claude provides layout hints, engine enforces constraints.** Components can carry optional hints expressing layout intent. The engine respects valid hints and silently ignores bad ones. Claude gets expressiveness without raw pixel control.

## 1. Layout Hint Schema

Components gain an optional `layout` field:

```python
{"kind": "chart", "title": "CPU Usage", "layout": {"w": "half", "group": "resources"}}
```

### Hint vocabulary

| Hint | Meaning | Maps to |
|------|---------|---------|
| `w: "auto"` | Engine decides based on siblings (default) | adaptive |
| `w: "quarter"` | 1 of 4 columns | width=1 |
| `w: "half"` | 2 of 4 columns | width=2 |
| `w: "three_quarter"` | 3 of 4 columns | width=3 |
| `w: "full"` | 4 of 4 columns | width=4 |
| `h: "compact"` | Minimum height for kind | height * 0.7 |
| `h: "tall"` | Extra height for kind | height * 1.5 |
| `group: "<name>"` | Pack together in same row | row grouping |
| `priority: "top"` | Force to top of dashboard | role promotion |

**`w: "auto"` is the default.** Components without a `w` hint (or with `w: "auto"`) use the engine's adaptive packing: 1 KPI card -> w=4, 2 cards -> w=2 each, 3+ cards -> w=1 each, etc. Explicit width hints are opt-in overrides for when Claude has a specific layout intent.

### What the engine ignores

- Raw numeric values (no `w: 3`, `h: 15`)
- Absolute positioning (no `x`, `y`)
- Conflicting hints (two full-width items in the same group get ungrouped)

### Group overflow

When a group's total width exceeds 4 columns, the engine wraps to a new row. Items within the group are packed left-to-right: first row fills until full, remaining items continue on the next row. All rows belonging to the same group are placed consecutively (no interleaving with other groups).

### Responsive breakpoints

The backend computes layout for the 4-column (`lg`) grid only. At smaller breakpoints (`md: 2 cols`, `sm: 1 col`), `react-grid-layout` handles column clamping automatically — widgets wider than the available columns get clamped to fit. No separate layout maps per breakpoint are stored.

### Quality engine integration

`evaluate_components()` gains a warning for nonsensical hints (e.g., `w: "quarter"` on a `data_table`). Never errors — bad hints fall back to defaults.

New scoring rule (R8): +1 point if layout hints produce a balanced layout (no row exceeding 4 columns, no single-widget rows when grouping was possible).

## 2. Layout Engine Rewrite (`layout_engine.py`)

### Phase 1: Classify & Resolve Hints

Each component gets `(role, width, height)`:
- Classification via `_KIND_MAP` (same as today)
- Layout hints override defaults: `w: "half"` -> width=2, `h: "tall"` -> height * 1.5
- Invalid hints silently fall back to defaults

### Phase 2: Group & Order

- Components with matching `group` values get packed into the same row
- Within groups, items ordered by width (widest first for bin-packing)
- Ungrouped items follow `_ROLE_ORDER`: kpi_group -> kpi -> status -> chart -> detail -> table -> container
- `priority: "top"` items promoted above their natural role position
- Detail pairing (`_DETAIL_PAIRS`) reimplemented as auto-grouping for backward compatibility

### Phase 3: Pack

Row-based bin packing on 4-column grid:
- Each row fills left-to-right until full, then wraps
- Content-aware heights:

| Kind | Height formula |
|------|---------------|
| `data_table` | `3 + min(row_count, 12)` |
| `grid` (metric cards) | `1 + ceil(item_count / columns) * 3` |
| `grid` (resource_counts + cards) | resource_counts rows + card rows |
| `status_list` | `2 + min(ceil(item_count * 0.8), 8)` |
| `key_value` | `3 + min(pair_count, 5)` |
| `chart` | 10 (default), 8 (compact), 13 (tall) |
| Others | lookup from `_KIND_MAP` |

### Return value

Same as today: `{index: {"x": int, "y": int, "w": int, "h": int}}`

## 3. Frontend Changes

### Delete

- `idealWidth()` function from `CustomView.tsx`
- `idealHeight()` function from `CustomView.tsx`
- `generateDefaultLayout()` function from `CustomView.tsx`
- `layoutTemplates.ts` entire file (templates, `applyTemplate()`, types)
- `templateId` from `CustomView` interface in `customViewStore.ts`
- `templateId` handling in `CustomView.tsx` useMemo
- `applyTemplate` import in `CustomView.tsx`

### Keep

- `positionsToLayout()` — converts backend positions to react-grid-layout. Becomes the only layout path.
- `layoutToPositions()` — converts user drag/resize back to positions for `PUT /views/:id`
- `ResponsiveGrid` with `rowHeight={30}`, `cols={4}`, edit mode drag/resize

### Change `positionsToLayout()` fallback

Currently falls back to `idealHeight()` when position is missing. New behavior: missing position -> `{x: 0, y: max_y_from_other_positions, w: 4, h: 8}` (safe full-width default appended below existing widgets). `max_y` is computed as `max(pos.y + pos.h)` across all other positions, or `i * 8` if no positions exist at all. No height computation in the frontend.

### Edit mode contract

- User drags/resizes -> `handleLayoutChange` -> `PUT /views/:id` with new positions -> backend stores them
- Backend does NOT recompute layout on position updates — user overrides preserved
- `create_dashboard` triggers `compute_layout()` on all components (fresh dashboard, no user positions to preserve)
- `add_widget_to_view` does NOT trigger full `compute_layout()` — instead it uses single-widget placement:
  1. Read existing positions from the DB (preserves user-customized positions)
  2. Compute `max_y = max(pos.y + pos.h)` across existing positions
  3. Classify the new widget via `_classify()` to get its default `(role, width, height)`
  4. Place it at `{x: 0, y: max_y, w: default_w, h: default_h}`
  5. Save the updated positions map (existing positions unchanged, new widget appended)

This avoids the race condition where a full recompute during the 500ms drag debounce window could overwrite user drag/resize changes.

## 4. Hint Attachment — Embedded in Component Specs

**No separate tool.** Layout hints are embedded directly in the component dict under a `layout` key. This eliminates extra tool calls (each costing ~1-2s latency and ~200 tokens).

### How hints flow

1. **Tool-generated components** (e.g., `get_prometheus_query`, `list_pods`): These tools return `(text, component)`. The component dict can include a `layout` field. The API layer's `on_component` callback passes it through to `session_components` unchanged.

2. **`emit_component` tool**: Hints go directly in `spec_json` under a `layout` key. Already supported — no changes needed.

3. **Claude's role**: When Claude wants to hint a chart as half-width, it includes `"layout": {"w": "half"}` in the component spec it returns from the tool. Most components won't have hints — the engine's `w: "auto"` default handles them.

### Engine reads hints

In `_classify()`, after determining the default `(role, width, height)` from `_KIND_MAP`, the engine reads `component.get("layout", {})` and applies overrides:

```python
def _classify(component: dict) -> tuple[str, int, int]:
    kind = component.get("kind", "")
    role, default_w, default_h = _KIND_MAP.get(kind, ("container", 4, 5))
    
    hints = component.get("layout", {})
    w = _resolve_width(hints.get("w"), default_w)
    h = _resolve_height(hints.get("h"), default_h, component)
    
    return role, w, h
```

### No new tools, no new signals

This approach requires zero new tools and zero new API layer signal handling. The only change is in `layout_engine.py` — `_classify()` reads the `layout` field that's already present in the component dict.

## 5. View Designer Skill Prompt Changes

### Add to `skills/view-designer/skill.md`

Layout hints section teaching Claude the vocabulary with examples:
- When to use `w: "half"` + `group` for paired charts
- Explicit guidance: "Don't add hints unless you have a reason. The engine picks good defaults."

### Remove from skill

- References to deprecated `template` parameter
- `configurable.preferred_layout` enum (auto/compact/detailed)

### `plan_dashboard` tool

- Drop `template` parameter entirely (already deprecated, currently ignored)

## 6. Migration — Existing Dashboards

### `templateId` dashboards

Existing dashboards saved with `templateId` but empty `positions` will break when the frontend template system is deleted. Lazy migration in the API layer's view loading path:

```python
# In api/views.py rest_get_view or equivalent
if view.get("templateId") and not view.get("positions"):
    from ..layout_engine import compute_layout
    positions = compute_layout(view.get("layout", []))
    db.update_view(view_id, owner, positions=positions)
    view["positions"] = positions
```

This runs once per affected view on first load. After migration, the `templateId` field is ignored (not deleted from DB — no migration needed, just unused).

### User-customized positions

Dashboards where the user has manually resized/dragged widgets (non-empty `positions`) are **never recomputed**. The user's positions are preserved exactly as-is. Content-aware heights only apply to new dashboards created after this change.

## 7. Backend Cleanup — Dead Code

| File | Action |
|------|--------|
| `view_planner.py` | Remove `template` parameter from `plan_dashboard` |
| `view_tools.py` | Remove `template` from `create_dashboard` signal kwargs if present |
| `api/agent_ws.py` | Remove `view_template` / `templateId` handling in view_spec signal |

The `_DETAIL_PAIRS` dict and `_pack_details` function in `layout_engine.py` get absorbed into the group-based system (detail pairing becomes auto-grouping).

## 8. Test Updates

### Backend — `tests/test_layout_engine.py`

- All 20 existing tests preserved (backward compatibility)
- New `TestLayoutHints` class: `w: "half"`, `w: "full"`, `w: "auto"`, `group`, `priority: "top"`, invalid hints -> defaults
- New `TestContentAwareHeights` class: table height by row count, grid height by item count, status_list height by item count
- New `TestGroupOverflow` class: group with total width > 4 wraps correctly, mixed widths within groups
- New `TestSingleWidgetPlacement` class: `place_widget()` appends at `max_y` with correct sizing from `_classify`

### Frontend — `CustomView.test.ts`

- Delete entire `idealHeight` describe block (tests for deleted function)
- Update `positionsToLayout` fallback test: new fallback is `w: 4, h: 8` (was `w: 2, h: 10` via idealWidth/idealHeight)
- Remove `idealHeight` import

### Frontend types

- Remove `templateId` from `CustomView` interface in `customViewStore.ts`
- Remove `templateId` from `agentComponents.ts` if present

## 9. Eval Updates

### Existing scenarios — no changes needed

Current `view_designer.json` scenarios validate tool call sequences, not layout positions.

### New scenarios

- `view_layout_hints`: validates Claude embeds layout hints in component specs to pair related charts. Expected tools: `get_prometheus_query` x2, `plan_dashboard`, `create_dashboard`. Verify: resulting positions show two charts on the same row with `w: 2` each.
- `view_complex_layout`: 8-widget dashboard, validates no overlaps. Expected tools: `namespace_summary`, `cluster_metrics`, `get_prometheus_query` x2, `list_pods`, `plan_dashboard`, `create_dashboard`

### Baseline

Regenerate after new scenarios: `--save-baseline`

## Files Changed

### Backend (pulse-agent)

| File | Change |
|------|--------|
| `sre_agent/layout_engine.py` | Rewrite: add hint resolution, grouping, content-aware heights, `place_widget()` for single-widget append |
| `sre_agent/quality_engine.py` | Add hint validation warnings, R8 scoring rule |
| `sre_agent/view_tools.py` | Clean up template references |
| `sre_agent/view_planner.py` | Remove `template` parameter |
| `sre_agent/api/agent_ws.py` | Update `add_widget` to use single-widget placement, remove templateId handling, add lazy templateId migration |
| `sre_agent/api/views.py` | Add lazy templateId migration on view load |
| `sre_agent/skills/view-designer/skill.md` | Add layout hints section, remove template/preferred_layout config |
| `tests/test_layout_engine.py` | Add hint, content-aware height, group overflow, single-widget placement tests |
| `sre_agent/evals/scenarios_data/view_designer.json` | Add 2 new scenarios |

### Frontend (OpenshiftPulse)

| File | Change |
|------|--------|
| `src/kubeview/engine/layoutTemplates.ts` | Delete entire file |
| `src/kubeview/views/CustomView.tsx` | Remove idealWidth, idealHeight, generateDefaultLayout; simplify positionsToLayout fallback |
| `src/kubeview/store/customViewStore.ts` | Remove templateId from interface |
| `src/kubeview/engine/agentComponents.ts` | Remove templateId if present |
| `src/kubeview/views/__tests__/CustomView.test.ts` | Delete idealHeight tests, update positionsToLayout fallback assertions |
