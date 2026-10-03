# Semantic Layout Engine Implementation Plan

> Historical design/implementation record. This preserves the proposal and tasks at the date in the filename; unchecked tasks, paths, commands, and security claims are not evidence of the current implementation. Use [current documentation](../../README.md) and verify behavior against code before applying a procedure.


> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace fixed template slot matching with a smart auto-layout engine that computes grid positions from component roles — no template selection needed.

**Architecture:** New `layout_engine.py` classifies components by role (kpi/chart/detail/table), sorts into priority order, and packs into a 4-column grid with pairing rules for side-by-side placement. Replaces template-based layout in `api.py`. Agent prompt simplified to remove all template references.

**Tech Stack:** Python 3.11+, pytest, 4-column grid (x/y/w/h positions)

---

## Files Overview

| File | Action | Purpose |
|------|--------|---------|
| `sre_agent/layout_engine.py` | CREATE | Role-based auto-layout engine |
| `tests/test_layout_engine.py` | CREATE | ~20 layout tests |
| `sre_agent/api.py` | MODIFY | Replace template layout with compute_layout |
| `sre_agent/view_designer.py` | MODIFY | Remove template requirements from prompt |
| `sre_agent/view_tools.py` | MODIFY | Make template param no-op |
| `sre_agent/view_planner.py` | MODIFY | Make template param optional |
| `sre_agent/layout_templates.py` | MODIFY | Add deprecation comment |
| `tests/test_harness.py` | MODIFY | Update template tests to use layout_engine |

---

### Task 1: Layout Engine Module

**Files:**
- Create: `sre_agent/layout_engine.py`
- Create: `tests/test_layout_engine.py`

- [ ] **Step 1: Write layout engine tests**

Create `tests/test_layout_engine.py`:

