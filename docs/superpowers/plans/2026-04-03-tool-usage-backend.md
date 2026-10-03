# Tool Usage Tracking — Backend Implementation Plan

> Historical design/implementation record. This preserves the proposal and tasks at the date in the filename; unchecked tasks, paths, commands, and security claims are not evidence of the current implementation. Use [current documentation](../../README.md) and verify behavior against code before applying a procedure.


> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record every tool invocation to PostgreSQL and expose query/stats endpoints for the UI.

**Architecture:** New `tool_usage.py` module handles all DB operations (schema, recording, querying). The `api.py` WebSocket handler hooks into the existing `on_tool_result` callback to fire-and-forget audit writes. New REST endpoints serve paginated logs, aggregated stats, and agent metadata. A DB migration adds two new tables (`tool_usage`, `tool_turns`).

**Tech Stack:** Python 3.11, PostgreSQL, FastAPI, psycopg2, pytest

**Out of scope (separate plan):** Tool chain intelligence (discovery, hints, recipes, preloading), frontend UI, `tool_chains.py`.

---

## File Structure

| File | Responsibility |
|------|---------------|
| `sre_agent/tool_usage.py` (create) | Schema constants, `record_tool_call()`, `record_turn()`, `update_turn_feedback()`, `query_usage()`, `get_usage_stats()`, `get_agents_metadata()`, input sanitization |
| `sre_agent/db_schema.py` (modify) | Add `TOOL_USAGE_SCHEMA`, `TOOL_TURNS_SCHEMA` constants to `ALL_SCHEMAS` |
| `sre_agent/db_migrations.py` (modify) | Add migration 002 for new tables |
| `sre_agent/api.py` (modify) | Wire `on_tool_result` in WebSocket handlers, add `/agents`, `/tools/usage`, `/tools/usage/stats` endpoints, enhance `/tools`, link feedback |
| `tests/test_tool_usage.py` (create) | DB functions, recording, querying, stats |
| `tests/test_api_tools.py` (create) | REST endpoint tests |
| `API_CONTRACT.md` (modify) | Document new endpoints |

---

### Task 1: Add tool_usage and tool_turns tables to DB schema

**Files:**
- Modify: `sre_agent/db_schema.py`
- Modify: `sre_agent/db_migrations.py`
- Test: `tests/test_tool_usage.py` (create)

- [ ] **Step 1: Write failing test for table creation**

Create `tests/test_tool_usage.py`:

```python
"""Tests for tool usage tracking — DB functions, recording, querying."""

from __future__ import annotations

from sre_agent.db import Database, reset_database, set_database
from sre_agent.db_migrations import run_migrations

from .conftest import _TEST_DB_URL


def _make_test_db() -> Database:
    db = Database(_TEST_DB_URL)
    db.execute("DROP TABLE IF EXISTS tool_usage CASCADE")
    db.execute("DROP TABLE IF EXISTS tool_turns CASCADE")
    db.commit()
    return db


class TestToolUsageTables:
    def test_migration_creates_tables(self):
        db = _make_test_db()
        # Reset migrations so 002 re-runs
        db.execute("DELETE FROM schema_migrations WHERE version >= 2")
        db.commit()
        set_database(db)
        run_migrations(db)

        # Verify tool_usage table exists
        row = db.fetchone(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'tool_usage') AS exists"
        )
        assert row["exists"] is True

        # Verify tool_turns table exists
        row = db.fetchone(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'tool_turns') AS exists"
        )
        assert row["exists"] is True
        reset_database()

    def test_tool_usage_insert(self):
        db = _make_test_db()
        db.execute("DELETE FROM schema_migrations WHERE version >= 2")
        db.commit()
        set_database(db)
        run_migrations(db)

        db.execute(
            "INSERT INTO tool_usage (session_id, turn_number, agent_mode, tool_name, tool_category, "
            "input_summary, status, duration_ms, result_bytes, requires_confirmation, was_confirmed) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            ("sess-1", 1, "sre", "list_pods", "diagnostics", '{"namespace": "default"}',
             "success", 342, 4820, False, None),
        )
        db.commit()

        row = db.fetchone("SELECT * FROM tool_usage WHERE session_id = %s", ("sess-1",))
        assert row is not None
        assert row["tool_name"] == "list_pods"
        assert row["status"] == "success"
        assert row["duration_ms"] == 342
        reset_database()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_tool_usage.py::TestToolUsageTables -v`
Expected: FAIL — table does not exist or migration not found

- [ ] **Step 3: Add schema constants to db_schema.py**

Add to `sre_agent/db_schema.py` before `INDEX_SCHEMA`:

```python
TOOL_USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_usage (
    id              SERIAL PRIMARY KEY,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    session_id      TEXT NOT NULL,
    turn_number     INTEGER NOT NULL,
    agent_mode      TEXT NOT NULL,
    tool_name       TEXT NOT NULL,
    tool_category   TEXT,
    input_summary   JSONB,
    status          TEXT NOT NULL,
    error_message   TEXT,
    error_category  TEXT,
    duration_ms     INTEGER,
    result_bytes    INTEGER,
    requires_confirmation BOOLEAN DEFAULT FALSE,
    was_confirmed   BOOLEAN
);
"""

TOOL_TURNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_turns (
    id              SERIAL PRIMARY KEY,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    session_id      TEXT NOT NULL,
    turn_number     INTEGER NOT NULL,
    agent_mode      TEXT NOT NULL,
    query_summary   TEXT,
    tools_offered   TEXT[],
    tools_called    TEXT[],
    feedback        TEXT,
    UNIQUE(session_id, turn_number)
);
"""

TOOL_USAGE_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_tool_usage_timestamp ON tool_usage(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_tool_usage_tool_name ON tool_usage(tool_name);
CREATE INDEX IF NOT EXISTS idx_tool_usage_session ON tool_usage(session_id);
CREATE INDEX IF NOT EXISTS idx_tool_usage_mode ON tool_usage(agent_mode);
CREATE INDEX IF NOT EXISTS idx_tool_usage_status ON tool_usage(status);
CREATE INDEX IF NOT EXISTS idx_tool_turns_session ON tool_turns(session_id);
CREATE INDEX IF NOT EXISTS idx_tool_turns_feedback ON tool_turns(feedback) WHERE feedback IS NOT NULL;
"""
```

