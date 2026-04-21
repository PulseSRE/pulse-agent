# Phase 2: New View Components — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 4 new component types (action_button, confidence_badge, resolution_tracker, blast_radius) to the component registry, backend validation, action execution endpoint, frontend rendering, and tests.

**Architecture:** Register each component in `component_registry.py`, add validation rules in `quality_engine.py`, add an action execution REST endpoint in `api/views.py` with sanitization in `api/sanitize.py`, add TypeScript types in `agentComponents.ts`, and render each in `AgentComponentRenderer.tsx`. Action buttons execute tools through the standard `_execute_tool` infrastructure with ownership, trust-level, and circuit-breaker checks.

**Tech Stack:** Python 3.11 (FastAPI, Pydantic), React/TypeScript (Zustand, Lucide icons, Tailwind CSS)

---

## File Structure

### Backend (pulse-agent)

| File | Action | Responsibility |
|------|--------|----------------|
| `sre_agent/component_registry.py` | Modify | Register 4 new `ComponentKind` entries |
| `sre_agent/quality_engine.py` | Modify | Add validation rules for 4 new kinds, update title-required exclusion list |
| `sre_agent/api/views.py` | Modify | Add `POST /views/{view_id}/actions` endpoint |
| `sre_agent/api/sanitize.py` | Modify | Add `_sanitize_action_button` validator for action_input params |
| `tests/test_component_registry.py` | Modify | Tests for 4 new component registrations |
| `tests/test_quality_engine_new_kinds.py` | Create | Validation tests for new component schemas |
| `tests/test_view_action_endpoint.py` | Create | Tests for action execution endpoint |

### Frontend (OpenshiftPulse)

| File | Action | Responsibility |
|------|--------|----------------|
| `src/kubeview/engine/agentComponents.ts` | Modify | Add 4 TypeScript spec interfaces |
| `src/kubeview/components/agent/AgentComponentRenderer.tsx` | Modify | Add 4 renderer cases + imports |
| `src/kubeview/components/agent/AgentActionButton.tsx` | Create | Action button renderer with confirmation dialog |
| `src/kubeview/components/agent/AgentConfidenceBadge.tsx` | Create | Confidence badge renderer |
| `src/kubeview/components/agent/AgentResolutionTracker.tsx` | Create | Resolution tracker renderer |
| `src/kubeview/components/agent/AgentBlastRadius.tsx` | Create | Blast radius renderer |
| `src/kubeview/store/customViewStore.ts` | Modify | Add `executeAction()` method |

---

### Task 1: Register `confidence_badge` component (backend)

The simplest component — no action execution, no complex validation. Good warm-up.

**Files:**
- Modify: `sre_agent/component_registry.py:458` (after topology registration)
- Modify: `sre_agent/quality_engine.py:347` (title_required exclusion list)
- Modify: `tests/test_component_registry.py:39` (expected kinds set)

- [ ] **Step 1: Write the failing test**

Add `"confidence_badge"` to the expected kinds and add a specific test:

```python
# tests/test_component_registry.py — inside TestRegistry class

# Update test_has_all_existing_kinds expected set to include:
"confidence_badge",

# Add new test method:
def test_confidence_badge_schema(self):
    c = get_component("confidence_badge")
    assert c is not None
    assert c.category == "status"
    assert "score" in c.required_fields
    assert c.title_required is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_component_registry.py::TestRegistry::test_confidence_badge_schema -v`
Expected: FAIL — `AssertionError: assert None is not None`

- [ ] **Step 3: Register the component**

Add to `sre_agent/component_registry.py` after the topology registration (line ~458):

```python
register_component(
    ComponentKind(
        name="confidence_badge",
        description="Inline confidence score badge with color coding",
        category="status",
        required_fields=["score"],
        optional_fields=["label"],
        title_required=False,
        example={
            "kind": "confidence_badge",
            "score": 0.85,
            "label": "Root cause confidence",
        },
        prompt_hint="confidence_badge — Inline confidence score (0.0-1.0). Green >0.8, amber 0.5-0.8, red <0.5. Embed in card headers.",
    )
)
```

- [ ] **Step 4: Add validation rule**

In `sre_agent/quality_engine.py`, update the `title_required` exclusion in `_validate_component()` (line ~347):

```python
title_required = kind not in ("grid", "tabs", "section", "bar_list", "progress_list", "timeline", "confidence_badge")
```

Add validation after the `resource_counts` elif block (~line 436):

```python
elif kind == "confidence_badge":
    score = comp.get("score")
    if score is None:
        result.errors.append("confidence_badge must have 'score'.")
    elif not isinstance(score, (int, float)) or score < 0 or score > 1:
        result.errors.append("confidence_badge 'score' must be a number between 0.0 and 1.0.")
```

- [ ] **Step 5: Update the expected kinds count**

In `tests/test_component_registry.py`, update `test_get_valid_kinds_returns_frozenset`:

```python
assert len(kinds) >= 20  # was 19
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_component_registry.py -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add sre_agent/component_registry.py sre_agent/quality_engine.py tests/test_component_registry.py
git commit -m "feat: register confidence_badge component"
```

---

### Task 2: Register `resolution_tracker` component (backend)

**Files:**
- Modify: `sre_agent/component_registry.py` (after confidence_badge)
- Modify: `sre_agent/quality_engine.py` (validation + title exclusion)
- Modify: `tests/test_component_registry.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_component_registry.py — add to expected set:
"resolution_tracker",

# Add new test:
def test_resolution_tracker_schema(self):
    c = get_component("resolution_tracker")
    assert c is not None
    assert c.category == "status"
    assert "steps" in c.required_fields
    assert c.title_required is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_component_registry.py::TestRegistry::test_resolution_tracker_schema -v`
Expected: FAIL

- [ ] **Step 3: Register the component**

```python
register_component(
    ComponentKind(
        name="resolution_tracker",
        description="Vertical checklist tracking resolution steps with status icons",
        category="status",
        required_fields=["steps"],
        optional_fields=["title"],
        title_required=False,
        example={
            "kind": "resolution_tracker",
            "title": "Recovery Steps",
            "steps": [
                {"title": "Identify root cause", "status": "done", "detail": "OOM kill detected", "timestamp": "2024-01-15T10:30:00Z"},
                {"title": "Scale up replicas", "status": "running", "detail": "2 → 4 replicas"},
                {"title": "Verify health", "status": "pending", "detail": "Waiting for rollout"},
            ],
        },
        prompt_hint="resolution_tracker — Vertical step checklist. Each step: title, status (done|running|pending), detail, optional output (monospace), optional timestamp.",
    )
)
```