```python
"""Tests for the semantic layout engine."""

from __future__ import annotations

from sre_agent.layout_engine import compute_layout


class TestSingleComponents:
    def test_single_chart_full_width(self):
        components = [{"kind": "chart", "title": "CPU"}]
        pos = compute_layout(components)
        assert pos[0]["w"] == 4
        assert pos[0]["x"] == 0

    def test_single_table_full_width(self):
        components = [{"kind": "data_table", "title": "Pods"}]
        pos = compute_layout(components)
        assert pos[0]["w"] == 4

    def test_single_metric_card(self):
        components = [{"kind": "metric_card", "title": "CPU"}]
        pos = compute_layout(components)
        assert pos[0]["w"] == 1
        assert pos[0]["x"] == 0

    def test_empty_list(self):
        assert compute_layout([]) == {}

    def test_unknown_kind_full_width(self):
        components = [{"kind": "foobar", "title": "Unknown"}]
        pos = compute_layout(components)
        assert pos[0]["w"] == 4


class TestKPIPacking:
    def test_four_metric_cards_in_row(self):
        components = [
            {"kind": "metric_card", "title": f"M{i}"} for i in range(4)
        ]
        pos = compute_layout(components)
        assert all(pos[i]["y"] == pos[0]["y"] for i in range(4))
        assert [pos[i]["x"] for i in range(4)] == [0, 1, 2, 3]
        assert all(pos[i]["w"] == 1 for i in range(4))

    def test_grid_kpi_group_full_width(self):
        components = [
            {"kind": "grid", "title": "KPIs", "items": [
                {"kind": "metric_card", "title": "CPU"},
            ]},
        ]
        pos = compute_layout(components)
        assert pos[0]["w"] == 4

    def test_info_card_grid_full_width(self):
        components = [{"kind": "info_card_grid", "title": "Summary"}]
        pos = compute_layout(components)
        assert pos[0]["w"] == 4

    def test_kpi_before_charts(self):
        components = [
            {"kind": "chart", "title": "CPU Trend"},
            {"kind": "metric_card", "title": "CPU"},
        ]
        pos = compute_layout(components)
        # metric_card should have lower y than chart
        mc_idx = next(i for i, c in enumerate(components) if c["kind"] == "metric_card")
        ch_idx = next(i for i, c in enumerate(components) if c["kind"] == "chart")
        assert pos[mc_idx]["y"] < pos[ch_idx]["y"]


class TestChartPacking:
    def test_two_charts_side_by_side(self):
        components = [
            {"kind": "chart", "title": "CPU"},
            {"kind": "chart", "title": "Memory"},
        ]
        pos = compute_layout(components)
        assert pos[0]["w"] == 2
        assert pos[1]["w"] == 2
        assert pos[0]["y"] == pos[1]["y"]
        assert pos[0]["x"] != pos[1]["x"]

    def test_three_charts(self):
        components = [
            {"kind": "chart", "title": "CPU"},
            {"kind": "chart", "title": "Memory"},
            {"kind": "chart", "title": "Network"},
        ]
        pos = compute_layout(components)
        # First two side-by-side
        assert pos[0]["w"] == 2
        assert pos[1]["w"] == 2
        assert pos[0]["y"] == pos[1]["y"]
        # Third full-width below
        assert pos[2]["w"] == 4
        assert pos[2]["y"] > pos[0]["y"]

    def test_node_map_full_width(self):
        components = [{"kind": "node_map", "title": "Nodes"}]
        pos = compute_layout(components)
        assert pos[0]["w"] == 4


class TestDetailPairing:
    def test_log_viewer_key_value_paired(self):
        components = [
            {"kind": "log_viewer", "title": "Logs"},
            {"kind": "key_value", "title": "Details"},
        ]
        pos = compute_layout(components)
        assert pos[0]["w"] == 2
        assert pos[1]["w"] == 2
        assert pos[0]["y"] == pos[1]["y"]

    def test_key_value_relationship_tree_paired(self):
        components = [
            {"kind": "key_value", "title": "Details"},
            {"kind": "relationship_tree", "title": "Owners"},
        ]
        pos = compute_layout(components)
        assert pos[0]["w"] == 2
        assert pos[1]["w"] == 2
        assert pos[0]["y"] == pos[1]["y"]

    def test_unpaired_detail_full_width(self):
        components = [{"kind": "log_viewer", "title": "Logs"}]
        pos = compute_layout(components)
        assert pos[0]["w"] == 4


class TestFullDashboards:
    def test_sre_dashboard(self):
        """grid + 2 charts + table → 3 rows, proper hierarchy."""
        components = [
            {"kind": "grid", "title": "KPIs", "items": [
                {"kind": "metric_card", "title": "CPU"},
            ]},
            {"kind": "chart", "title": "CPU Trend"},
            {"kind": "chart", "title": "Memory Trend"},
            {"kind": "data_table", "title": "Pods"},
        ]
        pos = compute_layout(components)
        # KPI grid at top
        assert pos[0]["y"] == 0
        assert pos[0]["w"] == 4
        # Charts below KPIs, side-by-side
        assert pos[1]["y"] > pos[0]["y"]
        assert pos[2]["y"] == pos[1]["y"]
        assert pos[1]["w"] == 2
        assert pos[2]["w"] == 2
        # Table below charts
        assert pos[3]["y"] > pos[1]["y"]
        assert pos[3]["w"] == 4

    def test_incident_report(self):
        """status_list + log_viewer + key_value + table."""
        components = [
            {"kind": "status_list", "title": "Status"},
            {"kind": "log_viewer", "title": "Logs"},
            {"kind": "key_value", "title": "Details"},
            {"kind": "data_table", "title": "Events"},
        ]
        pos = compute_layout(components)
        # Status at top
        assert pos[0]["y"] == 0
        # Log + key_value paired below
        assert pos[1]["y"] > pos[0]["y"]
        assert pos[1]["w"] == 2
        assert pos[2]["y"] == pos[1]["y"]
        assert pos[2]["w"] == 2
        # Table at bottom
        assert pos[3]["y"] > pos[1]["y"]

    def test_no_overlapping_positions(self):
        """Verify no two components occupy the same grid space."""
        components = [
            {"kind": "grid", "title": "KPIs", "items": [{"kind": "metric_card", "title": "X"}]},
            {"kind": "chart", "title": "A"},
            {"kind": "chart", "title": "B"},
            {"kind": "data_table", "title": "T"},
            {"kind": "log_viewer", "title": "L"},
            {"kind": "key_value", "title": "K"},
        ]
        pos = compute_layout(components)
        # Check no two items share the same (x, y) starting point
        coords = [(p["x"], p["y"]) for p in pos.values()]
        assert len(coords) == len(set(coords)), f"Overlapping positions: {coords}"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_layout_engine.py -v`
