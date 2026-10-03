# Phase 3A: Agent View Schema & CRUD — Implementation Plan

> Historical design/implementation record. This preserves the proposal and tasks at the date in the filename; unchecked tasks, paths, commands, and security claims are not evidence of the current implementation. Use [current documentation](../../README.md) and verify behavior against code before applying a procedure.


> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add view_type, status, visibility, trigger_source, finding_id, cluster_id, claimed_by, claimed_at columns to the views table, update all CRUD operations to handle them, add REST endpoints for filtering, status transitions, and claiming.

**Architecture:** Migration 019 adds 8 columns with safe defaults (existing views become type=custom, status=active, visibility=private). DB functions get new optional parameters. REST endpoints get query filters. Frontend gets TypeScript types for the new fields (rendering changes are Phase 3C).

**Tech Stack:** Python 3.11 (FastAPI, PostgreSQL, psycopg2), TypeScript

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `sre_agent/db_migrations.py` | Modify | Add migration 019 (8 columns on views table) |
| `sre_agent/db.py` | Modify | Update save_view, list_views, get_view, update_view + new transition/claim functions |
| `sre_agent/api/views.py` | Modify | Add filter params to GET /views, add status transition + claim endpoints |
| `tests/test_views.py` | Modify | Tests for new columns in CRUD operations |
| `tests/test_view_lifecycle.py` | Create | Tests for status transitions, claiming, visibility filtering |
| `src/kubeview/engine/agentComponents.ts` | Modify (OpenshiftPulse) | Update ViewSpec with new fields |

---

### Task 1: Migration 019 — add agent view columns

**Files:**
- Modify: `sre_agent/db_migrations.py:203`

- [ ] **Step 1: Write the migration function**

Add before the `MIGRATIONS` list:

```python
def _migrate_019_agent_views(db: Database) -> None:
    """Add agent view columns: type, status, visibility, trigger, finding, cluster, claim."""
    for col, typ, default in [
        ("view_type", "TEXT", "'custom'"),
        ("status", "TEXT", "'active'"),
        ("trigger_source", "TEXT", "'user'"),
        ("finding_id", "TEXT", None),
        ("cluster_id", "TEXT", "''"),
        ("claimed_by", "TEXT", None),
        ("claimed_at", "TEXT", None),
        ("visibility", "TEXT", "'private'"),
    ]:
        try:
            default_clause = f" DEFAULT {default}" if default else ""
            not_null = " NOT NULL" if default else ""
            db.execute(f"ALTER TABLE views ADD COLUMN {col} {typ}{not_null}{default_clause}")
        except Exception:
            pass
    db.commit()
```

Register it in the `MIGRATIONS` list:

```python
    (19, "agent_views", _migrate_019_agent_views),
```

- [ ] **Step 2: Update db_schema.py**

Update `VIEWS_SCHEMA` to include the new columns so fresh installs get them:

```python
VIEWS_SCHEMA = """
CREATE TABLE IF NOT EXISTS views (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    icon TEXT DEFAULT '',
    layout TEXT NOT NULL,
    positions TEXT DEFAULT '{}',
    view_type TEXT NOT NULL DEFAULT 'custom',
    status TEXT NOT NULL DEFAULT 'active',
    trigger_source TEXT NOT NULL DEFAULT 'user',
    finding_id TEXT,
    cluster_id TEXT NOT NULL DEFAULT '',
    claimed_by TEXT,
    claimed_at TEXT,
    visibility TEXT NOT NULL DEFAULT 'private',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""
```

- [ ] **Step 3: Run tests to verify migration doesn't break existing views**

Run: `python3 -m pytest tests/test_views.py -v`
Expected: All PASS (existing tests use fresh schema which now includes new columns with defaults)

- [ ] **Step 4: Commit**

```bash
git add sre_agent/db_migrations.py sre_agent/db_schema.py
git commit -m "feat: add migration 019 — agent view columns (type, status, visibility, claim)"
```

---

### Task 2: Update save_view to accept new fields

