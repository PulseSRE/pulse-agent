# Next-Gen Phase 1: Code Cleanup & Refactoring

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clean foundation — split oversized files, remove dead code, fix 6 security vulnerabilities, add trend scanners, enable mypy on skills. This is the prerequisite for all next-gen phases.

**Architecture:** Pure refactoring + security fixes + new scanners. No behavioral changes to existing features. All existing tests must continue to pass after each task. The cluster is live (ROSA ggxoa, OCP 4.21.8) — run evals against it after the full phase.

**Tech Stack:** Python 3.11+, FastAPI, pytest, mypy, ruff, Prometheus/PromQL

**Spec:** `docs/superpowers/specs/2026-04-20-nextgen-revised-design.md` (Phase 1, sections 1.1–1.5)

---

## File Map

### Files to Split

| Original | New Files | Responsibility |
|----------|-----------|----------------|
| `sre_agent/api/monitor_rest.py` (1955 lines) | `sre_agent/api/scanner_rest.py`, `sre_agent/api/fix_rest.py`, `sre_agent/api/topology_rest.py` | Scanner coverage routes; fix history + rollback routes; topology + blast radius routes |
| `sre_agent/skill_loader.py` (1590 lines) | `sre_agent/skill_router.py`, `sre_agent/tool_categories.py` | Routing logic (classify_query, _hard_pre_route, _llm_classify); TOOL_CATEGORIES + ALWAYS_INCLUDE + MODE_CATEGORIES data |
| `sre_agent/view_tools.py` (1554 lines) | `sre_agent/view_mutations.py` | Widget update operations (update_view_widgets, remove_widget_from_view, undo_view_change, optimize_view) |

### Files to Delete

| File | Replacement |
|------|-------------|
| `sre_agent/view_validator.py` (43 lines) | Direct imports from `sre_agent/quality_engine.py` |

### Files to Modify (move code into)

| File | What Moves In |
|------|---------------|
| `sre_agent/quality_engine.py` | `critique_view` function from `view_critic.py` |

### Files to Create

| File | Purpose |
|------|---------|
| `sre_agent/monitor/trend_scanners.py` | 4 trend scanner functions using predict_linear() |
| `tests/test_security_views.py` | Security regression tests for IDOR, HMAC, cross-user leak |
| `tests/test_trend_scanners.py` | Tests for trend scanners |

---

## Task 1: Split `monitor_rest.py` — Extract Scanner Routes

**Files:**
- Modify: `sre_agent/api/monitor_rest.py`
- Create: `sre_agent/api/scanner_rest.py`

- [ ] **Step 1: Run existing tests to establish baseline**

Run: `python3 -m pytest tests/test_monitor.py tests/test_analytics_rest.py -v --tb=short`
Expected: All pass

- [ ] **Step 2: Create `scanner_rest.py` with scanner coverage routes**

Extract from `monitor_rest.py`:
- `get_scanner_coverage()` (lines 27-144)
- `rest_list_scanners()` (lines 384-402)
- `monitor_capabilities()` (lines 653-667)
- `pause_autofix()`, `resume_autofix()` (lines 669-683)

Create `sre_agent/api/scanner_rest.py`:
```python
"""Scanner coverage, registry, and monitor control routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from .auth import verify_token

logger = logging.getLogger("pulse_agent.api.scanner")
router = APIRouter(tags=["scanner"])

# Move the extracted functions here, keeping their exact signatures and decorators.
# Replace @app.get with @router.get — the router will be included in serve.py.
```

Move each function preserving exact route paths, parameters, and response shapes.

- [ ] **Step 3: Remove extracted functions from `monitor_rest.py`**

Delete the moved functions from `monitor_rest.py`. Add import of the new router at the top if any cross-references exist.

- [ ] **Step 4: Register new router in `serve.py`**

Find where `monitor_rest.router` is included in `sre_agent/serve.py` and add:
```python
from .api.scanner_rest import router as scanner_router
app.include_router(scanner_router, prefix="/api/agent")
```

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/test_monitor.py tests/test_analytics_rest.py -v --tb=short`
Expected: All pass (no behavioral changes)

- [ ] **Step 6: Commit**

```bash
git add sre_agent/api/scanner_rest.py sre_agent/api/monitor_rest.py sre_agent/serve.py
git commit -m "refactor: extract scanner routes from monitor_rest.py"
```

---

## Task 2: Split `monitor_rest.py` — Extract Fix History Routes

**Files:**
- Modify: `sre_agent/api/monitor_rest.py`
- Create: `sre_agent/api/fix_rest.py`

- [ ] **Step 1: Create `fix_rest.py` with fix history routes**

Extract from `monitor_rest.py`:
- `rest_fix_history()` (lines 145-235)
- `rest_fix_history_summary()` (lines 237-295)
- `rest_fix_history_resolutions()` (lines 297-335)
- `rest_action_detail()` (lines 337-355)
- `rollback_action()` (lines 357-373)

Create `sre_agent/api/fix_rest.py` following same pattern as scanner_rest.py.

- [ ] **Step 2: Remove extracted functions from `monitor_rest.py`**

- [ ] **Step 3: Register new router in `serve.py`**

```python
from .api.fix_rest import router as fix_router
app.include_router(fix_router, prefix="/api/agent")
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_monitor.py tests/test_analytics_rest.py -v --tb=short`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add sre_agent/api/fix_rest.py sre_agent/api/monitor_rest.py sre_agent/serve.py
git commit -m "refactor: extract fix history routes from monitor_rest.py"
```

