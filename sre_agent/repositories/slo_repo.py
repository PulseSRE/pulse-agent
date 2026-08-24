"""SLO repository — persistence for SLO definitions.

The ``slo_definitions`` table has existed since migration 016, but nothing
ever read or wrote it: SLOs registered through the API lived only in the
in-memory registry and vanished on every pod restart — which is why the SLO
stack (burn-rate scanner, inbox SLO-impact chips, slo_management skill) sat
idle. This repository closes that loop; ``slo_registry`` persists through it
on register/unregister and reloads on startup.
"""

from __future__ import annotations

import logging

from .base import BaseRepository

logger = logging.getLogger("pulse_agent.slo")


class SLORepository(BaseRepository):
    """Database operations for SLO definitions."""

    def save(
        self,
        service_name: str,
        slo_type: str,
        target: float,
        window_days: int,
        description: str,
    ) -> None:
        self.db.execute(
            "INSERT INTO slo_definitions (service_name, slo_type, target, window_days, description) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (service_name, slo_type) DO UPDATE SET "
            "target = EXCLUDED.target, "
            "window_days = EXCLUDED.window_days, "
            "description = EXCLUDED.description",
            (service_name, slo_type, target, window_days, description),
        )
        self.db.commit()

    def delete(self, service_name: str, slo_type: str) -> None:
        self.db.execute(
            "DELETE FROM slo_definitions WHERE service_name = %s AND slo_type = %s",
            (service_name, slo_type),
        )
        self.db.commit()

    def fetch_all(self) -> list[dict]:
        rows = self.db.fetchall(
            "SELECT service_name, slo_type, target, window_days, description FROM slo_definitions ORDER BY service_name, slo_type"
        )
        return [
            {
                "service_name": r["service_name"],
                "slo_type": r["slo_type"],
                "target": float(r["target"]),
                "window_days": int(r["window_days"]),
                "description": r["description"] or "",
            }
            for r in rows
        ]


_slo_repo: SLORepository | None = None


def get_slo_repo() -> SLORepository:
    """Get or create the singleton SLORepository instance."""
    global _slo_repo
    if _slo_repo is None:
        _slo_repo = SLORepository()
    return _slo_repo