**Files:**
- Modify: `sre_agent/db.py:267`
- Modify: `tests/test_views.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_views.py — add to TestSaveView class

def test_saves_with_view_type_and_status(self):
    result = db_module.save_view(
        "alice", "cv-incident-1", "CrashLoop Investigation", "desc", _layout(),
        view_type="incident", status="investigating", trigger_source="monitor",
        finding_id="f-123", visibility="team",
    )
    assert result == "cv-incident-1"
    view = db_module.get_view("cv-incident-1", "alice")
    assert view["view_type"] == "incident"
    assert view["status"] == "investigating"
    assert view["trigger_source"] == "monitor"
    assert view["finding_id"] == "f-123"
    assert view["visibility"] == "team"

def test_save_defaults_to_custom(self):
    db_module.save_view("alice", "cv-default", "Default View", "desc", _layout())
    view = db_module.get_view("cv-default", "alice")
    assert view["view_type"] == "custom"
    assert view["status"] == "active"
    assert view["visibility"] == "private"
    assert view["trigger_source"] == "user"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_views.py::TestSaveView::test_saves_with_view_type_and_status -v`
Expected: FAIL — save_view doesn't accept view_type parameter

- [ ] **Step 3: Update save_view**

```python
def save_view(
    owner: str,
    view_id: str,
    title: str,
    description: str,
    layout: list,
    positions: dict | None = None,
    icon: str = "",
    *,
    view_type: str = "custom",
    status: str = "active",
    trigger_source: str = "user",
    finding_id: str | None = None,
    visibility: str = "private",
) -> str | None:
    """Save a new view for a user. Returns the view ID."""
    import json
    from datetime import UTC, datetime

    db = get_database()
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO views (id, owner, title, description, icon, layout, positions, "
        "view_type, status, trigger_source, finding_id, visibility, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (id) DO UPDATE SET "
        "title = EXCLUDED.title, description = EXCLUDED.description, icon = EXCLUDED.icon, "
        "layout = EXCLUDED.layout, positions = EXCLUDED.positions, updated_at = EXCLUDED.updated_at "
        "WHERE views.owner = EXCLUDED.owner",
        (
            view_id, owner, title, description, icon,
            json.dumps(layout), json.dumps(positions or {}),
            view_type, status, trigger_source, finding_id, visibility,
            now, now,
        ),
    )
    db.commit()

    try:
        snapshot_view(view_id, "created")
    except Exception:
        pass

    return view_id
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_views.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/db.py tests/test_views.py
git commit -m "feat: save_view accepts view_type, status, trigger_source, finding_id, visibility"
```

---

### Task 3: Update list_views with filtering

**Files:**
- Modify: `sre_agent/db.py:298`
- Modify: `tests/test_views.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_views.py — new class

class TestListViewsFiltering:
    def test_filter_by_view_type(self):
        db_module.save_view("alice", "cv-1", "Custom View", "", _layout())
        db_module.save_view(
            "alice", "cv-2", "Incident View", "", _layout(),
            view_type="incident", status="investigating", visibility="team",
        )
        views = db_module.list_views("alice", view_type="incident")
        assert len(views) == 1
        assert views[0]["view_type"] == "incident"

    def test_filter_by_visibility_team(self):
        db_module.save_view("alice", "cv-1", "Private", "", _layout(), visibility="private")
        db_module.save_view(
            "alice", "cv-2", "Team Incident", "", _layout(),
            view_type="incident", visibility="team",
        )
        # Bob should see team views
        views = db_module.list_views("bob", visibility="team")
        assert len(views) == 1
        assert views[0]["id"] == "cv-2"

    def test_filter_excludes_status(self):
        db_module.save_view(
            "alice", "cv-1", "Active", "", _layout(),
            view_type="plan", status="analyzing", visibility="team",
        )
        db_module.save_view(
            "alice", "cv-2", "Done", "", _layout(),
            view_type="plan", status="completed", visibility="team",
        )
        views = db_module.list_views("alice", view_type="plan", exclude_status="completed")
        assert len(views) == 1
        assert views[0]["status"] == "analyzing"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_views.py::TestListViewsFiltering -v`
Expected: FAIL

- [ ] **Step 3: Update list_views**