Expected: FAIL — module doesn't exist

- [ ] **Step 3: Implement layout_engine.py**

Create `sre_agent/layout_engine.py`:

```python
"""Semantic Layout Engine — computes grid positions from component roles.

Replaces fixed template slot matching. Components are classified by kind
into roles (kpi, chart, detail, table), sorted by visual hierarchy, and
packed into a 4-column grid with smart pairing for side-by-side placement.
"""

from __future__ import annotations

from dataclasses import dataclass

# 4-column grid
GRID_COLS = 4


@dataclass
class _Classified:
    index: int       # Original index in the input list
    comp: dict       # The component dict
    role: str        # kpi, kpi_group, chart, detail, table, status, container
    default_w: int
    default_h: int


# Role priority — components are laid out in this order top-to-bottom
_ROLE_ORDER = ["kpi_group", "kpi", "status", "chart", "detail", "table", "container"]

# Kinds that can be paired side-by-side when two details appear
_DETAIL_PAIRS = {
    "log_viewer": {"key_value", "yaml_viewer"},
    "key_value": {"relationship_tree", "log_viewer"},
    "yaml_viewer": {"log_viewer"},
    "relationship_tree": {"key_value"},
}


def _classify(comp: dict) -> tuple[str, int, int]:
    """Return (role, default_w, default_h) for a component."""
    kind = comp.get("kind", "")

    if kind == "metric_card":
        return "kpi", 1, 2
    if kind == "grid":
        # Check if it contains metric_cards
        items = comp.get("items", [])
        if any(item.get("kind") == "metric_card" for item in items):
            return "kpi_group", 4, 2
        return "container", 4, 5
    if kind == "info_card_grid":
        return "kpi_group", 4, 2
    if kind == "chart":
        return "chart", 2, 5
    if kind == "node_map":
        return "chart", 4, 6
    if kind == "data_table":
        return "table", 4, 6
    if kind in ("status_list", "badge_list"):
        return "status", 4, 3
    if kind in ("log_viewer",):
        return "detail", 2, 6
    if kind in ("key_value", "yaml_viewer", "relationship_tree"):
        return "detail", 2, 5
    if kind in ("tabs",):
        return "container", 4, 8
    if kind in ("section",):
        return "container", 4, 5

    # Unknown kind — full-width at bottom
    return "container", 4, 5


def compute_layout(components: list[dict]) -> dict[int, dict]:
    """Compute grid positions for dashboard components.

    Returns {original_index: {"x": int, "y": int, "w": int, "h": int}}.
    Components are sorted by role priority (KPIs → charts → details → tables)
    and packed into a 4-column grid.
    """
    if not components:
        return {}

    # Classify all components
    classified = []
    for i, comp in enumerate(components):
        role, w, h = _classify(comp)
        classified.append(_Classified(index=i, comp=comp, role=role, default_w=w, default_h=h))

    # Group by role
    groups: dict[str, list[_Classified]] = {}
    for c in classified:
        groups.setdefault(c.role, []).append(c)

    # Pack groups in role order
    positions: dict[int, dict] = {}
    y = 0

    for role in _ROLE_ORDER:
        items = groups.get(role, [])
        if not items:
            continue

        if role == "kpi":
            y = _pack_kpis(items, positions, y)
        elif role == "chart":
            y = _pack_charts(items, positions, y)
        elif role == "detail":
            y = _pack_details(items, positions, y)
        else:
            # kpi_group, status, table, container — all full-width
            for item in items:
                positions[item.index] = {"x": 0, "y": y, "w": item.default_w, "h": item.default_h}
                y += item.default_h

    return positions


def _pack_kpis(items: list[_Classified], positions: dict[int, dict], y: int) -> int:
    """Pack individual metric_cards side-by-side, up to 4 per row."""
    x = 0
    row_h = 0
    for item in items:
        if x >= GRID_COLS:
            y += row_h
            x = 0
            row_h = 0
        positions[item.index] = {"x": x, "y": y, "w": 1, "h": item.default_h}
        row_h = max(row_h, item.default_h)
        x += 1
    return y + row_h


def _pack_charts(items: list[_Classified], positions: dict[int, dict], y: int) -> int:
    """Pack charts: first 2 side-by-side (w=2), rest full-width. node_map always full-width."""
    half_width = [it for it in items if it.default_w <= 2]
    full_width = [it for it in items if it.default_w > 2]

    # Pair first two half-width charts
    if len(half_width) >= 2:
        positions[half_width[0].index] = {"x": 0, "y": y, "w": 2, "h": half_width[0].default_h}
        positions[half_width[1].index] = {"x": 2, "y": y, "w": 2, "h": half_width[1].default_h}
        row_h = max(half_width[0].default_h, half_width[1].default_h)
        y += row_h
        remaining_half = half_width[2:]
    elif len(half_width) == 1:
        positions[half_width[0].index] = {"x": 0, "y": y, "w": 4, "h": half_width[0].default_h}
        y += half_width[0].default_h
        remaining_half = []
    else:
        remaining_half = []

    # Remaining half-width charts go full-width
    for item in remaining_half:
        positions[item.index] = {"x": 0, "y": y, "w": 4, "h": item.default_h}
        y += item.default_h

    # Full-width charts (node_map etc.)
    for item in full_width:
        positions[item.index] = {"x": 0, "y": y, "w": 4, "h": item.default_h}
        y += item.default_h

    return y


def _pack_details(items: list[_Classified], positions: dict[int, dict], y: int) -> int:
    """Pack detail components, pairing complementary kinds side-by-side."""
    used = set()

    # Try to pair complementary kinds
    pairs = []
    for i, a in enumerate(items):
        if i in used:
            continue
        kind_a = a.comp.get("kind", "")
        partners = _DETAIL_PAIRS.get(kind_a, set())
        paired = False
        for j, b in enumerate(items):
            if j in used or j == i:
                continue
            kind_b = b.comp.get("kind", "")
            if kind_b in partners:
                pairs.append((a, b))
                used.add(i)
                used.add(j)
                paired = True
                break
        if not paired:
            used.add(i)
            pairs.append((a, None))

    # Layout pairs
    for a, b in pairs:
        if b is not None:
            h = max(a.default_h, b.default_h)
            positions[a.index] = {"x": 0, "y": y, "w": 2, "h": h}
            positions[b.index] = {"x": 2, "y": y, "w": 2, "h": h}
            y += h
        else:
            positions[a.index] = {"x": 0, "y": y, "w": 4, "h": a.default_h}
            y += a.default_h

    return y
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_layout_engine.py -v`
Expected: ALL PASS

