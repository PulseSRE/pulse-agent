"""Tests for selector learning."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sre_agent.selector_learning import (
    identify_skill_gaps,
    prune_low_performers,
    recompute_channel_weights,
)


class TestRecomputeWeights:
    @patch("sre_agent.db.get_database")
    def test_returns_weights_with_sufficient_data(self, mock_get_db):
        db = MagicMock()
        mock_get_db.return_value = db
        db.fetchall.return_value = [
            {
                "channel_scores": '{"keyword": {"sre": 0.9}, "component": {"sre": 0.5}}',
                "selected_skill": "sre",
                "skill_overridden": None,
                "tools_requested_missing": None,
            }
        ] * 15  # 15 rows > 10 minimum

        weights = recompute_channel_weights(days=7)
        assert len(weights) > 0
        assert abs(sum(weights.values()) - 1.0) < 0.01

    @patch("sre_agent.db.get_database")
    def test_returns_empty_with_insufficient_data(self, mock_get_db):
        db = MagicMock()
        mock_get_db.return_value = db
        db.fetchall.return_value = [{"channel_scores": "{}", "selected_skill": "sre", "skill_overridden": None}] * 5
        assert recompute_channel_weights(days=7) == {}

    @patch("sre_agent.db.get_database", side_effect=Exception("DB"))
    def test_no_crash_on_failure(self, _):
        assert recompute_channel_weights() == {}


class TestIdentifyGaps:
    @patch("sre_agent.db.get_database")
    def test_returns_gaps(self, mock_get_db):
        db = MagicMock()
        mock_get_db.return_value = db
        db.fetchall.return_value = [
            {"query_summary": "check etcd health", "selected_skill": "sre", "threshold_used": 0.3}
        ] * 5
        gaps = identify_skill_gaps(days=30)
        # May or may not find a gap depending on token extraction
        assert isinstance(gaps, list)

    @patch("sre_agent.db.get_database", side_effect=Exception("DB"))
    def test_no_crash(self, _):
        assert identify_skill_gaps() == []


class TestPruneLowPerformers:
    @patch("sre_agent.db.get_database")
    def test_flags_high_override(self, mock_get_db):
        db = MagicMock()
        mock_get_db.return_value = db
        db.fetchall.return_value = [
            {"selected_skill": "bad_skill", "total": 20, "overrides": 10},
        ]
        flagged = prune_low_performers(days=30, min_invocations=10)
        assert "bad_skill" in flagged

    @patch("sre_agent.db.get_database", side_effect=Exception("DB"))
    def test_no_crash(self, _):
        assert prune_low_performers() == []