```python
@_db_safe
def list_views(
    owner: str,
    limit: int = 50,
    *,
    view_type: str | None = None,
    visibility: str | None = None,
    exclude_status: str | None = None,
) -> list[dict]:
    """List views. By default returns owner's views. With visibility='team', returns all team-visible views."""
    db = get_database()
    conditions = []
    params: list = []

    if visibility == "team":
        conditions.append("visibility = 'team'")
    else:
        conditions.append("owner = ?")
        params.append(owner)

    if view_type:
        conditions.append("view_type = ?")
        params.append(view_type)
    if exclude_status:
        conditions.append("status != ?")
        params.append(exclude_status)

    where = " AND ".join(conditions)
    params.append(min(limit, 50))

    rows = db.fetchall(
        "SELECT id, owner, title, description, icon, layout, positions, "
        "view_type, status, trigger_source, finding_id, cluster_id, "
        "claimed_by, claimed_at, visibility, created_at, updated_at "
        f"FROM views WHERE {where} ORDER BY updated_at DESC LIMIT ?",
        tuple(params),
    )
    return [_deserialize_view_row(row) for row in rows]
```

Also update `get_view`, `get_view_by_title` to select the new columns:

```python
# In get_view and get_view_by_title, the SELECT * or explicit column list
# needs to include the new columns. get_view already uses SELECT *, so it works.
# get_view_by_title and list_views need the explicit columns updated.
```

Update `get_view_by_title`:
```python
@_db_safe
def get_view_by_title(owner: str, title: str) -> dict | None:
    db = get_database()
    row = db.fetchone(
        "SELECT * FROM views WHERE owner = ? AND title = ? LIMIT 1",
        (owner, title),
    )
    return _deserialize_view_row(row) if row else None
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_views.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/db.py tests/test_views.py
git commit -m "feat: list_views supports view_type, visibility, exclude_status filters"
```

---

### Task 4: Status transition and claim functions

**Files:**
- Modify: `sre_agent/db.py`
- Create: `tests/test_view_lifecycle.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for view lifecycle — status transitions and claim mechanism."""

from __future__ import annotations

import pytest

from sre_agent import db as db_module
from sre_agent.db import Database, reset_database, set_database


@pytest.fixture(autouse=True)
def _view_db():
    from tests.conftest import _TEST_DB_URL
    from sre_agent.db_schema import ALL_SCHEMAS

    test_db = Database(_TEST_DB_URL)
    test_db.execute("DROP TABLE IF EXISTS view_versions CASCADE")
    test_db.execute("DROP TABLE IF EXISTS views CASCADE")
    test_db.commit()
    test_db.executescript(ALL_SCHEMAS)
    set_database(test_db)
    yield test_db
    reset_database()


def _layout():
    return [{"kind": "data_table", "title": "Pods", "columns": [], "rows": []}]


def _create_incident():
    db_module.save_view(
        "alice", "cv-inc-1", "CrashLoop", "desc", _layout(),
        view_type="incident", status="investigating", visibility="team",
        trigger_source="monitor", finding_id="f-crash-1",
    )
    return "cv-inc-1"


class TestStatusTransition:
    def test_valid_incident_transition(self):
        view_id = _create_incident()
        ok = db_module.transition_view_status(view_id, "alice", "action_taken")
        assert ok is True
        view = db_module.get_view(view_id)
        assert view["status"] == "action_taken"

    def test_invalid_transition_rejected(self):
        view_id = _create_incident()
        ok = db_module.transition_view_status(view_id, "alice", "completed")
        assert ok is False
        view = db_module.get_view(view_id)
        assert view["status"] == "investigating"

    def test_custom_views_cannot_transition(self):
        db_module.save_view("alice", "cv-custom", "Custom", "", _layout())
        ok = db_module.transition_view_status("cv-custom", "alice", "resolved")
        assert ok is False

    def test_transition_creates_version(self):
        view_id = _create_incident()
        db_module.transition_view_status(view_id, "alice", "action_taken")
        versions = db_module.list_view_versions(view_id)
        actions = [v["action"] for v in versions]
        assert any("action_taken" in a for a in actions)


class TestClaimView:
    def test_claim_view(self):
        view_id = _create_incident()
        ok = db_module.claim_view(view_id, "bob")
        assert ok is True
        view = db_module.get_view(view_id)
        assert view["claimed_by"] == "bob"
        assert view["claimed_at"] is not None

    def test_unclaim_view(self):
        view_id = _create_incident()
        db_module.claim_view(view_id, "bob")
        ok = db_module.unclaim_view(view_id, "bob")
        assert ok is True
        view = db_module.get_view(view_id)
        assert view["claimed_by"] is None

    def test_claim_private_view_denied(self):
        db_module.save_view("alice", "cv-priv", "Private", "", _layout())
        ok = db_module.claim_view("cv-priv", "bob")
        assert ok is False


class TestFindByFinding:
    def test_find_view_by_finding_id(self):
        _create_incident()
        view = db_module.get_view_by_finding("f-crash-1")
        assert view is not None
        assert view["id"] == "cv-inc-1"

    def test_find_returns_none_for_unknown(self):
        view = db_module.get_view_by_finding("nonexistent")
        assert view is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_view_lifecycle.py -v`