- [ ] **Step 5: Lint and format**

Run: `python3 -m ruff check sre_agent/layout_engine.py tests/test_layout_engine.py && python3 -m ruff format sre_agent/layout_engine.py tests/test_layout_engine.py`

- [ ] **Step 6: Commit**

```bash
git add sre_agent/layout_engine.py tests/test_layout_engine.py
git commit -m "feat: add semantic layout engine with role-based auto-layout"
```

---

### Task 2: Wire Layout Engine into API

**Files:**
- Modify: `sre_agent/api.py`

- [ ] **Step 1: Replace template-based layout with compute_layout**

In `sre_agent/api.py`, find lines 550-555:

```python
            # Compute positions from template if specified
            positions = None
            if view_template:
                from .layout_templates import apply_template as _apply_tpl

                positions = _apply_tpl(view_template, session_components)
```

Replace with:

```python
            # Compute positions using semantic layout engine
            from .layout_engine import compute_layout

            positions = compute_layout(session_components)
```

Also find the merge path (around line 572 after `merged_layout = _vr_merged.components`) and ensure positions are recomputed for merged layouts too:

Find:
```python
                merged_layout = _vr_merged.components
                update_kwargs: dict = {"layout": merged_layout, "description": view_desc}
                if positions:
                    update_kwargs["positions"] = positions
```

Replace with:
```python
                merged_layout = _vr_merged.components
                positions = compute_layout(merged_layout)
                update_kwargs: dict = {"layout": merged_layout, "description": view_desc}
                if positions:
                    update_kwargs["positions"] = positions
```

- [ ] **Step 2: Run existing tests**

