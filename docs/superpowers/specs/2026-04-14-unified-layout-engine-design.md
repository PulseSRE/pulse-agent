# Unified Layout Engine

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
| `w: "quarter"` | 1 of 4 columns | width=1 |
| `w: "half"` | 2 of 4 columns | width=2 |
| `w: "three_quarter"` | 3 of 4 columns | width=3 |
| `w: "full"` | 4 of 4 columns | width=4 |
| `h: "compact"` | Minimum height for kind | height * 0.7 |
| `h: "tall"` | Extra height for kind | height * 1.5 |
| `group: "<name>"` | Pack together in same row | row grouping |
| `priority: "top"` | Force to top of dashboard | role promotion |

### What the engine ignores

- Raw numeric values (no `w: 3`, `h: 15`)
- Absolute positioning (no `x`, `y`)
- Conflicting hints (two full-width items in the same group get ungrouped)

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
- Only `create_dashboard` and `add_widget_to_view` trigger backend `compute_layout()`

## 4. Hint Attachment — `set_layout_hint` Tool

New tool for annotating tool-generated components with layout hints:

```python
@beta_tool
def set_layout_hint(hint_json: str, widget_index: int = -1):
    """Set layout hints on a component. Call AFTER the data tool that created it.
    
    Args:
        hint_json: JSON with layout hints. Keys: w (quarter/half/three_quarter/full),
                   h (compact/tall), group (string), priority (top).
        widget_index: Which component to annotate. -1 = most recent.
    """
```

Uses `__SIGNAL__` to tell the API layer to merge hints onto the component in `session_components`.

For `emit_component`: hints go directly in `spec_json` under a `layout` key.

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

## 6. Backend Cleanup — Dead Code

| File | Action |
|------|--------|
| `view_planner.py` | Remove `template` parameter from `plan_dashboard` |
| `view_tools.py` | Remove `template` from `create_dashboard` signal kwargs if present |
| `api/agent_ws.py` | Remove `view_template` / `templateId` handling in view_spec signal |

The `_DETAIL_PAIRS` dict and `_pack_details` function in `layout_engine.py` get absorbed into the group-based system (detail pairing becomes auto-grouping).

## 7. Test Updates

### Backend — `tests/test_layout_engine.py`

- All 20 existing tests preserved (backward compatibility)
- New `TestLayoutHints` class: `w: "half"`, `w: "full"`, `group`, `priority: "top"`, invalid hints -> defaults
- New `TestContentAwareHeights` class: table height by row count, grid height by item count, status_list height by item count

### Frontend — `CustomView.test.ts`

- Delete entire `idealHeight` describe block (tests for deleted function)
- Update `positionsToLayout` fallback test: new fallback is `w: 4, h: 8` (was `w: 2, h: 10` via idealWidth/idealHeight)
- Remove `idealHeight` import

### Frontend types

- Remove `templateId` from `CustomView` interface in `customViewStore.ts`
- Remove `templateId` from `agentComponents.ts` if present

## 8. Eval Updates

### Existing scenarios — no changes needed

Current `view_designer.json` scenarios validate tool call sequences, not layout positions.

### New scenarios

- `view_layout_hints`: validates Claude uses `set_layout_hint` to pair related charts. Expected tools: `get_prometheus_query` x2, `set_layout_hint`, `plan_dashboard`, `create_dashboard`
- `view_complex_layout`: 8-widget dashboard, validates no overlaps. Expected tools: `namespace_summary`, `cluster_metrics`, `get_prometheus_query` x2, `list_pods`, `plan_dashboard`, `create_dashboard`

### Baseline

Regenerate after new scenarios: `--save-baseline`

## Files Changed

### Backend (pulse-agent)

| File | Change |
|------|--------|
| `sre_agent/layout_engine.py` | Rewrite: add hint resolution, grouping, content-aware heights |
| `sre_agent/quality_engine.py` | Add hint validation warnings, R8 scoring rule |
| `sre_agent/view_tools.py` | Add `set_layout_hint` tool, clean up template references |
| `sre_agent/view_planner.py` | Remove `template` parameter |
| `sre_agent/api/agent_ws.py` | Handle `set_layout_hint` signal, remove templateId handling |
| `sre_agent/skills/view-designer/skill.md` | Add layout hints section, remove template/preferred_layout config |
| `tests/test_layout_engine.py` | Add hint + content-aware height tests |
| `sre_agent/evals/scenarios_data/view_designer.json` | Add 2 new scenarios |

### Frontend (OpenshiftPulse)

| File | Change |
|------|--------|
| `src/kubeview/engine/layoutTemplates.ts` | Delete entire file |
| `src/kubeview/views/CustomView.tsx` | Remove idealWidth, idealHeight, generateDefaultLayout; simplify positionsToLayout fallback |
| `src/kubeview/store/customViewStore.ts` | Remove templateId from interface |
| `src/kubeview/engine/agentComponents.ts` | Remove templateId if present |
| `src/kubeview/views/__tests__/CustomView.test.ts` | Delete idealHeight tests, update positionsToLayout fallback assertions |
