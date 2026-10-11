"""Analytics repository -- all analytics/metrics/session database operations.

Covers: analytics_rest.py, metrics_rest.py confidence calibration, accuracy,
cost stats, session analytics, user events, and interaction queries.
"""

from __future__ import annotations

import logging

from .base import BaseRepository

logger = logging.getLogger("pulse_agent.analytics")


class AnalyticsRepository(BaseRepository):
    """Database operations for analytics and operational metrics."""

    # -- Confidence calibration ------------------------------------------------

    def fetch_confidence_pairs(self, days: int) -> list[dict]:
        """Fetch investigation confidence + action verification pairs."""
        cutoff_sql = "EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * ?) * 1000"
        return self.db.fetchall(
            f"""
            SELECT i.confidence, a.verification_status
            FROM investigations i
            INNER JOIN actions a ON a.finding_id = i.finding_id
            WHERE i.confidence IS NOT NULL
              AND a.verification_status IS NOT NULL
              AND a.timestamp > {cutoff_sql}
            """,
            (days,),
        )

    # -- Accuracy / override rate ----------------------------------------------

    def fetch_override_rate(self, days: int) -> dict | None:
        """Query confirmation-required actions for override rate."""
        cutoff_sql = "EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * ?) * 1000"
        return self.db.fetchone(
            f"""
            SELECT
                COUNT(*) FILTER (WHERE requires_confirmation = TRUE) as total_proposed,
                COUNT(*) FILTER (WHERE requires_confirmation = TRUE AND was_confirmed = FALSE) as rejected_actions
            FROM tool_usage
            WHERE timestamp > TO_TIMESTAMP({cutoff_sql})
            """,
            (days,),
        )

    # -- Cost stats (tool_turns) -----------------------------------------------

    def fetch_token_totals(self, days: int) -> dict | None:
        """Current period token totals and incident count."""
        return self.db.fetchone(
            "SELECT "
            "  COALESCE(SUM(input_tokens), 0) AS total_input, "
            "  COALESCE(SUM(output_tokens), 0) AS total_output, "
            "  COALESCE(SUM(cache_read_tokens), 0) AS total_cache_read, "
            "  COALESCE(SUM(cache_creation_tokens), 0) AS total_cache_write, "
            "  COUNT(DISTINCT session_id) AS total_incidents "
            "FROM tool_turns WHERE timestamp > NOW() - INTERVAL '1 day' * ? AND input_tokens IS NOT NULL",
            (days,),
        )

    def fetch_prev_period_tokens(self, days: int) -> dict | None:
        """Previous period token totals for trend comparison."""
        return self.db.fetchone(
            "SELECT "
            "  COALESCE(SUM(input_tokens + output_tokens), 0) AS total_tokens, "
            "  COUNT(DISTINCT session_id) AS total_incidents "
            "FROM tool_turns "
            "WHERE timestamp > NOW() - INTERVAL '1 day' * ? AND timestamp <= NOW() - INTERVAL '1 day' * ? "
            "AND input_tokens IS NOT NULL",
            (days * 2, days),
        )

    def fetch_tokens_by_mode(self, days: int) -> list[dict]:
        """Token breakdown by agent_mode."""
        return self.db.fetchall(
            "SELECT agent_mode, "
            "  COUNT(DISTINCT session_id) AS incident_count, "
            "  COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            "  COALESCE(SUM(output_tokens), 0) AS output_tokens, "
            "  COALESCE(SUM(input_tokens + output_tokens), 0) AS total_tokens "
            "FROM tool_turns WHERE timestamp > NOW() - INTERVAL '1 day' * ? AND input_tokens IS NOT NULL "
            "GROUP BY agent_mode ORDER BY total_tokens DESC",
            (days,),
        )

    def fetch_daily_cost_totals(self, days: int) -> list[dict]:
        """Fetch daily token totals for cost forecasting."""
        return self.db.fetchall(
            "SELECT DATE(timestamp) AS day, "
            "  COALESCE(SUM(input_tokens), 0) AS input_t, "
            "  COALESCE(SUM(output_tokens), 0) AS output_t, "
            "  COALESCE(SUM(cache_read_tokens), 0) AS cache_read_t, "
            "  COALESCE(SUM(cache_creation_tokens), 0) AS cache_write_t "
            "FROM tool_turns "
            "WHERE timestamp > NOW() - INTERVAL '1 day' * ? "
            "AND input_tokens IS NOT NULL "
            "GROUP BY DATE(timestamp) ORDER BY day",
            (days,),
        )

    # -- Recommendations -------------------------------------------------------

    def fetch_tool_usage_by_pattern(self, pattern: str, days: int = 30) -> dict | None:
        """Check tool usage matching a LIKE pattern."""
        return self.db.fetchone(
            "SELECT COUNT(*) AS cnt FROM tool_usage "
            "WHERE tool_name LIKE %s "
            "AND timestamp > EXTRACT(EPOCH FROM NOW() - INTERVAL '30 days') * 1000",
            (pattern,),
        )

    # -- User events -----------------------------------------------------------

    def insert_user_event(
        self,
        session_id: str,
        user_id: str,
        event_type: str,
        page: str,
        data_json: str,
    ) -> None:
        """Insert a user session event."""
        self.db.execute(
            "INSERT INTO user_events (session_id, user_id, event_type, page, data) VALUES (%s, %s, %s, %s, %s)",
            (session_id, user_id, event_type, page, data_json),
        )

    def commit(self) -> None:
        """Commit the current transaction."""
        self.db.commit()

    # -- Session analytics -----------------------------------------------------

    def fetch_session_summary(self, days: int) -> dict | None:
        """Total page views, sessions, unique pages."""
        return self.db.fetchone(
            "SELECT COUNT(*) as total_views, "
            "COUNT(DISTINCT session_id) as total_sessions, "
            "COUNT(DISTINCT page) as unique_pages "
            "FROM user_events WHERE event_type = 'page_view' "
            "AND timestamp > NOW() - INTERVAL '%s days'",
            (days,),
        )

    def fetch_total_queries(self, days: int) -> dict | None:
        """Total agent queries."""
        return self.db.fetchone(
            "SELECT COUNT(*) as total_queries "
            "FROM user_events WHERE event_type = 'agent_query' "
            "AND timestamp > NOW() - INTERVAL '%s days'",
            (days,),
        )

    def fetch_avg_page_duration(self, days: int) -> dict | None:
        """Average duration from page_leave events."""
        return self.db.fetchone(
            "SELECT AVG((data->>'duration_ms')::int) as avg_ms "
            "FROM user_events WHERE event_type = 'page_leave' "
            "AND timestamp > NOW() - INTERVAL '%s days'",
            (days,),
        )

    def fetch_top_pages(self, days: int, limit: int = 20) -> list[dict]:
        """Top pages by visit count."""
        return self.db.fetchall(
            "SELECT page, COUNT(*) as views, COUNT(DISTINCT session_id) as sessions "
            "FROM user_events WHERE event_type = 'page_view' "
            "AND timestamp > NOW() - INTERVAL '%s days' "
            "GROUP BY page ORDER BY views DESC LIMIT %s",
            (days, limit),
        )

    def fetch_page_durations(self, days: int, limit: int = 20) -> list[dict]:
        """Average time on page."""
        return self.db.fetchall(
            "SELECT page, AVG((data->>'duration_ms')::int) as avg_ms, "
            "COUNT(*) as sample_count "
            "FROM user_events WHERE event_type = 'page_leave' "
            "AND timestamp > NOW() - INTERVAL '%s days' "
            "GROUP BY page ORDER BY avg_ms DESC LIMIT %s",
            (days, limit),
        )

    def fetch_queries_by_page(self, days: int, limit: int = 20) -> list[dict]:
        """Agent queries by page."""
        return self.db.fetchall(
            "SELECT page, COUNT(*) as queries "
            "FROM user_events WHERE event_type = 'agent_query' "
            "AND timestamp > NOW() - INTERVAL '%s days' "
            "GROUP BY page ORDER BY queries DESC LIMIT %s",
            (days, limit),
        )

    def fetch_top_suggestions(self, days: int, limit: int = 10) -> list[dict]:
        """Top follow-up suggestions clicked."""
        return self.db.fetchall(
            "SELECT data->>'text' as suggestion, COUNT(*) as clicks "
            "FROM user_events WHERE event_type = 'suggestion_click' "
            "AND timestamp > NOW() - INTERVAL '%s days' "
            "GROUP BY data->>'text' ORDER BY clicks DESC LIMIT %s",
            (days, limit),
        )

    def fetch_feature_usage(self, days: int, limit: int = 10) -> list[dict]:
        """Feature usage counts."""
        return self.db.fetchall(
            "SELECT data->>'feature' as feature, COUNT(*) as uses "
            "FROM user_events WHERE event_type = 'feature_use' "
            "AND timestamp > NOW() - INTERVAL '%s days' "
            "GROUP BY data->>'feature' ORDER BY uses DESC LIMIT %s",
            (days, limit),
        )

    # -- Metrics: response latency ---------------------------------------------

    def fetch_response_latency(self, period: int) -> dict | None:
        """Response latency percentiles from tool_usage."""
        return self.db.fetchone(
            "SELECT "
            "  percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50, "
            "  percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95, "
            "  percentile_cont(0.99) WITHIN GROUP (ORDER BY duration_ms) AS p99, "
            "  AVG(duration_ms) AS avg_ms, "
            "  COUNT(*) AS cnt "
            "FROM tool_usage "
            "WHERE timestamp >= NOW() - INTERVAL '1 day' * ? "
            "AND duration_ms > 0",
            (period,),
        )

    # -- Metrics: eval trend ---------------------------------------------------

    def fetch_eval_scores(self, suite: str, limit: int) -> list[dict]:
        """Fetch recent eval scores for sparkline."""
        return self.db.fetchall(
            "SELECT average_overall AS score, gate_passed AS pass, scenario_count AS scenarios, timestamp "
            "FROM eval_runs WHERE suite_name = ? ORDER BY id DESC LIMIT ?",
            (suite, limit),
        )

    # -- Usage summary ---------------------------------------------------------

    def fetch_usage_by_mode(self, cutoff_s: float) -> list[dict]:
        """Tool usage grouped by agent_mode."""
        return self.db.fetchall(
            "SELECT agent_mode, COUNT(*) as calls, COALESCE(SUM(duration_ms), 0) as total_ms "
            "FROM tool_usage WHERE timestamp > to_timestamp(?) GROUP BY agent_mode",
            (cutoff_s,),
        )

    # -- Interactions ----------------------------------------------------------

    def fetch_interactions(self, where: str, params: tuple, limit: int) -> list[dict]:
        """Query user_interactions audit log."""
        return self.db.fetchall(
            f"SELECT * FROM user_interactions {where} ORDER BY timestamp DESC LIMIT ?",
            params + (limit,),
        )


# -- Singleton ---------------------------------------------------------------

_analytics_repo: AnalyticsRepository | None = None


def get_analytics_repo() -> AnalyticsRepository:
    """Return the module-level AnalyticsRepository singleton."""
    global _analytics_repo
    if _analytics_repo is None:
        _analytics_repo = AnalyticsRepository()
    return _analytics_repo