Run: `python3 -m pytest tests/test_api_tools.py tests/test_views.py -v --tb=short`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add sre_agent/api.py
git commit -m "feat: replace template layout with semantic auto-layout in API"
```

---

### Task 3: Update View Designer Prompt

**Files:**
- Modify: `sre_agent/view_designer.py`

- [ ] **Step 1: Remove template references from design patterns**

In `sre_agent/view_designer.py`, find the 5 Design Patterns sections (lines ~118-157). Each has `(template: xyz)` in the header and `create_dashboard(title, template="xyz")` at the end. Update each:

Replace `### Executive Summary (template: sre_dashboard)` with `### Executive Summary`
Replace `5. \`create_dashboard(title, template="sre_dashboard")\`` with `5. \`create_dashboard(title)\``

Do the same for all 5 patterns:
- `### Namespace Deep-Dive (template: namespace_overview)` → `### Namespace Deep-Dive`
- `### Incident Triage (template: incident_report)` → `### Incident Triage`
- `### Capacity Planning (template: monitoring_panel)` → `### Capacity Planning`
- `### Resource Detail (template: resource_detail)` → `### Resource Detail`

And remove `template=` from each `create_dashboard` call in the patterns.

- [ ] **Step 2: Remove template from BUILD step**

Find line ~208: `Execute the plan by calling data tools, then \`create_dashboard(template=...)\`.`

Replace with: `Execute the plan by calling data tools, then \`create_dashboard(title)\`. Layout is computed automatically.`

- [ ] **Step 3: Remove Rule 4 and update Rule numbering**

Find line ~246: `4. ALWAYS use a layout template — never create views without one`

Replace with: `4. Layout is automatic — you do not need to specify a template`

- [ ] **Step 4: Update worked example**

Find the worked example (line ~288). Replace:
`1. \`plan_dashboard(title="Production Overview", template="namespace_overview", rows="...")\``
with:
`1. \`plan_dashboard(title="Production Overview", rows="...")\``

Replace:
`7. \`create_dashboard(title="Production Overview", template="namespace_overview")\``
with:
`7. \`create_dashboard(title="Production Overview")\``

- [ ] **Step 5: Remove "NO TEMPLATE" fix from common fixes**

Find line ~314: `- "NO TEMPLATE" → this means you forgot the \`template\` parameter in \`create_dashboard\``

Remove this line entirely.

- [ ] **Step 6: Run tests**

Run: `python3 -m pytest tests/test_harness.py -v --tb=short`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add sre_agent/view_designer.py
git commit -m "feat: remove template requirements from view designer prompt"
```

---

### Task 4: Update View Tools and Planner

**Files:**
- Modify: `sre_agent/view_tools.py`
- Modify: `sre_agent/view_planner.py`

- [ ] **Step 1: Make template param no-op in create_dashboard**

In `sre_agent/view_tools.py`, find the `create_dashboard` function (line ~44). Update the docstring to note layout is automatic:

Replace the existing docstring paragraph about templates:
```python
    """Create a custom dashboard view. IMPORTANT: Call plan_dashboard() FIRST to show the user a plan before building. Only call create_dashboard after the user approves the plan.

    If a layout template is specified, widgets are automatically arranged in a
    professional grid layout instead of stacking vertically.

    Args:
        title: Name for the dashboard (e.g. "SRE Overview", "Node Health").
        description: Brief description of what the dashboard shows.
        template: Optional layout template ID. Available templates:
                  'sre_dashboard' — 4 metric cards + 2 charts side-by-side + table
                  'namespace_overview' — summary cards + 2 charts + table + events
                  'incident_report' — status timeline + logs/details side-by-side + table
                  'monitoring_panel' — 4 metric cards + 2x2 chart grid + alerts
                  'resource_detail' — key-value + resource tree + yaml + table
    """
```

Replace with:
```python
    """Create a custom dashboard view. IMPORTANT: Call plan_dashboard() FIRST to show the user a plan before building. Only call create_dashboard after the user approves the plan.

    Layout is computed automatically based on component types — no template needed.

    Args:
        title: Name for the dashboard (e.g. "SRE Overview", "Node Health").
        description: Brief description of what the dashboard shows.
        template: Deprecated — layout is now automatic. Ignored if provided.
    """