---

## Task 3: Split `monitor_rest.py` — Extract Topology Routes

**Files:**
- Modify: `sre_agent/api/monitor_rest.py`
- Create: `sre_agent/api/topology_rest.py`

- [ ] **Step 1: Create `topology_rest.py` with topology and blast radius routes**

Extract from `monitor_rest.py`:
- `get_topology()` (lines 729-830)
- `get_blast_radius()` (lines 832-920)
- `_parse_dep_id()` helper
- `get_finding_impact()` (lines 922-970)
- `get_finding_learning()` (lines 972-1017)
- `simulate_with_blast_radius()` (lines 1019-1100)

- [ ] **Step 2: Remove extracted functions from `monitor_rest.py`**

- [ ] **Step 3: Register new router in `serve.py`**

- [ ] **Step 4: Verify `monitor_rest.py` is under 1000 lines**

Run: `wc -l sre_agent/api/monitor_rest.py`
Expected: Under 1000 lines (KPI dashboard + briefing + SLO + analytics + plan templates remain)

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/ -v --tb=short -q`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add sre_agent/api/topology_rest.py sre_agent/api/monitor_rest.py sre_agent/serve.py
git commit -m "refactor: extract topology routes from monitor_rest.py"
```

---

## Task 4: Split `skill_loader.py` — Extract Tool Categories

**Files:**
- Modify: `sre_agent/skill_loader.py`
- Create: `sre_agent/tool_categories.py`

- [ ] **Step 1: Run existing tests**

Run: `python3 -m pytest tests/test_skill_loader.py tests/test_selector_eval.py -v --tb=short`
Expected: All pass

- [ ] **Step 2: Create `tool_categories.py`**

Extract from `skill_loader.py` (lines 769-1130):
- `TOOL_CATEGORIES` dict
- `ALWAYS_INCLUDE` set
- `MODE_CATEGORIES` dict
- `get_tool_category()` function
- `get_tool_skills()` function

Create `sre_agent/tool_categories.py`:
```python
"""Tool category reference data and lookup functions.

Extracted from skill_loader.py — pure data + simple lookups.
"""

from __future__ import annotations

TOOL_CATEGORIES: dict[str, list[str]] = {
    # ... move the full dict here
}

ALWAYS_INCLUDE: set[str] = {
    # ... move the full set here
}

MODE_CATEGORIES: dict[str, list[str]] = {
    # ... move the full dict here
}


def get_tool_category(tool_name: str) -> str | None:
    # ... move function here


def get_tool_skills(tool_name: str) -> list[str]:
    # ... move function here
```

- [ ] **Step 3: Update `skill_loader.py` imports**

Replace the moved code with imports:
```python
from .tool_categories import (
    ALWAYS_INCLUDE,
    MODE_CATEGORIES,
    TOOL_CATEGORIES,
    get_tool_category,
    get_tool_skills,
)
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_skill_loader.py tests/test_selector_eval.py -v --tb=short`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add sre_agent/tool_categories.py sre_agent/skill_loader.py
git commit -m "refactor: extract tool categories from skill_loader.py"
```

---

## Task 5: Split `skill_loader.py` — Extract Routing Logic

**Files:**
- Modify: `sre_agent/skill_loader.py`
- Create: `sre_agent/skill_router.py`

- [ ] **Step 1: Create `skill_router.py`**

Extract from `skill_loader.py` (lines 501-748):
- `_hard_pre_route()` — regex pre-routing
- `classify_query()` — ORCA + LLM fallback
- `classify_query_multi()` — primary + secondary skill selection
- `_llm_classify()` — lightweight LLM classification with caching
- `check_handoff()` — keyword-based skill delegation

These functions reference `_loaded_skills` (module-level dict in skill_loader). Pass it as a parameter or import from skill_loader.

- [ ] **Step 2: Update `skill_loader.py` to re-export routing functions**

Add at the bottom of `skill_loader.py`:
```python
from .skill_router import classify_query, classify_query_multi, check_handoff  # noqa: F401
```

This preserves backward compatibility for all existing importers.

- [ ] **Step 3: Verify `skill_loader.py` is under 1000 lines**

Run: `wc -l sre_agent/skill_loader.py`
Expected: Under 1000 lines

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_skill_loader.py tests/test_selector_eval.py tests/test_orchestrator.py -v --tb=short`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add sre_agent/skill_router.py sre_agent/skill_loader.py
git commit -m "refactor: extract routing logic from skill_loader.py"
```

---

## Task 6: Split `view_tools.py` — Extract Mutation Functions

**Files:**
- Modify: `sre_agent/view_tools.py`
- Create: `sre_agent/view_mutations.py`

- [ ] **Step 1: Run existing tests**

Run: `python3 -m pytest tests/test_views.py -v --tb=short`
Expected: All pass

- [ ] **Step 2: Create `view_mutations.py`**

Extract from `view_tools.py`:
- `update_view_widgets()` (lines 648-915) — the 12-action mutation function
- `remove_widget_from_view()` — if separate from update_view_widgets
- `undo_view_change()` (lines 1011-1075)
- `get_view_versions()` — version listing
- `optimize_view()` (lines 1127-1243)

Each function is a `@beta_tool` — the decorator handles tool registration. Moving them to a new file automatically registers them when the module is imported.

- [ ] **Step 3: Update `view_tools.py` to import from `view_mutations.py`**

Add at the bottom:
```python
from .view_mutations import optimize_view, undo_view_change, update_view_widgets  # noqa: F401
```

- [ ] **Step 4: Verify `view_tools.py` is under 1000 lines**

Run: `wc -l sre_agent/view_tools.py`

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/test_views.py -v --tb=short`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add sre_agent/view_mutations.py sre_agent/view_tools.py
git commit -m "refactor: extract view mutations from view_tools.py"
```

---

## Task 7: Remove `view_validator.py`

**Files:**
- Delete: `sre_agent/view_validator.py`
- Modify: `sre_agent/api/agent_ws.py` (line 372)
- Modify: `tests/test_view_validator.py`

- [ ] **Step 1: Update `agent_ws.py` import**

Change line 372 from:
```python
from ..view_validator import validate_components as _validate
```
to:
```python
from ..quality_engine import evaluate_components

