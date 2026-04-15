"""Tests for tool usage tracking — DB functions, recording, querying."""

from __future__ import annotations

from sre_agent.db import Database, reset_database, set_database
from sre_agent.db_migrations import run_migrations
from sre_agent.tool_usage import (
    _RETRY_KEYWORDS,
    get_agents_metadata,
    get_learned_eval_prompts,
    get_usage_stats,
    query_usage,
    record_tool_call,
    record_turn,
    sanitize_input,
    update_turn_feedback,
)

from .conftest import _TEST_DB_URL


def _make_test_db() -> Database:
    db = Database(_TEST_DB_URL)
    db.execute("DROP TABLE IF EXISTS tool_usage CASCADE")
    db.execute("DROP TABLE IF EXISTS tool_turns CASCADE")
    db.commit()
    return db


def _seed_usage(db):
    """Insert test data for query/stats tests."""
    for i in range(5):
        record_tool_call(
            session_id="stats-s1",
            turn_number=i + 1,
            agent_mode="sre",
            tool_name="list_pods",
            tool_category="diagnostics",
            input_data={"namespace": "default"},
            status="success",
            error_message=None,
            error_category=None,
            duration_ms=100 + i * 10,
            result_bytes=500 + i * 100,
            requires_confirmation=False,
            was_confirmed=None,
        )
    record_tool_call(
        session_id="stats-s1",
        turn_number=6,
        agent_mode="sre",
        tool_name="bad_tool",
        tool_category="operations",
        input_data={},
        status="error",
        error_message="RuntimeError",
        error_category="server",
        duration_ms=50,
        result_bytes=0,
        requires_confirmation=False,
        was_confirmed=None,
    )
    record_tool_call(
        session_id="stats-s2",
        turn_number=1,
        agent_mode="security",
        tool_name="scan_rbac_risks",
        tool_category="security",
        input_data={},
        status="success",
        error_message=None,
        error_category=None,
        duration_ms=200,
        result_bytes=1000,
        requires_confirmation=False,
        was_confirmed=None,
    )
    record_turn(
        session_id="stats-s1",
        turn_number=1,
        agent_mode="sre",
        query_summary="show me pods",
        tools_offered=["list_pods", "get_events"],
        tools_called=["list_pods"],
    )


class TestToolUsageTables:
    def test_migration_creates_tables(self):
        db = _make_test_db()
        db.execute("DELETE FROM schema_migrations WHERE version >= 2")
        db.commit()
        set_database(db)
        run_migrations(db)

        row = db.fetchone(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'tool_usage') AS exists"
        )
        assert row["exists"] is True

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
            (
                "sess-1",
                1,
                "sre",
                "list_pods",
                "diagnostics",
                '{"namespace": "default"}',
                "success",
                342,
                4820,
                False,
                None,
            ),
        )
        db.commit()

        row = db.fetchone("SELECT * FROM tool_usage WHERE session_id = %s", ("sess-1",))
        assert row is not None
        assert row["tool_name"] == "list_pods"
        assert row["status"] == "success"
        assert row["duration_ms"] == 342
        reset_database()

    def test_tool_turns_insert(self):
        db = _make_test_db()
        db.execute("DELETE FROM schema_migrations WHERE version >= 2")
        db.commit()
        set_database(db)
        run_migrations(db)

        db.execute(
            "INSERT INTO tool_turns (session_id, turn_number, agent_mode, query_summary, tools_offered, tools_called) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            ("sess-1", 1, "sre", "show pods", ["list_pods", "get_events"], ["list_pods"]),
        )
        db.commit()

        row = db.fetchone("SELECT * FROM tool_turns WHERE session_id = %s", ("sess-1",))
        assert row is not None
        assert row["tools_offered"] == ["list_pods", "get_events"]
        assert row["tools_called"] == ["list_pods"]
        reset_database()

    def test_tool_turns_unique_constraint(self):
        db = _make_test_db()
        db.execute("DELETE FROM schema_migrations WHERE version >= 2")
        db.commit()
        set_database(db)
        run_migrations(db)

        db.execute(
            "INSERT INTO tool_turns (session_id, turn_number, agent_mode, query_summary, tools_offered, tools_called) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            ("sess-1", 1, "sre", "q1", [], []),
        )
        db.commit()

        # Upsert should work
        db.execute(
            "INSERT INTO tool_turns (session_id, turn_number, agent_mode, query_summary, tools_offered, tools_called) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (session_id, turn_number) DO UPDATE SET tools_called = EXCLUDED.tools_called",
            ("sess-1", 1, "sre", "q1", [], ["list_pods"]),
        )
        db.commit()

        row = db.fetchone("SELECT * FROM tool_turns WHERE session_id = %s", ("sess-1",))
        assert row["tools_called"] == ["list_pods"]
        reset_database()


