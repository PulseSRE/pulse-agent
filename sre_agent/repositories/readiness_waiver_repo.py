"""Readiness waiver repository — accepted-risk records for readiness gates.

One row per gate for the cluster this agent is installed on (the agent and its
database belong to one cluster). A waiver past its ``expires_at`` is kept as a
row but is not active: ``fetch_active`` leaves it out, so an expired waiver
stops counting without anyone having to remember to lift it.
"""

from __future__ import annotations

from .base import BaseRepository

_COLUMNS = "gate_id, reason, waived_by, created_at, expires_at"


class ReadinessWaiverRepository(BaseRepository):
    """Database operations for readiness gate waivers."""

    def fetch_active(self, now_ms: int) -> list[dict]:
        """Waivers that have not expired at ``now_ms``, oldest first."""
        rows = self.db.fetchall(
            f"SELECT {_COLUMNS} FROM readiness_waivers "
            "WHERE expires_at IS NULL OR expires_at > ? ORDER BY created_at, gate_id",
            (now_ms,),
        )
        return [dict(r) for r in rows or []]

    def upsert(
        self,
        gate_id: str,
        reason: str,
        waived_by: str,
        created_at: int,
        expires_at: int | None,
    ) -> dict:
        """Waive a gate, replacing any earlier waiver of it.

        Re-waiving is a new decision: the new reason, author, time and expiry
        replace the old ones rather than being merged into them.
        """
        self.db.execute(
            "INSERT INTO readiness_waivers (gate_id, reason, waived_by, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (gate_id) DO UPDATE SET "
            "reason = EXCLUDED.reason, waived_by = EXCLUDED.waived_by, "
            "created_at = EXCLUDED.created_at, expires_at = EXCLUDED.expires_at",
            (gate_id, reason, waived_by, created_at, expires_at),
        )
        self.db.commit()
        return {
            "gate_id": gate_id,
            "reason": reason,
            "waived_by": waived_by,
            "created_at": created_at,
            "expires_at": expires_at,
        }

    def delete(self, gate_id: str) -> bool:
        """Lift a gate's waiver. True when there was one (expired or not)."""
        # execute + commit, not fetchone: fetchone runs on its own pooled
        # connection and never commits, so a DELETE there would not stick.
        cur = self.db.execute("DELETE FROM readiness_waivers WHERE gate_id = ?", (gate_id,))
        deleted = cur.rowcount > 0
        self.db.commit()
        return deleted


# -- Singleton ---------------------------------------------------------------

_readiness_waiver_repo: ReadinessWaiverRepository | None = None


def get_readiness_waiver_repo() -> ReadinessWaiverRepository:
    """Return the module-level ReadinessWaiverRepository singleton."""
    global _readiness_waiver_repo
    if _readiness_waiver_repo is None:
        _readiness_waiver_repo = ReadinessWaiverRepository()
    return _readiness_waiver_repo