- [ ] **Step 4: Add validation rule**

Update the title exclusion list to include `"resolution_tracker"`.

Add validation:

```python
elif kind == "resolution_tracker":
    steps = comp.get("steps")
    if not steps:
        result.errors.append("resolution_tracker must have at least 1 step.")
    elif isinstance(steps, list):
        valid_statuses = {"done", "running", "pending"}
        for step in steps:
            if not step.get("title"):
                result.errors.append("resolution_tracker step missing 'title'.")
            status = step.get("status")
            if status not in valid_statuses:
                result.errors.append(f"resolution_tracker step status must be one of {valid_statuses}, got '{status}'.")
```

- [ ] **Step 5: Update expected kinds count**

```python
assert len(kinds) >= 21
```

- [ ] **Step 6: Run tests**

Run: `python3 -m pytest tests/test_component_registry.py -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add sre_agent/component_registry.py sre_agent/quality_engine.py tests/test_component_registry.py
git commit -m "feat: register resolution_tracker component"
```

---

### Task 3: Register `blast_radius` component (backend)

**Files:**
- Modify: `sre_agent/component_registry.py`
- Modify: `sre_agent/quality_engine.py`
- Modify: `tests/test_component_registry.py`

- [ ] **Step 1: Write the failing test**

```python
# Add to expected set:
"blast_radius",

# Add new test:
def test_blast_radius_schema(self):
    c = get_component("blast_radius")
    assert c is not None
    assert c.category == "status"
    assert "items" in c.required_fields
    assert c.title_required is True  # blast_radius should have a title
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_component_registry.py::TestRegistry::test_blast_radius_schema -v`
Expected: FAIL

- [ ] **Step 3: Register the component**

```python
register_component(
    ComponentKind(
        name="blast_radius",
        description="Downstream dependency impact list with status indicators",
        category="status",
        required_fields=["items"],
        optional_fields=["title", "perspective"],
        example={
            "kind": "blast_radius",
            "title": "Blast Radius — payment-api",
            "items": [
                {
                    "kind_abbrev": "Svc",
                    "name": "payment-api",
                    "relationship": "Service → payment-api (selector match)",
                    "status": "degraded",
                    "status_detail": "0 endpoints",
                },
                {
                    "kind_abbrev": "Ing",
                    "name": "payment-ingress",
                    "relationship": "Ingress → payment-api (backend)",
                    "status": "healthy",
                    "status_detail": "3 active backends",
                },
            ],
        },
        prompt_hint=(
            "blast_radius — Downstream dependency impact list. Each item: kind_abbrev, name, relationship, "
            "status (degraded|healthy|retrying|paused), status_detail. "
            "Optional perspective: physical|logical|network|multi_tenant|helm to filter dependencies."
        ),
    )
)
```

- [ ] **Step 4: Add validation rule**

Add validation:

```python
elif kind == "blast_radius":
    items = comp.get("items")
    if not items:
        result.errors.append("blast_radius must have at least 1 item.")
    elif isinstance(items, list):
        valid_statuses = {"degraded", "healthy", "retrying", "paused"}
        for item in items:
            if not item.get("kind_abbrev"):
                result.errors.append("blast_radius item missing 'kind_abbrev'.")
            if not item.get("name"):
                result.errors.append("blast_radius item missing 'name'.")
            status = item.get("status")
            if status and status not in valid_statuses:
                result.errors.append(f"blast_radius item status must be one of {valid_statuses}, got '{status}'.")
```

- [ ] **Step 5: Update expected kinds count**

```python
assert len(kinds) >= 22
```

- [ ] **Step 6: Run tests**

Run: `python3 -m pytest tests/test_component_registry.py -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add sre_agent/component_registry.py sre_agent/quality_engine.py tests/test_component_registry.py
git commit -m "feat: register blast_radius component"
```

---

### Task 4: Register `action_button` component (backend)

The most complex component. Requires sanitization of `action_input` to prevent prompt injection.

**Files:**
- Modify: `sre_agent/component_registry.py`
- Modify: `sre_agent/quality_engine.py`
- Modify: `sre_agent/api/sanitize.py`
- Modify: `tests/test_component_registry.py`

- [ ] **Step 1: Write the failing test**

```python
# Add to expected set:
"action_button",

# Add new test:
def test_action_button_schema(self):
    c = get_component("action_button")
    assert c is not None
    assert c.category == "action"
    assert "label" in c.required_fields
    assert "action" in c.required_fields
    assert "action_input" in c.required_fields
    assert c.title_required is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_component_registry.py::TestRegistry::test_action_button_schema -v`
Expected: FAIL

- [ ] **Step 3: Register the component**

```python
register_component(
    ComponentKind(
        name="action_button",
        description="Executable action button that triggers a tool with confirmation for write operations",
        category="action",
        required_fields=["label", "action", "action_input"],
        optional_fields=["style", "confirm_text"],
        title_required=False,
        example={
            "kind": "action_button",
            "label": "Restart Deployment",
            "action": "restart_deployment",
            "action_input": {"name": "payment-api", "namespace": "production"},
            "style": "danger",
            "confirm_text": "This will restart all pods in the deployment.",
        },
        prompt_hint=(
            "action_button — Executable button. action must be a registered tool name. "
            "style: primary|danger|ghost. Write tools show confirmation dialog. "
            "confirm_text: optional tooltip for dangerous actions."
        ),
    )
)
```

- [ ] **Step 4: Add validation rule**

In `quality_engine.py`, add validation. Note: `action_button` does NOT need a title (it has a `label`), so add it to the exclusion list.

Update title exclusion:
```python
title_required = kind not in ("grid", "tabs", "section", "bar_list", "progress_list", "timeline", "confidence_badge", "resolution_tracker", "action_button")
```

Add validation:

```python
elif kind == "action_button":
    if not comp.get("label"):
        result.errors.append("action_button must have 'label'.")
    if not comp.get("action"):
        result.errors.append("action_button must have 'action'.")
    if not isinstance(comp.get("action_input"), dict):
        result.errors.append("action_button must have 'action_input' (dict).")
    style = comp.get("style", "primary")
    if style not in ("primary", "danger", "ghost"):
        result.errors.append(f"action_button style must be primary|danger|ghost, got '{style}'.")
```