class TestSanitizeInput:
    def test_strips_secret_fields(self):
        result = sanitize_input({"namespace": "prod", "token": "abc123", "password": "hunter2"})
        assert result["namespace"] == "prod"
        assert "abc123" not in str(result)
        assert "hunter2" not in str(result)

    def test_truncates_long_values(self):
        result = sanitize_input({"data": "x" * 500})
        assert len(result["data"]) <= 260

    def test_caps_total_size(self):
        big = {f"key_{i}": "v" * 200 for i in range(20)}
        result = sanitize_input(big)
        import json

        assert len(json.dumps(result)) <= 1100

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
        reset_database()
        record_tool_call(
            session_id="s4",
            turn_number=1,
            agent_mode="sre",
            tool_name="t",
            tool_category=None,
            input_data={},
            status="success",
            error_message=None,
            error_category=None,
            duration_ms=0,
            result_bytes=0,
            requires_confirmation=False,
            was_confirmed=None,
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
            session_id="s2",
            turn_number=1,
            agent_mode="sre",
            query_summary="x" * 500,
            tools_offered=[],
            tools_called=[],
        )
        row = self.db.fetchone("SELECT * FROM tool_turns WHERE session_id = %s", ("s2",))
        assert len(row["query_summary"]) <= 200

    def test_upsert_on_duplicate(self):
        record_turn(
            session_id="s3",
            turn_number=1,
            agent_mode="sre",
            query_summary="first",
            tools_offered=[],
            tools_called=[],
        )
        record_turn(
            session_id="s3",
            turn_number=1,
            agent_mode="sre",
            query_summary="first",
            tools_offered=[],
            tools_called=["list_pods"],
        )
        row = self.db.fetchone("SELECT * FROM tool_turns WHERE session_id = %s", ("s3",))
        assert row["tools_called"] == ["list_pods"]


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
        record_turn(
            session_id="fb-s1",
            turn_number=1,
            agent_mode="sre",
            query_summary="q1",
            tools_offered=[],
            tools_called=[],
        )
        record_turn(
            session_id="fb-s1",
            turn_number=2,
            agent_mode="sre",
            query_summary="q2",
            tools_offered=[],
            tools_called=[],
        )
        update_turn_feedback(session_id="fb-s1", feedback="positive")
        row = self.db.fetchone("SELECT feedback FROM tool_turns WHERE session_id = %s AND turn_number = 2", ("fb-s1",))
        assert row["feedback"] == "positive"
        row1 = self.db.fetchone("SELECT feedback FROM tool_turns WHERE session_id = %s AND turn_number = 1", ("fb-s1",))
        assert row1["feedback"] is None

    def test_no_turns_does_not_raise(self):
        update_turn_feedback(session_id="nonexistent", feedback="negative")


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
        self.db.execute("DELETE FROM schema_migrations WHERE version >= 2")
        self.db.commit()
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
        assert 0 < stats["error_rate"] < 1

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


class TestGetAgentsMetadata:
    def test_returns_list(self):
        result = get_agents_metadata()
        assert isinstance(result, list)
        assert len(result) >= 3

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

    def test_no_both_mode(self):
        result = get_agents_metadata()
        names = {a["name"] for a in result}
        assert "both" not in names


import pytest


@pytest.mark.requires_pg
class TestLearnedEvalPrompts:
    """Tests for get_learned_eval_prompts — implicit positive feedback detection."""

    def _setup_db(self):
        db = Database(_TEST_DB_URL)
        set_database(db)
        # Drop and recreate tool tables for a clean state
        db.execute("DROP TABLE IF EXISTS tool_usage CASCADE")
        db.execute("DROP TABLE IF EXISTS tool_turns CASCADE")
        db.execute("DROP TABLE IF EXISTS schema_migrations CASCADE")
        db.commit()
        run_migrations(db)
        return db

    def _turn(self, sid, num, mode, query, tools_offered, tools_called):
        record_turn(
            session_id=sid,
            turn_number=num,
            agent_mode=mode,
            query_summary=query,
            tools_offered=tools_offered,
            tools_called=tools_called,
        )

    def test_positive_signal_detected(self):
        db = self._setup_db()
        try:
            self._turn("sess-1", 1, "sre", "list all pods in production", ["list_pods"], ["list_pods"])
            self._turn("sess-1", 2, "sre", "show me node metrics", ["get_node_metrics"], ["get_node_metrics"])
            db.commit()

            prompts = get_learned_eval_prompts(days=1)
            assert len(prompts) >= 1
            assert prompts[0][0] == "list all pods in production"
            assert "list_pods" in prompts[0][1]
            assert prompts[0][2] == "sre"
            assert prompts[0][3] == "Learned from usage"
        finally:
            reset_database()

    def test_retry_filtered_out(self):
        db = self._setup_db()
        try:
            self._turn("sess-2", 1, "sre", "show pods", ["list_pods"], ["list_pods"])
            self._turn("sess-2", 2, "sre", "no try again with namespace default", [], [])
            db.commit()

            prompts = get_learned_eval_prompts(days=1)
            queries = [p[0].lower() for p in prompts]
            assert "show pods" not in queries
        finally:
            reset_database()

    def test_deduplication(self):
        db = self._setup_db()
        try:
            self._turn("sess-3", 1, "sre", "list pods", ["list_pods"], ["list_pods"])
            self._turn("sess-3", 2, "sre", "what is the weather", [], [])
            self._turn("sess-4", 1, "sre", "list pods", ["list_pods"], ["list_pods"])
            self._turn("sess-4", 2, "sre", "show deployments", ["list_deployments"], ["list_deployments"])
            db.commit()

            prompts = get_learned_eval_prompts(days=1)
            pod_prompts = [p for p in prompts if p[0].lower() == "list pods"]
            assert len(pod_prompts) == 1
        finally:
            reset_database()

    def test_empty_db_returns_empty(self):
        self._setup_db()
        try:
            prompts = get_learned_eval_prompts(days=1)
            assert prompts == []
        finally:
            reset_database()

    def test_retry_keywords_coverage(self):
        """All retry keywords should be present in the frozenset."""
        assert "try again" in _RETRY_KEYWORDS
        assert "wrong" in _RETRY_KEYWORDS
        assert "i meant" in _RETRY_KEYWORDS
