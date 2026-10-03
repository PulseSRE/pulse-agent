# New Dashboard Components Implementation Plan

> Historical design/implementation record. This preserves the proposal and tasks at the date in the filename; unchecked tasks, paths, commands, and security claims are not evidence of the current implementation. Use [current documentation](../../README.md) and verify behavior against code before applying a procedure.


> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add bar_list, progress_list, and stat_card component types to the dashboard widget system, plus editable descriptions in edit mode.

**Architecture:** Three new TypeScript interfaces in agentComponents.ts, three renderer functions in AgentComponentRenderer.tsx, backend validation in quality_engine.py, layout sizing in layout_engine.py. Editable descriptions added to CustomView.tsx edit mode block.

**Tech Stack:** React/TypeScript (frontend), Python (backend), Tailwind CSS

**Spec:** `docs/superpowers/specs/2026-04-07-new-dashboard-components-design.md`

---

### Task 1: Add TypeScript interfaces for 3 new component types

**Files:**
- Modify: `/Users/amobrem/ali/OpenshiftPulse/src/kubeview/engine/agentComponents.ts`

- [ ] **Step 1: Add BarListSpec interface**

After the `NodeMapSpec` interface (line 189), add:

```typescript
export interface BarListSpec {
  kind: 'bar_list';
  title?: string;
  description?: string;
  items: Array<{
    label: string;
    value: number;
    color?: string;
    badge?: string;
    badgeVariant?: 'error' | 'warning' | 'info';
    href?: string;
    gvr?: string;
  }>;
  maxItems?: number;
  valueLabel?: string;
}

export interface ProgressListSpec {
  kind: 'progress_list';
  title?: string;
  description?: string;
  items: Array<{
    label: string;
    value: number;
    max: number;
    unit?: string;
    detail?: string;
  }>;
  thresholds?: { warning: number; critical: number };
}

export interface StatCardSpec {
  kind: 'stat_card';
  title: string;
  value: string;
  unit?: string;
  trend?: 'up' | 'down' | 'stable';
  trendValue?: string;
  trendGood?: 'up' | 'down';
  description?: string;
  status?: 'healthy' | 'warning' | 'error';
}
```

- [ ] **Step 2: Add to ComponentSpec union type**

Update the `ComponentSpec` union (line 8-22) to include the 3 new types:

```typescript
export type ComponentSpec =
  | DataTableSpec
  | InfoCardGridSpec
  | BadgeListSpec
  | StatusListSpec
  | KeyValueSpec
  | ChartSpec
  | TabsSpec
  | GridSpec
  | SectionSpec
  | RelationshipTreeSpec
  | LogViewerSpec
  | YamlViewerSpec
  | MetricCardSpec
  | NodeMapSpec
  | BarListSpec
  | ProgressListSpec
  | StatCardSpec;
```

- [ ] **Step 3: Verify types compile**

Run: `cd /Users/amobrem/ali/OpenshiftPulse && npx tsc --noEmit 2>&1 | head -5`
Expected: No errors

- [ ] **Step 4: Commit**

```bash
cd /Users/amobrem/ali/OpenshiftPulse && git add src/kubeview/engine/agentComponents.ts && git commit -m "feat: add BarListSpec, ProgressListSpec, StatCardSpec types"
```

---

### Task 2: Add bar_list renderer

**Files:**
- Modify: `/Users/amobrem/ali/OpenshiftPulse/src/kubeview/components/agent/AgentComponentRenderer.tsx`

- [ ] **Step 1: Add import for BarListSpec**

At the top of the file, add `BarListSpec` to the import from agentComponents:

```typescript
import type { ..., BarListSpec } from '../../engine/agentComponents';
```

Find the existing import line and add `BarListSpec` to it.

- [ ] **Step 2: Add switch case**

In the `AgentComponentRenderer` switch (after the `node_map` case, before `default`), add:

```typescript
    case 'bar_list':
      return <AgentBarList spec={spec} />;
```

- [ ] **Step 3: Add renderer function**

Add before the `AgentMetricCard` function:

