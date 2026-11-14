"""Per-target circuit breaker for auto-fix: stop, and ask a person once.

Measured on a live cluster at trust 3 over one week: 440 auto-fix actions,
most of them the same three resources fixed again every few minutes. Pods of a
Deployment with a bad image deleted 143 times (each recorded "completed"), a
degraded Deployment "restarted" 140 times (every one failed), a crashlooping
pod deleted again and again. Nothing that existed stopped it:

- The attempt cap (``ClusterMonitor._MAX_FIX_ATTEMPTS``) is two attempts per
  rolling hour, so a fix that never holds is retried twice an hour forever.
  It also counted only *completed* fixes, and a failed attempt set no cooldown,
  so a fix that failed every time was retried on every scan.
- ``fix_learning`` judges a *strategy for a cause across the whole cluster*.
  One workload's 0% is diluted by everyone else's successes, "unverifiable"
  verdicts and execution failures teach it nothing, and it needs the plan to
  go through it at all.

This module is the hard stop under both, keyed on the one thing that matters
here: the same condition on the same resource (the correlation key, which is
category + namespace + resource, with generated pod suffixes stripped). Every
earlier attempt still inside the window counts against the target: if the
condition is back and auto-fix wants to act again, the earlier fix did not
hold, whatever its verdict said at the time. After ``fix_breaker_max_attempts``
of them the target is tripped. Auto-fix stops acting on it (and stops
proposing), and one inbox item, carrying the attempt history, asks a person.

The breaker stays tripped while that item is open, so the window running out
does not quietly re-arm a fix a person has not looked at. When the item is
resolved or dismissed and the attempts have aged out of the window, auto-fix
may try again.

It fails closed: if the history cannot be read, nothing is done to the target
this scan. Not knowing how often a fix already failed is not a reason to try it
once more.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

logger = logging.getLogger("pulse_agent.monitor")

ESCALATION_PREFIX = "fix-breaker:"
ESCALATION_CREATOR = "system:fix-breaker"
_ESCALATION_TYPE = "task"


@dataclass
class BreakerState:
    """What the breaker concluded for one target."""

    tripped: bool
    reason: str = ""
    attempts: list[dict] = field(default_factory=list)
    # True when the trip came from the attempt count rather than an open
    # escalation or a read failure — the case that should raise the item.
    should_escalate: bool = False


def _settings():
    from ..config import get_settings

    return get_settings().monitor


def limits() -> tuple[int, int]:
    """``(max_attempts, window_seconds)``, from settings."""
    s = _settings()
    return max(1, int(s.fix_breaker_max_attempts)), max(60, int(s.fix_breaker_window))


def min_confidence() -> float:
    """The lowest confidence at which auto-fix acts without asking (trust 3+)."""
    return float(_settings().autofix_min_confidence)


def escalation_key(corr_key: str) -> str:
    return f"{ESCALATION_PREFIX}{corr_key}"


def check(corr_key: str, now_ms: int | None = None) -> BreakerState:
    """May auto-fix act on this condition? Never raises."""
    if not corr_key:
        # No identity, nothing to count against. The caller's own guards apply.
        return BreakerState(tripped=False)
    max_attempts, window_s = limits()
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    try:
        from ..repositories.inbox_repo import get_inbox_repo
        from ..repositories.monitor_repo import get_monitor_repo

        attempts = get_monitor_repo().recent_fix_attempts(
            corr_key, now_ms - window_s * 1000, limit=max(20, max_attempts)
        )
        open_item = get_inbox_repo().find_active_by_correlation(escalation_key(corr_key), _ESCALATION_TYPE)
    except Exception:
        logger.warning("Fix breaker: could not read the fix history for %s — not acting", corr_key, exc_info=True)
        return BreakerState(tripped=True, reason="the fix history for this resource could not be read")

    if open_item:
        return BreakerState(
            tripped=True,
            reason="auto-fix was stopped for this resource and is waiting for a person",
            attempts=attempts,
        )
    if len(attempts) >= max_attempts:
        hours = window_s / 3600
        return BreakerState(
            tripped=True,
            reason=f"{len(attempts)} fixes in the last {hours:g}h did not make the condition go away",
            attempts=attempts,
            should_escalate=True,
        )
    return BreakerState(tripped=False, attempts=attempts)


def _when(ts_ms: int) -> str:
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError, OverflowError):
        return "?"


def _describe_attempt(a: dict) -> str:
    verdict = a.get("verification_status") or "not verified"
    line = f"{_when(a.get('timestamp') or 0)}: {a.get('tool') or '?'} — {a.get('status') or '?'}, {verdict}"
    detail = a.get("error") or a.get("verification_evidence") or ""
    if detail:
        line += f" ({str(detail)[:140]})"
    return line


def escalate(corr_key: str, finding: dict, state: BreakerState) -> str:
    """Raise the one inbox item for a tripped target. Returns its id, or ""."""
    resources = finding.get("resources") or []
    r0 = resources[0] if resources else {}
    target = f"{r0.get('kind', 'resource')} {r0.get('namespace', '')}/{r0.get('name', '')}".strip()
    category = finding.get("category", "")
    _, window_s = limits()
    history = [_describe_attempt(a) for a in state.attempts]
    summary = (
        f"Pulse stopped auto-fixing {category} on {target}: {state.reason}. "
        "Repeating the same fix is not working, so a person needs to find the cause. "
        "Auto-fix stays off for this resource while this item is open.\n\nAttempts (newest first):\n- "
        + "\n- ".join(history)
    )
    try:
        from ..inbox import upsert_inbox_item

        item_id = upsert_inbox_item(
            {
                "item_type": _ESCALATION_TYPE,
                "title": f"Auto-fix stopped: {len(state.attempts)} {category} fixes on {target} did not hold",
                "summary": summary,
                "severity": finding.get("severity") or "warning",
                "confidence": 1.0,
                "noise_score": 0,
                "namespace": r0.get("namespace") or None,
                "resources": resources,
                "correlation_key": escalation_key(corr_key),
                # Not system:monitor: inbox reconciliation resolves monitor items
                # whose key is not a live finding key, and untouched-item expiry
                # archives them after 48h. This one stays until a person acts.
                # No "generator" key either, for the same reason.
                "created_by": ESCALATION_CREATOR,
                "metadata": {
                    "source": "fix_breaker",
                    "target": corr_key,
                    "finding_title": finding.get("title", ""),
                    "window_seconds": window_s,
                    "attempts": [
                        {
                            "action_id": a.get("id"),
                            "timestamp": a.get("timestamp"),
                            "tool": a.get("tool"),
                            "status": a.get("status"),
                            "verification_status": a.get("verification_status"),
                            "error": (a.get("error") or "")[:300],
                        }
                        for a in state.attempts
                    ],
                },
            }
        )
    except Exception:
        logger.warning("Fix breaker: could not raise the inbox item for %s", corr_key, exc_info=True)
        return ""
    logger.warning("Fix breaker tripped for %s: %s (inbox %s)", corr_key, state.reason, item_id or "-")
    return item_id