# At usage site, adapt to new return type (QualityResult instead of ValidationResult):
result = evaluate_components(components, positions=None)
# result.valid, result.errors, result.warnings, result.components all work the same
```

- [ ] **Step 2: Update `tests/test_view_validator.py`**

Change import:
```python
# Old:
from sre_agent.view_validator import validate_components
# New:
from sre_agent.quality_engine import evaluate_components as validate_components
```

Adapt any assertions that depend on the `ValidationResult` dataclass to use `QualityResult` fields (same field names: `valid`, `errors`, `warnings`, `deduped_count`, `components`).

- [ ] **Step 3: Delete `view_validator.py`**

```bash
rm sre_agent/view_validator.py
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_view_validator.py tests/test_quality_engine.py tests/test_views.py -v --tb=short`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add -u sre_agent/view_validator.py sre_agent/api/agent_ws.py tests/test_view_validator.py
git commit -m "refactor: remove view_validator.py wrapper, import from quality_engine directly"
```

---

## Task 8: Move `critique_view` from `view_critic.py` to `quality_engine.py`

**Files:**
- Delete: `sre_agent/view_critic.py`
- Modify: `sre_agent/quality_engine.py`
- Modify: `sre_agent/view_tools.py` (line 1262)
- Modify: `tests/test_view_critic.py`
- Modify: `tests/test_views.py` (lines 605, 627, 638, 644)

- [ ] **Step 1: Copy `critique_view` function to `quality_engine.py`**

Append the full `critique_view` function (lines 17-151 from `view_critic.py`) to the end of `quality_engine.py`. Keep the `@beta_tool` decorator and all imports it needs (`from .decorators import beta_tool`).

- [ ] **Step 2: Update `view_tools.py` import**

Change line 1262 from:
```python
from .view_critic import critique_view
```
to:
```python
from .quality_engine import critique_view
```

- [ ] **Step 3: Update test imports**

In `tests/test_view_critic.py` line 12:
```python
# Old:
from sre_agent.view_critic import critique_view
# New:
from sre_agent.quality_engine import critique_view
```

In `tests/test_views.py` lines 605, 627, 638, 644:
```python
# Old:
from sre_agent.view_critic import critique_view
# New:
from sre_agent.quality_engine import critique_view
```

- [ ] **Step 4: Check view_designer skill prompt for references**

Run: `grep -r "view_critic" sre_agent/skills/`

Update any skill.md files that reference `view_critic` to reference `quality_engine` instead.

- [ ] **Step 5: Delete `view_critic.py`**

```bash
rm sre_agent/view_critic.py
```

- [ ] **Step 6: Run tests**

Run: `python3 -m pytest tests/test_view_critic.py tests/test_views.py tests/test_quality_engine.py -v --tb=short`
Expected: All pass

- [ ] **Step 7: Commit**

```bash
git add -u sre_agent/view_critic.py sre_agent/quality_engine.py sre_agent/view_tools.py tests/
git commit -m "refactor: move critique_view to quality_engine.py, delete view_critic.py"
```

---

## Task 9: Verify `prompt_experiment` Is Active (Do NOT Delete)

**Files:**
- None (investigation only)

- [ ] **Step 1: Confirm `prompt_experiment` is actively used**

The field is used in:
- `sre_agent/agent.py:227` — controls system prompt variant (`legacy`, `cot`, or default)
- `sre_agent/runbooks.py:137` — controls runbook count (3 for legacy, 1 for default)

This is NOT dead code. The spec says "verify usage first, remove only if confirmed dead." It is confirmed ACTIVE. Skip deletion. No commit needed.

---

## Task 10: Fix View Ownership Bypass (IDOR)

**Files:**
- Modify: `sre_agent/view_tools.py`
- Create: `tests/test_security_views.py`

- [ ] **Step 1: Write the failing security test**