Add them to `ALL_SCHEMAS`:

```python
ALL_SCHEMAS = (
    INCIDENTS_SCHEMA
    + RUNBOOKS_SCHEMA
    + PATTERNS_SCHEMA
    + METRICS_SCHEMA
    + ACTIONS_SCHEMA
    + INVESTIGATIONS_SCHEMA
    + CONTEXT_ENTRIES_SCHEMA
    + FINDINGS_SCHEMA
    + VIEWS_SCHEMA
    + VIEW_VERSIONS_SCHEMA
    + TOOL_USAGE_SCHEMA
    + TOOL_TURNS_SCHEMA
    + INDEX_SCHEMA
    + TOOL_USAGE_INDEX_SCHEMA
)
```

- [ ] **Step 4: Add migration 002 to db_migrations.py**

```python
def _migrate_002_tool_usage(db: Database) -> None:
    """Add tool_usage and tool_turns tables for tool call tracking."""
    from .db_schema import TOOL_TURNS_SCHEMA, TOOL_USAGE_INDEX_SCHEMA, TOOL_USAGE_SCHEMA

    db.executescript(TOOL_USAGE_SCHEMA + TOOL_TURNS_SCHEMA + TOOL_USAGE_INDEX_SCHEMA)


MIGRATIONS = [
    (1, "baseline", _migrate_001_baseline),
    (2, "tool_usage", _migrate_002_tool_usage),
]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_tool_usage.py::TestToolUsageTables -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add sre_agent/db_schema.py sre_agent/db_migrations.py tests/test_tool_usage.py
git commit -m "feat: add tool_usage and tool_turns tables with migration"
```

---

### Task 2: Create tool_usage.py — recording functions

**Files:**
- Create: `sre_agent/tool_usage.py`
- Modify: `tests/test_tool_usage.py`

- [ ] **Step 1: Write failing tests for record_tool_call and record_turn**

Add to `tests/test_tool_usage.py`:

```python
from sre_agent.tool_usage import record_tool_call, record_turn, sanitize_input


class TestSanitizeInput:
    def test_strips_secret_fields(self):
        result = sanitize_input({"namespace": "prod", "token": "abc123", "password": "hunter2"})
        assert result["namespace"] == "prod"
        assert "abc123" not in str(result)
        assert "hunter2" not in str(result)

    def test_truncates_long_values(self):
        result = sanitize_input({"data": "x" * 500})
        assert len(result["data"]) <= 260  # 256 + "..."

    def test_caps_total_size(self):
        big = {f"key_{i}": "v" * 200 for i in range(20)}
        result = sanitize_input(big)
        import json
        assert len(json.dumps(result)) <= 1100  # ~1KB with some margin

    def test_empty_input(self):
        assert sanitize_input({}) == {}

    def test_none_input(self):
        assert sanitize_input(None) is None


class TestRecordToolCall:
    def setup_method(self):
        self.db = _make_test_db()
        db2 = Database(_TEST_DB_URL)
        db2.execute("DELETE FROM schema_migrations WHERE version >= 2")
        db2.commit()
        db2.close()
        set_database(self.db)
        run_migrations(self.db)

    def teardown_method(self):
        reset_database()

    def test_records_successful_call(self):
        record_tool_call(
            session_id="s1",
            turn_number=1,
            agent_mode="sre",
            tool_name="list_pods",
            tool_category="diagnostics",
            input_data={"namespace": "default"},
            status="success",
            error_message=None,
            error_category=None,
            duration_ms=100,
            result_bytes=500,
            requires_confirmation=False,
            was_confirmed=None,
        )
        rows = self.db.fetchall("SELECT * FROM tool_usage WHERE session_id = %s", ("s1",))
        assert len(rows) == 1
        assert rows[0]["tool_name"] == "list_pods"
        assert rows[0]["status"] == "success"
        assert rows[0]["duration_ms"] == 100

    def test_records_error_call(self):
        record_tool_call(
            session_id="s2",
            turn_number=1,
            agent_mode="sre",
            tool_name="bad_tool",
            tool_category=None,
            input_data={},
            status="error",
            error_message="RuntimeError: failed",
            error_category="server",
            duration_ms=50,
            result_bytes=0,
            requires_confirmation=False,
            was_confirmed=None,
        )
        row = self.db.fetchone("SELECT * FROM tool_usage WHERE session_id = %s", ("s2",))
        assert row["status"] == "error"
        assert row["error_message"] == "RuntimeError: failed"

    def test_sanitizes_input(self):
        record_tool_call(
            session_id="s3",
            turn_number=1,
            agent_mode="sre",
            tool_name="apply_yaml",
            tool_category="operations",
            input_data={"yaml_content": "secret: hunter2\n" * 100, "namespace": "prod"},
            status="success",
            error_message=None,
            error_category=None,
            duration_ms=200,
            result_bytes=100,
            requires_confirmation=True,
            was_confirmed=True,
        )
        row = self.db.fetchone("SELECT * FROM tool_usage WHERE session_id = %s", ("s3",))
        assert "hunter2" not in str(row["input_summary"])

    def test_recording_failure_does_not_raise(self):
        """Fire-and-forget: DB errors should be swallowed."""
        reset_database()  # Break the DB connection
        # Should not raise
        record_tool_call(
            session_id="s4", turn_number=1, agent_mode="sre", tool_name="t",
            tool_category=None, input_data={}, status="success",
            error_message=None, error_category=None, duration_ms=0,
            result_bytes=0, requires_confirmation=False, was_confirmed=None,
        )


class TestRecordTurn:
    def setup_method(self):
        self.db = _make_test_db()
        db2 = Database(_TEST_DB_URL)
        db2.execute("DELETE FROM schema_migrations WHERE version >= 2")
        db2.commit()
        db2.close()
        set_database(self.db)
        run_migrations(self.db)

    def teardown_method(self):
        reset_database()

    def test_records_turn(self):
        record_turn(
            session_id="s1",
            turn_number=1,
            agent_mode="sre",
            query_summary="what pods are crashing",
            tools_offered=["list_pods", "get_events", "describe_pod"],
            tools_called=["list_pods", "get_events"],
        )
        row = self.db.fetchone("SELECT * FROM tool_turns WHERE session_id = %s", ("s1",))
        assert row is not None
        assert row["query_summary"] == "what pods are crashing"
        assert row["tools_offered"] == ["list_pods", "get_events", "describe_pod"]
        assert row["tools_called"] == ["list_pods", "get_events"]

    def test_truncates_query_summary(self):
        record_turn(
            session_id="s2", turn_number=1, agent_mode="sre",
            query_summary="x" * 500, tools_offered=[], tools_called=[],
        )
        row = self.db.fetchone("SELECT * FROM tool_turns WHERE session_id = %s", ("s2",))
        assert len(row["query_summary"]) <= 200

    def test_upsert_on_duplicate(self):
        """Second insert for same session+turn should update, not fail."""
        record_turn(session_id="s3", turn_number=1, agent_mode="sre",
                    query_summary="first", tools_offered=[], tools_called=[])
        record_turn(session_id="s3", turn_number=1, agent_mode="sre",
                    query_summary="first", tools_offered=[], tools_called=["list_pods"])
        row = self.db.fetchone("SELECT * FROM tool_turns WHERE session_id = %s", ("s3",))
        assert row["tools_called"] == ["list_pods"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_tool_usage.py::TestSanitizeInput tests/test_tool_usage.py::TestRecordToolCall tests/test_tool_usage.py::TestRecordTurn -v`
