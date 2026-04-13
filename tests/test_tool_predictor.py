"""Tests for adaptive tool prediction engine."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from sre_agent.db import Database
from sre_agent.db_migrations import run_migrations
from sre_agent.tool_predictor import PredictionResult, decay_scores, learn, llm_pick_tools, predict_tools

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


class TestPredictTools:
    @patch("sre_agent.tool_predictor._get_db")
    def test_returns_prediction_result(self, mock_get_db):
        db = MagicMock()
        db.fetchall.return_value = [
            {"tool_name": "list_pods", "total_score": 10.0, "total_hits": 15},
            {"tool_name": "describe_pod", "total_score": 8.0, "total_hits": 12},
            {"tool_name": "get_pod_logs", "total_score": 6.0, "total_hits": 11},
        ]
        mock_get_db.return_value = db

        result = predict_tools("show me pods", top_k=10)
        assert isinstance(result, PredictionResult)
        assert result.confidence == "high"
        assert "list_pods" in result.tools

    @patch("sre_agent.tool_predictor._get_db")
    def test_low_confidence_when_no_data(self, mock_get_db):
        db = MagicMock()
        db.fetchall.return_value = []
        mock_get_db.return_value = db

        result = predict_tools("show me pods", top_k=10)
        assert result.confidence == "low"
        assert result.tools == []

    @patch("sre_agent.tool_predictor._get_db")
    def test_low_confidence_when_sparse_hits(self, mock_get_db):
        db = MagicMock()
        db.fetchall.return_value = [
            {"tool_name": "list_pods", "total_score": 2.0, "total_hits": 2},
        ]
        mock_get_db.return_value = db

        result = predict_tools("show me pods", top_k=10)
        assert result.confidence == "low"

    @patch("sre_agent.tool_predictor._get_db")
    def test_cooccurrence_expansion(self, mock_get_db):
        db = MagicMock()
        db.fetchall.side_effect = [
            [
                {"tool_name": "describe_pod", "total_score": 20.0, "total_hits": 15},
            ],
            [
                {"tool_b": "get_pod_logs", "frequency": 50},
                {"tool_b": "get_events", "frequency": 30},
            ],
        ]
        mock_get_db.return_value = db

        result = predict_tools("describe the pod", top_k=10)
        assert "describe_pod" in result.tools
        assert "get_pod_logs" in result.tools


class TestDecay:
    @patch("sre_agent.tool_predictor._get_db")
    def test_decay_multiplies_scores(self, mock_get_db):
        db = MagicMock()
        mock_get_db.return_value = db

        decay_scores(factor=0.95, prune_days=30)

        calls = [str(c) for c in db.execute.call_args_list]
        assert any("score" in c and "0.95" in c for c in calls)
        assert any("DELETE" in c for c in calls)
        assert db.commit.called

    @patch("sre_agent.tool_predictor._get_db")
    def test_no_crash_on_failure(self, mock_get_db):
        mock_get_db.side_effect = Exception("DB down")
        decay_scores()


class TestLLMPicker:
    @patch("sre_agent.agent.create_client")
    def test_returns_tool_list(self, mock_create):
        client = MagicMock()
        mock_create.return_value = client
        client.messages.create.return_value = MagicMock(
            content=[MagicMock(text="list_pods, describe_pod, get_pod_logs")]
        )

        tools = llm_pick_tools(
            query="why are pods crashing",
            tool_names=["list_pods", "describe_pod", "get_pod_logs", "scale_deployment", "drain_node"],
            top_k=3,
        )
        assert "list_pods" in tools
        assert "describe_pod" in tools
        assert len(tools) <= 3

    @patch("sre_agent.agent.create_client")
    def test_filters_invalid_names(self, mock_create):
        client = MagicMock()
        mock_create.return_value = client
        client.messages.create.return_value = MagicMock(content=[MagicMock(text="list_pods, FAKE_TOOL, describe_pod")])

        tools = llm_pick_tools(
            query="check pods",
            tool_names=["list_pods", "describe_pod"],
            top_k=10,
        )
        assert "FAKE_TOOL" not in tools
        assert "list_pods" in tools

    @patch("sre_agent.agent.create_client")
    def test_returns_empty_on_failure(self, mock_create):
        mock_create.side_effect = Exception("API down")
        tools = llm_pick_tools(query="test", tool_names=["list_pods"], top_k=5)
        assert tools == []