- [ ] **Step 5: Add action_button sanitization**

In `sre_agent/api/sanitize.py`, add sanitization for action_button components. This prevents prompt injection attacks via crafted `action_input`:

```python
from ..tool_registry import TOOL_REGISTRY, WRITE_TOOL_NAMES

# Allowed tools for action buttons — excludes high-risk cluster ops
_ACTION_BLOCKED_TOOLS = frozenset({"drain_node", "exec_command"})


def _sanitize_action_button(comp: dict) -> dict | None:
    """Validate an action_button component at save time.

    Returns the component if valid, or None if it should be rejected.
    Sets ``_is_write`` flag so the frontend knows to show confirmation.
    """
    action = comp.get("action", "")
    if not action or action not in TOOL_REGISTRY:
        return None
    if action in _ACTION_BLOCKED_TOOLS:
        return None

    action_input = comp.get("action_input")
    if not isinstance(action_input, dict):
        return None

    # Validate K8s names/namespaces in action_input
    from ..k8s_tools.validators import _validate_k8s_name, _validate_k8s_namespace

    ns = action_input.get("namespace")
    if ns and _validate_k8s_namespace(ns):
        return None
    name = action_input.get("name")
    if name and _validate_k8s_name(name):
        return None

    # Enforce replica limits
    replicas = action_input.get("replicas")
    if replicas is not None:
        try:
            r = int(replicas)
            if r < 0 or r > 100:
                return None
        except (ValueError, TypeError):
            return None

    # Flag write tools so frontend knows to show confirmation
    comp["_is_write"] = action in WRITE_TOOL_NAMES
    return comp
```

Update `_sanitize_components` to call this:

```python
def _sanitize_components(components: list[dict]) -> list[dict]:
    out: list[dict] = []
    for comp in components:
        if comp.get("kind") == "action_button":
            sanitized = _sanitize_action_button(comp)
            if sanitized is None:
                continue  # strip invalid action buttons
            out.append(sanitized)
            continue

        if comp.get("kind") == "metric_card" and comp.get("query"):
            comp["query"] = _fix_promql(comp["query"])
        for container_key in ("items", "components"):
            nested = comp.get(container_key)
            if isinstance(nested, list):
                comp[container_key] = _sanitize_components(nested)
        tabs = comp.get("tabs")
        if isinstance(tabs, list):
            for tab in tabs:
                tab_comps = tab.get("components")
                if isinstance(tab_comps, list):
                    tab["components"] = _sanitize_components(tab_comps)
        out.append(comp)
    return out
```

- [ ] **Step 6: Update expected kinds count**

```python
assert len(kinds) >= 23
```

- [ ] **Step 7: Run tests**

Run: `python3 -m pytest tests/test_component_registry.py -v`
Expected: All PASS

- [ ] **Step 8: Commit**

```bash
git add sre_agent/component_registry.py sre_agent/quality_engine.py sre_agent/api/sanitize.py tests/test_component_registry.py
git commit -m "feat: register action_button component with input sanitization"
```

---

### Task 5: Validation tests for new component schemas

**Files:**
- Create: `tests/test_quality_engine_new_kinds.py`

- [ ] **Step 1: Write tests for all 4 new kinds**