Expected: FAIL — `ImportError: cannot import name 'record_tool_call'`

- [ ] **Step 3: Implement tool_usage.py**

Create `sre_agent/tool_usage.py`:

```python
"""Tool usage tracking — records every tool invocation for system improvement.

Fire-and-forget: all recording functions swallow errors to avoid
impacting tool execution. Failed writes are logged at debug level.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger("pulse_agent.tool_usage")

# Fields whose values should never be recorded
_SECRET_FIELDS = {"token", "password", "key", "secret", "credential", "yaml_content", "new_content", "content"}

_MAX_VALUE_LEN = 256
_MAX_JSON_BYTES = 1024
_MAX_QUERY_SUMMARY_LEN = 200


def sanitize_input(input_data: dict | None) -> dict | None:
    """Strip secrets and truncate values for safe storage."""
    if input_data is None:
        return None
    if not input_data:
        return {}

    sanitized = {}
    for k, v in input_data.items():
        if k in _SECRET_FIELDS:
            sanitized[k] = f"<redacted {len(str(v))} chars>"
        elif isinstance(v, str) and len(v) > _MAX_VALUE_LEN:
            sanitized[k] = v[:_MAX_VALUE_LEN] + "..."
        else:
            sanitized[k] = v

    # Cap total JSON size
    encoded = json.dumps(sanitized, default=str)
    if len(encoded) > _MAX_JSON_BYTES:
        # Keep only first few keys that fit
        trimmed = {}
        size = 2  # {}
        for k, v in sanitized.items():
            entry = json.dumps({k: v}, default=str)
            if size + len(entry) > _MAX_JSON_BYTES:
                break
            trimmed[k] = v
            size += len(entry)
        sanitized = trimmed

    return sanitized


def record_tool_call(
    *,
    session_id: str,
    turn_number: int,
    agent_mode: str,
    tool_name: str,
    tool_category: str | None,
    input_data: dict | None,
    status: str,
    error_message: str | None,
    error_category: str | None,
    duration_ms: int,
    result_bytes: int,
    requires_confirmation: bool,
    was_confirmed: bool | None,
) -> None:
    """Record a single tool invocation. Fire-and-forget."""
    try:
        from .db import get_database

        db = get_database()
        db.execute(
            "INSERT INTO tool_usage "
            "(session_id, turn_number, agent_mode, tool_name, tool_category, "
            "input_summary, status, error_message, error_category, "
            "duration_ms, result_bytes, requires_confirmation, was_confirmed) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                session_id,
                turn_number,
                agent_mode,
                tool_name,
                tool_category,
                json.dumps(sanitize_input(input_data), default=str) if input_data else None,
                status,
                error_message,
                error_category,
                duration_ms,
                result_bytes,
                requires_confirmation,
                was_confirmed,
            ),
        )
        db.commit()
    except Exception:
        logger.debug("Failed to record tool call: %s", tool_name, exc_info=True)


def record_turn(
    *,
    session_id: str,
    turn_number: int,
    agent_mode: str,
    query_summary: str,
    tools_offered: list[str],
    tools_called: list[str],
) -> None:
    """Record turn-level context. Upserts to handle retries."""
    try:
        from .db import get_database

        db = get_database()
        summary = query_summary[:_MAX_QUERY_SUMMARY_LEN] if query_summary else ""
        db.execute(
            "INSERT INTO tool_turns (session_id, turn_number, agent_mode, query_summary, tools_offered, tools_called) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (session_id, turn_number) DO UPDATE SET "
            "tools_called = EXCLUDED.tools_called",
            (session_id, turn_number, agent_mode, summary, tools_offered, tools_called),
        )
        db.commit()
    except Exception:
        logger.debug("Failed to record turn: session=%s turn=%d", session_id, turn_number, exc_info=True)


def update_turn_feedback(*, session_id: str, feedback: str) -> None:
    """Link user feedback to the most recent turn in a session."""
    try:
        from .db import get_database

        db = get_database()
        db.execute(
            "UPDATE tool_turns SET feedback = %s "
            "WHERE id = (SELECT id FROM tool_turns WHERE session_id = %s ORDER BY turn_number DESC LIMIT 1)",
            (feedback, session_id),
        )
        db.commit()
    except Exception:
        logger.debug("Failed to update feedback: session=%s", session_id, exc_info=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_tool_usage.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/tool_usage.py tests/test_tool_usage.py
git commit -m "feat: add tool_usage.py with recording functions"
```

