"""Skill analytics repository -- skill_usage and skill_selector database operations.

Covers: skill_analytics.py (record/query skill usage), skill_selector.py
(temporal signal, historical co-occurrence, success rates, selection outcome logging).
"""

from __future__ import annotations

import logging

from .base import BaseRepository

logger = logging.getLogger("pulse_agent.skill_analytics")


class SkillAnalyticsRepository(BaseRepository):
    """Database operations for skill usage tracking and ORCA selector state."""

    # -- Recording (skill_analytics.py) ----------------------------------------

    def record_invocation(
        self,
        *,
        session_id: str,
        user_id: str,
        skill_name: str,
        skill_version: int,
        query_summary: str,
        tools_called: list[str] | None,
        handoff_from: str | None,
        handoff_to: str | None,
        duration_ms: int,
        input_tokens: int,
        output_tokens: int,
        feedback: str | None,
        eval_score: float | None,
    ) -> None:
        """Record a skill invocation."""
        self.db.execute(
            "INSERT INTO skill_usage "
            "(session_id, user_id, skill_name, skill_version, query_summary, "
            "tools_called, tool_count, handoff_from, handoff_to, duration_ms, "
            "input_tokens, output_tokens, feedback, eval_score) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                session_id,
                user_id,
                skill_name,
                skill_version,
                (query_summary[:200] if query_summary else ""),
                tools_called or [],
                len(tools_called or []),
                handoff_from,
                handoff_to,
                duration_ms,
                input_tokens,
                output_tokens,
                feedback,
                eval_score,
            ),
        )
        self.db.commit()

    # -- Per-skill stats -------------------------------------------------------

    def fetch_skill_stats(self, days: int) -> list[dict]:
        """Per-skill aggregate stats."""
        return self.db.fetchall(
            "SELECT skill_name, COUNT(*) as invocations, "
            "COALESCE(ROUND(AVG(tool_count)), 0) as avg_tools, "
            "COALESCE(ROUND(AVG(duration_ms)), 0) as avg_duration_ms, "
            "COALESCE(ROUND(AVG(input_tokens)), 0) as avg_input_tokens, "
            "COALESCE(ROUND(AVG(output_tokens)), 0) as avg_output_tokens, "
            "COUNT(*) FILTER (WHERE feedback = 'positive') as feedback_positive, "
            "COUNT(*) FILTER (WHERE feedback = 'negative') as feedback_negative "
            "FROM skill_usage "
            "WHERE timestamp > NOW() - INTERVAL '1 day' * %s "
            "GROUP BY skill_name ORDER BY invocations DESC",
            (days,),
        )

    def fetch_all_skill_tools(self, days: int) -> list[dict]:
        """Top tools per skill in a single query."""
        return self.db.fetchall(
            "SELECT skill_name, unnest(tools_called) as tool_name, COUNT(*) as cnt "
            "FROM skill_usage "
            "WHERE timestamp > NOW() - INTERVAL '1 day' * %s AND tools_called IS NOT NULL "
            "GROUP BY skill_name, tool_name ORDER BY skill_name, cnt DESC",
            (days,),
        )

    def fetch_handoffs(self, days: int) -> list[dict]:
        """Handoff flows between skills."""
        return self.db.fetchall(
            "SELECT handoff_from, handoff_to, COUNT(*) as cnt "
            "FROM skill_usage "
            "WHERE handoff_from IS NOT NULL AND handoff_to IS NOT NULL "
            "AND timestamp > NOW() - INTERVAL '1 day' * %s "
            "GROUP BY handoff_from, handoff_to ORDER BY cnt DESC",
            (days,),
        )

    # -- Skill trend -----------------------------------------------------------

    def fetch_skill_trend(self, skill_name: str, days: int) -> list[dict]:
        """Daily skill usage with average duration."""
        return self.db.fetchall(
            "SELECT DATE(timestamp) as day, COUNT(*) as cnt, "
            "COALESCE(ROUND(AVG(duration_ms)), 0) as avg_duration "
            "FROM skill_usage "
            "WHERE skill_name = %s AND timestamp > NOW() - INTERVAL '1 day' * %s "
            "GROUP BY DATE(timestamp) ORDER BY day",
            (skill_name, days),
        )

    # -- Per-user breakdown ----------------------------------------------------

    def fetch_skill_user_breakdown(self, skill_name: str, days: int, limit: int = 20) -> list[dict]:
        """Per-user usage for a skill."""
        return self.db.fetchall(
            "SELECT user_id, COUNT(*) as invocations, "
            "COALESCE(ROUND(AVG(duration_ms)), 0) as avg_duration_ms "
            "FROM skill_usage "
            "WHERE skill_name = %s AND timestamp > NOW() - INTERVAL '1 day' * %s "
            "GROUP BY user_id ORDER BY invocations DESC LIMIT %s",
            (skill_name, days, limit),
        )

    # -- Feedback --------------------------------------------------------------

    def update_feedback(self, session_id: str, feedback: str) -> None:
        """Link user feedback to the most recent skill invocation."""
        self.db.execute(
            "UPDATE skill_usage SET feedback = %s "
            "WHERE id = (SELECT id FROM skill_usage WHERE session_id = %s ORDER BY timestamp DESC LIMIT 1)",
            (feedback, session_id),
        )
        self.db.commit()

    # -- Selector: temporal signal (skill_selector.py) -------------------------

    def fetch_recent_deploy_count(self) -> dict | None:
        """Count recent audit_deployment findings (last 30 minutes)."""
        return self.db.fetchone(
            "SELECT COUNT(*) as cnt FROM inbox_items WHERE created_by = 'system:monitor' "
            "AND finding_id IS NOT NULL AND finding_id <> '' "
            "AND split_part(correlation_key, ':', 1) = 'audit_deployment' "
            "AND created_at > EXTRACT(EPOCH FROM NOW() - INTERVAL '30 minutes')::BIGINT"
        )

    def fetch_active_incident_count(self) -> dict | None:
        """Count unresolved non-audit findings."""
        return self.db.fetchone(
            "SELECT COUNT(*) as cnt FROM inbox_items WHERE created_by = 'system:monitor' "
            "AND finding_id IS NOT NULL AND finding_id <> '' "
            "AND status NOT IN ('resolved', 'archived') "
            "AND LEFT(split_part(correlation_key, ':', 1), 6) <> 'audit_'"
        )

    # -- Selector: historical co-occurrence ------------------------------------

    def fetch_recent_skill_queries(self, days: int = 7, limit: int = 200) -> list[dict]:
        """Recent skill_usage rows for historical token->skill mapping."""
        return self.db.fetchall(
            "SELECT skill_name, query_summary "
            "FROM skill_usage "
            "WHERE (feedback IS NULL OR feedback != 'negative') "
            "AND timestamp > NOW() - INTERVAL '%s days' "
            "ORDER BY timestamp DESC "
            "LIMIT %s",
            (days, limit),
        )

    # -- Selector: success rate ------------------------------------------------

    def fetch_skill_success_rate(self, skill_name: str) -> dict | None:
        """Success rate for a skill (positive or no-negative feedback)."""
        return self.db.fetchone(
            "SELECT COUNT(*) FILTER (WHERE feedback IS NULL OR feedback != 'negative') AS good, "
            "COUNT(*) AS total "
            "FROM skill_usage "
            "WHERE skill_name = %s "
            "AND timestamp > NOW() - INTERVAL '30 days'",
            (skill_name,),
        )

    # -- Selector: selection outcome logging -----------------------------------

    def record_selection_outcome(
        self,
        *,
        session_id: str,
        query_summary: str,
        channel_scores_json: str,
        fused_scores_json: str,
        selected_skill: str,
        threshold_used: float,
        conflicts_json: str | None,
        skill_overridden: str | None,
        tools_requested_missing: list[str] | None,
        selection_ms: int,
        channel_weights_json: str,
    ) -> None:
        """Log a selection outcome to skill_selection_log."""
        self.db.execute(
            "INSERT INTO skill_selection_log "
            "(session_id, query_summary, channel_scores, fused_scores, selected_skill, "
            "threshold_used, conflicts_detected, skill_overridden, tools_requested_missing, "
            "selection_ms, channel_weights) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                session_id,
                query_summary[:200],
                channel_scores_json,
                fused_scores_json,
                selected_skill,
                threshold_used,
                conflicts_json,
                skill_overridden,
                tools_requested_missing or None,
                selection_ms,
                channel_weights_json,
            ),
        )
        self.db.commit()


# -- Singleton ---------------------------------------------------------------

_skill_analytics_repo: SkillAnalyticsRepository | None = None


def get_skill_analytics_repo() -> SkillAnalyticsRepository:
    """Return the module-level SkillAnalyticsRepository singleton."""
    global _skill_analytics_repo
    if _skill_analytics_repo is None:
        _skill_analytics_repo = SkillAnalyticsRepository()
    return _skill_analytics_repo