Expected: FAIL — functions don't exist

- [ ] **Step 3: Implement the functions**

Add to `sre_agent/db.py`:

```python
# Valid status transitions per view_type
_STATUS_TRANSITIONS: dict[str, dict[str, set[str]]] = {
    "incident": {
        "investigating": {"action_taken"},
        "action_taken": {"verifying"},
        "verifying": {"resolved", "investigating"},
        "resolved": {"investigating", "archived"},
    },
    "plan": {
        "analyzing": {"ready"},
        "ready": {"executing"},
        "executing": {"ready", "completed"},
    },
    "assessment": {
        "analyzing": {"ready"},
        "ready": {"acknowledged", "investigating"},
    },
}


@_db_safe
def transition_view_status(view_id: str, actor: str, new_status: str) -> bool:
    """Transition a view's status. Validates the transition is legal. Creates a version snapshot."""
    from datetime import UTC, datetime

    db = get_database()
    row = db.fetchone("SELECT view_type, status FROM views WHERE id = ?", (view_id,))
    if not row:
        return False

    view_type = row["view_type"]
    current_status = row["status"]
    allowed = _STATUS_TRANSITIONS.get(view_type, {}).get(current_status, set())
    if new_status not in allowed:
        return False

    try:
        snapshot_view(view_id, f"status:{new_status}")
    except Exception:
        pass

    cursor = db.execute(
        "UPDATE views SET status = ?, updated_at = ? WHERE id = ?",
        (new_status, datetime.now(UTC).isoformat(), view_id),
    )
    db.commit()
    return getattr(cursor, "rowcount", 1) > 0


@_db_safe
def claim_view(view_id: str, username: str) -> bool:
    """Claim a team-visible view. Only team views can be claimed."""
    from datetime import UTC, datetime

    db = get_database()
    cursor = db.execute(
        "UPDATE views SET claimed_by = ?, claimed_at = ? WHERE id = ? AND visibility = 'team'",
        (username, datetime.now(UTC).isoformat(), view_id),
    )
    db.commit()
    return getattr(cursor, "rowcount", 1) > 0


@_db_safe
def unclaim_view(view_id: str, username: str) -> bool:
    """Release a claim on a view. Only the claimant can unclaim."""
    db = get_database()
    cursor = db.execute(
        "UPDATE views SET claimed_by = NULL, claimed_at = NULL WHERE id = ? AND claimed_by = ?",
        (view_id, username),
    )
    db.commit()
    return getattr(cursor, "rowcount", 1) > 0


@_db_safe
def get_view_by_finding(finding_id: str) -> dict | None:
    """Find a view linked to a monitor finding. Returns the most recent match."""
    db = get_database()
    row = db.fetchone(
        "SELECT * FROM views WHERE finding_id = ? ORDER BY updated_at DESC LIMIT 1",
        (finding_id,),
    )
    return _deserialize_view_row(row) if row else None
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_view_lifecycle.py -v`
Expected: All PASS

- [ ] **Step 5: Run all view tests**

Run: `python3 -m pytest tests/test_views.py tests/test_view_lifecycle.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add sre_agent/db.py tests/test_view_lifecycle.py
git commit -m "feat: add status transitions, claim/unclaim, get_view_by_finding"
```

---

### Task 5: REST endpoints — filtering, transitions, claims

