"""Tests for tool chain discovery and next-tool hints."""

from __future__ import annotations

from sre_agent.db import Database, reset_database, set_database
from sre_agent.db_migrations import run_migrations
from sre_agent.tool_usage import record_tool_call

from .conftest import _TEST_DB_URL


def _make_test_db() -> Database:
    db = Database(_TEST_DB_URL)
    db.execute("DROP TABLE IF EXISTS tool_usage CASCADE")
    db.execute("DROP TABLE IF EXISTS tool_turns CASCADE")
    db.commit()
    return db


def _seed_chains(db):
    """Insert tool call sequences that form discoverable chains."""
    for session_num in range(1, 6):
        sid = f"chain-s{session_num}"
        record_tool_call(
            session_id=sid,
            turn_number=1,
            agent_mode="sre",
            tool_name="list_resources",
            tool_category="diagnostics",
            input_data={},
            status="success",
            error_message=None,
            error_category=None,
            duration_ms=100,
            result_bytes=500,
            requires_confirmation=False,
            was_confirmed=None,
        )
        record_tool_call(
            session_id=sid,
            turn_number=2,
            agent_mode="sre",
            tool_name="get_pod_logs",
            tool_category="diagnostics",
            input_data={},
            status="success",
            error_message=None,
            error_category=None,
            duration_ms=200,
            result_bytes=1000,
            requires_confirmation=False,
            was_confirmed=None,
        )
        record_tool_call(
            session_id=sid,
            turn_number=3,
            agent_mode="sre",
            tool_name="describe_resource",
            tool_category="diagnostics",
            input_data={},
            status="success",
            error_message=None,
            error_category=None,
            duration_ms=150,
            result_bytes=800,
            requires_confirmation=False,
            was_confirmed=None,
        )
    for session_num in range(6, 9):
        sid = f"chain-s{session_num}"
        record_tool_call(
            session_id=sid,
            turn_number=1,
            agent_mode="sre",
            tool_name="list_resources",
            tool_category="diagnostics",
            input_data={},
            status="success",
            error_message=None,
            error_category=None,
            duration_ms=100,
            result_bytes=500,
            requires_confirmation=False,
            was_confirmed=None,
        )
        record_tool_call(
            session_id=sid,
            turn_number=2,
            agent_mode="sre",
            tool_name="get_events",
            tool_category="diagnostics",
            input_data={},
            status="success",
            error_message=None,
            error_category=None,
            duration_ms=100,
            result_bytes=300,
            requires_confirmation=False,
            was_confirmed=None,
        )


class TestDiscoverChains:
    def setup_method(self):
        self.db = _make_test_db()
        db2 = Database(_TEST_DB_URL)
        db2.execute("DELETE FROM schema_migrations WHERE version >= 2")
        db2.commit()
        db2.close()
        set_database(self.db)
        run_migrations(self.db)
        _seed_chains(self.db)

    def teardown_method(self):
        reset_database()

    def test_discovers_bigrams(self):
        from sre_agent.tool_chains import discover_chains

        result = discover_chains(min_frequency=3)
        assert len(result["bigrams"]) > 0
        # Should find list_resources -> get_pod_logs with frequency 5
        bigrams_dict = {(b["from_tool"], b["to_tool"]): b for b in result["bigrams"]}
        assert ("list_resources", "get_pod_logs") in bigrams_dict
        lr_gpl = bigrams_dict[("list_resources", "get_pod_logs")]
        assert lr_gpl["frequency"] == 5

    def test_probability_calculated(self):
        from sre_agent.tool_chains import discover_chains

        result = discover_chains(min_frequency=3)
        top = result["bigrams"][0]
        assert 0.5 < top["probability"] <= 1.0

    def test_min_frequency_filters(self):
        from sre_agent.tool_chains import discover_chains

        result = discover_chains(min_frequency=10)
        assert len(result["bigrams"]) == 0

    def test_includes_session_count(self):
        from sre_agent.tool_chains import discover_chains

        result = discover_chains(min_frequency=1)
        assert result["total_sessions_analyzed"] > 0

    def test_empty_table(self):
        self.db.execute("DELETE FROM tool_usage")
        self.db.commit()
        from sre_agent.tool_chains import discover_chains

        result = discover_chains()
        assert result["bigrams"] == []
        assert result["total_sessions_analyzed"] == 0


class TestChainHints:
    def setup_method(self):
        self.db = _make_test_db()
        db2 = Database(_TEST_DB_URL)
        db2.execute("DELETE FROM schema_migrations WHERE version >= 2")
        db2.commit()
        db2.close()
        set_database(self.db)
        run_migrations(self.db)
        _seed_chains(self.db)

    def teardown_method(self):
        from sre_agent.tool_chains import _chain_hints_cache

        _chain_hints_cache.clear()
        reset_database()

    def test_refresh_populates_cache(self):
        from sre_agent.tool_chains import _chain_hints_cache, refresh_chain_hints

        refresh_chain_hints()
        assert len(_chain_hints_cache) > 0
        assert "list_resources" in _chain_hints_cache

    def test_get_chain_hints_text(self):
        from sre_agent.tool_chains import get_chain_hints_text, refresh_chain_hints

        refresh_chain_hints()
        text = get_chain_hints_text()
        assert "list_resources" in text
        assert "get_pod_logs" in text

    def test_hints_text_empty_when_no_data(self):
        from sre_agent.tool_chains import get_chain_hints_text

        text = get_chain_hints_text()
        assert text == ""

    def test_hints_respect_min_probability(self):
        from sre_agent.tool_chains import _chain_hints_cache, refresh_chain_hints

        refresh_chain_hints(min_probability=0.99)
        for hints in _chain_hints_cache.values():
            for _, prob in hints:
                assert prob >= 0.99