---

### Task 3: Add query and stats functions to tool_usage.py

**Files:**
- Modify: `sre_agent/tool_usage.py`
- Modify: `tests/test_tool_usage.py`

- [ ] **Step 1: Write failing tests for query_usage and get_usage_stats**

Add to `tests/test_tool_usage.py`:

```python
from sre_agent.tool_usage import query_usage, get_usage_stats


def _seed_usage(db):
    """Insert test data for query/stats tests."""
    from sre_agent.tool_usage import record_tool_call, record_turn

    for i in range(5):
        record_tool_call(
            session_id="stats-s1", turn_number=i + 1, agent_mode="sre",
            tool_name="list_pods", tool_category="diagnostics",
            input_data={"namespace": "default"}, status="success",
            error_message=None, error_category=None,
            duration_ms=100 + i * 10, result_bytes=500 + i * 100,
            requires_confirmation=False, was_confirmed=None,
        )
    # Add some errors
    record_tool_call(
        session_id="stats-s1", turn_number=6, agent_mode="sre",
        tool_name="bad_tool", tool_category="operations",
        input_data={}, status="error",
        error_message="RuntimeError", error_category="server",
        duration_ms=50, result_bytes=0,
        requires_confirmation=False, was_confirmed=None,
    )
    # Security mode call
    record_tool_call(
        session_id="stats-s2", turn_number=1, agent_mode="security",
        tool_name="scan_rbac_risks", tool_category="security",
        input_data={}, status="success",
        error_message=None, error_category=None,
        duration_ms=200, result_bytes=1000,
        requires_confirmation=False, was_confirmed=None,
    )
    # Record a turn with feedback
    record_turn(
        session_id="stats-s1", turn_number=1, agent_mode="sre",
        query_summary="show me pods", tools_offered=["list_pods", "get_events"],
        tools_called=["list_pods"],
    )


class TestQueryUsage:
    def setup_method(self):
        self.db = _make_test_db()
        db2 = Database(_TEST_DB_URL)
        db2.execute("DELETE FROM schema_migrations WHERE version >= 2")
        db2.commit()
        db2.close()
        set_database(self.db)
        run_migrations(self.db)
        _seed_usage(self.db)

    def teardown_method(self):
        reset_database()

    def test_basic_query(self):
        result = query_usage()
        assert result["total"] == 7
        assert len(result["entries"]) == 7

    def test_filter_by_tool_name(self):
        result = query_usage(tool_name="list_pods")
        assert result["total"] == 5
        assert all(e["tool_name"] == "list_pods" for e in result["entries"])

    def test_filter_by_status(self):
        result = query_usage(status="error")
        assert result["total"] == 1

    def test_filter_by_mode(self):
        result = query_usage(agent_mode="security")
        assert result["total"] == 1

    def test_pagination(self):
        result = query_usage(page=1, per_page=3)
        assert len(result["entries"]) == 3
        assert result["total"] == 7
        assert result["page"] == 1
        assert result["per_page"] == 3

    def test_page_2(self):
        result = query_usage(page=2, per_page=3)
        assert len(result["entries"]) == 3

    def test_filter_by_session(self):
        result = query_usage(session_id="stats-s2")
        assert result["total"] == 1


class TestGetUsageStats:
    def setup_method(self):
        self.db = _make_test_db()
        db2 = Database(_TEST_DB_URL)
        db2.execute("DELETE FROM schema_migrations WHERE version >= 2")
        db2.commit()
        db2.close()
        set_database(self.db)
        run_migrations(self.db)
        _seed_usage(self.db)

    def teardown_method(self):
        reset_database()

    def test_total_calls(self):
        stats = get_usage_stats()
        assert stats["total_calls"] == 7

    def test_unique_tools(self):
        stats = get_usage_stats()
        assert stats["unique_tools_used"] == 3

    def test_error_rate(self):
        stats = get_usage_stats()
        assert 0 < stats["error_rate"] < 1  # 1/7

    def test_by_tool(self):
        stats = get_usage_stats()
        assert len(stats["by_tool"]) > 0
        pods = next(t for t in stats["by_tool"] if t["tool_name"] == "list_pods")
        assert pods["count"] == 5

    def test_by_mode(self):
        stats = get_usage_stats()
        sre = next(m for m in stats["by_mode"] if m["mode"] == "sre")
        assert sre["count"] == 6

    def test_by_status(self):
        stats = get_usage_stats()
        assert stats["by_status"]["success"] == 6
        assert stats["by_status"]["error"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_tool_usage.py::TestQueryUsage tests/test_tool_usage.py::TestGetUsageStats -v`