```python
"""Tests for Phase 2 component validation rules."""

from __future__ import annotations

from sre_agent.quality_engine import _validate_component, QualityResult


class TestConfidenceBadgeValidation:
    def _validate(self, comp: dict) -> QualityResult:
        result = QualityResult()
        _validate_component(comp, result)
        return result

    def test_valid(self):
        r = self._validate({"kind": "confidence_badge", "score": 0.85})
        assert not r.errors

    def test_missing_score(self):
        r = self._validate({"kind": "confidence_badge"})
        assert any("score" in e for e in r.errors)

    def test_score_out_of_range_high(self):
        r = self._validate({"kind": "confidence_badge", "score": 1.5})
        assert any("0.0 and 1.0" in e for e in r.errors)

    def test_score_out_of_range_negative(self):
        r = self._validate({"kind": "confidence_badge", "score": -0.1})
        assert any("0.0 and 1.0" in e for e in r.errors)

    def test_score_zero_valid(self):
        r = self._validate({"kind": "confidence_badge", "score": 0.0})
        assert not r.errors

    def test_score_one_valid(self):
        r = self._validate({"kind": "confidence_badge", "score": 1.0})
        assert not r.errors

    def test_no_title_required(self):
        r = self._validate({"kind": "confidence_badge", "score": 0.5})
        assert not any("title" in e for e in r.errors)


class TestResolutionTrackerValidation:
    def _validate(self, comp: dict) -> QualityResult:
        result = QualityResult()
        _validate_component(comp, result)
        return result

    def test_valid(self):
        r = self._validate({
            "kind": "resolution_tracker",
            "steps": [{"title": "Step 1", "status": "done", "detail": "Complete"}],
        })
        assert not r.errors

    def test_empty_steps(self):
        r = self._validate({"kind": "resolution_tracker", "steps": []})
        assert any("at least 1 step" in e for e in r.errors)

    def test_missing_steps(self):
        r = self._validate({"kind": "resolution_tracker"})
        assert any("at least 1 step" in e for e in r.errors)

    def test_step_missing_title(self):
        r = self._validate({
            "kind": "resolution_tracker",
            "steps": [{"status": "done", "detail": "no title"}],
        })
        assert any("step missing 'title'" in e for e in r.errors)

    def test_step_invalid_status(self):
        r = self._validate({
            "kind": "resolution_tracker",
            "steps": [{"title": "Step 1", "status": "invalid_status"}],
        })
        assert any("status must be one of" in e for e in r.errors)

    def test_all_valid_statuses(self):
        for status in ("done", "running", "pending"):
            r = self._validate({
                "kind": "resolution_tracker",
                "steps": [{"title": "Step", "status": status, "detail": "x"}],
            })
            assert not r.errors, f"Status '{status}' should be valid"


class TestBlastRadiusValidation:
    def _validate(self, comp: dict) -> QualityResult:
        result = QualityResult()
        _validate_component(comp, result)
        return result

    def test_valid(self):
        r = self._validate({
            "kind": "blast_radius",
            "title": "Blast Radius — payment-api",
            "items": [{"kind_abbrev": "Svc", "name": "payment-api", "relationship": "selects", "status": "degraded", "status_detail": "0 endpoints"}],
        })
        assert not r.errors

    def test_empty_items(self):
        r = self._validate({"kind": "blast_radius", "title": "Blast Radius", "items": []})
        assert any("at least 1 item" in e for e in r.errors)

    def test_missing_items(self):
        r = self._validate({"kind": "blast_radius", "title": "Blast Radius"})
        assert any("at least 1 item" in e for e in r.errors)

    def test_item_missing_kind_abbrev(self):
        r = self._validate({
            "kind": "blast_radius",
            "title": "Blast Radius",
            "items": [{"name": "svc", "relationship": "selects", "status": "healthy"}],
        })
        assert any("kind_abbrev" in e for e in r.errors)

    def test_item_missing_name(self):
        r = self._validate({
            "kind": "blast_radius",
            "title": "Blast Radius",
            "items": [{"kind_abbrev": "Svc", "relationship": "selects", "status": "healthy"}],
        })
        assert any("'name'" in e for e in r.errors)

    def test_item_invalid_status(self):
        r = self._validate({
            "kind": "blast_radius",
            "title": "Blast Radius",
            "items": [{"kind_abbrev": "Svc", "name": "x", "relationship": "selects", "status": "unknown_status"}],
        })
        assert any("status must be one of" in e for e in r.errors)


class TestActionButtonValidation:
    def _validate(self, comp: dict) -> QualityResult:
        result = QualityResult()
        _validate_component(comp, result)
        return result

    def test_valid(self):
        r = self._validate({
            "kind": "action_button",
            "label": "Restart",
            "action": "restart_deployment",
            "action_input": {"name": "nginx", "namespace": "default"},
        })
        assert not r.errors

    def test_missing_label(self):
        r = self._validate({
            "kind": "action_button",
            "action": "restart_deployment",
            "action_input": {},
        })
        assert any("label" in e for e in r.errors)

    def test_missing_action(self):
        r = self._validate({
            "kind": "action_button",
            "label": "Go",
            "action_input": {},
        })
        assert any("action" in e for e in r.errors)

    def test_missing_action_input(self):
        r = self._validate({
            "kind": "action_button",
            "label": "Go",
            "action": "restart_deployment",
        })
        assert any("action_input" in e for e in r.errors)

    def test_action_input_wrong_type(self):
        r = self._validate({
            "kind": "action_button",
            "label": "Go",
            "action": "restart_deployment",
            "action_input": "not a dict",
        })
        assert any("action_input" in e for e in r.errors)

    def test_invalid_style(self):
        r = self._validate({
            "kind": "action_button",
            "label": "Go",
            "action": "restart_deployment",
            "action_input": {},
            "style": "neon",
        })
        assert any("style" in e for e in r.errors)

    def test_valid_styles(self):
        for style in ("primary", "danger", "ghost"):
            r = self._validate({
                "kind": "action_button",
                "label": "Go",
                "action": "x",
                "action_input": {},
                "style": style,
            })
            assert not any("style" in e for e in r.errors)
```

- [ ] **Step 2: Run tests**

