"""Prompt log repository -- all prompt_log database operations.

Extracted from ``prompt_log.py`` to keep domain logic cohesive.  The original
module-level functions in ``prompt_log.py`` now delegate here for backward
compatibility.
"""

from __future__ import annotations

import logging

from .base import BaseRepository

logger = logging.getLogger("pulse_agent.prompt_log")


class PromptLogRepository(BaseRepository):
    """Database operations for prompt logging."""

    # -- Recording -------------------------------------------------------------

    def insert_prompt(
        self,
        *,
        session_id: str,
        turn_number: int,
        skill_name: str,
        skill_version: int,
        prompt_hash: str,
        static_chars: int,
        dynamic_chars: int,
        total_tokens: int,
        sections_json: str,
        input_tokens: int | None,
        output_tokens: int | None,
        cache_read_tokens: int | None,
        cache_creation_tokens: int | None,
    ) -> None:
        """Insert a prompt log entry."""
        self.db.execute(
            "INSERT INTO prompt_log "
            "(session_id, turn_number, skill_name, skill_version, prompt_hash, "
            "static_chars, dynamic_chars, total_tokens, sections, "
            "input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                session_id,
                turn_number,
                skill_name,
                skill_version,
                prompt_hash,
                static_chars,
                dynamic_chars,
                total_tokens,
                sections_json,
                input_tokens,
                output_tokens,
                cache_read_tokens,
                cache_creation_tokens,
            ),
        )
        self.db.commit()

    # -- Stats -----------------------------------------------------------------

    def fetch_overall_stats(self, days: int) -> dict | None:
        """Fetch overall prompt log stats."""
        return self.db.fetchone(
            "SELECT COUNT(*) AS total, "
            "COALESCE(ROUND(AVG(total_tokens)), 0) AS avg_tokens, "
            "COALESCE(ROUND(AVG(static_chars)), 0) AS avg_static, "
            "COALESCE(ROUND(AVG(dynamic_chars)), 0) AS avg_dynamic "
            "FROM prompt_log "
            "WHERE timestamp > NOW() - INTERVAL '1 day' * %s",
            (days,),
        )

    def fetch_by_skill(self, days: int) -> list[dict]:
        """Fetch prompt stats grouped by skill_name."""
        return self.db.fetchall(
            "SELECT skill_name, COUNT(*) AS count, "
            "COALESCE(ROUND(AVG(total_tokens)), 0) AS avg_tokens, "
            "COALESCE(ROUND(AVG(static_chars)), 0) AS avg_static, "
            "COALESCE(ROUND(AVG(dynamic_chars)), 0) AS avg_dynamic, "
            "COUNT(DISTINCT prompt_hash) AS prompt_versions "
            "FROM prompt_log "
            "WHERE timestamp > NOW() - INTERVAL '1 day' * %s "
            "GROUP BY skill_name ORDER BY count DESC",
            (days,),
        )

    def fetch_cache_hit_rate(self, days: int) -> dict | None:
        """Fetch cache hit rate from prompt_log."""
        return self.db.fetchone(
            "SELECT "
            "COUNT(*) FILTER (WHERE cache_read_tokens > 0) AS cache_hits, "
            "COUNT(*) AS total "
            "FROM prompt_log "
            "WHERE timestamp > NOW() - INTERVAL '1 day' * %s "
            "AND input_tokens IS NOT NULL",
            (days,),
        )

    def fetch_sections(self, days: int) -> list[dict]:
        """Fetch sections JSON from prompt_log entries."""
        return (
            self.db.fetchall(
                "SELECT sections FROM prompt_log "
                "WHERE timestamp > NOW() - INTERVAL '1 day' * %s "
                "AND sections IS NOT NULL",
                (days,),
            )
            or []
        )

    # -- Versions --------------------------------------------------------------

    def fetch_prompt_versions(self, skill_name: str, days: int) -> list[dict]:
        """Fetch prompt version aggregates for a skill."""
        return self.db.fetchall(
            "SELECT prompt_hash, "
            "COUNT(*) AS count, "
            "MIN(timestamp) AS first_seen, "
            "MAX(timestamp) AS last_seen, "
            "MAX(skill_version) AS skill_version, "
            "COALESCE(ROUND(AVG(total_tokens)), 0) AS avg_tokens, "
            "COALESCE(ROUND(AVG(input_tokens)), 0) AS avg_input_tokens, "
            "COALESCE(ROUND(AVG(output_tokens)), 0) AS avg_output_tokens, "
            "COALESCE(ROUND(AVG(cache_read_tokens)), 0) AS avg_cache_read, "
            "MAX(static_chars) AS static_chars, "
            "COALESCE(ROUND(AVG(dynamic_chars)), 0) AS avg_dynamic_chars "
            "FROM prompt_log "
            "WHERE skill_name = %s "
            "AND timestamp > NOW() - INTERVAL '1 day' * %s "
            "GROUP BY prompt_hash "
            "ORDER BY MIN(timestamp) DESC",
            (skill_name, days),
        )

    def fetch_section_breakdown_by_hash(self, skill_name: str, days: int) -> list[dict]:
        """Fetch section breakdown per prompt_hash (most recent entry)."""
        return (
            self.db.fetchall(
                "SELECT DISTINCT ON (prompt_hash) prompt_hash, sections "
                "FROM prompt_log "
                "WHERE skill_name = %s "
                "AND timestamp > NOW() - INTERVAL '1 day' * %s "
                "AND sections IS NOT NULL "
                "ORDER BY prompt_hash, timestamp DESC",
                (skill_name, days),
            )
            or []
        )

    # -- Session log -----------------------------------------------------------

    def fetch_session_log(self, session_id: str) -> list[dict]:
        """Fetch prompt log entries for a session."""
        return self.db.fetchall(
            "SELECT id, timestamp, session_id, turn_number, skill_name, skill_version, "
            "prompt_hash, static_chars, dynamic_chars, total_tokens, sections, "
            "input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens "
            "FROM prompt_log "
            "WHERE session_id = %s "
            "ORDER BY turn_number ASC",
            (session_id,),
        )


# -- Singleton ---------------------------------------------------------------

_prompt_log_repo: PromptLogRepository | None = None


def get_prompt_log_repo() -> PromptLogRepository:
    """Return the module-level PromptLogRepository singleton."""
    global _prompt_log_repo
    if _prompt_log_repo is None:
        _prompt_log_repo = PromptLogRepository()
    return _prompt_log_repo
