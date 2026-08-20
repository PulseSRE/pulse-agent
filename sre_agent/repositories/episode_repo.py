"""Episode repository — persistence for episodes and their symptoms.

Episodes are deliberately DB-backed rather than held on the monitor. The
in-memory ``_last_findings`` dict had exactly this shape and lost every open
condition on restart, which meant nothing created before a restart could ever
be resolved. An episode that forgets it is open is worse than no episode.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .base import BaseRepository

logger = logging.getLogger("pulse_agent.monitor")

_OPEN = "open"
_CLOSED = "closed"


class EpisodeRepository(BaseRepository):
    """Database operations for episodes and episode_symptoms."""

    # -- episodes ----------------------------------------------------------

    def create(
        self,
        *,
        episode_id: str,
        cause_category: str,
        cause_title: str,
        cause_finding_id: str,
        cause_layer: int,
        started_at: int,
        correlation_key: str,
        recurrence_of: str | None = None,
    ) -> None:
        self.db.execute(
            "INSERT INTO episodes (id, status, cause_category, cause_title, cause_finding_id, "
            "cause_layer, started_at, last_seen_at, correlation_key, recurrence_of) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                episode_id,
                _OPEN,
                cause_category,
                cause_title,
                cause_finding_id,
                cause_layer,
                started_at,
                started_at,
                correlation_key,
                recurrence_of,
            ),
        )
        self.db.commit()

    def get(self, episode_id: str) -> dict[str, Any] | None:
        return self.db.fetchone("SELECT * FROM episodes WHERE id = ?", (episode_id,))

    def find_open_by_correlation(self, correlation_key: str) -> dict[str, Any] | None:
        return self.db.fetchone(
            "SELECT * FROM episodes WHERE correlation_key = ? AND status = ? ORDER BY started_at DESC LIMIT 1",
            (correlation_key, _OPEN),
        )

    def find_recent_closed_by_correlation(self, correlation_key: str, since: int) -> dict[str, Any] | None:
        return self.db.fetchone(
            "SELECT * FROM episodes WHERE correlation_key = ? AND status = ? AND ended_at >= ? "
            "ORDER BY ended_at DESC LIMIT 1",
            (correlation_key, _CLOSED, since),
        )

    def touch(self, episode_id: str, now: int) -> None:
        self.db.execute("UPDATE episodes SET last_seen_at = ? WHERE id = ?", (now, episode_id))
        self.db.commit()

    def close(self, episode_id: str, now: int) -> None:
        self.db.execute(
            "UPDATE episodes SET status = ?, ended_at = ? WHERE id = ? AND status = ?",
            (_CLOSED, now, episode_id, _OPEN),
        )
        self.db.commit()

    def dismiss(self, episode_id: str, actor: str, now: int) -> bool:
        """Close an episode because an operator says it is over.

        Recorded as a distinct reason from a self-closing episode: an operator
        overriding the scanner is evidence the clearing logic is wrong, and
        that is worth being able to count later.
        """
        cur = self.db.execute(
            "UPDATE episodes SET status = ?, ended_at = ?, dismissed_by = ? WHERE id = ? AND status = ?",
            (_CLOSED, now, actor, episode_id, _OPEN),
        )
        self.db.commit()
        return bool(getattr(cur, "rowcount", 0))

    def list_open(self) -> list[dict[str, Any]]:
        return self.db.fetchall("SELECT * FROM episodes WHERE status = ? ORDER BY started_at DESC", (_OPEN,)) or []

    def recurrence_chain(self, episode_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Walk `recurrence_of` back through earlier occurrences, newest first.

        The chain is what makes recurrence actionable. "etcd lost its leader"
        is a page; "sixth time today, every two hours, each worse than the
        last" is a diagnosis, and it was the single most useful sentence
        available about a real outage — found by a human reading graphs.
        """
        chain: list[dict[str, Any]] = []
        seen: set[str] = set()
        current = self.get(episode_id)
        while current and len(chain) < limit:
            prior_id = current.get("recurrence_of")
            if not prior_id or prior_id in seen:
                break
            seen.add(prior_id)
            prior = self.get(prior_id)
            if not prior:
                break
            chain.append(prior)
            current = prior
        return chain

    # -- symptoms ----------------------------------------------------------

    def attach(
        self, episode_id: str, correlation_key: str, category: str, title: str, namespace: str, now: int
    ) -> bool:
        """Attach a symptom. False if it was already attached."""
        existing = self.db.fetchone(
            "SELECT correlation_key FROM episode_symptoms WHERE episode_id = ? AND correlation_key = ?",
            (episode_id, correlation_key),
        )
        if existing:
            return False
        self.db.execute(
            "INSERT INTO episode_symptoms (episode_id, correlation_key, category, title, namespace, attached_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (episode_id, correlation_key, category, title, namespace, now),
        )
        self.db.commit()
        return True

    def detach(self, episode_id: str, correlation_key: str, actor: str, now: int) -> bool:
        cur = self.db.execute(
            "UPDATE episode_symptoms SET detached_at = ?, detached_by = ? "
            "WHERE episode_id = ? AND correlation_key = ? AND detached_at IS NULL",
            (now, actor, episode_id, correlation_key),
        )
        self.db.commit()
        return bool(getattr(cur, "rowcount", 0))

    def detached_keys(self, episode_id: str) -> set[str]:
        rows = self.db.fetchall(
            "SELECT correlation_key FROM episode_symptoms WHERE episode_id = ? AND detached_at IS NOT NULL",
            (episode_id,),
        )
        return {r["correlation_key"] for r in rows or []}

    def symptoms(self, episode_id: str) -> list[dict[str, Any]]:
        return (
            self.db.fetchall(
                "SELECT * FROM episode_symptoms WHERE episode_id = ? AND detached_at IS NULL ORDER BY attached_at",
                (episode_id,),
            )
            or []
        )

    def open_symptom_index(self) -> dict[str, str]:
        """Correlation key -> episode id for every live symptom of an open episode."""
        rows = self.db.fetchall(
            "SELECT s.correlation_key, s.episode_id FROM episode_symptoms s "
            "JOIN episodes e ON e.id = s.episode_id "
            "WHERE e.status = ? AND s.detached_at IS NULL",
            (_OPEN,),
        )
        return {r["correlation_key"]: r["episode_id"] for r in rows or []}

    def refresh_rollup(self, episode_id: str) -> None:
        """Recompute the symptom count and affected namespaces on the episode."""
        rows = self.symptoms(episode_id)
        namespaces = sorted({r["namespace"] for r in rows if r.get("namespace")})
        self.db.execute(
            "UPDATE episodes SET symptom_count = ?, namespaces = ? WHERE id = ?",
            (len(rows), json.dumps(namespaces), episode_id),
        )
        self.db.commit()


_episode_repo: EpisodeRepository | None = None


def get_episode_repo() -> EpisodeRepository:
    """Return the module-level EpisodeRepository singleton."""
    global _episode_repo
    if _episode_repo is None:
        _episode_repo = EpisodeRepository()
    return _episode_repo