Run: `python3 -m pytest tests/test_quality_engine_new_kinds.py -v`
Expected: All PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_quality_engine_new_kinds.py
git commit -m "test: validation tests for 4 new component kinds"
```

---

### Task 6: Action execution endpoint (`POST /views/{view_id}/actions`)

**Files:**
- Modify: `sre_agent/api/views.py`
- Create: `tests/test_view_action_endpoint.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for POST /views/{view_id}/actions endpoint."""

from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient


@pytest.fixture
def app():
    """Create a minimal FastAPI app with the views router."""
    from fastapi import FastAPI
    from sre_agent.api.views import router
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


class TestActionEndpoint:
    def test_missing_action_field(self, client):
        with patch("sre_agent.api.views.get_owner", return_value="testuser"):
            resp = client.post("/views/cv-test/actions", json={"action_input": {}})
        assert resp.status_code == 400
        assert "action" in resp.json()["error"]

    def test_blocked_tool(self, client):
        with patch("sre_agent.api.views.get_owner", return_value="testuser"):
            resp = client.post("/views/cv-test/actions", json={
                "action": "drain_node",
                "action_input": {"node_name": "worker-1"},
            })
        assert resp.status_code == 403
        assert "blocked" in resp.json()["error"].lower() or "not allowed" in resp.json()["error"].lower()

    def test_unknown_tool(self, client):
        with patch("sre_agent.api.views.get_owner", return_value="testuser"):
            resp = client.post("/views/cv-test/actions", json={
                "action": "nonexistent_tool_xyz",
                "action_input": {},
            })
        assert resp.status_code == 400
        assert "not found" in resp.json()["error"].lower() or "unknown" in resp.json()["error"].lower()

    def test_view_not_found(self, client):
        with (
            patch("sre_agent.api.views.get_owner", return_value="testuser"),
            patch("sre_agent.api.views.db") as mock_db,
        ):
            mock_db.get_view.return_value = None
            # Use a real tool name
            from sre_agent.tool_registry import TOOL_REGISTRY
            tool_name = next(
                (n for n in TOOL_REGISTRY if n not in {"drain_node", "exec_command"}),
                None,
            )
            if tool_name is None:
                pytest.skip("No tools registered")
            resp = client.post(f"/views/cv-test/actions", json={
                "action": tool_name,
                "action_input": {},
            })
        assert resp.status_code == 404

    def test_trust_level_exceeded(self, client):
        with (
            patch("sre_agent.api.views.get_owner", return_value="testuser"),
            patch("sre_agent.api.views.db") as mock_db,
            patch("sre_agent.api.views.get_settings") as mock_settings,
        ):
            mock_db.get_view.return_value = {"id": "cv-test", "owner": "testuser", "layout": []}
            mock_settings.return_value.max_trust_level = 0
            # Try a write tool
            resp = client.post("/views/cv-test/actions", json={
                "action": "restart_deployment",
                "action_input": {"name": "nginx", "namespace": "default"},
            })
        assert resp.status_code == 403
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_view_action_endpoint.py -v`
Expected: FAIL — endpoint doesn't exist

- [ ] **Step 3: Implement the endpoint**

Add to `sre_agent/api/views.py`:

```python
# ---------------------------------------------------------------------------
# Action Execution — execute a tool from a view's action_button component
# ---------------------------------------------------------------------------

# Tools that are never allowed via action buttons (too destructive for one-click)
_ACTION_BLOCKED_TOOLS = frozenset({"drain_node", "exec_command"})


@router.post("/views/{view_id}/actions")
async def rest_execute_action(
    view_id: str,
    request: Request,
    owner: str = Depends(get_owner),
):
    """Execute a tool action from a view's action_button component.

    Validates ownership, trust level, tool whitelist, and circuit breaker
    before executing through the standard tool infrastructure.
    """
    from fastapi.responses import JSONResponse

    from .. import db
    from ..config import get_settings
    from ..tool_registry import TOOL_REGISTRY, WRITE_TOOL_NAMES

    body = await request.json()
    action = body.get("action", "")
    action_input = body.get("action_input", {})

    if not action:
        return JSONResponse(status_code=400, content={"error": "Missing 'action' field"})
    if not isinstance(action_input, dict):
        return JSONResponse(status_code=400, content={"error": "'action_input' must be a dict"})

    # Tool whitelist check
    if action in _ACTION_BLOCKED_TOOLS:
        return JSONResponse(status_code=403, content={"error": f"Tool '{action}' is not allowed via action buttons"})
    if action not in TOOL_REGISTRY:
        return JSONResponse(status_code=400, content={"error": f"Tool '{action}' not found"})

    # Trust level check for write tools
    settings = get_settings()
    if action in WRITE_TOOL_NAMES and settings.max_trust_level < 1:
        return JSONResponse(status_code=403, content={"error": "Write operations disabled (trust level 0)"})

    # Ownership check
    view = db.get_view(view_id, owner)
    if view is None:
        return JSONResponse(status_code=404, content={"error": "View not found or not owned by you"})

    # Circuit breaker check
    from ..agent import _circuit_breaker

    if _circuit_breaker.is_open:
        return JSONResponse(status_code=503, content={"error": "Service temporarily unavailable (circuit breaker open)"})

    # Input validation
    from ..k8s_tools.validators import _validate_k8s_name, _validate_k8s_namespace

    ns = action_input.get("namespace")
    if ns:
        ns_err = _validate_k8s_namespace(ns)
        if ns_err:
            return JSONResponse(status_code=400, content={"error": ns_err})
    name = action_input.get("name")
    if name:
        name_err = _validate_k8s_name(name)
        if name_err:
            return JSONResponse(status_code=400, content={"error": name_err})
    replicas = action_input.get("replicas")
    if replicas is not None:
        try:
            r = int(replicas)
            if r < 0 or r > 100:
                return JSONResponse(status_code=400, content={"error": "Replicas must be 0-100"})
        except (ValueError, TypeError):
            return JSONResponse(status_code=400, content={"error": "Replicas must be a number"})

    # Execute the tool
    import asyncio

    from ..agent import _execute_tool

    tool_map = {action: TOOL_REGISTRY[action]}
    text, component, meta = await asyncio.to_thread(_execute_tool, action, action_input, tool_map)

    # Record in tool usage
    try:
        from ..tool_usage import record_tool_call

        record_tool_call(
            session_id=f"view-action-{view_id}",
            turn_number=0,
            agent_mode="view_action",
            tool_name=action,
            tool_input=action_input,
            result_text=text[:500],
        )
    except Exception:
        pass  # fire-and-forget

    status_code = 200 if meta.get("status") == "success" else 500
    return JSONResponse(
        status_code=status_code,
        content={
            "result": text,
            "component": component,
            "status": meta.get("status", "error"),
            "error_message": meta.get("error_message"),
        },
    )
```

You'll need to add `from .. import db` at the function level (it's already the pattern used throughout the file).

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_view_action_endpoint.py -v`
Expected: All PASS

- [ ] **Step 5: Run full test suite**

Run: `python3 -m pytest tests/ -v --timeout=60`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add sre_agent/api/views.py tests/test_view_action_endpoint.py
git commit -m "feat: add POST /views/{view_id}/actions endpoint for action button execution"
```

---

### Task 7: Action button sanitization tests

**Files:**
- Create: `tests/test_sanitize_action_button.py`

- [ ] **Step 1: Write tests**

```python
"""Tests for action_button sanitization in _sanitize_components."""

from __future__ import annotations

from sre_agent.api.sanitize import _sanitize_components


class TestActionButtonSanitization:
    def test_valid_action_button_preserved(self):
        comps = [
            {
                "kind": "action_button",
                "label": "Scale Up",
                "action": "scale_deployment",
                "action_input": {"name": "nginx", "namespace": "default", "replicas": 3},
            }
        ]
        result = _sanitize_components(comps)
        assert len(result) == 1
        assert result[0]["kind"] == "action_button"
        assert result[0]["_is_write"] is True  # scale_deployment is a write tool

    def test_blocked_tool_stripped(self):
        comps = [
            {
                "kind": "action_button",
                "label": "Drain",
                "action": "drain_node",
                "action_input": {"node_name": "worker-1"},
            }
        ]
        result = _sanitize_components(comps)
        assert len(result) == 0

    def test_unknown_tool_stripped(self):
        comps = [
            {
                "kind": "action_button",
                "label": "Go",
                "action": "nonexistent_tool_xyz",
                "action_input": {},
            }
        ]
        result = _sanitize_components(comps)
        assert len(result) == 0

    def test_invalid_namespace_stripped(self):
        comps = [
            {
                "kind": "action_button",
                "label": "Scale",
                "action": "scale_deployment",
                "action_input": {"name": "nginx", "namespace": "INVALID-NS!!"},
            }
        ]
        result = _sanitize_components(comps)
        assert len(result) == 0

    def test_replicas_out_of_range_stripped(self):
        comps = [
            {
                "kind": "action_button",
                "label": "Scale",
                "action": "scale_deployment",
                "action_input": {"name": "nginx", "namespace": "default", "replicas": 999},
            }
        ]
        result = _sanitize_components(comps)
        assert len(result) == 0

    def test_read_tool_not_flagged_as_write(self):
        comps = [
            {
                "kind": "action_button",
                "label": "List Pods",
                "action": "list_pods",
                "action_input": {"namespace": "default"},
            }
        ]
        result = _sanitize_components(comps)
        assert len(result) == 1
        assert result[0].get("_is_write") is False

    def test_other_components_unchanged(self):
        comps = [
            {"kind": "metric_card", "title": "CPU", "value": "72%"},
            {
                "kind": "action_button",
                "label": "Go",
                "action": "drain_node",
                "action_input": {},
            },
            {"kind": "status_list", "title": "Alerts", "items": []},
        ]
        result = _sanitize_components(comps)
        assert len(result) == 2
        assert result[0]["kind"] == "metric_card"
        assert result[1]["kind"] == "status_list"

    def test_nested_in_grid(self):
        comps = [
            {
                "kind": "grid",
                "items": [
                    {
                        "kind": "action_button",
                        "label": "Drain",
                        "action": "drain_node",
                        "action_input": {},
                    },
                    {"kind": "metric_card", "title": "CPU", "value": "50%"},
                ],
            }
        ]
        result = _sanitize_components(comps)
        assert len(result) == 1
        assert result[0]["kind"] == "grid"
        assert len(result[0]["items"]) == 1
        assert result[0]["items"][0]["kind"] == "metric_card"
```

- [ ] **Step 2: Run tests**

Run: `python3 -m pytest tests/test_sanitize_action_button.py -v`
Expected: All PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_sanitize_action_button.py
git commit -m "test: action_button sanitization tests"
```

---

### Task 8: Frontend TypeScript types

**Files:**
- Modify: `src/kubeview/engine/agentComponents.ts`

- [ ] **Step 1: Add the 4 new spec interfaces**

Add after the `TopologySpec` interface (before `ResourceCountsSpec`):

```typescript
export interface ActionButtonSpec {
  kind: 'action_button';
  label: string;
  action: string;
  action_input: Record<string, unknown>;
  style?: 'primary' | 'danger' | 'ghost';
  confirm_text?: string;
  /** Set by backend sanitization — true if action is a write tool */
  _is_write?: boolean;
}

export interface ConfidenceBadgeSpec {
  kind: 'confidence_badge';
  score: number;
  label?: string;
}

export interface ResolutionStep {
  title: string;
  status: 'done' | 'running' | 'pending';
  detail: string;
  output?: string | null;
  timestamp?: string | null;
}

export interface ResolutionTrackerSpec {
  kind: 'resolution_tracker';
  title?: string;
  steps: ResolutionStep[];
}

export interface BlastItem {
  kind_abbrev: string;
  name: string;
  relationship: string;
  status: 'degraded' | 'healthy' | 'retrying' | 'paused';
  status_detail: string;
}

export interface BlastRadiusSpec {
  kind: 'blast_radius';
  title?: string;
  items: BlastItem[];
  perspective?: 'physical' | 'logical' | 'network' | 'multi_tenant' | 'helm';
}
```

- [ ] **Step 2: Update the `ComponentSpec` union type**

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
  | StatCardSpec
  | TimelineSpec
  | ResourceCountsSpec
  | TopologySpec
  | ActionButtonSpec
  | ConfidenceBadgeSpec
  | ResolutionTrackerSpec
  | BlastRadiusSpec;
```

- [ ] **Step 3: Update `truncateForPersistence`**

Action buttons, confidence badges, resolution trackers, and blast radius components don't need truncation (they're small), but `resolution_tracker` steps could be long. Add a truncation case:

```typescript
if (spec.kind === 'resolution_tracker' && spec.steps.length > MAX_PERSISTED_ROWS) {
  return { ...spec, steps: spec.steps.slice(-MAX_PERSISTED_ROWS) };
}
```

- [ ] **Step 4: Verify TypeScript compiles**

Run: `cd /Users/amobrem/ali/OpenshiftPulse && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 5: Commit**

```bash
cd /Users/amobrem/ali/OpenshiftPulse && git add src/kubeview/engine/agentComponents.ts && git commit -m "feat: add TypeScript types for 4 new component specs"
```

---

### Task 9: Frontend — ConfidenceBadge renderer

**Files:**
- Create: `src/kubeview/components/agent/AgentConfidenceBadge.tsx`
- Modify: `src/kubeview/components/agent/AgentComponentRenderer.tsx`

- [ ] **Step 1: Create the renderer**

```tsx
import { cn } from '@/lib/utils';
import type { ConfidenceBadgeSpec } from '../../engine/agentComponents';

export function AgentConfidenceBadge({ spec }: { spec: ConfidenceBadgeSpec }) {
  const pct = Math.round(spec.score * 100);
  const color =
    spec.score > 0.8
      ? 'bg-emerald-900/40 text-emerald-400 border-emerald-800'
      : spec.score >= 0.5
        ? 'bg-amber-900/40 text-amber-400 border-amber-800'
        : 'bg-red-900/40 text-red-400 border-red-800';

  return (
    <span
      className={cn('inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium border', color)}
      role="status"
      aria-label={`Confidence: ${pct}%`}
    >
      {spec.label && <span className="text-slate-400 mr-0.5">{spec.label}:</span>}
      {pct}%
    </span>
  );
}
```

- [ ] **Step 2: Add to renderer switch**

In `AgentComponentRenderer.tsx`, add the import:

```typescript
import { AgentConfidenceBadge } from './AgentConfidenceBadge';
import type { ConfidenceBadgeSpec } from '../../engine/agentComponents';
```

Add the case before `default:`:

```typescript
case 'confidence_badge':
  return <AgentConfidenceBadge spec={spec as ConfidenceBadgeSpec} />;
```

- [ ] **Step 3: Verify TypeScript compiles**

Run: `cd /Users/amobrem/ali/OpenshiftPulse && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 4: Commit**

```bash
cd /Users/amobrem/ali/OpenshiftPulse && git add src/kubeview/components/agent/AgentConfidenceBadge.tsx src/kubeview/components/agent/AgentComponentRenderer.tsx && git commit -m "feat: add ConfidenceBadge frontend renderer"
```

---

### Task 10: Frontend — ResolutionTracker renderer

**Files:**
- Create: `src/kubeview/components/agent/AgentResolutionTracker.tsx`
- Modify: `src/kubeview/components/agent/AgentComponentRenderer.tsx`

- [ ] **Step 1: Create the renderer**

```tsx
import { CheckCircle, Loader2, Circle } from 'lucide-react';
import { cn } from '@/lib/utils';
import type { ResolutionTrackerSpec } from '../../engine/agentComponents';

const STATUS_CONFIG = {
  done: { Icon: CheckCircle, color: 'text-emerald-400', bg: 'bg-emerald-500/15', label: 'completed' },
  running: { Icon: Loader2, color: 'text-blue-400', bg: 'bg-blue-500/15', label: 'in progress' },
  pending: { Icon: Circle, color: 'text-slate-500', bg: 'bg-slate-500/15', label: 'pending' },
} as const;

export function AgentResolutionTracker({ spec }: { spec: ResolutionTrackerSpec }) {
  if (!spec.steps || spec.steps.length === 0) {
    return (
      <div className="my-2 border border-slate-700 rounded-lg p-4 text-center text-xs text-slate-500">
        No resolution steps yet
      </div>
    );
  }

  return (
    <div className="my-2 border border-slate-700 rounded-lg overflow-hidden min-w-0">
      {spec.title && (
        <div className="px-3 py-2 bg-slate-800/50 border-b border-slate-700 text-xs font-semibold text-slate-200 tracking-wide">
          {spec.title}
        </div>
      )}
      <div role="list" className="divide-y divide-slate-800/60">
        {spec.steps.map((step, i) => {
          const config = STATUS_CONFIG[step.status] || STATUS_CONFIG.pending;
          const { Icon } = config;
          return (
            <div key={i} role="listitem" className="px-3 py-2.5 flex gap-3">
              <div className={cn('flex items-center justify-center w-5 h-5 rounded-full shrink-0 mt-0.5', config.bg)}>
                <Icon
                  className={cn('h-3 w-3', config.color, step.status === 'running' && 'animate-spin')}
                  aria-label={config.label}
                />
              </div>
              <div className="flex-1 min-w-0" aria-live={step.status === 'running' ? 'polite' : undefined}>
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-slate-200">{step.title}</span>
                  {step.timestamp && (
                    <span className="text-[10px] text-slate-500">{step.timestamp}</span>
                  )}
                </div>
                {step.detail && (
                  <div className="text-xs text-slate-400 mt-0.5">{step.detail}</div>
                )}
                {step.output && (
                  <pre className="mt-1 p-2 bg-slate-950 rounded text-[11px] text-slate-300 font-mono overflow-x-auto max-h-32 whitespace-pre-wrap">
                    {step.output}
                  </pre>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Add to renderer switch**

Import:

```typescript
import { AgentResolutionTracker } from './AgentResolutionTracker';
import type { ResolutionTrackerSpec } from '../../engine/agentComponents';
```

Case:

```typescript
case 'resolution_tracker':
  return <AgentResolutionTracker spec={spec as ResolutionTrackerSpec} />;
```

- [ ] **Step 3: Verify TypeScript compiles**

Run: `cd /Users/amobrem/ali/OpenshiftPulse && npx tsc --noEmit`

- [ ] **Step 4: Commit**

```bash
cd /Users/amobrem/ali/OpenshiftPulse && git add src/kubeview/components/agent/AgentResolutionTracker.tsx src/kubeview/components/agent/AgentComponentRenderer.tsx && git commit -m "feat: add ResolutionTracker frontend renderer"
```

---

### Task 11: Frontend — BlastRadius renderer

**Files:**
- Create: `src/kubeview/components/agent/AgentBlastRadius.tsx`
- Modify: `src/kubeview/components/agent/AgentComponentRenderer.tsx`

- [ ] **Step 1: Create the renderer**

```tsx
import { cn } from '@/lib/utils';
import type { BlastRadiusSpec } from '../../engine/agentComponents';

const STATUS_STYLES = {
  degraded: { text: 'text-red-400', bg: 'bg-red-500/15', border: 'border-red-800' },
  healthy: { text: 'text-emerald-400', bg: 'bg-emerald-500/15', border: 'border-emerald-800' },
  retrying: { text: 'text-amber-400', bg: 'bg-amber-500/15', border: 'border-amber-800' },
  paused: { text: 'text-slate-400', bg: 'bg-slate-500/15', border: 'border-slate-700' },
} as const;

export function AgentBlastRadius({ spec }: { spec: BlastRadiusSpec }) {
  if (!spec.items || spec.items.length === 0) {
    return (
      <div className="my-2 border border-slate-700 rounded-lg p-4 text-center text-xs text-slate-500">
        No downstream dependencies detected
      </div>
    );
  }

  return (
    <div className="my-2 border border-slate-700 rounded-lg overflow-hidden min-w-0">
      {spec.title && (
        <div className="px-3 py-2 bg-slate-800/50 border-b border-slate-700 text-xs font-semibold text-slate-200 tracking-wide">
          {spec.title}
        </div>
      )}
      <div role="list" className="divide-y divide-slate-800/60">
        {spec.items.map((item, i) => {
          const style = STATUS_STYLES[item.status] || STATUS_STYLES.healthy;
          return (
            <div
              key={i}
              role="listitem"
              aria-label={`${item.kind_abbrev} ${item.name}: ${item.status} — ${item.status_detail}`}
              className="px-3 py-2.5 flex items-center gap-3"
            >
              <span className={cn('text-[10px] font-bold uppercase px-1.5 py-0.5 rounded border', style.bg, style.text, style.border)}>
                {item.kind_abbrev}
              </span>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-slate-200 truncate">{item.name}</span>
                  <span className={cn('text-[10px] px-1 rounded', style.bg, style.text)}>
                    {item.status}
                  </span>
                </div>
                <div className="text-[11px] text-slate-500 truncate">{item.relationship}</div>
              </div>
              <span className="text-xs text-slate-400 shrink-0">{item.status_detail}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Add to renderer switch**

Import:

```typescript
import { AgentBlastRadius } from './AgentBlastRadius';
import type { BlastRadiusSpec } from '../../engine/agentComponents';
```

Case:

```typescript
case 'blast_radius':
  return <AgentBlastRadius spec={spec as BlastRadiusSpec} />;
```

- [ ] **Step 3: Verify TypeScript compiles**

Run: `cd /Users/amobrem/ali/OpenshiftPulse && npx tsc --noEmit`

- [ ] **Step 4: Commit**

```bash
cd /Users/amobrem/ali/OpenshiftPulse && git add src/kubeview/components/agent/AgentBlastRadius.tsx src/kubeview/components/agent/AgentComponentRenderer.tsx && git commit -m "feat: add BlastRadius frontend renderer"
```

---

### Task 12: Frontend — ActionButton renderer + store method

**Files:**
- Create: `src/kubeview/components/agent/AgentActionButton.tsx`
- Modify: `src/kubeview/components/agent/AgentComponentRenderer.tsx`
- Modify: `src/kubeview/store/customViewStore.ts`

- [ ] **Step 1: Add `executeAction` to the store**

In `src/kubeview/store/customViewStore.ts`, add to the store interface and implementation:

```typescript
// Interface addition:
executeAction: (viewId: string, action: string, actionInput: Record<string, unknown>) => Promise<{ result: string; status: string; error_message?: string }>;

// Implementation:
executeAction: async (viewId, action, actionInput) => {
  const resp = await fetch(`/api/views/${viewId}/actions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${get().wsToken || ''}` },
    body: JSON.stringify({ action, action_input: actionInput }),
  });
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.error || 'Action failed');
  return data;
},
```

- [ ] **Step 2: Create the ActionButton renderer**

```tsx
import { useState, useCallback } from 'react';
import { Loader2 } from 'lucide-react';
import { cn } from '@/lib/utils';
import { ConfirmDialog } from '../feedback/ConfirmDialog';
import type { ActionButtonSpec } from '../../engine/agentComponents';
import { useCustomViewStore } from '../../store/customViewStore';

const STYLE_CLASSES = {
  primary: 'bg-blue-600 hover:bg-blue-700 text-white',
  danger: 'bg-red-600 hover:bg-red-700 text-white',
  ghost: 'bg-transparent hover:bg-slate-700 text-slate-300 border border-slate-600',
} as const;

interface Props {
  spec: ActionButtonSpec;
  viewId?: string;
}

export function AgentActionButton({ spec, viewId }: Props) {
  const [loading, setLoading] = useState(false);
  const [showConfirm, setShowConfirm] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const executeAction = useCustomViewStore((s) => s.executeAction);

  const style = spec.style || 'primary';
  const isWrite = spec._is_write === true;

  const handleExecute = useCallback(async () => {
    if (!viewId) {
      setError('No view context for action execution');
      return;
    }
    setLoading(true);
    setError(null);
    setResult(null);
    setShowConfirm(false);
    try {
      const data = await executeAction(viewId, spec.action, spec.action_input);
      setResult(data.result);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Action failed');
    } finally {
      setLoading(false);
    }
  }, [viewId, spec.action, spec.action_input, executeAction]);

  const handleClick = useCallback(() => {
    if (isWrite) {
      setShowConfirm(true);
    } else {
      handleExecute();
    }
  }, [isWrite, handleExecute]);

  return (
    <div className="inline-flex flex-col gap-1">
      <button
        onClick={handleClick}
        disabled={loading}
        role="button"
        aria-label={`${spec.label} — ${spec.action}`}
        aria-description={style === 'danger' ? spec.confirm_text : undefined}
        className={cn(
          'inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed',
          STYLE_CLASSES[style] || STYLE_CLASSES.primary,
        )}
      >
        {loading && <Loader2 className="w-3 h-3 animate-spin" />}
        {spec.label}
      </button>

      {result && (
        <div className="text-[10px] text-emerald-400 max-w-xs truncate" title={result}>
          {result.length > 80 ? result.slice(0, 80) + '...' : result}
        </div>
      )}
      {error && (
        <div className="text-[10px] text-red-400 max-w-xs truncate" title={error}>
          {error}
        </div>
      )}

      {isWrite && (
        <ConfirmDialog
          open={showConfirm}
          onClose={() => setShowConfirm(false)}
          onConfirm={handleExecute}
          title={spec.label}
          description={spec.confirm_text || `This will execute "${spec.action}" — are you sure?`}
          confirmLabel={spec.label}
          variant={style === 'danger' ? 'danger' : 'warning'}
          loading={loading}
        />
      )}
    </div>
  );
}
```

- [ ] **Step 3: Add to renderer switch**

Import:

```typescript
import { AgentActionButton } from './AgentActionButton';
import type { ActionButtonSpec } from '../../engine/agentComponents';
```

Case:

```typescript
case 'action_button':
  return <AgentActionButton spec={spec as ActionButtonSpec} />;
```

Note: The `AgentActionButton` needs a `viewId` prop that the switch statement doesn't have yet. For now, the button will render but won't execute actions when rendered inline in chat (only in custom views where `viewId` is available). This is fine — action buttons are designed for custom views, not inline chat. The `viewId` prop wiring happens in Phase 3 when views get their own renderer context.

- [ ] **Step 4: Verify TypeScript compiles**

Run: `cd /Users/amobrem/ali/OpenshiftPulse && npx tsc --noEmit`

- [ ] **Step 5: Commit**

```bash
cd /Users/amobrem/ali/OpenshiftPulse && git add src/kubeview/components/agent/AgentActionButton.tsx src/kubeview/components/agent/AgentComponentRenderer.tsx src/kubeview/store/customViewStore.ts && git commit -m "feat: add ActionButton frontend renderer with confirmation dialog"
```

---

### Task 13: Run full test suite + verify

**Files:** None (verification only)

- [ ] **Step 1: Run backend tests**

Run: `cd /Users/amobrem/ali/pulse-agent && python3 -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 2: Run mypy**

Run: `cd /Users/amobrem/ali/pulse-agent && python3 -m mypy sre_agent/ --ignore-missing-imports`
Expected: 0 errors

- [ ] **Step 3: Run ruff**

Run: `cd /Users/amobrem/ali/pulse-agent && python3 -m ruff check sre_agent/`
Expected: Clean

- [ ] **Step 4: Run frontend type check**

Run: `cd /Users/amobrem/ali/OpenshiftPulse && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 5: Verify `make verify`**

Run: `cd /Users/amobrem/ali/pulse-agent && make verify`
Expected: Green

---

### Task 14: Update CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update component count**

Change the project description line to reflect 29 component types (was 25, adding 4).

Update `component_registry.py` description to mention the new component count.

- [ ] **Step 2: Update the views.py entry in Key Files**

Add a note about the `POST /views/{view_id}/actions` endpoint.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for Phase 2 — 4 new view components"
```
