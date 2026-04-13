"""Tests for adaptive tool prediction engine."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from sre_agent.db import Database
from sre_agent.db_migrations import run_migrations
from sre_agent.tool_predictor import learn

_TEST_DB_URL = os.environ.get(
    "PULSE_AGENT_TEST_DATABASE_URL",
    "postgresql://pulse:pulse@localhost:5433/pulse_test",
)


def _make_test_db() -> Database:
    db = Database(_TEST_DB_URL)
    run_migrations(db)
    return db


class TestMigration:
    def test_tool_predictions_table_exists(self):
        db = _make_test_db()
        db.execute("SELECT 1 FROM tool_predictions LIMIT 0")

    def test_tool_cooccurrence_table_exists(self):
        db = _make_test_db()
        db.execute("SELECT 1 FROM tool_cooccurrence LIMIT 0")

    def test_tool_predictions_columns(self):
        db = _make_test_db()
        row = db.fetchone(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'tool_predictions' AND column_name = 'miss_count'"
        )
        assert row is not None


class TestExtractTokens:
    def test_basic_query(self):
        from sre_agent.tool_predictor import extract_tokens

        tokens = extract_tokens("why are pods crashlooping in production")
        assert "pods" in tokens
        assert "crashlooping" in tokens
        assert "production" in tokens

    def test_drops_stopwords(self):
        from sre_agent.tool_predictor import extract_tokens

        tokens = extract_tokens("can you please show me the pods")
        assert "can" not in tokens
        assert "you" not in tokens
        assert "please" not in tokens
        assert "pods" in tokens

    def test_bigrams(self):
        from sre_agent.tool_predictor import extract_tokens

        tokens = extract_tokens("check node pressure")
        assert "node pressure" in tokens

    def test_k8s_terms_intact(self):
        from sre_agent.tool_predictor import extract_tokens

        tokens = extract_tokens("pod is in CrashLoopBackOff state")
        assert "crashloopbackoff" in tokens

    def test_punctuation_stripped(self):
        from sre_agent.tool_predictor import extract_tokens

        tokens = extract_tokens("what's wrong with my pods?")
        assert "pods" in tokens
        assert "wrong" in tokens

    def test_empty_query(self):
        from sre_agent.tool_predictor import extract_tokens

        assert extract_tokens("") == []

    def test_deduplication(self):
        from sre_agent.tool_predictor import extract_tokens

        tokens = extract_tokens("pods pods pods")
        assert tokens.count("pods") == 1


class TestLearn:
    def _make_mock_db(self):
        db = MagicMock()
        return db

    @patch("sre_agent.tool_predictor._get_db")
    def test_records_positive_signals(self, mock_get_db):
        db = self._make_mock_db()
        mock_get_db.return_value = db

        learn(
            query="show pods in production",
            tools_called=["list_pods", "describe_pod"],
            tools_offered=["list_pods", "describe_pod", "get_configmap"],
        )

        assert db.execute.call_count > 0
        assert db.commit.called

    @patch("sre_agent.tool_predictor._get_db")
    def test_records_cooccurrence(self, mock_get_db):
        db = self._make_mock_db()
        mock_get_db.return_value = db

        learn(
            query="check pods",
            tools_called=["list_pods", "describe_pod", "get_pod_logs"],
            tools_offered=["list_pods", "describe_pod", "get_pod_logs"],
        )

        calls = [str(c) for c in db.execute.call_args_list]
        cooccurrence_calls = [c for c in calls if "tool_cooccurrence" in c]
        assert len(cooccurrence_calls) == 3

    @patch("sre_agent.tool_predictor._get_db")
    def test_no_crash_on_db_failure(self, mock_get_db):
        mock_get_db.side_effect = Exception("DB down")
        learn(query="test", tools_called=["list_pods"], tools_offered=["list_pods"])

    @patch("sre_agent.tool_predictor._get_db")
    def test_skips_empty_calls(self, mock_get_db):
        db = self._make_mock_db()
        mock_get_db.return_value = db
        learn(query="test", tools_called=[], tools_offered=["list_pods"])
        assert not db.commit.called