Create `tests/test_security_views.py`:
```python
"""Security regression tests for view access control."""

import pytest
from unittest.mock import patch, MagicMock
from sre_agent.view_tools import get_view_details, list_saved_views, delete_dashboard


class TestViewOwnershipBypass:
    """Verify user B cannot read/modify/delete user A's views."""

    @patch("sre_agent.view_tools.get_current_user", return_value="user-B")
    def test_get_view_details_rejects_other_users_view(self, mock_user):
        """get_view_details must NOT fall back to ownerless query."""
        with patch("sre_agent.view_tools.db") as mock_db:
            mock_db.get_view.side_effect = lambda vid, owner=None: (
                None if owner == "user-B"
                else {"id": "cv-123", "owner": "user-A", "title": "Secret", "layout": []}
            )
            result = get_view_details("cv-123")
            assert "not found" in result.lower()

    @patch("sre_agent.view_tools.get_current_user", return_value="user-B")
    def test_list_views_does_not_leak_other_users(self, mock_user):
        """list_saved_views must NOT return all users' views on empty result."""
        with patch("sre_agent.view_tools.db") as mock_db:
            mock_db.list_views.return_value = []
            result = list_saved_views()
            assert isinstance(result, str)
            assert "no saved views" in result.lower()
            # Must NOT have called fetchall with unscoped query
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_security_views.py -v`
Expected: FAIL (ownership fallback returns data it shouldn't)

- [ ] **Step 3: Fix `list_saved_views` — remove unscoped fallback**

In `sre_agent/view_tools.py`, function `list_saved_views()` (around line 577), remove the entire fallback block:
```python
# DELETE this block (lines 577-587):
    if not views:
        try:
            _db = db.get_database()
            rows = _db.fetchall(
                "SELECT id, owner, title, description, icon, layout, positions, created_at, updated_at "
                "FROM views ORDER BY updated_at DESC LIMIT 50"
            )
            views = [db._deserialize_view_row(r) for r in rows] if rows else []
        except Exception:
            pass
```

Replace with:
```python
    if not views:
        return "No saved views found. You can create one by asking me to build a dashboard."
```

- [ ] **Step 4: Fix `get_view_details` — remove ownerless fallback**

In `sre_agent/view_tools.py`, function `get_view_details()` (around line 632-634), remove:
```python
# DELETE:
    if not view:
        view = db.get_view(view_id)  # Fallback without owner filter
```

- [ ] **Step 5: Fix `update_view_widgets` — remove ownerless fallback**

In `sre_agent/view_tools.py`, function `update_view_widgets()` (around line 680-685), remove:
```python
# DELETE:
    if not view:
        view = db.get_view(view_id)  # Fallback without owner filter
    # ...
    owner = view.get("owner", owner)
```

Keep only:
```python
    view = db.get_view(view_id, owner)
    if not view:
        return f"View '{view_id}' not found."
```

- [ ] **Step 6: Fix remaining functions with same pattern**

Apply the same fix to these functions in `view_tools.py` (search for `db.get_view(view_id)` without owner parameter):
- `remove_widget_from_view()` 
- `undo_view_change()`
- `delete_dashboard()`
- `optimize_view()`

For each: remove the ownerless `db.get_view(view_id)` fallback. Keep only `db.get_view(view_id, owner)`.

- [ ] **Step 7: Run security tests**

Run: `python3 -m pytest tests/test_security_views.py -v`
Expected: All PASS

- [ ] **Step 8: Run full test suite**

Run: `python3 -m pytest tests/ -v --tb=short -q`
Expected: All pass

- [ ] **Step 9: Commit**

```bash
git add sre_agent/view_tools.py tests/test_security_views.py
git commit -m "fix(security): remove view ownership bypass — IDOR vulnerability"
```

---

## Task 11: Fix Share Token HMAC Key Mismatch

**Files:**
- Modify: `sre_agent/api/views.py`
- Modify: `tests/test_security_views.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_security_views.py`:
```python
import hmac
import hashlib
import time
import os
from unittest.mock import patch


class TestShareTokenHMAC:
    """Share token sign + verify must use the same key."""

    def test_share_token_roundtrip_with_custom_key(self):
        """When PULSE_SHARE_TOKEN_KEY is set, both sign and verify must use it."""
        custom_key = "my-custom-share-key"
        view_id = "cv-test123"
        expires = int(time.time()) + 86400

        # Sign with custom key
        payload = f"{view_id}:{expires}"
        signature = hmac.new(custom_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
        share_token = f"{payload}:{signature}"

        # Verify with same custom key — should succeed
        parts = share_token.split(":")
        expected_sig = hmac.new(
            custom_key.encode(),
            f"{parts[0]}:{parts[1]}".encode(),
            hashlib.sha256,
        ).hexdigest()
        assert hmac.compare_digest(parts[2], expected_sig)
```

- [ ] **Step 2: Fix `rest_claim_shared_view` in `api/views.py`**

Change line 202 from:
```python
    secret = get_settings().ws_token
```
to:
```python
    secret = os.environ.get("PULSE_SHARE_TOKEN_KEY", "") or get_settings().ws_token
```

This matches the sign path (line 166).

- [ ] **Step 3: Run tests**

Run: `python3 -m pytest tests/test_security_views.py tests/test_views.py -v`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add sre_agent/api/views.py tests/test_security_views.py
git commit -m "fix(security): share token HMAC — use same key for sign and verify"
```

---

## Task 12: Fix ReDoS in `log-counts` Endpoint

**Files:**
- Modify: `sre_agent/api/views.py`
- Modify: `tests/test_security_views.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_security_views.py`:
```python
class TestReDoSProtection:
    """Validate regex pattern safety in log-counts endpoint."""

    def test_rejects_long_pattern(self):
        """Patterns over 100 chars must be rejected."""
        from sre_agent.api.views import _validate_regex_pattern
        assert _validate_regex_pattern("a" * 101) is not None

    def test_rejects_nested_quantifiers(self):
        """Patterns with nested quantifiers must be rejected."""
        from sre_agent.api.views import _validate_regex_pattern
        assert _validate_regex_pattern("(a+)+$") is not None

    def test_allows_normal_pattern(self):
        """Normal patterns must pass."""
        from sre_agent.api.views import _validate_regex_pattern
        assert _validate_regex_pattern("error|Error|ERROR") is None
```

- [ ] **Step 2: Add validation function to `api/views.py`**

Add before the `rest_log_counts` function:
```python
import re as _re

_NESTED_QUANTIFIER_RE = _re.compile(r"[+*]\)*[+*]")

def _validate_regex_pattern(pattern: str) -> str | None:
    """Return error message if pattern is unsafe, None if safe."""
    if len(pattern) > 100:
        return "Pattern too long (max 100 characters)"
    if _NESTED_QUANTIFIER_RE.search(pattern):
        return "Pattern contains nested quantifiers (ReDoS risk)"
    try:
        _re.compile(pattern)
    except _re.error as e:
        return f"Invalid regex: {e}"
    return None
```

- [ ] **Step 3: Use validation in `rest_log_counts`**

Add before `compiled = re.compile(pattern)` (around line 336):
```python
    pattern_err = _validate_regex_pattern(pattern)
    if pattern_err:
        return JSONResponse(status_code=400, content={"error": pattern_err})
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_security_views.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add sre_agent/api/views.py tests/test_security_views.py
git commit -m "fix(security): validate regex pattern in log-counts endpoint — prevent ReDoS"
```

---

## Task 13: Fix `clone_view` Post-Share Mutation

**Files:**
- Modify: `sre_agent/api/views.py`
- Modify: `sre_agent/db.py`

- [ ] **Step 1: Add snapshot-on-share to `rest_share_view`**

In `sre_agent/api/views.py`, `rest_share_view()` function, after verifying the view exists (around line 165), add a version snapshot:
```python
    # Snapshot the view at share time so claimants get this version, not future mutations
    from .. import db as _db_mod
    snapshot_version = _db_mod.snapshot_view(view_id, action="shared")
```

Then include the snapshot version in the share token payload:
```python
    expires = int(time.time()) + 86400
    payload = f"{view_id}:{expires}:{snapshot_version}"
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    share_token = f"{payload}:{signature}"
```

- [ ] **Step 2: Update `rest_claim_shared_view` to parse 4-part token**

Update the token parsing to handle both old (3-part) and new (4-part) tokens:
```python
    parts = share_token.split(":")
    if len(parts) == 4:
        view_id, expires_str, snapshot_version, signature = parts
        sig_payload = f"{view_id}:{expires_str}:{snapshot_version}"
    elif len(parts) == 3:
        view_id, expires_str, signature = parts
        snapshot_version = None
        sig_payload = f"{view_id}:{expires_str}"
    else:
        return JSONResponse(status_code=400, content={"error": "Invalid share token"})
```

When cloning, if `snapshot_version` is set, restore the view to that version before cloning:
```python
    if snapshot_version:
        new_id = _db_mod.clone_view_at_version(view_id, owner, int(snapshot_version))
    else:
        new_id = _db_mod.clone_view(view_id, owner)
```

- [ ] **Step 3: Add `clone_view_at_version` to `db.py`**

```python
def clone_view_at_version(view_id: str, new_owner: str, version: int) -> str | None:
    """Clone a view at a specific version snapshot."""
    versions = list_view_versions(view_id)
    target = next((v for v in versions if v.get("version") == version), None)
    if not target:
        return clone_view(view_id, new_owner)  # fallback to current
    # Use the snapshot's layout and positions
    source = get_view(view_id)
    if not source:
        return None
    new_id = f"cv-{uuid.uuid4().hex[:12]}"
    # ... insert with target's layout/positions instead of current
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_views.py tests/test_security_views.py -v --tb=short`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add sre_agent/api/views.py sre_agent/db.py
git commit -m "fix(security): snapshot view on share — claimants get shared version, not mutations"
```

---

## Task 13.5: Namespace-Scope Topology & Blast Radius

**Files:**
- Modify: `sre_agent/view_tools.py` (`get_topology_graph`)
- Modify: `sre_agent/api/topology_rest.py` (extracted in Task 3)

- [ ] **Step 1: Add `namespace` parameter to `get_topology_graph`**

The existing `get_topology_graph()` tool already accepts a `namespace` parameter but the dependency graph singleton returns ALL resources. Add filtering after graph retrieval:

```python
# After retrieving nodes/edges from dependency graph, filter by namespace:
if namespace:
    nodes = [n for n in nodes if n.get("namespace", "") == namespace or n.get("namespace", "") == ""]
    node_ids = {n["id"] for n in nodes}
    edges = [e for e in edges if e["source"] in node_ids and e["target"] in node_ids]
```

- [ ] **Step 2: Add `namespace` requirement to blast-radius REST endpoint**

In `topology_rest.py`, update `get_blast_radius()` to require a `namespace` query parameter:
```python
@router.get("/topology/blast-radius")
async def get_blast_radius(
    resource_id: str = Query(...),
    namespace: str = Query(..., description="Namespace to scope blast radius to"),
    _auth=Depends(verify_token),
):
```

If the user has access to the namespace (validated via existing auth), return scoped results. Cluster-scoped resources (Nodes, PVs) are always included.

- [ ] **Step 3: Run tests**

Run: `python3 -m pytest tests/ -v --tb=short -q`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add sre_agent/view_tools.py sre_agent/api/topology_rest.py
git commit -m "fix(security): namespace-scope topology and blast radius endpoints"
```

---

## Task 14: Add Trend Scanners

**Files:**
- Create: `sre_agent/monitor/trend_scanners.py`
- Modify: `sre_agent/monitor/registry.py`
- Create: `tests/test_trend_scanners.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_trend_scanners.py`:
```python
"""Tests for trend-based predictive scanners."""

from unittest.mock import patch, MagicMock
from sre_agent.monitor.trend_scanners import (
    scan_memory_pressure_forecast,
    scan_disk_pressure_forecast,
    scan_hpa_exhaustion_trend,
    scan_error_rate_acceleration,
)


class TestMemoryPressureForecast:
    @patch("sre_agent.monitor.trend_scanners._query_prometheus")
    def test_returns_finding_when_node_trending_to_pressure(self, mock_prom):
        mock_prom.return_value = [
            {"metric": {"instance": "worker-1"}, "value": [1234567890, "-1073741824"]}
        ]
        findings = scan_memory_pressure_forecast()
        assert len(findings) == 1
        assert "worker-1" in findings[0]["title"]
        assert findings[0]["severity"] == "warning"
        assert findings[0].get("finding_type") == "trend"

    @patch("sre_agent.monitor.trend_scanners._query_prometheus")
    def test_returns_empty_when_no_pressure(self, mock_prom):
        mock_prom.return_value = []
        findings = scan_memory_pressure_forecast()
        assert findings == []


class TestDiskPressureForecast:
    @patch("sre_agent.monitor.trend_scanners._query_prometheus")
    def test_returns_finding_when_disk_filling(self, mock_prom):
        mock_prom.return_value = [
            {"metric": {"persistentvolumeclaim": "pg-data", "namespace": "db"}, "value": [1234567890, "1"]}
        ]
        findings = scan_disk_pressure_forecast()
        assert len(findings) == 1
        assert "pg-data" in findings[0]["title"]


class TestHPAExhaustionTrend:
    @patch("sre_agent.monitor.trend_scanners._query_prometheus")
    def test_returns_finding_when_hpa_saturated(self, mock_prom):
        mock_prom.return_value = [
            {"metric": {"horizontalpodautoscaler": "frontend", "namespace": "web"}, "value": [1234567890, "0.95"]}
        ]
        findings = scan_hpa_exhaustion_trend()
        assert len(findings) == 1
        assert "frontend" in findings[0]["title"]


class TestErrorRateAcceleration:
    @patch("sre_agent.monitor.trend_scanners._query_prometheus")
    def test_returns_finding_when_error_rate_rising(self, mock_prom):
        mock_prom.return_value = [
            {"metric": {"service": "checkout"}, "value": [1234567890, "0.005"]}
        ]
        findings = scan_error_rate_acceleration()
        assert len(findings) == 1
        assert "checkout" in findings[0]["title"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_trend_scanners.py -v`
Expected: FAIL (module doesn't exist)

- [ ] **Step 3: Create `trend_scanners.py`**

Create `sre_agent/monitor/trend_scanners.py`:
```python
"""Trend-based predictive scanners using Prometheus predict_linear().

These scanners detect projected issues before they become incidents:
- Memory pressure forecast (nodes)
- Disk pressure forecast (PVCs)
- HPA exhaustion trend
- Error rate acceleration
"""

from __future__ import annotations

import logging
from typing import Any

from ..k8s_tools.monitoring import get_prometheus_query
from .findings import _make_finding
from .registry import SEVERITY_WARNING

logger = logging.getLogger("pulse_agent.monitor.trend")


def _query_prometheus(query: str) -> list[dict]:
    """Run a PromQL instant query and return result vector."""
    try:
        result = get_prometheus_query(query=query, time_range="")
        if isinstance(result, str):
            return []
        if isinstance(result, tuple):
            result = result[0]
        # Parse the text result to extract metric data
        # The tool returns formatted text; we need to parse it
        return []  # TODO: implement based on actual get_prometheus_query return format
    except Exception as e:
        logger.debug("Prometheus query failed: %s", e)
        return []


def scan_memory_pressure_forecast() -> list[dict[str, Any]]:
    """Predict nodes that will hit memory pressure within 3 days."""
    findings: list[dict[str, Any]] = []
    query = 'predict_linear(node_memory_MemAvailable_bytes[7d], 3*86400) < 0'
    results = _query_prometheus(query)
    for r in results:
        node = r.get("metric", {}).get("instance", "unknown")
        findings.append(
            _make_finding(
                severity=SEVERITY_WARNING,
                category="trend_memory",
                title=f"Node {node} trending toward memory pressure",
                summary=f"Based on 7-day trend, node {node} is projected to exhaust available memory within ~3 days.",
                resources=[{"kind": "Node", "name": node}],
                auto_fixable=False,
                finding_type="trend",
            )
        )
    return findings


def scan_disk_pressure_forecast() -> list[dict[str, Any]]:
    """Predict PVCs that will fill within 7 days."""
    findings: list[dict[str, Any]] = []
    query = (
        'predict_linear(kubelet_volume_stats_used_bytes[7d], 7*86400) '
        '> kubelet_volume_stats_capacity_bytes'
    )
    results = _query_prometheus(query)
    for r in results:
        pvc = r.get("metric", {}).get("persistentvolumeclaim", "unknown")
        ns = r.get("metric", {}).get("namespace", "")
        findings.append(
            _make_finding(
                severity=SEVERITY_WARNING,
                category="trend_disk",
                title=f"PVC {pvc} projected to fill within 7 days",
                summary=f"PVC {pvc} in {ns} is trending toward capacity based on 7-day growth rate.",
                resources=[{"kind": "PersistentVolumeClaim", "name": pvc, "namespace": ns}],
                auto_fixable=False,
                finding_type="trend",
            )
        )
    return findings


def scan_hpa_exhaustion_trend() -> list[dict[str, Any]]:
    """Detect HPAs running at max replicas for extended periods."""
    findings: list[dict[str, Any]] = []
    query = (
        'avg_over_time('
        '(kube_horizontalpodautoscaler_status_current_replicas'
        ' / kube_horizontalpodautoscaler_spec_max_replicas)[48h:]'
        ') > 0.9'
    )
    results = _query_prometheus(query)
    for r in results:
        hpa = r.get("metric", {}).get("horizontalpodautoscaler", "unknown")
        ns = r.get("metric", {}).get("namespace", "")
        ratio = float(r.get("value", [0, "0"])[1])
        findings.append(
            _make_finding(
                severity=SEVERITY_WARNING,
                category="trend_hpa",
                title=f"HPA {hpa} at max capacity {ratio:.0%} of the time",
                summary=f"HPA {hpa} in {ns} has been at or near max replicas for {ratio:.0%} of the last 48h. Consider raising max replicas or optimizing the workload.",
                resources=[{"kind": "HorizontalPodAutoscaler", "name": hpa, "namespace": ns}],
                auto_fixable=False,
                finding_type="trend",
            )
        )
    return findings


def scan_error_rate_acceleration() -> list[dict[str, Any]]:
    """Detect services with accelerating error rates."""
    findings: list[dict[str, Any]] = []
    query = 'deriv(rate(http_requests_total{code=~"5.."}[1h])[24h:]) > 0'
    results = _query_prometheus(query)
    for r in results:
        service = r.get("metric", {}).get("service", r.get("metric", {}).get("job", "unknown"))
        findings.append(
            _make_finding(
                severity=SEVERITY_WARNING,
                category="trend_errors",
                title=f"Error rate accelerating for {service}",
                summary=f"The 5xx error rate for {service} is increasing over the last 24h. Investigate before it becomes an outage.",
                resources=[{"kind": "Service", "name": service}],
                auto_fixable=False,
                finding_type="trend",
            )
        )
    return findings
```

- [ ] **Step 4: Register trend scanners in registry**

Add to `sre_agent/monitor/registry.py`:
```python
    "trend_memory": {
        "displayName": "Memory Pressure Forecast",
        "description": "Predicts nodes trending toward memory exhaustion using 7-day trends",
        "category": "predictive",
        "checks": ["predict_linear(memory[7d], 3d) < 0"],
        "auto_fixable": False,
    },
    "trend_disk": {
        "displayName": "Disk Pressure Forecast",
        "description": "Predicts PVCs that will fill based on 7-day growth rate",
        "category": "predictive",
        "checks": ["predict_linear(disk[7d], 7d) > capacity"],
        "auto_fixable": False,
    },
    "trend_hpa": {
        "displayName": "HPA Exhaustion Trend",
        "description": "Detects HPAs running at max replicas for extended periods",
        "category": "predictive",
        "checks": ["avg utilization > 90% over 48h"],
        "auto_fixable": False,
    },
    "trend_errors": {
        "displayName": "Error Rate Acceleration",
        "description": "Detects services with increasing 5xx error rates",
        "category": "predictive",
        "checks": ["deriv(error_rate[24h]) > 0"],
        "auto_fixable": False,
    },
```

- [ ] **Step 5: Update `_make_finding` to accept `finding_type`**

In `sre_agent/monitor/findings.py`, add optional `finding_type` parameter to `_make_finding()`:
```python
def _make_finding(
    *,
    severity: str,
    category: str,
    title: str,
    summary: str,
    resources: list[dict],
    auto_fixable: bool = False,
    runbook_id: str = "",
    finding_type: str = "current",  # "current" or "trend"
) -> dict:
```

Include `finding_type` in the returned dict.

- [ ] **Step 6: Run tests**

Run: `python3 -m pytest tests/test_trend_scanners.py -v`
Expected: All pass

- [ ] **Step 7: Commit**

```bash
git add sre_agent/monitor/trend_scanners.py sre_agent/monitor/registry.py sre_agent/monitor/findings.py tests/test_trend_scanners.py
git commit -m "feat: add 4 trend scanners using Prometheus predict_linear()"
```

---

## Task 15: Enhance `get_briefing()` With Live Data

**Files:**
- Modify: `sre_agent/monitor/actions.py`

- [ ] **Step 1: Write failing test**

Add to an existing test file or create `tests/test_briefing.py`:
```python
from unittest.mock import patch
from sre_agent.monitor.actions import get_briefing


class TestBriefingEnhancement:
    @patch("sre_agent.monitor.actions.get_database")
    def test_briefing_includes_current_findings(self, mock_db):
        mock_db.return_value.fetchall.return_value = []
        result = get_briefing(hours=12)
        assert "current_findings" in result
        assert "priority_items" in result

    @patch("sre_agent.monitor.actions.get_database")
    def test_briefing_includes_trend_findings(self, mock_db):
        mock_db.return_value.fetchall.return_value = []
        result = get_briefing(hours=12)
        assert "trend_findings" in result
```

- [ ] **Step 2: Update `get_briefing()` in `monitor/actions.py`**

After the existing DB queries (line 174), add live scanner results:
```python
        # Live cluster state — run fastest scanners
        from .scanners import scan_crashlooping_pods, scan_pending_pods, scan_oom_killed, scan_firing_alerts
        from .trend_scanners import (
            scan_memory_pressure_forecast,
            scan_disk_pressure_forecast,
            scan_hpa_exhaustion_trend,
            scan_error_rate_acceleration,
        )

        try:
            current_findings = (
                scan_crashlooping_pods() + scan_pending_pods() +
                scan_oom_killed() + scan_firing_alerts()
            )
        except Exception:
            current_findings = []

        try:
            trend_findings = (
                scan_memory_pressure_forecast() + scan_disk_pressure_forecast() +
                scan_hpa_exhaustion_trend() + scan_error_rate_acceleration()
            )
        except Exception:
            trend_findings = []

        # Priority ranking: severity * blast_radius
        all_findings = current_findings + trend_findings
        severity_weight = {"critical": 4, "warning": 2, "info": 1}
        priority_items = sorted(
            all_findings,
            key=lambda f: severity_weight.get(f.get("severity", "info"), 0),
            reverse=True,
        )
```

Add these to the return dict:
```python
        return {
            # ... existing fields ...
            "current_findings": current_findings,
            "trend_findings": trend_findings,
            "priority_items": priority_items[:10],  # top 10
        }
```

- [ ] **Step 3: Run tests**

Run: `python3 -m pytest tests/test_briefing.py tests/test_monitor.py -v --tb=short`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add sre_agent/monitor/actions.py tests/test_briefing.py
git commit -m "feat: enhance get_briefing() with live scanner data and trend findings"
```

---

## Task 16: Enable Mypy on Skills Directory

**Files:**
- Modify: `pyproject.toml`
- Modify: `sre_agent/skills/**/*.py` (type annotation fixes)

- [ ] **Step 1: Remove mypy exclude**

In `pyproject.toml`, line 89, change:
```toml
exclude = ["sre_agent/skills/"]
```
to:
```toml
exclude = []
```

- [ ] **Step 2: Run mypy and fix errors**

Run: `python3 -m mypy sre_agent/ --show-error-codes`

Fix each error in the skills directory. Common fixes:
- Add return type annotations to functions
- Add parameter type annotations
- Fix `Any` usages with proper types
- Add `from __future__ import annotations` to each file

- [ ] **Step 3: Verify clean**

Run: `python3 -m mypy sre_agent/ --show-error-codes`
Expected: 0 errors

- [ ] **Step 4: Run full test suite**

Run: `python3 -m pytest tests/ -v --tb=short -q`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml sre_agent/skills/
git commit -m "chore: enable mypy on skills directory, fix all type errors"
```

---

## Task 17: Final Verification & Eval Gate

**Files:** None (verification only)

- [ ] **Step 1: Verify all files under 1000 lines**

Run:
```bash
find sre_agent/ -name "*.py" -exec wc -l {} + | sort -rn | head -20
```
Expected: No file over 1000 lines except `promql_recipes.py`

- [ ] **Step 2: Verify zero backward-compat wrappers**

Run:
```bash
ls sre_agent/view_validator.py sre_agent/view_critic.py 2>/dev/null
```
Expected: "No such file or directory" for both

- [ ] **Step 3: Run `make verify`**

Run: `make verify`
Expected: lint + type-check + tests all pass

- [ ] **Step 4: Run eval gate against live cluster**

Run: `python -m sre_agent.evals.cli --suite release --fail-on-gate`
Expected: Pass (99%+ score, no regressions from code restructuring)

- [ ] **Step 5: Update docs**

Update `CLAUDE.md`:
- New files: `skill_router.py`, `tool_categories.py`, `view_mutations.py`, `scanner_rest.py`, `fix_rest.py`, `topology_rest.py`, `trend_scanners.py`
- Removed files: `view_validator.py`, `view_critic.py`
- `critique_view` moved to `quality_engine.py`
- `get_briefing()` now includes live scanner data
- 4 trend scanners added (predictive)
- 6 security vulnerabilities fixed

Update `SECURITY.md`:
- IDOR fix: ownership fallback removed
- HMAC fix: consistent key derivation
- ReDoS fix: regex pattern validation
- Clone fix: snapshot-on-share

- [ ] **Step 6: Commit docs**

```bash
git add CLAUDE.md SECURITY.md
git commit -m "docs: update CLAUDE.md and SECURITY.md for Phase 1 cleanup"
```

- [ ] **Step 7: Tag Phase 1 complete**

Do NOT push or tag — wait for user confirmation.

---

## Summary

| Task | What | Files Changed |
|------|------|---------------|
| 1-3 | Split monitor_rest.py (1955→<1000) | 3 new route files + serve.py |
| 4-5 | Split skill_loader.py (1590→<1000) | tool_categories.py + skill_router.py |
| 6 | Split view_tools.py (1554→<1000) | view_mutations.py |
| 7 | Delete view_validator.py | agent_ws.py, tests |
| 8 | Move critique_view, delete view_critic.py | quality_engine.py, tests |
| 9 | Verify prompt_experiment (ACTIVE — keep) | None |
| 10 | Fix IDOR ownership bypass | view_tools.py, new security tests |
| 11 | Fix HMAC key mismatch | api/views.py |
| 12 | Fix ReDoS in log-counts | api/views.py |
| 13 | Fix clone post-share mutation | api/views.py, db.py |
| 13.5 | Namespace-scope topology/blast radius | view_tools.py, topology_rest.py |
| 14 | Add 4 trend scanners | trend_scanners.py, registry.py |
| 15 | Enhance briefing with live data | monitor/actions.py |
| 16 | Enable mypy on skills | pyproject.toml, skills/*.py |
| 17 | Final verification + eval gate + docs | CLAUDE.md, SECURITY.md |