**Files:**
- Modify: `sre_agent/api/views.py`
- Create: `tests/test_view_lifecycle_api.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for view lifecycle REST endpoints."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sre_agent.api.auth import get_owner
from sre_agent.api.views import router


def _mock_owner():
    return "testuser"


@pytest.fixture
def app():
    a = FastAPI()
    a.include_router(router)
    a.dependency_overrides[get_owner] = _mock_owner
    yield a
    a.dependency_overrides.clear()


@pytest.fixture
def client(app):
    return TestClient(app)


class TestListViewsFilters:
    def test_filter_by_view_type(self, client):
        with patch("sre_agent.db.list_views", return_value=[]) as mock:
            client.get("/views?view_type=incident")
            mock.assert_called_once_with("testuser", view_type="incident", visibility=None, exclude_status=None)

    def test_filter_by_visibility(self, client):
        with patch("sre_agent.db.list_views", return_value=[]) as mock:
            client.get("/views?visibility=team")
            mock.assert_called_once_with("testuser", view_type=None, visibility="team", exclude_status=None)

    def test_filter_exclude_status(self, client):
        with patch("sre_agent.db.list_views", return_value=[]) as mock:
            client.get("/views?view_type=plan&exclude_status=completed")
            mock.assert_called_once_with("testuser", view_type="plan", visibility=None, exclude_status="completed")


class TestStatusTransitionEndpoint:
    def test_valid_transition(self, client):
        with patch("sre_agent.db.transition_view_status", return_value=True):
            resp = client.post("/views/cv-1/status", json={"status": "action_taken"})
        assert resp.status_code == 200
        assert resp.json()["transitioned"] is True

    def test_invalid_transition(self, client):
        with patch("sre_agent.db.transition_view_status", return_value=False):
            resp = client.post("/views/cv-1/status", json={"status": "completed"})
        assert resp.status_code == 409

    def test_missing_status_field(self, client):
        resp = client.post("/views/cv-1/status", json={})
        assert resp.status_code == 400


class TestClaimEndpoint:
    def test_claim(self, client):
        with patch("sre_agent.db.claim_view", return_value=True):
            resp = client.post("/views/cv-1/claim")
        assert resp.status_code == 200

    def test_claim_denied(self, client):
        with patch("sre_agent.db.claim_view", return_value=False):
            resp = client.post("/views/cv-1/claim")
        assert resp.status_code == 409

    def test_unclaim(self, client):
        with patch("sre_agent.db.unclaim_view", return_value=True):
            resp = client.delete("/views/cv-1/claim")
        assert resp.status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_view_lifecycle_api.py -v`
Expected: FAIL

- [ ] **Step 3: Implement the endpoints**

Update `GET /views` to accept query params:

```python
@router.get("/views")
async def rest_list_views(
    owner: str = Depends(get_owner),
    view_type: str | None = Query(None),
    visibility: str | None = Query(None),
    exclude_status: str | None = Query(None),
):
    """List views with optional filtering by type, visibility, and status."""
    from .. import db

    views = db.list_views(owner, view_type=view_type, visibility=visibility, exclude_status=exclude_status)
    return {"views": views or [], "owner": owner}
```

Add status transition endpoint:

```python
@router.post("/views/{view_id}/status")
async def rest_transition_status(
    view_id: str,
    request: Request,
    owner: str = Depends(get_owner),
):
    """Transition a view's status."""
    from fastapi.responses import JSONResponse

    from .. import db

    body = await request.json()
    new_status = body.get("status")
    if not new_status:
        return JSONResponse(status_code=400, content={"error": "Missing 'status' field"})

    ok = db.transition_view_status(view_id, owner, new_status)
    if not ok:
        return JSONResponse(status_code=409, content={"error": "Invalid status transition"})
    return {"transitioned": True, "view_id": view_id, "status": new_status}
```

Add claim/unclaim endpoints:

```python
@router.post("/views/{view_id}/claim")
async def rest_claim_view(
    view_id: str,
    owner: str = Depends(get_owner),
):
    """Claim a team view."""
    from fastapi.responses import JSONResponse

    from .. import db

    ok = db.claim_view(view_id, owner)
    if not ok:
        return JSONResponse(status_code=409, content={"error": "Cannot claim this view"})
    return {"claimed": True, "view_id": view_id, "claimed_by": owner}


@router.delete("/views/{view_id}/claim")
async def rest_unclaim_view(
    view_id: str,
    owner: str = Depends(get_owner),
):
    """Release a claim on a view."""
    from fastapi.responses import JSONResponse

    from .. import db

    ok = db.unclaim_view(view_id, owner)
    if not ok:
        return JSONResponse(status_code=409, content={"error": "Cannot unclaim — you don't hold the claim"})
    return {"unclaimed": True, "view_id": view_id}
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_view_lifecycle_api.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/api/views.py tests/test_view_lifecycle_api.py
git commit -m "feat: REST endpoints for view filtering, status transitions, claim/unclaim"
```