Expected: FAIL — `ImportError: cannot import name 'query_usage'`

- [ ] **Step 3: Implement query_usage and get_usage_stats**

Add to `sre_agent/tool_usage.py`:

```python
def query_usage(
    *,
    tool_name: str | None = None,
    agent_mode: str | None = None,
    status: str | None = None,
    session_id: str | None = None,
    time_from: str | None = None,
    time_to: str | None = None,
    page: int = 1,
    per_page: int = 50,
) -> dict:
    """Query tool usage with filters and pagination."""
    from .db import get_database

    db = get_database()
    where_parts: list[str] = []
    params: list = []

    if tool_name:
        where_parts.append("tu.tool_name = %s")
        params.append(tool_name)
    if agent_mode:
        where_parts.append("tu.agent_mode = %s")
        params.append(agent_mode)
    if status:
        where_parts.append("tu.status = %s")
        params.append(status)
    if session_id:
        where_parts.append("tu.session_id = %s")
        params.append(session_id)
    if time_from:
        where_parts.append("tu.timestamp >= %s::timestamptz")
        params.append(time_from)
    if time_to:
        where_parts.append("tu.timestamp <= %s::timestamptz")
        params.append(time_to)

    where = "WHERE " + " AND ".join(where_parts) if where_parts else ""

    # Count total
    count_row = db.fetchone(f"SELECT COUNT(*) as cnt FROM tool_usage tu {where}", tuple(params))
    total = count_row["cnt"] if count_row else 0

    # Fetch page with turn context
    per_page = min(per_page, 200)
    offset = (page - 1) * per_page
    rows = db.fetchall(
        f"SELECT tu.*, tt.query_summary FROM tool_usage tu "
        f"LEFT JOIN tool_turns tt ON tu.session_id = tt.session_id AND tu.turn_number = tt.turn_number "
        f"{where} ORDER BY tu.timestamp DESC LIMIT %s OFFSET %s",
        tuple(params + [per_page, offset]),
    )

    # Convert timestamps to ISO strings
    for row in rows:
        if row.get("timestamp"):
            row["timestamp"] = row["timestamp"].isoformat() if hasattr(row["timestamp"], "isoformat") else str(row["timestamp"])
        # Parse JSONB input_summary
        if isinstance(row.get("input_summary"), str):
            try:
                row["input_summary"] = json.loads(row["input_summary"])
            except (ValueError, TypeError):
                pass

    return {
        "entries": rows,
        "total": total,
        "page": page,
        "per_page": per_page,
    }


def get_usage_stats(
    *,
    time_from: str | None = None,
    time_to: str | None = None,
) -> dict:
    """Get aggregated usage statistics."""
    from .db import get_database

    db = get_database()
    where_parts: list[str] = []
    params: list = []

    if time_from:
        where_parts.append("timestamp >= %s::timestamptz")
        params.append(time_from)
    if time_to:
        where_parts.append("timestamp <= %s::timestamptz")
        params.append(time_to)

    where = "WHERE " + " AND ".join(where_parts) if where_parts else ""

    # Basic aggregates
    agg = db.fetchone(
        f"SELECT COUNT(*) as total, "
        f"COUNT(DISTINCT tool_name) as unique_tools, "
        f"AVG(duration_ms)::integer as avg_duration_ms, "
        f"AVG(result_bytes)::integer as avg_result_bytes, "
        f"SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as error_count "
        f"FROM tool_usage {where}",
        tuple(params),
    )
    total = agg["total"] if agg else 0
    error_count = agg["error_count"] if agg else 0

    # By tool
    by_tool = db.fetchall(
        f"SELECT tool_name, COUNT(*) as count, "
        f"SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as error_count, "
        f"AVG(duration_ms)::integer as avg_duration_ms, "
        f"AVG(result_bytes)::integer as avg_result_bytes "
        f"FROM tool_usage {where} GROUP BY tool_name ORDER BY count DESC",
        tuple(params),
    )

    # By mode
    by_mode = db.fetchall(
        f"SELECT agent_mode as mode, COUNT(*) as count "
        f"FROM tool_usage {where} GROUP BY agent_mode ORDER BY count DESC",
        tuple(params),
    )

    # By category
    by_category = db.fetchall(
        f"SELECT tool_category as category, COUNT(*) as count "
        f"FROM tool_usage {where} AND tool_category IS NOT NULL GROUP BY tool_category ORDER BY count DESC"
        if where else
        f"SELECT tool_category as category, COUNT(*) as count "
        f"FROM tool_usage WHERE tool_category IS NOT NULL GROUP BY tool_category ORDER BY count DESC",
        tuple(params),
    )

    # By status
    by_status_rows = db.fetchall(
        f"SELECT status, COUNT(*) as count FROM tool_usage {where} GROUP BY status",
        tuple(params),
    )
    by_status = {row["status"]: row["count"] for row in by_status_rows}

    return {
        "total_calls": total,
        "unique_tools_used": agg["unique_tools"] if agg else 0,
        "error_rate": round(error_count / total, 4) if total > 0 else 0,
        "avg_duration_ms": agg["avg_duration_ms"] if agg else 0,
        "avg_result_bytes": agg["avg_result_bytes"] if agg else 0,
        "by_tool": by_tool,
        "by_mode": by_mode,
        "by_category": by_category,
        "by_status": by_status,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_tool_usage.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/tool_usage.py tests/test_tool_usage.py
git commit -m "feat: add query_usage and get_usage_stats functions"
```

---

### Task 4: Add get_agents_metadata function

**Files:**
- Modify: `sre_agent/tool_usage.py`
- Modify: `tests/test_tool_usage.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_tool_usage.py`:

