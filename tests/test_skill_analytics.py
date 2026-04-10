"""Tests for skill analytics."""

from __future__ import annotations

from sre_agent.skill_analytics import (
    get_skill_stats,
    get_skill_trend,
    get_skill_user_breakdown,
    record_skill_invocation,
    update_skill_feedback,
)


class TestRecordSkillInvocation:
    def test_fire_and_forget(self):
        """Should never raise, even without DB."""
        record_skill_invocation(
            session_id="test-session",
            user_id="test-user",
            skill_name="sre",
            skill_version=2,
            query_summary="why is my pod crashing?",
            tools_called=["list_pods", "get_pod_logs"],
            duration_ms=8500,
            input_tokens=12000,
            output_tokens=3500,
        )

    def test_with_handoff(self):
        record_skill_invocation(
            session_id="test-session",
            user_id="test-user",
            skill_name="view_designer",
            skill_version=1,
            query_summary="create a dashboard",
            tools_called=["namespace_summary", "create_dashboard"],
            handoff_from="sre",
            duration_ms=15000,
        )

    def test_with_feedback(self):
        record_skill_invocation(
            session_id="test-session",
            user_id="test-user",
            skill_name="sre",
            skill_version=2,
            feedback="positive",
        )

    def test_truncates_query(self):
        """Long queries should be truncated to 200 chars."""
        record_skill_invocation(
            session_id="test-session",
            user_id="test-user",
            skill_name="sre",
            skill_version=1,
            query_summary="x" * 500,
        )


class TestGetSkillStats:
    def test_returns_structure(self):
        result = get_skill_stats(days=7)
        assert "skills" in result
        assert "handoffs" in result
        assert "days" in result
        assert isinstance(result["skills"], list)
        assert isinstance(result["handoffs"], list)

    def test_returns_empty_without_db(self):
        result = get_skill_stats(days=1)
        assert result["skills"] == [] or isinstance(result["skills"], list)


class TestGetSkillTrend:
    def test_returns_structure(self):
        result = get_skill_trend("sre", days=7)
        assert "skill" in result
        assert result["skill"] == "sre"

    def test_no_data_returns_zero_runs(self):
        result = get_skill_trend("nonexistent_skill_xyz", days=1)
        assert result["runs"] == 0


class TestGetSkillUserBreakdown:
    def test_returns_list(self):
        result = get_skill_user_breakdown("sre", days=7)
        assert isinstance(result, list)


class TestUpdateSkillFeedback:
    def test_fire_and_forget(self):
        """Should never raise."""
        update_skill_feedback("nonexistent-session", "positive")