---

### Task 6: Update API_CONTRACT.md and frontend types

**Files:**
- Modify: `API_CONTRACT.md`
- Modify: `src/kubeview/engine/agentComponents.ts` (OpenshiftPulse)

- [ ] **Step 1: Add new endpoints to API contract**

Add to the views endpoint table:

```markdown
| `POST` | `/views/:id/status` | token + owner | Transition view status |
| `POST` | `/views/:id/claim` | token + owner | Claim a team view |
| `DELETE` | `/views/:id/claim` | token + owner | Release a claim |
```

Add filter params documentation for `GET /views`:

```markdown
Query params: `view_type` (incident|plan|assessment|custom), `visibility` (private|team), `exclude_status` (status to exclude)
```

- [ ] **Step 2: Update frontend ViewSpec type**

In `src/kubeview/engine/agentComponents.ts`, update `ViewSpec`:

```typescript
export interface ViewSpec {
  id: string;
  title: string;
  icon?: string;
  description?: string;
  layout: ComponentSpec[];
  positions?: Record<number, { x: number; y: number; w: number; h: number }>;
  generatedAt: number;
  owner?: string;
  templateId?: string;
  view_type?: 'custom' | 'incident' | 'plan' | 'assessment';
  status?: string;
  trigger_source?: 'user' | 'monitor' | 'agent';
  finding_id?: string;
  claimed_by?: string;
  claimed_at?: string;
  visibility?: 'private' | 'team';
}
```

- [ ] **Step 3: Verify TypeScript compiles**

Run: `npx --prefix /Users/amobrem/ali/OpenshiftPulse tsc --project /Users/amobrem/ali/OpenshiftPulse/tsconfig.json --noEmit`
Expected: No errors

- [ ] **Step 4: Commit both repos**

```bash
# Backend
git add API_CONTRACT.md
git commit -m "docs: add view lifecycle endpoints to API_CONTRACT.md"

# Frontend
cd /Users/amobrem/ali/OpenshiftPulse
git add src/kubeview/engine/agentComponents.ts
git commit -m "feat: add view_type, status, visibility fields to ViewSpec"
```

---

### Task 7: Update update_view to allow status/visibility changes

**Files:**
- Modify: `sre_agent/db.py:335`
- Modify: `tests/test_views.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_views.py — add to a new class

class TestUpdateViewNewFields:
    def test_update_visibility(self):
        db_module.save_view(
            "alice", "cv-vis", "View", "", _layout(),
            view_type="incident", visibility="team",
        )
        ok = db_module.update_view("cv-vis", "alice", visibility="private")
        assert ok is True
        view = db_module.get_view("cv-vis", "alice")
        assert view["visibility"] == "private"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_views.py::TestUpdateViewNewFields -v`
Expected: FAIL — visibility not in allowed set

- [ ] **Step 3: Add new fields to allowed set in update_view**

In `sre_agent/db.py` `update_view()`, change:

```python
allowed = {"title", "description", "icon", "layout", "positions", "visibility", "status", "view_type"}
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_views.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/db.py tests/test_views.py
git commit -m "feat: update_view allows visibility, status, view_type changes"
```

---

### Task 8: Full verification + CLAUDE.md update

**Files:** None (verification) + `CLAUDE.md`

- [ ] **Step 1: Run full backend test suite**

Run: `python3 -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 2: Run mypy**

Run: `python3 -m mypy sre_agent/ --ignore-missing-imports`
Expected: 0 errors

- [ ] **Step 3: Run ruff**

Run: `python3 -m ruff check sre_agent/`
Expected: Clean

- [ ] **Step 4: Run frontend tsc**

Run: `npx --prefix /Users/amobrem/ali/OpenshiftPulse tsc --project /Users/amobrem/ali/OpenshiftPulse/tsconfig.json --noEmit`
Expected: No errors

- [ ] **Step 5: Update CLAUDE.md**

Update migration version from 017 to 019. Add view lifecycle description. Update key files section for db.py.

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for Phase 3A — migration 019, view lifecycle"
```
