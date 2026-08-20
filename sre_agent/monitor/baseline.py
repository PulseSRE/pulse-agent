"""The "since when" every counting scanner needs.

An inbox reset does not delete anything. It records a moment and says: from
here, count from now. Scanners that report a cumulative number — restarts,
event frequency, a burst of pods that all died together — consult this module
so that what comes back after a reset is what is *happening*, not what has ever
happened.

Scanners that report current state (a deployment at 0/2, a pending pod, a
firing alert) need none of this. They already only ever describe now, which is
why a reset leaves them untouched and they reappear immediately if still true.

The watermark is cached because every scanner in a cycle asks for it and it
changes only when an operator presses the button. ``invalidate()`` is called by
the reset path, and by tests.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger("pulse_agent.monitor")

_lock = threading.Lock()
_cached: dict[str, Any] | None = None
_cache_loaded = False


def invalidate() -> None:
    """Forget the cached reset — next read goes back to the database."""
    global _cached, _cache_loaded
    with _lock:
        _cached = None
        _cache_loaded = False


def _load() -> dict[str, Any] | None:
    global _cached, _cache_loaded
    with _lock:
        if _cache_loaded:
            return _cached
        try:
            from ..repositories.reset_repo import get_reset_repo

            repo = get_reset_repo()
            latest = repo.latest()
            if latest is None:
                _cached, _cache_loaded = None, True
                return None
            _cached = {
                "id": int(latest["id"]),
                "reset_at": int(latest["reset_at"]),
                "reset_by": latest.get("reset_by") or "",
                "restarts": repo.restart_baseline(int(latest["id"])),
            }
            _cache_loaded = True
            return _cached
        except Exception as e:
            # A missing table or an unreachable database must not stop a scan.
            # No baseline means lifetime counts — the behaviour before resets
            # existed — which is worse than the truth but better than blind.
            logger.warning("Reset baseline unavailable, counting from cluster lifetime: %s", e)
            _cached, _cache_loaded = None, True
            return None


def watermark() -> int | None:
    """Epoch seconds of the last inbox reset, or None if never reset."""
    current = _load()
    return current["reset_at"] if current else None


def reset_by() -> str | None:
    """Who performed the last reset, or None if never reset."""
    current = _load()
    return current["reset_by"] if current else None


def restarts_since_reset(namespace: str, pod: str, container: str, current_count: int) -> int:
    """Restarts attributable to the window since the reset.

    A container with no baseline is new since the reset, so all of its restarts
    count. A negative difference means the pod was recreated and its counter
    started over; the current count is then already the post-reset figure.
    """
    current = _load()
    if not current:
        return current_count
    before = current["restarts"].get((namespace, pod, container))
    if before is None:
        return current_count
    delta = current_count - before
    return current_count if delta < 0 else delta


def occurred_since_reset(when: int | None) -> bool:
    """Whether an event at ``when`` (epoch seconds) counts after the reset.

    An unknown time counts. Absence of a timestamp is not evidence that
    something is old, and silently dropping those would hide live problems —
    the failure mode a reset must never have.
    """
    mark = watermark()
    if mark is None or when is None:
        return True
    return when >= mark
