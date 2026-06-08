"""Tool usage repository -- all tool_usage/tool_turns database operations.

Extracted from ``tool_usage.py`` to keep domain logic cohesive.  The original
module-level functions in ``tool_usage.py`` now delegate here for backward
compatibility.
"""

from __future__ import annotations

import logging

from .base import BaseRepository

logger = logging.getLogger("pulse_agent.tool_usage")


class ToolUsageRepository(BaseRepository):
    """Database operations for tool usage tracking."""

    # -- Recording -------------------------------------------------------------

    def insert_tool_call(
        self,
        *,
        session_id: str,
        turn_number: int,
        agent_mode: str,
        tool_name: str,
        tool_category: str | None,
        input_summary: str | None,
        status: str,
        error_message: str | None,
        error_category: str | None,
        duration_ms: int,
        result_bytes: int,
        requires_confirmation: bool,
        was_confirmed: bool | None,
        tool_source: str = "native",
    ) -> None:
        """Insert a tool call record into tool_usage."""
        self.db.execute(
            "INSERT INTO tool_usage "
            "(session_id, turn_number, agent_mode, tool_name, tool_category, "
            "input_summary, status, error_message, error_category, "
            "duration_ms, result_bytes, requires_confirmation, was_confirmed, tool_source) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                session_id,
                turn_number,
                agent_mode,
                tool_name,
                tool_category,
                input_summary,
                status,
                error_message,
                error_category,
                duration_ms,
                result_bytes,
                requires_confirmation,
                was_confirmed,
                tool_source,
            ),
        )
        self.db.commit()

    async def async_insert_tool_call(
        self,
        *,
        session_id: str,
        turn_number: int,
        agent_mode: str,
        tool_name: str,
        tool_category: str | None,
        input_summary: str | None,
        status: str,
        error_message: str | None,
        error_category: str | None,
        duration_ms: int,
        result_bytes: int,
        requires_confirmation: bool,
        was_confirmed: bool | None,
        tool_source: str = "native",
    ) -> None:
        """Async version of insert_tool_call for use in async contexts."""
        db = await self.get_async_db()
        await db.execute(
            "INSERT INTO tool_usage "
            "(session_id, turn_number, agent_mode, tool_name, tool_category, "
            "input_summary, status, error_message, error_category, "
            "duration_ms, result_bytes, requires_confirmation, was_confirmed, tool_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            session_id,
            turn_number,
            agent_mode,
            tool_name,
            tool_category,
            input_summary,
            status,
            error_message,
            error_category,
            duration_ms,
            result_bytes,
            requires_confirmation,
            was_confirmed,
            tool_source,
        )

    def upsert_turn(
        self,
        *,
        session_id: str,
        turn_number: int,
        agent_mode: str,
        query_summary: str,
        tools_offered: list[str],
        tools_called: list[str],
        input_tokens: int | None,
        output_tokens: int | None,
        cache_read_tokens: int | None,
        cache_creation_tokens: int | None,
        routing_skill: str | None,
        routing_score: float | None,
        routing_competing: str | None,
        routing_used_llm: bool,
    ) -> None:
        """Upsert a turn into tool_turns."""
        self.db.execute(
            "INSERT INTO tool_turns "
            "(session_id, turn_number, agent_mode, query_summary, tools_offered, tools_called, "
            "input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, "
            "routing_skill, routing_score, routing_competing, routing_used_llm) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (session_id, turn_number) DO UPDATE SET "
            "tools_called = EXCLUDED.tools_called, "
            "input_tokens = EXCLUDED.input_tokens, "
            "output_tokens = EXCLUDED.output_tokens, "
            "cache_read_tokens = EXCLUDED.cache_read_tokens, "
            "cache_creation_tokens = EXCLUDED.cache_creation_tokens, "
            "routing_skill = EXCLUDED.routing_skill, "
            "routing_score = EXCLUDED.routing_score, "
            "routing_competing = EXCLUDED.routing_competing, "
            "routing_used_llm = EXCLUDED.routing_used_llm",
            (
                session_id,
                turn_number,
                agent_mode,
                query_summary,
                tools_offered,
                tools_called,
                input_tokens,
                output_tokens,
                cache_read_tokens,
                cache_creation_tokens,
                routing_skill,
                routing_score,
                routing_competing,
                routing_used_llm,
            ),
        )
        self.db.commit()

    def update_turn_feedback(self, *, session_id: str, feedback: str) -> None:
        """Update the most recent turn for a session with feedback."""
        self.db.execute(
            "UPDATE tool_turns SET feedback = %s "
            "WHERE id = (SELECT id FROM tool_turns WHERE session_id = %s ORDER BY turn_number DESC LIMIT 1)",
            (feedback, session_id),
        )
        self.db.commit()

    # -- Queries ---------------------------------------------------------------

    def count_usage(self, where_sql: str, params: tuple) -> int:
        """Count matching rows in tool_usage."""
        count_row = self.db.fetchone(
            f"SELECT COUNT(*) AS total FROM tool_usage u {where_sql}",
            params,
        )
        return count_row["total"] if count_row else 0

    def fetch_usage_page(self, where_sql: str, params: tuple, per_page: int, offset: int) -> list[dict]:
        """Fetch a page of tool_usage rows with LEFT JOIN on tool_turns."""
        query_sql = f"""
            SELECT
                u.id, u.timestamp, u.session_id, u.turn_number, u.agent_mode,
                u.tool_name, u.tool_category, u.input_summary, u.status,
                u.error_message, u.error_category, u.duration_ms, u.result_bytes,
                u.requires_confirmation, u.was_confirmed, u.tool_source,
                t.query_summary
            FROM tool_usage u
            LEFT JOIN tool_turns t ON u.session_id = t.session_id AND u.turn_number = t.turn_number
            {where_sql}
            ORDER BY u.timestamp DESC
            LIMIT %s OFFSET %s
        """
        return self.db.fetchall(query_sql, params + (per_page, offset))

    def fetch_overall_stats(self, where_sql: str, params: tuple) -> dict | None:
        """Fetch overall aggregate stats from tool_usage."""
        return self.db.fetchone(
            f"""
            SELECT
                COUNT(*) AS total_calls,
                COUNT(DISTINCT tool_name) AS unique_tools_used,
                COALESCE(AVG(CASE WHEN status = 'error' THEN 1.0 ELSE 0.0 END), 0) AS error_rate,
                COALESCE(ROUND(AVG(duration_ms)), 0) AS avg_duration_ms,
                COALESCE(ROUND(AVG(result_bytes)), 0) AS avg_result_bytes
            FROM tool_usage
            {where_sql}
            """,
            params,
        )

    def fetch_stats_by_tool(self, where_sql: str, params: tuple) -> list[dict]:
        """Fetch stats grouped by tool_name."""
        return self.db.fetchall(
            f"""
            SELECT
                tool_name,
                COUNT(*) AS count,
                SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS error_count,
                COALESCE(ROUND(AVG(duration_ms)), 0) AS avg_duration_ms,
                COALESCE(ROUND(AVG(result_bytes)), 0) AS avg_result_bytes
            FROM tool_usage
            {where_sql}
            GROUP BY tool_name
            ORDER BY count DESC
            """,
            params,
        )

    def fetch_stats_by_mode(self, where_sql: str, params: tuple) -> list[dict]:
        """Fetch stats grouped by agent_mode."""
        return self.db.fetchall(
            f"""
            SELECT agent_mode AS mode, COUNT(*) AS count
            FROM tool_usage
            {where_sql}
            GROUP BY agent_mode
            ORDER BY count DESC
            """,
            params,
        )

    def fetch_stats_by_category(self, where_sql: str, params: tuple) -> list[dict]:
        """Fetch stats grouped by tool_category (excluding NULLs)."""
        category_where_sql = where_sql
        if category_where_sql:
            category_where_sql += " AND tool_category IS NOT NULL"
        else:
            category_where_sql = "WHERE tool_category IS NOT NULL"

        return self.db.fetchall(
            f"""
            SELECT tool_category AS category, COUNT(*) AS count
            FROM tool_usage
            {category_where_sql}
            GROUP BY tool_category
            ORDER BY count DESC
            """,
            params,
        )

    def fetch_stats_by_status(self, where_sql: str, params: tuple) -> list[dict]:
        """Fetch stats grouped by status."""
        return self.db.fetchall(
            f"""
            SELECT status, COUNT(*) AS count
            FROM tool_usage
            {where_sql}
            GROUP BY status
            """,
            params,
        )

    def fetch_stats_by_source(self, where_sql: str, params: tuple) -> list[dict]:
        """Fetch stats grouped by tool_source."""
        return self.db.fetchall(
            f"""
            SELECT COALESCE(tool_source, 'native') AS source,
                COUNT(*) AS count,
                SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS error_count,
                COALESCE(ROUND(AVG(duration_ms)), 0) AS avg_duration_ms,
                COUNT(DISTINCT tool_name) AS unique_tools
            FROM tool_usage
            {where_sql}
            GROUP BY COALESCE(tool_source, 'native')
            """,
            params,
        )

    def fetch_token_averages(self, where_sql: str, params: tuple) -> dict | None:
        """Fetch average token usage from tool_turns."""
        token_where = where_sql.replace("timestamp", "t.timestamp") if where_sql else ""
        token_sql = f"""
            SELECT
                COALESCE(ROUND(AVG(input_tokens)), 0) AS avg_input,
                COALESCE(ROUND(AVG(output_tokens)), 0) AS avg_output,
                COALESCE(ROUND(AVG(cache_read_tokens)), 0) AS avg_cache_read
            FROM tool_turns t
            {token_where}
            {"AND" if token_where else "WHERE"} input_tokens IS NOT NULL
        """
        return self.db.fetchone(token_sql, params)

    def fetch_learned_eval_turns(self, days: int, limit: int) -> list[dict]:
        """Fetch turns for learned eval prompt generation."""
        return self.db.fetchall(
            "SELECT t1.query_summary, t1.tools_called, t1.agent_mode, t2.query_summary AS next_query "
            "FROM tool_turns t1 "
            "JOIN tool_turns t2 ON t1.session_id = t2.session_id AND t2.turn_number = t1.turn_number + 1 "
            "WHERE t1.tools_called IS NOT NULL "
            "AND array_length(t1.tools_called, 1) > 0 "
            "AND t1.query_summary IS NOT NULL AND t1.query_summary != '' "
            "AND t1.timestamp > NOW() - INTERVAL '1 day' * ? "
            "ORDER BY t1.timestamp DESC "
            "LIMIT ?",
            (days, limit),
        )

    # -- Chain discovery (tool_chains.py) --------------------------------------

    def fetch_session_count(self) -> dict | None:
        """Count distinct sessions in tool_usage."""
        return self.db.fetchone("SELECT COUNT(DISTINCT session_id) AS cnt FROM tool_usage")

    def fetch_bigrams(self, min_frequency: int, limit: int) -> list[dict]:
        """Discover frequent tool call bigrams."""
        return self.db.fetchall(
            """
            WITH ordered AS (
                SELECT session_id, tool_name,
                       LAG(tool_name) OVER (PARTITION BY session_id ORDER BY turn_number, id) AS prev_tool
                FROM tool_usage
                WHERE status = 'success'
            ),
            bigram_counts AS (
                SELECT prev_tool AS from_tool, tool_name AS to_tool, COUNT(*) AS frequency
                FROM ordered
                WHERE prev_tool IS NOT NULL
                GROUP BY prev_tool, tool_name
                HAVING COUNT(*) >= %s
            ),
            from_totals AS (
                SELECT prev_tool AS tool, COUNT(*) AS total
                FROM ordered
                WHERE prev_tool IS NOT NULL
                GROUP BY prev_tool
            )
            SELECT b.from_tool, b.to_tool, b.frequency,
                   ROUND(b.frequency::numeric / f.total, 4) AS probability
            FROM bigram_counts b
            JOIN from_totals f ON b.from_tool = f.tool
            ORDER BY b.frequency DESC, b.from_tool ASC, b.to_tool ASC
            LIMIT %s
            """,
            (min_frequency, limit),
        )

    def fetch_trigrams(self, min_frequency: int, limit: int) -> list[dict]:
        """Discover frequent 3-tool sequences."""
        return self.db.fetchall(
            """
            WITH ordered AS (
                SELECT session_id, tool_name,
                       LAG(tool_name, 1) OVER (PARTITION BY session_id ORDER BY turn_number, id) AS prev_1,
                       LAG(tool_name, 2) OVER (PARTITION BY session_id ORDER BY turn_number, id) AS prev_2
                FROM tool_usage
                WHERE status = 'success'
            ),
            trigram_counts AS (
                SELECT prev_2 AS tool_a, prev_1 AS tool_b, tool_name AS tool_c,
                       COUNT(*) AS frequency
                FROM ordered
                WHERE prev_1 IS NOT NULL AND prev_2 IS NOT NULL
                GROUP BY prev_2, prev_1, tool_name
                HAVING COUNT(*) >= %s
            ),
            pair_totals AS (
                SELECT prev_2 AS tool_a, prev_1 AS tool_b, COUNT(*) AS total
                FROM ordered
                WHERE prev_1 IS NOT NULL AND prev_2 IS NOT NULL
                GROUP BY prev_2, prev_1
            )
            SELECT t.tool_a, t.tool_b, t.tool_c, t.frequency,
                   ROUND(t.frequency::numeric / p.total, 4) AS probability
            FROM trigram_counts t
            JOIN pair_totals p ON t.tool_a = p.tool_a AND t.tool_b = p.tool_b
            ORDER BY t.frequency DESC
            LIMIT %s
            """,
            (min_frequency, limit),
        )


# -- Singleton ---------------------------------------------------------------

_tool_usage_repo: ToolUsageRepository | None = None


def get_tool_usage_repo() -> ToolUsageRepository:
    """Return the module-level ToolUsageRepository singleton."""
    global _tool_usage_repo
    if _tool_usage_repo is None:
        _tool_usage_repo = ToolUsageRepository()
    return _tool_usage_repo
