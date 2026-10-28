"""The noise filter's memory, kept across restarts.

``ClusterMonitor._transient_counts`` counts how often each finding went away on
its own; three or more and it is suppressed as flapping. The counts lived only
in memory, so every restart wiped them: each known-flaky condition paged again
until it had flapped three more times. Loaded on the first scan, written back
for the keys a scan changed.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger("pulse_agent.monitor")

# The monitor trims its in-memory map to the top 500 above 1000 keys; loading
# more than that back would be undone on the first scan.
_LOAD_LIMIT = 1000


def load_counts() -> dict[str, int]:
    """Transient counts saved by earlier processes. Empty on any error."""
    try:
        from ..db import get_database

        rows = get_database().fetchall(
            "SELECT finding_key, transient_count FROM monitor_noise ORDER BY transient_count DESC LIMIT ?",
            (_LOAD_LIMIT,),
        )
        return {r["finding_key"]: int(r["transient_count"]) for r in rows or []}
    except Exception:
        logger.debug("Could not load noise memory", exc_info=True)
        return {}


def save_counts(counts: dict[str, int]) -> None:
    """Upsert the given counts. Best effort: the filter still works in memory."""
    if not counts:
        return
    try:
        from ..db import get_database

        db = get_database()
        now = int(time.time())
        for key, count in counts.items():
            db.execute(
                "INSERT INTO monitor_noise (finding_key, transient_count, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT (finding_key) DO UPDATE SET transient_count = EXCLUDED.transient_count, "
                "updated_at = EXCLUDED.updated_at",
                (key, int(count), now),
            )
        db.commit()
    except Exception:
        logger.debug("Could not save noise memory", exc_info=True)