```

- [ ] **Step 2: Make template param optional in plan_dashboard**

In `sre_agent/view_planner.py`, update the function signature and docstring:

Replace:
```python
@beta_tool
def plan_dashboard(
    title: str,
    template: str,
    rows: str,
) -> str:
    """Present a dashboard plan to the user for approval BEFORE building it.
    Call this INSTEAD of create_dashboard. After user approves, then call the
    data tools and create_dashboard.

    Args:
        title: Proposed dashboard title.
        template: Layout template ID (sre_dashboard, namespace_overview, incident_report, monitoring_panel, resource_detail).
        rows: A structured description of each row, formatted as:
```

Replace with:
```python
@beta_tool
def plan_dashboard(
    title: str,
    rows: str,
    template: str = "",
) -> str:
    """Present a dashboard plan to the user for approval BEFORE building it.
    Call this INSTEAD of create_dashboard. After user approves, then call the
    data tools and create_dashboard.

    Args:
        title: Proposed dashboard title.
        rows: A structured description of each row, formatted as:
```

Also update the plan output. Find:
```python
        f"**Template:** `{template}`",
```

Replace with:
```python
        f"**Layout:** automatic (computed from component types)",
```

- [ ] **Step 3: Run tests**

Run: `python3 -m pytest tests/test_views.py tests/test_harness.py -v --tb=short`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add sre_agent/view_tools.py sre_agent/view_planner.py
git commit -m "feat: make template param deprecated/optional in view tools"
```

---

### Task 5: Update Template Tests and Deprecate Old Module

**Files:**
- Modify: `sre_agent/layout_templates.py`
- Modify: `tests/test_harness.py`

- [ ] **Step 1: Add deprecation comment to layout_templates.py**

Add at the top of `sre_agent/layout_templates.py`, after the existing module docstring:

```python
# DEPRECATED: This module is superseded by layout_engine.py.
# Kept for backward compatibility with existing saved views.
# New views use compute_layout() from layout_engine.py.
```

- [ ] **Step 2: Update template tests in test_harness.py**

In `tests/test_harness.py`, find the `TestPickChartType` class (line ~358). It has 3 template tests. Update them to also test the new layout engine:

Find `test_layout_template_apply` (line ~363) and add a new test after the class:

```python
class TestLayoutEngine:
    def test_compute_layout_sre_dashboard(self):
        from sre_agent.layout_engine import compute_layout

        components = [
            {"kind": "grid", "title": "KPIs", "items": [{"kind": "metric_card", "title": "CPU"}]},
            {"kind": "chart", "title": "CPU Trend"},
            {"kind": "chart", "title": "Memory Trend"},
            {"kind": "data_table", "title": "Pods"},
        ]
        pos = compute_layout(components)
        assert len(pos) == 4
        # KPIs at top
        assert pos[0]["y"] == 0
        assert pos[0]["w"] == 4
        # Charts side-by-side below
        assert pos[1]["w"] == 2
        assert pos[2]["w"] == 2
        assert pos[1]["y"] == pos[2]["y"]
        # Table at bottom
        assert pos[3]["y"] > pos[1]["y"]

    def test_compute_layout_empty(self):
        from sre_agent.layout_engine import compute_layout

        assert compute_layout([]) == {}

    def test_old_templates_still_work(self):
        """Verify backward compatibility — old apply_template still functions."""
        from sre_agent.layout_templates import apply_template

        components = [
            {"kind": "metric_card", "title": "CPU"},
            {"kind": "chart", "title": "CPU Trend"},
            {"kind": "data_table", "title": "Pods"},
        ]
        result = apply_template("sre_dashboard", components)
        assert result is not None
```

- [ ] **Step 3: Run all tests**

Run: `python3 -m pytest tests/ -v --tb=short`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add sre_agent/layout_templates.py tests/test_harness.py
git commit -m "feat: deprecate layout_templates, add layout engine tests to harness"
```

---

### Task 6: Update Documentation

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update CLAUDE.md**

Add to Key Files section after `promql_recipes.py`:

```
- `layout_engine.py` — semantic auto-layout engine (role-based row packing, replaces fixed templates)
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add layout_engine to CLAUDE.md key files"
```

---

## Verification

After all tasks:

```bash
# Run full test suite
python3 -m pytest tests/ -v

# Run just the new + updated tests
python3 -m pytest tests/test_layout_engine.py tests/test_harness.py -v

# Lint
make verify
```

**Manual verification:**
1. Start: `pulse-agent-api`
2. Create a dashboard: "Create a dashboard for the production namespace"
3. Verify: no template ID in the tool calls
4. Verify: components have proper positions (KPIs top, charts middle, table bottom)
5. Verify: two charts appear side-by-side, not stacked