```typescript
/** Horizontal ranked bar chart — like "Top Tools" */
function AgentBarList({ spec }: { spec: BarListSpec }) {
  const maxItems = spec.maxItems ?? 10;
  const items = spec.items.slice(0, maxItems);
  const maxValue = Math.max(...items.map((i) => i.value), 1);

  return (
    <div className="my-2 border border-slate-700 rounded-lg overflow-hidden min-w-0">
      {spec.title && (
        <div className="px-3 py-1.5 bg-slate-800/50 border-b border-slate-700 text-xs font-medium text-slate-300">
          <span>{spec.title}</span>
          {spec.description && <span className="text-[10px] text-slate-500 ml-2">{spec.description}</span>}
        </div>
      )}
      <div className="p-3 space-y-1.5">
        {items.map((item, i) => (
          <div key={i} className="flex items-center gap-2 text-xs">
            {item.href || item.gvr ? (
              <a
                href={item.href || `#/resource/${item.gvr}`}
                className="w-36 truncate font-mono text-slate-300 hover:text-blue-400 hover:underline cursor-pointer"
                title={item.label}
              >
                {item.label}
              </a>
            ) : (
              <span className="w-36 truncate font-mono text-slate-300" title={item.label}>{item.label}</span>
            )}
            <div className="flex-1 h-4 bg-slate-800 rounded-sm overflow-hidden">
              <div
                className="h-full rounded-sm"
                style={{
                  width: `${(item.value / maxValue) * 100}%`,
                  backgroundColor: item.color || '#3b82f6',
                }}
              />
            </div>
            <span className="w-10 text-right text-slate-400 tabular-nums">{item.value}</span>
            {item.badge && (
              <span className={cn(
                'text-[10px] font-medium',
                item.badgeVariant === 'error' ? 'text-red-400' :
                item.badgeVariant === 'warning' ? 'text-amber-400' : 'text-blue-400'
              )}>
                {item.badge}
              </span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Verify it compiles**

Run: `cd /Users/amobrem/ali/OpenshiftPulse && npx tsc --noEmit 2>&1 | head -5`
Expected: No errors

- [ ] **Step 5: Commit**

```bash
cd /Users/amobrem/ali/OpenshiftPulse && git add src/kubeview/components/agent/AgentComponentRenderer.tsx && git commit -m "feat: add bar_list component renderer"
```

---

### Task 3: Add progress_list renderer

**Files:**
- Modify: `/Users/amobrem/ali/OpenshiftPulse/src/kubeview/components/agent/AgentComponentRenderer.tsx`

- [ ] **Step 1: Add import and switch case**

Add `ProgressListSpec` to the import from agentComponents. Add switch case:

```typescript
    case 'progress_list':
      return <AgentProgressList spec={spec} />;
```

- [ ] **Step 2: Add renderer function**

Add after the `AgentBarList` function:

```typescript
/** Utilization/capacity progress bars with auto-coloring */
function AgentProgressList({ spec }: { spec: ProgressListSpec }) {
  const warn = spec.thresholds?.warning ?? 70;
  const crit = spec.thresholds?.critical ?? 90;

  function barColor(pct: number): string {
    if (pct >= crit) return '#ef4444';  // red
    if (pct >= warn) return '#f59e0b';  // amber
    return '#10b981';                    // green
  }

  return (
    <div className="my-2 border border-slate-700 rounded-lg overflow-hidden min-w-0">
      {spec.title && (
        <div className="px-3 py-1.5 bg-slate-800/50 border-b border-slate-700 text-xs font-medium text-slate-300">
          <span>{spec.title}</span>
          {spec.description && <span className="text-[10px] text-slate-500 ml-2">{spec.description}</span>}
        </div>
      )}
      <div className="p-3 space-y-2.5">
        {spec.items.map((item, i) => {
          const pct = item.max > 0 ? (item.value / item.max) * 100 : 0;
          return (
            <div key={i}>
              <div className="flex items-center justify-between text-xs mb-0.5">
                <div>
                  <span className="text-slate-300">{item.label}</span>
                  {item.detail && <span className="text-[10px] text-slate-500 ml-1.5">{item.detail}</span>}
                </div>
                <span className="text-slate-400 tabular-nums">
                  {item.value}/{item.max}{item.unit ? ` ${item.unit}` : ''}
                </span>
              </div>
              <div className="h-2 bg-slate-800 rounded-full overflow-hidden">
                <div
                  className="h-full rounded-full transition-all"
                  style={{ width: `${Math.min(pct, 100)}%`, backgroundColor: barColor(pct) }}
                />
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Verify it compiles**

Run: `cd /Users/amobrem/ali/OpenshiftPulse && npx tsc --noEmit 2>&1 | head -5`
Expected: No errors

- [ ] **Step 4: Commit**

```bash
cd /Users/amobrem/ali/OpenshiftPulse && git add src/kubeview/components/agent/AgentComponentRenderer.tsx && git commit -m "feat: add progress_list component renderer"
```

---

### Task 4: Add stat_card renderer

**Files:**
- Modify: `/Users/amobrem/ali/OpenshiftPulse/src/kubeview/components/agent/AgentComponentRenderer.tsx`

- [ ] **Step 1: Add import and switch case**

Add `StatCardSpec` to the import from agentComponents. Add switch case:

```typescript
    case 'stat_card':
      return <AgentStatCard spec={spec} />;
```

- [ ] **Step 2: Add renderer function**

Add after the `AgentProgressList` function. Reuses `METRIC_STATUS_BORDER` already defined in the file:

```typescript
/** Single big number with trend indicator */
function AgentStatCard({ spec }: { spec: StatCardSpec }) {
  const goodDir = spec.trendGood || 'down';
  const trendIsGood = spec.trend === goodDir;
  const trendColor = !spec.trend || spec.trend === 'stable'
    ? 'text-slate-400'
    : trendIsGood ? 'text-emerald-400' : 'text-red-400';
  const trendArrow = spec.trend === 'up' ? '\u2191' : spec.trend === 'down' ? '\u2193' : '';

  return (
    <div className={cn(
      'bg-slate-900 rounded-lg border p-4 flex flex-col items-center justify-center text-center',
      METRIC_STATUS_BORDER[spec.status || ''] || 'border-slate-800'
    )}>
      <span className="text-xs text-slate-400 mb-1">{spec.title}</span>
      <div className="text-2xl font-bold text-slate-100 font-mono">
        {spec.value}{spec.unit && <span className="text-sm text-slate-400 ml-0.5">{spec.unit}</span>}
      </div>
      {spec.trend && spec.trendValue && (
        <div className={cn('text-xs mt-1 font-medium', trendColor)}>
          {trendArrow} {spec.trendValue}
        </div>
      )}
      {spec.description && <div className="text-[10px] text-slate-500 mt-1">{spec.description}</div>}
    </div>
  );
}
```

- [ ] **Step 3: Verify it compiles**

Run: `cd /Users/amobrem/ali/OpenshiftPulse && npx tsc --noEmit 2>&1 | head -5`
Expected: No errors

- [ ] **Step 4: Run tests**

Run: `cd /Users/amobrem/ali/OpenshiftPulse && pnpm test -- --run 2>&1 | tail -5`
Expected: All 1882 tests pass

- [ ] **Step 5: Commit**

```bash
cd /Users/amobrem/ali/OpenshiftPulse && git add src/kubeview/components/agent/AgentComponentRenderer.tsx && git commit -m "feat: add stat_card component renderer"
```

---

### Task 5: Add editable descriptions in edit mode

**Files:**
- Modify: `/Users/amobrem/ali/OpenshiftPulse/src/kubeview/views/CustomView.tsx`

- [ ] **Step 1: Add description editor below the title editor**

In the edit mode block (around line 409-418), after the existing title `<input>`, add a description editor:

Find this block:
```typescript
                  {editMode && (spec as any).title && (
                    <input
                      defaultValue={(spec as any).title}
                      onBlur={(e) => {
                        if (e.target.value !== (spec as any).title) {
                          updateWidget(view.id, i, { title: e.target.value } as any);
                        }
                      }}
                      onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }}
                      className="w-full text-xs font-medium text-slate-300 bg-transparent border-b border-slate-700 focus:border-violet-500 outline-none mb-1 px-1 py-0.5"
                    />
```

Replace with:
```typescript
                  {editMode && (
                    <>
                      {(spec as any).title && (
                        <input
                          defaultValue={(spec as any).title}
                          onBlur={(e) => {
                            if (e.target.value !== (spec as any).title) {
                              updateWidget(view.id, i, { title: e.target.value } as any);
                            }
                          }}
                          onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }}
                          className="w-full text-xs font-medium text-slate-300 bg-transparent border-b border-slate-700 focus:border-violet-500 outline-none mb-1 px-1 py-0.5"
                        />
                      )}
                      <input
                        defaultValue={(spec as any).description || ''}
                        placeholder="Add description..."
                        onBlur={(e) => {
                          if (e.target.value !== ((spec as any).description || '')) {
                            updateWidget(view.id, i, { description: e.target.value } as any);
                          }
                        }}
                        onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }}
                        className="w-full text-[10px] text-slate-500 bg-transparent border-b border-slate-700/50 focus:border-violet-500 outline-none mb-1 px-1 py-0.5"
                      />
                    </>
