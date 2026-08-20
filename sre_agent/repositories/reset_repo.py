"""Reset repository — the inbox watermark and the restart baseline behind it.

A reset answers one question for every scanner that counts: *since when?*
Before the first reset the answer is "for the life of the cluster", which is
how an inbox ends up saying a pod is "restarting (122x)" when 118 of those
happened yesterday and the workload has been stable since.

The watermark alone is not enough for restarts. ``restart_count`` is
cumulative for the life of the pod and never decreases, and the Kubernetes API
offers no way to ask how many of those happened after a given moment. So the
count at reset time is snapshotted here, and the scanner reports the
difference. Without the snapshot the next scan re-reports 122x and the reset
looks like it did nothing.
"""

from __future__ import annotations

import logging

from .base import BaseRepository

logger = logging.getLogger("pulse_agent.monitor")


class ResetRepository(BaseRepository):
    """Database operations for inbox resets and their restart baselines."""

    def record(self, *, reset_at: int, reset_by: str) -> int:
        """Insert a reset row and return its id.

        Read back through the same cursor rather than ``fetchone``: that helper
        borrows its own pooled connection and never commits it, so an INSERT
        issued through it would be rolled back when the connection went home.
        """
        cur = self.db.execute(
            "INSERT INTO inbox_resets (reset_at, reset_by) VALUES (?, ?) RETURNING id",
            (reset_at, reset_by),
        )
        row = cur.fetchone()
        self.db.commit()
        return int(row[0]) if row else 0

    def record_outcome(self, *, reset_id: int, items_archived: int, episodes_closed: int) -> None:
        self.db.execute(
            "UPDATE inbox_resets SET items_archived = ?, episodes_closed = ? WHERE id = ?",
            (items_archived, episodes_closed, reset_id),
        )
        self.db.commit()

    def save_restart_baseline(self, reset_id: int, containers: list[dict]) -> int:
        """Snapshot the current restart count of every container.

        Every container, not only the ones over the threshold. A container
        sitting at two restarts today is exactly the one that will cross the
        threshold tomorrow, and if it has no baseline its first post-reset
        finding reports the lifetime count again.
        """
        saved = 0
        for c in containers:
            self.db.execute(
                """INSERT INTO restart_baselines (reset_id, namespace, pod, container, restart_count)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (reset_id, namespace, pod, container) DO UPDATE
                   SET restart_count = EXCLUDED.restart_count""",
                (reset_id, c["namespace"], c["pod"], c["container"], int(c["restart_count"])),
            )
            saved += 1
        self.db.commit()
        return saved

    def latest(self) -> dict | None:
        """The most recent reset, or None if the inbox has never been reset."""
        return self.db.fetchone(
            "SELECT id, reset_at, reset_by, items_archived, episodes_closed "
            "FROM inbox_resets ORDER BY reset_at DESC, id DESC LIMIT 1"
        )

    def restart_baseline(self, reset_id: int) -> dict[tuple[str, str, str], int]:
        """The snapshot as a lookup keyed by (namespace, pod, container)."""
        rows = self.db.fetchall(
            "SELECT namespace, pod, container, restart_count FROM restart_baselines WHERE reset_id = ?",
            (reset_id,),
        )
        return {(r["namespace"], r["pod"], r["container"]): int(r["restart_count"]) for r in rows}

    def prune_baselines_before(self, reset_id: int) -> None:
        """Drop snapshots from superseded resets — only the newest is ever read."""
        self.db.execute("DELETE FROM restart_baselines WHERE reset_id < ?", (reset_id,))
        self.db.commit()


_reset_repo: ResetRepository | None = None


def get_reset_repo() -> ResetRepository:
    """Return the module-level ResetRepository singleton."""
    global _reset_repo
    if _reset_repo is None:
        _reset_repo = ResetRepository()
    return _reset_repo
