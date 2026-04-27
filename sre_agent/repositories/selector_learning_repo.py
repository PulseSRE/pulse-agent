"""Selector learning repository -- all skill_selection_log database operations.

Extracted from ``selector_learning.py`` to keep domain logic cohesive.  The
original module-level functions in ``selector_learning.py`` now delegate here
for backward compatibility.
"""

from __future__ import annotations

import json
import logging

from .base import BaseRepository

logger = logging.getLogger("pulse_agent.selector_learning")


class SelectorLearningRepository(BaseRepository):
    """Database operations for the ORCA skill selector learning system."""

    # -- Weight recomputation --------------------------------------------------

    def fetch_selection_log(self, days: int) -> list[dict]:
        """Fetch skill_selection_log rows for weight recomputation."""
        return self.db.fetchall(
            "SELECT channel_scores, selected_skill, skill_overridden, "
            "tools_requested_missing "
            "FROM skill_selection_log "
            "WHERE timestamp > NOW() - INTERVAL '%s days' "
            "AND channel_scores IS NOT NULL",
            (days,),
        )

    def persist_weights(self, weights: dict[str, float]) -> None:
        """Save learned weights to DB so they survive pod restarts."""
        self.db.execute(
            "INSERT INTO skill_selection_log (session_id, query_summary, selected_skill, "
            "threshold_used, selection_ms, channel_weights) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                "__weight_snapshot__",
                "learned_weights",
                "__system__",
                0.0,
                0,
                json.dumps(weights),
            ),
        )
        self.db.commit()

    def load_learned_weights(self) -> dict[str, float] | None:
        """Load the most recently persisted weights from DB."""
        row = self.db.fetchone(
            "SELECT channel_weights FROM skill_selection_log "
            "WHERE session_id = '__weight_snapshot__' "
            "ORDER BY timestamp DESC LIMIT 1"
        )
        if not row or not row.get("channel_weights"):
            return None

        weights = (
            json.loads(row["channel_weights"]) if isinstance(row["channel_weights"], str) else row["channel_weights"]
        )
        return weights

    # -- Skill gaps ------------------------------------------------------------

    def fetch_missing_tool_queries(self, days: int, limit: int = 50) -> list[dict]:
        """Fetch queries with missing tools for skill gap identification."""
        return (
            self.db.fetchall(
                "SELECT query_summary, selected_skill, threshold_used "
                "FROM skill_selection_log "
                "WHERE timestamp > NOW() - INTERVAL '%s days' "
                "AND (tools_requested_missing IS NOT NULL AND array_length(tools_requested_missing, 1) > 0) "
                "ORDER BY timestamp DESC "
                "LIMIT %s",
                (days, limit),
            )
            or []
        )

    # -- Low performers --------------------------------------------------------

    def fetch_override_rates(self, days: int, min_invocations: int) -> list[dict]:
        """Fetch skill override rates for pruning."""
        return (
            self.db.fetchall(
                "SELECT selected_skill, "
                "COUNT(*) as total, "
                "SUM(CASE WHEN skill_overridden IS NOT NULL THEN 1 ELSE 0 END) as overrides "
                "FROM skill_selection_log "
                "WHERE timestamp > NOW() - INTERVAL '%s days' "
                "GROUP BY selected_skill "
                "HAVING COUNT(*) >= %s",
                (days, min_invocations),
            )
            or []
        )


# -- Singleton ---------------------------------------------------------------

_selector_learning_repo: SelectorLearningRepository | None = None


def get_selector_learning_repo() -> SelectorLearningRepository:
    """Return the module-level SelectorLearningRepository singleton."""
    global _selector_learning_repo
    if _selector_learning_repo is None:
        _selector_learning_repo = SelectorLearningRepository()
    return _selector_learning_repo
