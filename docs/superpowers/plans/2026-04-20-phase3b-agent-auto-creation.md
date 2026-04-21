# Phase 3B: Agent Auto-Creation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable the agent to create typed views (incident, plan, assessment) through `create_dashboard` with lifecycle metadata, and prevent duplicate views when the monitor finds the same issue again.

**Architecture:** Extend `create_dashboard` tool to accept optional `view_type`/`trigger_source`/`finding_id`/`visibility` params. Pass these through the signal → `agent_ws.py` → `save_view`. Add finding dedup check. Include lifecycle fields in the `view_spec` WebSocket event so the frontend knows the view type.

**Tech Stack:** Python 3.11 (FastAPI, WebSocket), TypeScript

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `sre_agent/view_tools.py` | Modify | Add lifecycle params to `create_dashboard` |
| `sre_agent/api/agent_ws.py` | Modify | Pass lifecycle fields through signal → save_view, dedup by finding_id |
| `tests/test_view_tools.py` | Create | Tests for create_dashboard signal output with lifecycle params |
| `tests/test_agent_ws_lifecycle.py` | Create | Tests for signal processing with lifecycle fields |

---

### Task 1: Extend create_dashboard with lifecycle params

**Files:**
- Modify: `sre_agent/view_tools.py:44`
- Create: `tests/test_view_tools_lifecycle.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for create_dashboard lifecycle params."""

from __future__ import annotations

import json

from sre_agent.view_tools import SIGNAL_PREFIX


class TestCreateDashboardSignal:
    def test_default_signal_has_custom_type(self):
        from sre_agent.view_tools import create_dashboard

        result = create_dashboard.func("Test Dashboard", "desc")
        signal_json = result.split(SIGNAL_PREFIX, 1)[1].strip()
        sig = json.loads(signal_json)
        assert sig["type"] == "view_spec"
        assert sig["view_type"] == "custom"
        assert sig["visibility"] == "private"

    def test_incident_signal(self):
        from sre_agent.view_tools import create_dashboard

        result = create_dashboard.func(
            "CrashLoop Investigation",
            "OOM in payment-api",
            view_type="incident",
            trigger_source="monitor",
            finding_id="f-crash-1",
            visibility="team",
        )
        signal_json = result.split(SIGNAL_PREFIX, 1)[1].strip()
        sig = json.loads(signal_json)
        assert sig["view_type"] == "incident"
        assert sig["trigger_source"] == "monitor"
        assert sig["finding_id"] == "f-crash-1"
        assert sig["visibility"] == "team"
        assert sig["status"] == "investigating"

    def test_plan_signal(self):
        from sre_agent.view_tools import create_dashboard

        result = create_dashboard.func(
            "VM Support Plan",
            "Enable VMs for team B",
            view_type="plan",
            trigger_source="agent",
        )
        signal_json = result.split(SIGNAL_PREFIX, 1)[1].strip()
        sig = json.loads(signal_json)
        assert sig["view_type"] == "plan"
        assert sig["status"] == "analyzing"
        assert sig["visibility"] == "team"

    def test_assessment_signal(self):
        from sre_agent.view_tools import create_dashboard

        result = create_dashboard.func(
            "Memory Pressure Forecast",
            "worker-5 trending to pressure",
            view_type="assessment",
            trigger_source="monitor",
        )
        signal_json = result.split(SIGNAL_PREFIX, 1)[1].strip()
        sig = json.loads(signal_json)
        assert sig["view_type"] == "assessment"
        assert sig["status"] == "analyzing"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_view_tools_lifecycle.py -v`
Expected: FAIL — create_dashboard doesn't accept view_type

- [ ] **Step 3: Update create_dashboard**

