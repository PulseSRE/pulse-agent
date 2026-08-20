"""Make a scanner that is failing look different from a scanner that found nothing.

Twenty-two scanners caught their own top-level exception, logged it, and
returned an empty list. The dispatcher then recorded ``status: "clean"``,
because an empty list is exactly what a healthy scan of a healthy cluster
returns. On the reference cluster this went further: 1,111 consecutive
investigation failures produced an empty panel, which reads as "nothing worth
investigating" — the precise opposite of the truth.

The rule this module enforces: **absence of findings must never be
indistinguishable from absence of problems.**

Two halves. ``report_failure`` lets a scanner say "I failed" without changing
its signature — the dispatcher stamps the scanner's name into a ContextVar
before calling it, so the scanner does not need to know its own registry name.
And ``consecutive_failures`` reads the history the product was already
writing: ``scan_runs.scanner_results`` has recorded per-scanner status as JSON
since migration 005, and nothing has ever read it back.
"""

from __future__ import annotations

import contextlib
import logging
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger("pulse_agent.monitor")

# Set by the dispatcher around each scanner call. asyncio.to_thread copies the
# context, so a sync scanner running in the thread pool still sees it.
_current_scanner: ContextVar[str | None] = ContextVar("current_scanner", default=None)

# Failures reported during the scan currently in flight, name -> message.
_failures_this_cycle: dict[str, str] = {}

# How many consecutive failed runs before a scanner is reported as broken.
# One failure is a blip — an API timeout, a rolling apiserver. Three in a row
# across three scan cycles is a scanner that is not working.
FAILURE_STREAK_THRESHOLD = 3

# Consecutive failed investigations before the AI backend is called degraded.
# Deliberately low relative to the volume: the reference cluster ran 1,111 in a
# row without a word.
INVESTIGATION_STREAK_THRESHOLD = 5


@contextlib.contextmanager
def scanning(name: str):
    """Mark which scanner is running, so report_failure knows who called it."""
    token = _current_scanner.set(name)
    try:
        yield
    finally:
        _current_scanner.reset(token)


def report_failure(exc: BaseException | str) -> None:
    """Record that the scanner currently running hit an error it swallowed.

    Called from a scanner's own except block, next to the logging it already
    does. The scanner still returns whatever partial findings it gathered —
    losing 49 real findings because the 50th pod had an odd shape would be a
    worse trade than reporting the run as degraded and keeping them.
    """
    name = _current_scanner.get()
    if name is None:
        # Called outside a scan — a unit test, or a scanner invoked directly.
        return
    _failures_this_cycle[name] = str(exc)[:200]


def get_failure(name: str) -> str | None:
    """The error this scanner reported during the current cycle, if any."""
    return _failures_this_cycle.get(name)


def take_failures() -> dict[str, str]:
    """Drain and return the failures reported during this cycle."""
    failures = dict(_failures_this_cycle)
    _failures_this_cycle.clear()
    return failures


def reset() -> None:
    """Clear reported failures — for tests and for a fresh scan cycle."""
    _failures_this_cycle.clear()


def _recent_scanner_results(limit: int) -> list[list[dict[str, Any]]]:
    """Per-scanner result arrays from the last N scan runs, newest first."""
    from ..db import get_database

    rows = get_database().fetchall(
        "SELECT scanner_results FROM scan_runs ORDER BY timestamp DESC LIMIT %s",
        (limit,),
    )
    runs: list[list[dict[str, Any]]] = []
    for row in rows or []:
        results = row["scanner_results"] if isinstance(row, dict) else row[0]
        if isinstance(results, str):
            import json

            try:
                results = json.loads(results)
            except ValueError:
                continue
        if isinstance(results, list):
            runs.append(results)
    return runs


def consecutive_failures(limit: int = 20) -> dict[str, tuple[int, str]]:
    """Scanners whose most recent runs all errored: name -> (streak, last error).

    Counts back from the newest run and stops at the first success, so a
    scanner that failed yesterday and works now reports nothing.
    """
    streaks: dict[str, tuple[int, str]] = {}
    settled: set[str] = set()
    for results in _recent_scanner_results(limit):
        for entry in results:
            name = entry.get("name")
            if not name or name in settled:
                continue
            if entry.get("status") == "error":
                count, _ = streaks.get(name, (0, ""))
                streaks[name] = (count + 1, str(entry.get("error", ""))[:200])
            else:
                # A success ends the streak. Anything counted from newer runs
                # stays, because those are genuinely consecutive from now.
                settled.add(name)
    return {n: v for n, v in streaks.items() if v[0] >= FAILURE_STREAK_THRESHOLD}


def investigation_failure_streak() -> tuple[int, str]:
    """Consecutive failed investigations, newest first, and the last error."""
    from ..db import get_database

    rows = get_database().fetchall("SELECT status, error FROM investigations ORDER BY timestamp DESC LIMIT 200")
    streak = 0
    last_error = ""
    for row in rows or []:
        status = row["status"] if isinstance(row, dict) else row[0]
        error = (row["error"] if isinstance(row, dict) else row[1]) or ""
        if status != "failed":
            break
        streak += 1
        if not last_error:
            last_error = str(error)[:200]
    return streak, last_error


def scan_degraded_capabilities() -> list[dict]:
    """Report Pulse's own broken parts as findings, so silence is never mistaken for health.

    This is the only scanner that looks inward. It exists because every other
    scanner can fail quietly, and a monitoring product that goes quiet when it
    breaks trains people to stop trusting it when it doesn't.
    """
    from .findings import _make_finding
    from .registry import SEVERITY_CRITICAL, SEVERITY_WARNING

    findings: list[dict] = []

    try:
        for name, (streak, error) in consecutive_failures().items():
            findings.append(
                _make_finding(
                    severity=SEVERITY_WARNING,
                    category="degraded",
                    title=f"Scanner {name} has failed {streak} runs in a row",
                    summary=(
                        f"The {name} scanner has errored on its last {streak} runs and is "
                        f"reporting nothing. Whatever it watches is currently unwatched — treat "
                        f"the absence of {name} findings as unknown, not clear. Last error: {error}"
                    ),
                    resources=[{"kind": "Scanner", "name": name}],
                    runbook_id="pulse-degraded",
                    confidence=1.0,
                )
            )
    except Exception as e:
        logger.error("Scanner health check failed: %s", e)
        report_failure(e)

    try:
        streak, error = investigation_failure_streak()
        if streak >= INVESTIGATION_STREAK_THRESHOLD:
            findings.append(
                _make_finding(
                    severity=SEVERITY_CRITICAL if streak >= 50 else SEVERITY_WARNING,
                    category="degraded",
                    title=f"AI investigations failing — {streak} in a row",
                    summary=(
                        f"The last {streak} investigations all failed, so findings are being "
                        f"raised without any root-cause analysis behind them. An empty "
                        f"investigation reads as 'nothing worth investigating', which is the "
                        f"opposite of what is happening. Last error: {error}"
                    ),
                    resources=[{"kind": "Agent", "name": "investigations"}],
                    runbook_id="pulse-degraded",
                    confidence=1.0,
                )
            )
    except Exception as e:
        logger.error("Investigation health check failed: %s", e)
        report_failure(e)

    return findings
