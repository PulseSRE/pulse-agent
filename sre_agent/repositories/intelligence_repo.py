"""Intelligence repository -- all intelligence analytics database operations.

Extracted from ``intelligence.py`` to keep domain logic cohesive.  The original
module-level functions in ``intelligence.py`` now delegate here for backward
compatibility.
"""

from __future__ import annotations

import logging

from .base import BaseRepository

logger = logging.getLogger("pulse_agent.intelligence")


class IntelligenceRepository(BaseRepository):
    """Database operations for the intelligence analytics feedback loop."""

    # -- Query reliability -----------------------------------------------------

    def fetch_query_reliability(self, days: int) -> list[dict]:
        """Fetch PromQL query reliability data from promql_queries."""
        return (
            self.db.fetchall(
                "SELECT query_template, success_count, failure_count "
                "FROM promql_queries "
                "WHERE (last_success > NOW() - INTERVAL '1 day' * ? "
                "   OR last_failure > NOW() - INTERVAL '1 day' * ?) "
                "AND success_count + failure_count >= 3 "
                "ORDER BY success_count + failure_count DESC "
                "LIMIT 20",
                (days, days),
            )
            or []
        )

    # -- Dashboard patterns ----------------------------------------------------

    def fetch_dashboard_tool_usage(self, days: int) -> list[dict]:
        """Fetch view_designer tool usage stats."""
        return (
            self.db.fetchall(
                "SELECT tool_name, COUNT(*) as call_count "
                "FROM tool_usage "
                "WHERE agent_mode = 'view_designer' "
                "  AND timestamp > NOW() - INTERVAL '1 day' * ? "
                "  AND status = 'success' "
                "GROUP BY tool_name "
                "ORDER BY call_count DESC "
                "LIMIT 10",
                (days,),
            )
            or []
        )

    def fetch_avg_widgets_per_session(self, days: int) -> dict | None:
        """Fetch average tools per view_designer session."""
        return self.db.fetchone(
            "SELECT AVG(tool_count)::int as avg_tools FROM ("
            "    SELECT session_id, COUNT(*) as tool_count "
            "    FROM tool_usage "
            "    WHERE agent_mode = 'view_designer' "
            "      AND timestamp > NOW() - INTERVAL '1 day' * ? "
            "    GROUP BY session_id"
            ") sub",
            (days,),
        )

    # -- Error hotspots --------------------------------------------------------

    def fetch_error_hotspots(self, days: int) -> list[dict]:
        """Fetch tools with high error rates including top error messages."""
        return (
            self.db.fetchall(
                "WITH hotspots AS ("
                "    SELECT tool_name, "
                "           COUNT(*) FILTER (WHERE status = 'error') as error_count, "
                "           COUNT(*) as total_count "
                "    FROM tool_usage "
                "    WHERE timestamp > NOW() - INTERVAL '1 day' * ? "
                "    GROUP BY tool_name "
                "    HAVING COUNT(*) > 5 "
                "       AND COUNT(*) FILTER (WHERE status = 'error')::float / COUNT(*) > 0.05 "
                "    ORDER BY COUNT(*) FILTER (WHERE status = 'error')::float / COUNT(*) DESC "
                "    LIMIT 5"
                "), "
                "top_errors AS ("
                "    SELECT tool_name, error_message, COUNT(*) as cnt, "
                "           ROW_NUMBER() OVER (PARTITION BY tool_name ORDER BY COUNT(*) DESC) as rn "
                "    FROM tool_usage "
                "    WHERE tool_name IN (SELECT tool_name FROM hotspots) "
                "      AND status = 'error' "
                "      AND timestamp > NOW() - INTERVAL '1 day' * ? "
                "      AND error_message IS NOT NULL "
                "    GROUP BY tool_name, error_message"
                ") "
                "SELECT h.tool_name, h.error_count, h.total_count, "
                "       COALESCE(SUBSTRING(te.error_message, 1, 80), '') as common_error "
                "FROM hotspots h "
                "LEFT JOIN top_errors te ON h.tool_name = te.tool_name AND te.rn = 1 "
                "ORDER BY h.error_count::float / h.total_count DESC",
                (days, days),
            )
            or []
        )

    # -- Token efficiency ------------------------------------------------------

    def fetch_token_efficiency(self, days: int) -> dict | None:
        """Fetch token efficiency averages from tool_turns."""
        return self.db.fetchone(
            "SELECT COALESCE(ROUND(AVG(input_tokens)), 0) AS avg_input, "
            "COALESCE(ROUND(AVG(output_tokens)), 0) AS avg_output, "
            "COALESCE(ROUND(AVG(cache_read_tokens)), 0) AS avg_cache, "
            "COUNT(*) AS total_turns "
            "FROM tool_turns "
            "WHERE input_tokens IS NOT NULL "
            "AND timestamp > NOW() - INTERVAL '1 day' * ?",
            (days,),
        )

    # -- Harness effectiveness -------------------------------------------------

    def fetch_harness_accuracy(self, days: int) -> dict | None:
        """Fetch harness tool selection accuracy from tool_turns."""
        return self.db.fetchone(
            "SELECT AVG(CASE "
            "  WHEN array_length(tools_called, 1) IS NULL OR array_length(tools_called, 1) = 0 THEN NULL "
            "  ELSE LEAST(array_length(tools_called, 1)::float "
            "       / NULLIF(array_length(tools_offered, 1), 0), 1.0) "
            "END) as accuracy, "
            "COALESCE(ROUND(AVG(array_length(tools_called, 1))), 0) as avg_called, "
            "COALESCE(ROUND(AVG(array_length(tools_offered, 1))), 0) as avg_offered "
            "FROM tool_turns "
            "WHERE tools_offered IS NOT NULL AND tools_called IS NOT NULL "
            "AND array_length(tools_called, 1) > 0 "
            "AND timestamp > NOW() - INTERVAL '1 day' * ?",
            (days,),
        )

    def fetch_wasted_tools(self, days: int, threshold: float = 0.05, limit: int | None = 10) -> list[dict]:
        """Query tools that are offered frequently but rarely called."""
        query = (
            "WITH offered AS ("
            "    SELECT unnest(tools_offered) as tool_name, COUNT(*) as offered_count "
            "    FROM tool_turns "
            "    WHERE timestamp > NOW() - INTERVAL '1 day' * ? AND tools_offered IS NOT NULL "
            "    GROUP BY 1"
            "), "
            "called AS ("
            "    SELECT unnest(tools_called) as tool_name, COUNT(*) as called_count "
            "    FROM tool_turns "
            "    WHERE timestamp > NOW() - INTERVAL '1 day' * ? AND tools_called IS NOT NULL "
            "    GROUP BY 1"
            ") "
            "SELECT o.tool_name, o.offered_count, COALESCE(c.called_count, 0) as called_count "
            "FROM offered o "
            "LEFT JOIN called c ON o.tool_name = c.tool_name "
            "WHERE o.offered_count >= 20 "
            f"AND COALESCE(c.called_count, 0)::float / o.offered_count < {threshold} "
            "ORDER BY o.offered_count DESC"
        )
        if limit is not None:
            query += f" LIMIT {limit}"
        return self.db.fetchall(query, (days, days)) or []

    # -- Routing accuracy ------------------------------------------------------

    def fetch_mode_switch_rate(self, days: int) -> dict | None:
        """Fetch mode switch rate from tool_turns."""
        return self.db.fetchone(
            "SELECT "
            "    COUNT(*) FILTER (WHERE agent_mode != prev_mode) as switches, "
            "    COUNT(*) as total "
            "FROM ("
            "    SELECT agent_mode, "
            "           LAG(agent_mode) OVER (PARTITION BY session_id ORDER BY turn_number) as prev_mode "
            "    FROM tool_turns "
            "    WHERE timestamp > NOW() - INTERVAL '1 day' * ?"
            ") sub "
            "WHERE prev_mode IS NOT NULL",
            (days,),
        )

    def fetch_misroutes(self, days: int) -> list[dict]:
        """Fetch recent skill misroutes from tool_turns."""
        return (
            self.db.fetchall(
                "SELECT prev_skill, next_skill, query_summary, prev_score "
                "FROM ("
                "    SELECT routing_skill as prev_skill, "
                "           LEAD(routing_skill) OVER (PARTITION BY session_id ORDER BY turn_number) as next_skill, "
                "           query_summary, "
                "           routing_score as prev_score "
                "    FROM tool_turns "
                "    WHERE timestamp > NOW() - INTERVAL '1 day' * ? "
                "      AND routing_skill IS NOT NULL"
                ") sub "
                "WHERE next_skill IS NOT NULL AND prev_skill != next_skill "
                "ORDER BY prev_score ASC NULLS FIRST "
                "LIMIT 20",
                (days,),
            )
            or []
        )

    # -- Feedback analysis -----------------------------------------------------

    def fetch_negative_feedback(self, days: int) -> list[dict]:
        """Fetch tools with negative feedback."""
        return (
            self.db.fetchall(
                "SELECT u.tool_name, "
                "       COUNT(*) FILTER (WHERE t.feedback = 'negative') as negative, "
                "       COUNT(*) as total "
                "FROM tool_turns t "
                "JOIN tool_usage u ON t.session_id = u.session_id AND t.turn_number = u.turn_number "
                "WHERE t.feedback IS NOT NULL "
                "AND t.timestamp > NOW() - INTERVAL '1 day' * ? "
                "GROUP BY u.tool_name "
                "HAVING COUNT(*) FILTER (WHERE t.feedback = 'negative') > 0 "
                "ORDER BY COUNT(*) FILTER (WHERE t.feedback = 'negative')::float / COUNT(*) DESC "
                "LIMIT 5",
                (days,),
            )
            or []
        )

    # -- Token trending --------------------------------------------------------

    def fetch_token_trending(self, days: int) -> dict | None:
        """Fetch week-over-week token usage trending from tool_turns."""
        return self.db.fetchone(
            "SELECT "
            "    AVG(input_tokens) FILTER (WHERE timestamp > NOW() - INTERVAL '1 day' * ?) as current_input, "
            "    AVG(input_tokens) FILTER (WHERE timestamp BETWEEN "
            "NOW() - INTERVAL '1 day' * ? AND NOW() - INTERVAL '1 day' * ?) as prev_input, "
            "    AVG(output_tokens) FILTER (WHERE timestamp > NOW() - INTERVAL '1 day' * ?) as current_output, "
            "    AVG(output_tokens) FILTER (WHERE timestamp BETWEEN "
            "NOW() - INTERVAL '1 day' * ? AND NOW() - INTERVAL '1 day' * ?) as prev_output, "
            "    AVG(cache_read_tokens) FILTER (WHERE timestamp > NOW() - INTERVAL '1 day' * ?) as current_cache, "
            "    AVG(cache_read_tokens) FILTER (WHERE timestamp BETWEEN "
            "NOW() - INTERVAL '1 day' * ? AND NOW() - INTERVAL '1 day' * ?) as prev_cache "
            "FROM tool_turns "
            "WHERE input_tokens IS NOT NULL",
            (days, days * 2, days, days, days * 2, days, days, days * 2, days),
        )

    # -- Fix outcomes ----------------------------------------------------------

    def fetch_fix_outcomes(self, days: int) -> list[dict]:
        """Fetch fix strategy effectiveness from actions table."""
        return (
            self.db.fetchall(
                "SELECT tool, category, "
                "COUNT(*) AS total, "
                "SUM(CASE WHEN verification_status = 'verified' THEN 1 ELSE 0 END) AS resolved "
                "FROM actions "
                "WHERE timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * %s)::BIGINT * 1000 "
                "AND tool IS NOT NULL AND tool != '' "
                "GROUP BY tool, category "
                "HAVING COUNT(*) >= 2 "
                "ORDER BY COUNT(*) DESC "
                "LIMIT 10",
                (days,),
            )
            or []
        )


# -- Singleton ---------------------------------------------------------------

_intelligence_repo: IntelligenceRepository | None = None


def get_intelligence_repo() -> IntelligenceRepository:
    """Return the module-level IntelligenceRepository singleton."""
    global _intelligence_repo
    if _intelligence_repo is None:
        _intelligence_repo = IntelligenceRepository()
    return _intelligence_repo
