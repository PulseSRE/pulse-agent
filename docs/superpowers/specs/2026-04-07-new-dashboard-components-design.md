# New Dashboard Components: bar_list, progress_list, stat_card + Editable Descriptions

> Historical design/implementation record. This preserves the proposal and tasks at the date in the filename; unchecked tasks, paths, commands, and security claims are not evidence of the current implementation. Use [current documentation](../../README.md) and verify behavior against code before applying a procedure.


**Date:** 2026-04-07
**Status:** Approved

## Summary

Add 3 new component types to the dashboard widget system and inline description editing for all widgets in edit mode. These fill gaps in the current component library:

- **bar_list** — Horizontal ranked bar chart (like the Top Tools visualization on the Tools page)
- **progress_list** — Utilization/capacity bars with auto-coloring thresholds
- **stat_card** — Single big number with trend arrow and delta
- **Editable descriptions** — All widgets get inline description editing in edit mode

## Component Specifications

### 1. bar_list

Horizontal bar chart for ranked lists. Each item shows a label, proportional bar, count, and optional error badge. Items can be clickable.

```typescript
interface BarListSpec {
  kind: 'bar_list';
  title?: string;
  description?: string;
  items: Array<{
    label: string;
    value: number;
    color?: string;        // bar color (default: #3b82f6 blue-500)
    badge?: string;        // e.g. "2 err" in red
    badgeVariant?: 'error' | 'warning' | 'info';
    href?: string;         // clickable external link
    gvr?: string;          // K8s resource link (opens in resource browser)
  }>;
  maxItems?: number;       // default 10, truncate display
  valueLabel?: string;     // e.g. "calls", "pods" — shown in tooltip
}
```

**Rendering:** Matches the existing Top Tools bar chart on the Tools & Agents page. Monospace labels left-aligned (truncated at ~20 chars), proportional blue bars filling available width, count right-aligned, optional red/yellow badge after count. Header bar with title/description like data_table. Clickable items get hover underline + cursor pointer.

**Backend validation (quality_engine):** Title optional. Must have at least 1 item. Each item must have label and value.

**Layout engine:** role=`detail`, w=2, h=8 (rowHeight=30).

### 2. progress_list

Utilization/capacity progress bars with automatic threshold coloring. Shows resource consumption at a glance.

```typescript
interface ProgressListSpec {
  kind: 'progress_list';
  title?: string;
  description?: string;
  items: Array<{
    label: string;
    value: number;         // current usage
    max: number;           // total capacity
    unit?: string;         // e.g. "Mi", "cores", "%"
    detail?: string;       // secondary text below label
  }>;
  thresholds?: { warning: number; critical: number }; // default { warning: 70, critical: 90 } (percentage)
}
```

**Rendering:** Each row: label (left), progress bar (center, fills proportionally), "value/max unit" (right). Bar color auto-determined by percentage: green (<warning), yellow (warning..critical), red (>=critical). Header bar with title/description like data_table.

**Backend validation (quality_engine):** Title optional. Must have at least 1 item. Each item must have label, value, max. max must be > 0.

**Layout engine:** role=`detail`, w=2, h=8 (rowHeight=30).

### 3. stat_card

Single prominent statistic with optional trend indicator. Simpler than metric_card — no sparkline, bigger number, trend comparison.

```typescript
interface StatCardSpec {
  kind: 'stat_card';
  title: string;
  value: string;
  unit?: string;
  trend?: 'up' | 'down' | 'stable';
  trendValue?: string;    // e.g. "12%", "+5", "-3.2"
  trendGood?: 'up' | 'down'; // which direction is good (default: 'down')
  description?: string;
  status?: 'healthy' | 'warning' | 'error';
}
```

**Rendering:** Centered layout. Title in small text at top, large value+unit in the center (text-2xl font-bold), trend arrow with colored delta below (green if trend matches trendGood, red otherwise). Border color from status (same as metric_card). Description at bottom in small text.

**Backend validation (quality_engine):** Title required. Must have value.

**Layout engine:** role=`kpi`, w=1, h=4 (rowHeight=30).

### 4. Editable Descriptions

In edit mode on CustomView, each widget already shows an editable title input. Add a second editable field for description below it, following the same pattern:

- Show only in edit mode
- Default text: "Add description..." placeholder
- `onBlur` saves via `updateWidget(viewId, index, { description: value })`
- Same styling as title editor but `text-[10px] text-slate-500`

**Files changed:** `CustomView.tsx` only — the edit mode widget rendering block.

## Files to Create/Modify

### Frontend (OpenshiftPulse)

| File | Action | What |
|------|--------|------|
| `src/kubeview/engine/agentComponents.ts` | MODIFY | Add BarListSpec, ProgressListSpec, StatCardSpec interfaces + union type |
| `src/kubeview/components/agent/AgentComponentRenderer.tsx` | MODIFY | Add 3 renderer functions + switch cases |
| `src/kubeview/views/CustomView.tsx` | MODIFY | Add description editing in edit mode block |

### Backend (pulse-agent)

| File | Action | What |
|------|--------|------|
| `sre_agent/quality_engine.py` | MODIFY | Add 3 kinds to VALID_KINDS, validation in _validate_component |
| `sre_agent/layout_engine.py` | MODIFY | Add 3 entries to _KIND_MAP |
| `sre_agent/harness.py` | MODIFY | Add to _TOOL_COMPONENTS (bar_list, progress_list, stat_card) |
| `tests/test_harness.py` | MODIFY | Update EXCLUDED set if needed |
| `tests/eval_prompts.py` | NO CHANGE | No new tools — these are component types, not tools |

### Docs

| File | Action | What |
|------|--------|------|
| `CLAUDE.md` | MODIFY | Update tool/component count |
| `README.md` | MODIFY | Mention new component types |

## Testing

- Existing quality_engine tests cover the validation framework; add test cases for new kinds
- Frontend tests: component renderer tests for each new type
- No new tools needed — the agent already emits component specs from existing tools

## Out of Scope

- New agent tools that specifically produce these components (the agent can already emit any component kind from any tool)
- Live data/PromQL support on bar_list or progress_list (can be added later)
- Drag-and-drop reordering within bar_list/progress_list items