```

Note: The closing `)}` for the original block needs to match the new `<>` fragment structure. The original `{editMode && (spec as any).title && (` becomes `{editMode && (`, and the title check moves inside a conditional.

- [ ] **Step 2: Verify it compiles**

Run: `cd /Users/amobrem/ali/OpenshiftPulse && npx tsc --noEmit 2>&1 | head -5`
Expected: No errors

- [ ] **Step 3: Run tests**

Run: `cd /Users/amobrem/ali/OpenshiftPulse && pnpm test -- --run 2>&1 | tail -5`
Expected: All tests pass

- [ ] **Step 4: Commit**

```bash
cd /Users/amobrem/ali/OpenshiftPulse && git add src/kubeview/views/CustomView.tsx && git commit -m "feat: add editable descriptions in dashboard edit mode"
```

---

### Task 6: Add frontend default heights for new components

**Files:**
- Modify: `/Users/amobrem/ali/OpenshiftPulse/src/kubeview/views/CustomView.tsx`

- [ ] **Step 1: Add height cases to generateDefaultLayout**

In the `generateDefaultLayout` height switch (around line 49), add cases for the 3 new kinds before the default. Add after the `tabs` line:

```typescript
      spec.kind === 'bar_list' ? 8 :
      spec.kind === 'progress_list' ? 8 :
      spec.kind === 'stat_card' ? 4 :
```

- [ ] **Step 2: Commit**

```bash
cd /Users/amobrem/ali/OpenshiftPulse && git add src/kubeview/views/CustomView.tsx && git commit -m "feat: add default layout heights for bar_list, progress_list, stat_card"
```

---

### Task 7: Backend — add new kinds to quality_engine

**Files:**
- Modify: `/Users/amobrem/ali/pulse-agent/sre_agent/quality_engine.py`
- Test: `/Users/amobrem/ali/pulse-agent/tests/test_quality_engine.py`

- [ ] **Step 1: Add to VALID_KINDS**

In `VALID_KINDS` frozenset (line 18-35), add:

```python
        "bar_list",
        "progress_list",
        "stat_card",
```

- [ ] **Step 2: Update title_required**

In `_validate_component` (line 355), update the title exemption to include bar_list and progress_list:

```python
    title_required = kind not in ("grid", "tabs", "section", "bar_list", "progress_list")
```

- [ ] **Step 3: Add validation for new kinds**

After the `elif kind == "grid":` block (line 377-381), add:

```python
    elif kind == "bar_list":
        items = comp.get("items")
        if not items:
            result.errors.append("bar_list must have at least 1 item.")
        else:
            for item in items:
                if not item.get("label"):
                    result.errors.append("bar_list item missing 'label'.")
                if "value" not in item:
                    result.errors.append("bar_list item missing 'value'.")

    elif kind == "progress_list":
        items = comp.get("items")
        if not items:
            result.errors.append("progress_list must have at least 1 item.")
        else:
            for item in items:
                if not item.get("label"):
                    result.errors.append("progress_list item missing 'label'.")
                if "value" not in item:
                    result.errors.append("progress_list item missing 'value'.")
                if not item.get("max") or item.get("max", 0) <= 0:
                    result.errors.append(f"progress_list item '{item.get('label', '?')}' must have 'max' > 0.")

    elif kind == "stat_card":
        if not comp.get("value"):
            result.errors.append(f"Stat card '{title}' must have 'value'.")
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_quality_engine.py -v 2>&1 | tail -10`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add sre_agent/quality_engine.py && git commit -m "feat: add bar_list, progress_list, stat_card to quality_engine validation"
```

---

### Task 8: Backend — add new kinds to layout_engine

**Files:**
- Modify: `/Users/amobrem/ali/pulse-agent/sre_agent/layout_engine.py`

- [ ] **Step 1: Add to _KIND_MAP**

In `_KIND_MAP` (line 15-29), add 3 new entries:

```python
    "bar_list": ("detail", 2, 8),
    "progress_list": ("detail", 2, 8),
    "stat_card": ("kpi", 1, 4),
```

- [ ] **Step 2: Commit**

```bash
git add sre_agent/layout_engine.py && git commit -m "feat: add bar_list, progress_list, stat_card to layout_engine"
```

---

### Task 9: Add emit_component tool

**Files:**
- Modify: `/Users/amobrem/ali/pulse-agent/sre_agent/view_tools.py`
- Modify: `/Users/amobrem/ali/pulse-agent/tests/test_harness.py`
- Modify: `/Users/amobrem/ali/pulse-agent/tests/eval_prompts.py`

- [ ] **Step 1: Add the tool**

In `view_tools.py`, after the `remove_widget_from_view` function, add:

```python
@beta_tool
def emit_component(kind: str, spec_json: str) -> str:
    """Emit a custom component for the current dashboard. Use for bar_list, progress_list, stat_card, or any component type.

    The component is added to the session and will be included when create_dashboard is called.

    Args:
        kind: Component kind (e.g. 'bar_list', 'progress_list', 'stat_card', 'status_list').
        spec_json: JSON string with the component spec. Must include all required fields for the kind.
            Example bar_list: {"title": "Top Pods", "items": [{"label": "nginx", "value": 42}]}
            Example progress_list: {"title": "Node CPU", "items": [{"label": "node-1", "value": 70, "max": 100, "unit": "%"}]}
            Example stat_card: {"title": "Error Rate", "value": "2.3", "unit": "%", "trend": "down", "trendValue": "12%"}
    """
    import json as _json

    try:
        spec = _json.loads(spec_json)
    except _json.JSONDecodeError as e:
        return f"Invalid JSON: {e}"

    spec["kind"] = kind

    # Basic validation
    from .quality_engine import VALID_KINDS

    if kind not in VALID_KINDS:
        return f"Invalid kind '{kind}'. Valid: {', '.join(sorted(VALID_KINDS))}"

    text = f"Emitted {kind} component"
    if spec.get("title"):
        text += f": {spec['title']}"

    return (text, spec)
```

- [ ] **Step 2: Register the tool**

Add to the registration block:

```python
register_tool(emit_component)
```

Add to VIEW_TOOLS list:

```python
    emit_component,
```

- [ ] **Step 3: Add to test exclusions**

In `tests/test_harness.py`, add `"emit_component"` to the EXCLUDED set.

- [ ] **Step 4: Add eval prompt**

In `tests/eval_prompts.py`, add:

```python
    (
        "add a bar chart showing the top namespaces by pod count",
        ["emit_component"],
        "view_designer",
        "Emit bar_list component",
    ),
```

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/ -q 2>&1 | tail -3`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add sre_agent/view_tools.py tests/test_harness.py tests/eval_prompts.py && git commit -m "feat: add emit_component tool for custom component types"
```

---

### Task 10: Update view designer prompt with new component types

**Files:**
- Modify: `/Users/amobrem/ali/pulse-agent/sre_agent/view_designer.py`

- [ ] **Step 1: Add new components to Component Selection table**

In `VIEW_DESIGNER_SYSTEM_PROMPT` (line 128-141), add 3 rows to the table:

```
| Ranked list      | `emit_component("bar_list", ...)`      | bar_list       |
| Utilization bars | `emit_component("progress_list", ...)` | progress_list  |
| Single big stat  | `emit_component("stat_card", ...)`     | stat_card      |
```

- [ ] **Step 2: Add guidance for when to use new components**

After the "## Design Patterns" section (before "## Color Semantics"), add:

```
## Additional Component Types

Use `emit_component(kind, spec_json)` for these specialized components:

- **bar_list** — Horizontal ranked bars. Use for "top N" views (tools, namespaces by pod count, images by vulnerability). Spec: `{"title": "...", "items": [{"label": "name", "value": 42, "badge": "2 err", "badgeVariant": "error"}]}`
- **progress_list** — Utilization/capacity bars with auto green/yellow/red. Use for node CPU/memory, PVC usage, quota. Spec: `{"title": "...", "items": [{"label": "node-1", "value": 70, "max": 100, "unit": "%"}]}`
- **stat_card** — Single big number with trend arrow. Use for prominent KPIs like error rate, uptime, SLA. Spec: `{"title": "...", "value": "2.3", "unit": "%", "trend": "down", "trendValue": "12%", "trendGood": "down"}`
```

- [ ] **Step 3: Commit**

```bash
git add sre_agent/view_designer.py && git commit -m "feat: update view designer prompt with emit_component and new component types"
```

---

### Task 11: Update docs

**Files:**
- Modify: `/Users/amobrem/ali/pulse-agent/CLAUDE.md`
- Modify: `/Users/amobrem/ali/pulse-agent/README.md`

- [ ] **Step 1: Update CLAUDE.md**

In the project description line, update the component type count if mentioned.

- [ ] **Step 2: Update README.md**

Add the 3 new component types to the component list section if one exists.

- [ ] **Step 3: Run tests, commit, push both repos**

```bash
# Agent repo
python3 -m pytest tests/ -q
git add CLAUDE.md README.md && git commit -m "docs: add bar_list, progress_list, stat_card to docs"
git push

# UI repo
cd /Users/amobrem/ali/OpenshiftPulse
pnpm test -- --run
git push
```

---

### Task 12: Deploy and verify

- [ ] **Step 1: Full deploy**

```bash
cd /Users/amobrem/ali/OpenshiftPulse && ./deploy/deploy.sh
```

- [ ] **Step 2: Verify by asking the agent to create a dashboard using new components**

Test prompt: "Create a dashboard showing tool usage stats with a bar chart of top tools and resource utilization progress bars"