```python
from sre_agent.tool_usage import get_agents_metadata


class TestGetAgentsMetadata:
    def test_returns_list(self):
        result = get_agents_metadata()
        assert isinstance(result, list)
        assert len(result) >= 3  # sre, security, view_designer

    def test_sre_agent(self):
        result = get_agents_metadata()
        sre = next(a for a in result if a["name"] == "sre")
        assert sre["has_write_tools"] is True
        assert sre["tools_count"] > 0
        assert "diagnostics" in sre["categories"]

    def test_security_agent(self):
        result = get_agents_metadata()
        sec = next(a for a in result if a["name"] == "security")
        assert sec["has_write_tools"] is False

    def test_view_designer_agent(self):
        result = get_agents_metadata()
        vd = next(a for a in result if a["name"] == "view_designer")
        assert vd["has_write_tools"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_tool_usage.py::TestGetAgentsMetadata -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement get_agents_metadata**

Add to `sre_agent/tool_usage.py`:

```python
_AGENT_DESCRIPTIONS = {
    "sre": "Cluster diagnostics, incident triage, and resource management",
    "security": "Security scanning, RBAC analysis, and compliance checks",
    "view_designer": "Dashboard creation and component design",
}


def get_agents_metadata() -> list[dict]:
    """Return metadata for all agent modes."""
    from .harness import ALWAYS_INCLUDE, MODE_CATEGORIES, TOOL_CATEGORIES
    from .orchestrator import build_orchestrated_config

    agents = []
    for mode, categories in MODE_CATEGORIES.items():
        if mode == "both":
            continue  # "both" is a meta-mode, not a real agent

        config = build_orchestrated_config(mode)
        tool_names = {d.get("name") for d in config["tool_defs"]}

        agents.append({
            "name": mode,
            "description": _AGENT_DESCRIPTIONS.get(mode, ""),
            "tools_count": len(config["tool_defs"]),
            "has_write_tools": len(config["write_tools"]) > 0,
            "categories": categories or [],
        })

    return agents
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_tool_usage.py::TestGetAgentsMetadata -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/tool_usage.py tests/test_tool_usage.py
git commit -m "feat: add get_agents_metadata for /agents endpoint"
```

---

### Task 5: Add REST endpoints to api.py

**Files:**
- Modify: `sre_agent/api.py`
- Create: `tests/test_api_tools.py`

- [ ] **Step 1: Write tests for new endpoints**

Create `tests/test_api_tools.py`:

```python
"""Tests for tool usage REST endpoints."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

# Set required env vars before importing app
os.environ.setdefault("PULSE_AGENT_WS_TOKEN", "test-token-123")


class TestAgentsEndpoint:
    def test_returns_agents(self):
        from sre_agent.api import app

        client = TestClient(app)
        resp = client.get("/agents", headers={"Authorization": "Bearer test-token-123"})
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        names = {a["name"] for a in data}
        assert "sre" in names
        assert "security" in names

    def test_unauthorized(self):
        from sre_agent.api import app

        client = TestClient(app)
        resp = client.get("/agents")
        assert resp.status_code == 401


class TestToolsEndpointEnhanced:
    def test_includes_category(self):
        from sre_agent.api import app

        client = TestClient(app)
        resp = client.get("/tools", headers={"Authorization": "Bearer test-token-123"})
        assert resp.status_code == 200
        data = resp.json()
        sre_tools = data["sre"]
        assert len(sre_tools) > 0
        # At least some tools should have a category
        has_category = [t for t in sre_tools if t.get("category") is not None]
        assert len(has_category) > 0


class TestToolsUsageEndpoint:
    @patch("sre_agent.tool_usage.query_usage")
    def test_basic_query(self, mock_query):
        mock_query.return_value = {"entries": [], "total": 0, "page": 1, "per_page": 50}
        from sre_agent.api import app

        client = TestClient(app)
        resp = client.get("/tools/usage", headers={"Authorization": "Bearer test-token-123"})
        assert resp.status_code == 200
        assert resp.json()["total"] == 0
        mock_query.assert_called_once()

    @patch("sre_agent.tool_usage.query_usage")
    def test_passes_filters(self, mock_query):
        mock_query.return_value = {"entries": [], "total": 0, "page": 1, "per_page": 50}
        from sre_agent.api import app

        client = TestClient(app)
        resp = client.get(
            "/tools/usage?tool_name=list_pods&agent_mode=sre&status=success&page=2&per_page=10",
            headers={"Authorization": "Bearer test-token-123"},
        )
        assert resp.status_code == 200
        mock_query.assert_called_once_with(
            tool_name="list_pods", agent_mode="sre", status="success",
            session_id=None, time_from=None, time_to=None, page=2, per_page=10,
        )


class TestToolsUsageStatsEndpoint:
    @patch("sre_agent.tool_usage.get_usage_stats")
    def test_basic_stats(self, mock_stats):
        mock_stats.return_value = {
            "total_calls": 100, "unique_tools_used": 10, "error_rate": 0.05,
            "avg_duration_ms": 200, "avg_result_bytes": 3000,
            "by_tool": [], "by_mode": [], "by_category": [], "by_status": {},
        }
        from sre_agent.api import app

        client = TestClient(app)
        resp = client.get("/tools/usage/stats", headers={"Authorization": "Bearer test-token-123"})
        assert resp.status_code == 200
        assert resp.json()["total_calls"] == 100
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_api_tools.py -v`
Expected: FAIL — 404 (endpoints don't exist yet)

- [ ] **Step 3: Add endpoints to api.py**

Add after the existing `/tools` endpoint in `sre_agent/api.py`:

```python
@app.get("/agents")
async def list_agents(
    authorization: str | None = Header(None),
    token: str | None = Query(None),
):
    """List all agent modes with metadata."""
    _verify_rest_token(authorization, token)
    from .tool_usage import get_agents_metadata

    return get_agents_metadata()


@app.get("/tools/usage")
async def get_tools_usage(
    tool_name: str | None = Query(None),
    agent_mode: str | None = Query(None),
    status: str | None = Query(None),
    session_id: str | None = Query(None),
    time_from: str | None = Query(None, alias="from"),
    time_to: str | None = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    authorization: str | None = Header(None),
    token: str | None = Query(None),
):
    """Paginated audit log of tool invocations."""
    _verify_rest_token(authorization, token)
    from .tool_usage import query_usage

    return query_usage(
        tool_name=tool_name, agent_mode=agent_mode, status=status,
        session_id=session_id, time_from=time_from, time_to=time_to,
        page=page, per_page=per_page,
    )


@app.get("/tools/usage/stats")
async def get_tools_usage_stats(
    time_from: str | None = Query(None, alias="from"),
    time_to: str | None = Query(None, alias="to"),
    authorization: str | None = Header(None),
    token: str | None = Query(None),
):
    """Aggregated tool usage statistics."""
    _verify_rest_token(authorization, token)
    from .tool_usage import get_usage_stats

    return get_usage_stats(time_from=time_from, time_to=time_to)
```

- [ ] **Step 4: Enhance existing `/tools` endpoint to include category**

Update the `/tools` endpoint in `sre_agent/api.py`:

```python
@app.get("/tools")
async def list_tools(
    authorization: str | None = Header(None),
    token: str | None = Query(None),
):
    """List all available tools grouped by mode, with write-op flags."""
    _verify_rest_token(authorization, token)
    from .harness import get_tool_category

    return {
        "sre": [
            {
                "name": t.name,
                "description": t.description,
                "requires_confirmation": t.name in WRITE_TOOLS,
                "category": get_tool_category(t.name),
            }
            for t in SRE_ALL_TOOLS
        ],
        "security": [
            {
                "name": t.name,
                "description": t.description,
                "requires_confirmation": False,
                "category": get_tool_category(t.name),
            }
            for t in SEC_ALL_TOOLS
        ],
        "write_tools": sorted(WRITE_TOOLS),
    }
```

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/test_api_tools.py -v`
Expected: All PASS

- [ ] **Step 6: Run full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add sre_agent/api.py tests/test_api_tools.py
git commit -m "feat: add /agents, /tools/usage, /tools/usage/stats endpoints"
```

---

### Task 6: Wire on_tool_result in WebSocket handlers

This connects the recording layer to the agent loop. The `on_tool_result` callback (added in the prereqs) is used to fire-and-forget audit writes.

**Files:**
- Modify: `sre_agent/api.py` (the `_run_agent_ws` function and WebSocket handlers)

- [ ] **Step 1: Write test for recording integration**

Add to `tests/test_api_tools.py`:

```python
class TestToolResultRecording:
    @patch("sre_agent.tool_usage.record_tool_call")
    @patch("sre_agent.tool_usage.record_turn")
    def test_on_tool_result_records(self, mock_turn, mock_call):
        """Verify the on_tool_result callback calls record_tool_call."""
        # We test this by importing the callback builder and calling it directly
        # since testing WebSocket flows is complex
        from sre_agent.api import _build_tool_result_handler

        handler = _build_tool_result_handler(
            session_id="test-sess", agent_mode="sre", write_tools={"delete_pod"}
        )
        handler({
            "tool_name": "list_pods",
            "input": {"namespace": "default"},
            "status": "success",
            "error_message": None,
            "error_category": None,
            "duration_ms": 100,
            "result_bytes": 500,
            "was_confirmed": None,
            "turn_number": 1,
        })
        mock_call.assert_called_once()
        call_kwargs = mock_call.call_args[1]
        assert call_kwargs["session_id"] == "test-sess"
        assert call_kwargs["tool_name"] == "list_pods"
        assert call_kwargs["requires_confirmation"] is False

    @patch("sre_agent.tool_usage.record_tool_call")
    @patch("sre_agent.tool_usage.record_turn")
    def test_write_tool_flagged(self, mock_turn, mock_call):
        from sre_agent.api import _build_tool_result_handler

        handler = _build_tool_result_handler(
            session_id="test-sess", agent_mode="sre", write_tools={"delete_pod"}
        )
        handler({
            "tool_name": "delete_pod",
            "input": {"pod_name": "x"},
            "status": "success",
            "error_message": None,
            "error_category": None,
            "duration_ms": 50,
            "result_bytes": 10,
            "was_confirmed": True,
            "turn_number": 1,
        })
        call_kwargs = mock_call.call_args[1]
        assert call_kwargs["requires_confirmation"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_api_tools.py::TestToolResultRecording -v`
Expected: FAIL — `cannot import name '_build_tool_result_handler'`

- [ ] **Step 3: Add _build_tool_result_handler and wire it into _run_agent_ws**

Add to `sre_agent/api.py` (as a module-level function before `_run_agent_ws`):

```python
def _build_tool_result_handler(
    session_id: str, agent_mode: str, write_tools: set[str]
):
    """Build an on_tool_result callback that records to tool_usage table."""

    def on_tool_result(info: dict):
        try:
            from .harness import get_tool_category
            from .tool_usage import record_tool_call

            record_tool_call(
                session_id=session_id,
                turn_number=info["turn_number"],
                agent_mode=agent_mode,
                tool_name=info["tool_name"],
                tool_category=get_tool_category(info["tool_name"]),
                input_data=info.get("input"),
                status=info["status"],
                error_message=info.get("error_message"),
                error_category=info.get("error_category"),
                duration_ms=info.get("duration_ms", 0),
                result_bytes=info.get("result_bytes", 0),
                requires_confirmation=info["tool_name"] in write_tools,
                was_confirmed=info.get("was_confirmed"),
            )
        except Exception:
            logger.debug("Tool result recording failed", exc_info=True)

    return on_tool_result
```

Then in `_run_agent_ws`, add the `on_tool_result` callback to the `run_agent_streaming` call. Find the line that calls `run_agent_streaming` and add:

```python
    tool_result_handler = _build_tool_result_handler(ws_id, mode, write_tools)
```

And pass `on_tool_result=tool_result_handler` to the `run_agent_streaming` call.

Do the same for the `/ws/agent` handler.

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_api_tools.py -v`
Expected: All PASS

- [ ] **Step 5: Run full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add sre_agent/api.py tests/test_api_tools.py
git commit -m "feat: wire on_tool_result to record tool calls in WebSocket handlers"
```

---

### Task 7: Link feedback to tool_turns

**Files:**
- Modify: `sre_agent/api.py` (feedback handler in `_make_receive_loop`)
- Modify: `tests/test_tool_usage.py`

- [ ] **Step 1: Write failing test for feedback linking**

Add to `tests/test_tool_usage.py`:

```python
from sre_agent.tool_usage import update_turn_feedback


class TestUpdateTurnFeedback:
    def setup_method(self):
        self.db = _make_test_db()
        db2 = Database(_TEST_DB_URL)
        db2.execute("DELETE FROM schema_migrations WHERE version >= 2")
        db2.commit()
        db2.close()
        set_database(self.db)
        run_migrations(self.db)

    def teardown_method(self):
        reset_database()

    def test_links_feedback_to_latest_turn(self):
        record_turn(session_id="fb-s1", turn_number=1, agent_mode="sre",
                    query_summary="q1", tools_offered=[], tools_called=[])
        record_turn(session_id="fb-s1", turn_number=2, agent_mode="sre",
                    query_summary="q2", tools_offered=[], tools_called=[])

        update_turn_feedback(session_id="fb-s1", feedback="positive")

        row = self.db.fetchone(
            "SELECT feedback FROM tool_turns WHERE session_id = %s AND turn_number = 2", ("fb-s1",)
        )
        assert row["feedback"] == "positive"

        # Turn 1 should not have feedback
        row1 = self.db.fetchone(
            "SELECT feedback FROM tool_turns WHERE session_id = %s AND turn_number = 1", ("fb-s1",)
        )
        assert row1["feedback"] is None

    def test_no_turns_does_not_raise(self):
        update_turn_feedback(session_id="nonexistent", feedback="negative")
```

- [ ] **Step 2: Run test to verify it passes (already implemented)**

Run: `python3 -m pytest tests/test_tool_usage.py::TestUpdateTurnFeedback -v`
Expected: PASS (function already implemented in Task 2)

- [ ] **Step 3: Add feedback linking call to api.py**

In `sre_agent/api.py`, find the feedback handler inside `_make_receive_loop` (the block that handles `msg_type == "feedback"`). After the existing memory feedback logic, add:

```python
            # Link feedback to tool tracking
            try:
                from .tool_usage import update_turn_feedback
                feedback_value = "positive" if resolved else "negative"
                update_turn_feedback(session_id=ws_id, feedback=feedback_value)
            except Exception:
                pass
```

Where `ws_id` is the session ID (it's passed to `_make_receive_loop` — check the function signature and use the correct variable name).

- [ ] **Step 4: Run full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/api.py tests/test_tool_usage.py
git commit -m "feat: link user feedback to tool_turns for correlation"
```

---

### Task 8: Update API_CONTRACT.md and CLAUDE.md

**Files:**
- Modify: `API_CONTRACT.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add new endpoints to API_CONTRACT.md**

Add the following endpoint documentation to `API_CONTRACT.md` in the appropriate section:

```markdown
### `GET /agents`
Returns all agent modes with metadata (name, description, tool count, categories, write capability).
Auth: Bearer token or `?token=` query param.

### `GET /tools/usage`
Paginated audit log of tool invocations. Query params: `tool_name`, `agent_mode`, `status`, `session_id`, `from`, `to`, `page`, `per_page`.
Auth: Bearer token.

### `GET /tools/usage/stats`
Aggregated tool usage statistics (totals, by tool, by mode, by category, error rates).
Query params: `from`, `to`.
Auth: Bearer token.

### `GET /tools` (enhanced)
Now includes `category` field for each tool entry.
```

- [ ] **Step 2: Update CLAUDE.md key files section**

Add `tool_usage.py` to the Key Files section:

```markdown
- `tool_usage.py` — tool invocation audit log (PostgreSQL, fire-and-forget recording, query/stats)
```

- [ ] **Step 3: Commit**

```bash
git add API_CONTRACT.md CLAUDE.md
git commit -m "docs: add tool usage endpoints to API contract and CLAUDE.md"
```

---

### Task 9: Final verification

**Files:** None (verification only)

- [ ] **Step 1: Run full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: All tests pass.

- [ ] **Step 2: Verify endpoints exist**

```python
python3 -c "
from sre_agent.api import app
routes = [r.path for r in app.routes]
assert '/agents' in routes
assert '/tools/usage' in routes
assert '/tools/usage/stats' in routes
print('All endpoints registered')
"
```

- [ ] **Step 3: Verify recording function works end-to-end**

```python
python3 -c "
from sre_agent.tool_usage import record_tool_call, query_usage, get_usage_stats, get_agents_metadata

# Agents metadata
agents = get_agents_metadata()
assert len(agents) >= 3
print(f'Agents: {[a[\"name\"] for a in agents]}')

print('All functions verified')
"
```