```python
_INITIAL_STATUS = {
    "custom": "active",
    "incident": "investigating",
    "plan": "analyzing",
    "assessment": "analyzing",
}


@beta_tool
def create_dashboard(
    title: str,
    description: str = "",
    view_type: str = "custom",
    trigger_source: str = "user",
    finding_id: str = "",
    visibility: str = "",
):
    """Create a custom dashboard view. Quality is auto-validated on save — no need to call critique_view.

    Layout is computed automatically based on component types.

    Args:
        title: Name for the dashboard (e.g. "SRE Overview", "Incident — payment-api").
        description: Brief description of what the dashboard shows.
        view_type: Type of view: custom, incident, plan, or assessment.
        trigger_source: Who created it: user, monitor, or agent.
        finding_id: Monitor finding ID that triggered this view (for dedup).
        visibility: private (default for custom) or team (default for incident/plan/assessment).
    """
    view_id = f"cv-{uuid.uuid4().hex[:12]}"
    status = _INITIAL_STATUS.get(view_type, "active")
    if not visibility:
        visibility = "team" if view_type != "custom" else "private"

    return _signal(
        "view_spec",
        f"Created view '{title}' with ID {view_id}. "
        f"The dashboard is now saved and visible to the user. "
        f"Tell the user: 'Here is your dashboard. Would you like any changes?'",
        view_id=view_id,
        title=title,
        description=description,
        view_type=view_type,
        status=status,
        trigger_source=trigger_source,
        finding_id=finding_id or None,
        visibility=visibility,
    )
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_view_tools_lifecycle.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/view_tools.py tests/test_view_tools_lifecycle.py
git commit -m "feat: create_dashboard accepts view_type, trigger_source, finding_id, visibility"
```

---

### Task 2: Pass lifecycle fields through signal processing

**Files:**
- Modify: `sre_agent/api/agent_ws.py:399-464`

- [ ] **Step 1: Update signal processing to extract and pass lifecycle fields**

In `agent_ws.py`, where `view_spec` signals are processed, extract the new fields and pass them to `save_view`:

In the "new view" branch (where `_db.save_view` is called), add the lifecycle kwargs:

```python
# Extract lifecycle fields from signal
view_type = sig.get("view_type", "custom")
view_status = sig.get("status", "active")
trigger_source = sig.get("trigger_source", "user")
finding_id = sig.get("finding_id")
view_visibility = sig.get("visibility", "private")

# Dedup: if finding_id exists and a view already links to this finding, update it
if finding_id:
    existing_for_finding = _db.get_view_by_finding(finding_id)
    if existing_for_finding:
        # Reuse existing view — transition back to investigating if resolved
        existing = existing_for_finding
        # Fall through to the merge branch below
```

In the `save_view` call:
```python
_db.save_view(
    current_user, view_id, view_title, view_desc, session_components,
    positions=positions,
    view_type=view_type,
    status=view_status,
    trigger_source=trigger_source,
    finding_id=finding_id,
    visibility=view_visibility,
)
```

In the `view_spec` event sent to frontend, include the lifecycle fields:
```python
spec = {
    "id": view_id,
    "title": view_title,
    "description": view_desc,
    "layout": session_components,
    "positions": positions or {},
    "generatedAt": int(_time.time() * 1000),
    "view_type": view_type,
    "status": view_status,
    "trigger_source": trigger_source,
    "finding_id": finding_id,
    "visibility": view_visibility,
}
```

- [ ] **Step 2: Run existing tests**

Run: `python3 -m pytest tests/ -q --timeout=120`
Expected: All PASS

- [ ] **Step 3: Commit**

```bash
git add sre_agent/api/agent_ws.py
git commit -m "feat: pass lifecycle fields through view_spec signal → save_view"
```

---

### Task 3: Verification + docs

- [ ] **Step 1: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: All PASS

- [ ] **Step 2: Run mypy**

Run: `python3 -m mypy sre_agent/ --ignore-missing-imports`
Expected: 0 errors

- [ ] **Step 3: Update CLAUDE.md**

Note that create_dashboard now supports view_type/trigger_source/finding_id/visibility.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for Phase 3B — agent auto-creation"
```
