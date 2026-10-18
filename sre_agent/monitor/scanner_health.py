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
import re
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger("pulse_agent.monitor")

# Set by the dispatcher around each scanner call. asyncio.to_thread copies the
# context, so a sync scanner running in the thread pool still sees it.
_current_scanner: ContextVar[str | None] = ContextVar("current_scanner", default=None)

# Failures reported during the scan currently in flight, name -> message.
_failures_this_cycle: dict[str, str] = {}

# Inside optional_access(), a 403 is an expected gap rather than a failure.
_optional_access: ContextVar[bool] = ContextVar("optional_access", default=False)
_logged_optional_gaps: set[str] = set()

# How many consecutive failed runs before a scanner is reported as broken.
# One failure is a blip — an API timeout, a rolling apiserver. Three in a row
# across three scan cycles is a scanner that is not working.
FAILURE_STREAK_THRESHOLD = 3

# Consecutive failed investigations before the AI backend is called degraded.
# Deliberately low relative to the volume: the reference cluster ran 1,111 in a
# row without a word.
INVESTIGATION_STREAK_THRESHOLD = 5

# A streak alone is not enough, and this was found the hard way. Watching the
# same cluster after deploying the streak check: 65 of the last 70
# investigations had failed — a 93% failure rate — and the check said nothing,
# because the most recent one happened to succeed and the streak was 0.
#
# A quota-limited backend does not fail in a clean run. It flaps. So the rate
# is checked too, over a window big enough that a couple of transient failures
# do not trip it.
INVESTIGATION_RATE_WINDOW = 40
INVESTIGATION_RATE_THRESHOLD = 0.5


@contextlib.contextmanager
def optional_access():
    """Mark a sub-check whose permission the default install does not grant.

    A scanner that is entirely blind without a permission should fail loudly
    (see report_api_error). One extra check inside an otherwise working
    scanner — the SA-token look in audit_auth, which needs Secret access the
    operator only grants on opt-in — should not mark the whole scanner
    failed: a failed scanner cannot resolve its findings, so the other checks'
    findings would stand forever.
    """
    token = _optional_access.set(True)
    try:
        yield
    finally:
        _optional_access.reset(token)


@contextlib.contextmanager
def scanning(name: str):
    """Mark which scanner is running, so report_failure knows who called it."""
    token = _current_scanner.set(name)
    try:
        yield
    finally:
        _current_scanner.reset(token)


def report_api_error(err: Any) -> None:
    """Record a Kubernetes API error the running scanner received, if any.

    Called by ``safe()``/``safe_async()`` for every error they turn into a
    ``ToolError``. Scanners answered those with ``return findings`` — an
    empty list, which is what a clean scan returns — so a 403 from RBAC the
    operator never granted (secrets, clusteroperators, CRDs) showed as
    "clean" forever. A 404 is not blindness: the API is simply not served
    here (no ClusterOperators on vanilla Kubernetes), so it is not reported.
    """
    if _current_scanner.get() is None:
        return
    category = getattr(err, "category", "") or "error"
    if category == "not_found":
        return
    if category == "forbidden" and _optional_access.get():
        key = f"{_current_scanner.get()}:{getattr(err, 'operation', '')}"
        if key not in _logged_optional_gaps:
            _logged_optional_gaps.add(key)
            logger.info("Scanner %s skips a check it has no permission for: %s", _current_scanner.get(), err)
        return
    message = getattr(err, "message", None) or str(err)
    report_failure(f"{category}: {message}")


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
    _failures_this_cycle[name] = str(exc)[:300]


def get_failure(name: str) -> str | None:
    """The error this scanner reported during the current cycle, if any."""
    return _failures_this_cycle.get(name)


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
                streaks[name] = (count + 1, str(entry.get("error", ""))[:300])
            else:
                # A success ends the streak. Anything counted from newer runs
                # stays, because those are genuinely consecutive from now.
                settled.add(name)
    return {n: v for n, v in streaks.items() if v[0] >= FAILURE_STREAK_THRESHOLD}


def investigation_failure_rate() -> tuple[int, int, str]:
    """Failures out of the last INVESTIGATION_RATE_WINDOW attempts, and the last error.

    Catches the case a consecutive-failure streak cannot: a backend that fails
    most of the time but succeeds often enough to keep resetting the streak.
    """
    from ..db import get_database

    rows = get_database().fetchall(
        "SELECT status, error FROM investigations ORDER BY timestamp DESC LIMIT %s",
        (INVESTIGATION_RATE_WINDOW,),
    )
    total = 0
    failed = 0
    last_error = ""
    for row in rows or []:
        status = row["status"] if isinstance(row, dict) else row[0]
        error = (row["error"] if isinstance(row, dict) else row[1]) or ""
        total += 1
        if status == "failed":
            failed += 1
            if not last_error:
                last_error = str(error)[:200]
    return failed, total, last_error


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


_RBAC_DENIAL = re.compile(r'cannot (\w+) resource "([^"]+)" in API group "([^"]*)"')

# Resources the default install deliberately withholds, and the switch that
# grants them. Anything else Pulse is denied is a gap in its role.
_OPT_IN_ACCESS = {
    ("", "secrets"): "set spec.agent.allowSecretAccess: true on the OpenShiftPulse resource "
    "(read-only access to Secrets)",
}


def explain_failure(scanner: str, error: str) -> dict[str, Any]:
    """Say, in plain words, what a failing scanner cannot see and how to fix it.

    The raw error ("forbidden: secrets is forbidden: User system:serviceaccount:…
    cannot list resource…") is accurate and useless to most people looking at a
    dashboard. What they need is: which checks are off, why, and the one thing
    to change.
    """
    from .registry import SCANNER_REGISTRY

    meta = SCANNER_REGISTRY.get(scanner, {})
    name = meta.get("displayName") or scanner.replace("_", " ").title()
    checks = list(meta.get("checks", []))
    lower = error.lower()

    m = _RBAC_DENIAL.search(error)
    if m or lower.startswith("forbidden"):
        verb, resource, group = (m.group(1), m.group(2), m.group(3)) if m else ("read", "", "")
        what = f"{group}/{resource}" if group else resource
        reason = f"Pulse is not allowed to {verb} {what or 'what this scanner reads'}"
        fix = _OPT_IN_ACCESS.get((group, resource)) or (
            f"grant Pulse read-only get/list on {what} — upgrade pulse-operator, whose agent role includes it, "
            "or add the rule to the agent's ClusterRole"
            if what
            else "check the agent ServiceAccount's ClusterRole"
        )
    elif lower.startswith("unauthorized"):
        reason = "the API server rejected Pulse's credentials"
        fix = "check the agent ServiceAccount token; restarting the agent pod remounts it"
    elif "prometheus" in lower:
        reason = "Pulse cannot query Prometheus"
        fix = (
            "check that the cluster monitoring stack (thanos-querier) is up and reachable from the agent, "
            "and that the agent may use it (cluster-monitoring-view)"
        )
    elif any(k in lower for k in ("timed out", "timeout", "connection refused", "unreachable", "network")):
        reason = "the API server is not answering in time"
        fix = "this usually clears by itself; if it persists, check API server health"
    else:
        reason = "the scanner hit an error"
        fix = "see the agent logs for this scanner"

    return {
        "scanner": scanner,
        "name": name,
        "reason": reason,
        "fix": fix[0].upper() + fix[1:],
        "unwatched": checks,
        "error": error,
    }


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
            why = explain_failure(name, error)
            unwatched = ", ".join(why["unwatched"]) or "what it watches"
            finding = _make_finding(
                severity=SEVERITY_WARNING,
                category="degraded",
                title=f"{why['name']} checks are off: {why['reason']}",
                summary=(
                    f"{why['name']} has failed its last {streak} runs, so these are unwatched: "
                    f"{unwatched}. Treat the absence of these findings as unknown, not clear. "
                    f"To fix: {why['fix']}. Last error: {error}"
                ),
                resources=[{"kind": "Scanner", "name": name}],
                runbook_id="pulse-degraded",
                confidence=1.0,
            )
            finding["remedy"] = why["fix"]
            finding["unwatched"] = why["unwatched"]
            findings.append(finding)
    except Exception as e:
        logger.error("Scanner health check failed: %s", e)
        report_failure(e)

    try:
        streak, error = investigation_failure_streak()
        failed, total, rate_error = investigation_failure_rate()
        rate = (failed / total) if total else 0.0

        # Streak and rate catch different shapes of the same problem: a backend
        # that is flatly down, and one that is failing most of the time while
        # succeeding often enough to keep resetting the streak. Report whichever
        # is the stronger evidence rather than raising two findings for one fault.
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
        elif total >= INVESTIGATION_RATE_WINDOW and rate >= INVESTIGATION_RATE_THRESHOLD:
            findings.append(
                _make_finding(
                    severity=SEVERITY_CRITICAL if rate >= 0.9 else SEVERITY_WARNING,
                    category="degraded",
                    title=f"AI investigations mostly failing — {failed} of the last {total}",
                    summary=(
                        f"{failed} of the last {total} investigations failed ({rate:.0%}). The "
                        f"occasional success keeps a consecutive-failure check quiet, but most "
                        f"findings are still reaching you with no root-cause analysis behind "
                        f"them. Last error: {rate_error}"
                    ),
                    resources=[{"kind": "Agent", "name": "investigations"}],
                    runbook_id="pulse-degraded",
                    confidence=1.0,
                )
            )
    except Exception as e:
        logger.error("Investigation health check failed: %s", e)
        report_failure(e)

    try:
        undelivered = _undelivered_count()
        if undelivered:
            findings.append(
                _make_finding(
                    severity=SEVERITY_WARNING,
                    category="degraded",
                    # The count belongs in the summary, not the title. A title
                    # that changes with the number is a different correlation
                    # key every time it changes, so the item resolves and is
                    # raised again instead of staying open — observed live.
                    title="Nothing Pulse found will reach anyone",
                    summary=(
                        f"No notification channel is configured, and {undelivered} open "
                        f"episode(s) or proposed fix(es) are waiting for somebody to open the "
                        f"UI and look. On the reference cluster that meant a control-plane "
                        f"problem ran for 30 hours with the diagnosis sitting in a database. "
                        f"Set PULSE_AGENT_WEBHOOK_URL to change that."
                    ),
                    resources=[{"kind": "Agent", "name": "notifications"}],
                    runbook_id="pulse-degraded",
                    confidence=1.0,
                )
            )
    except Exception as e:
        logger.error("Notification channel check failed: %s", e)
        report_failure(e)

    return findings


def _undelivered_count() -> int:
    """How much is waiting that nobody will be told about.

    Zero when a channel is configured, and zero when there is nothing to say.
    An unconfigured webhook on a quiet cluster is a deployment's choice, not a
    fault — reporting it every scan regardless would be the same standing-posture
    nagging that `AlertmanagerReceiversNotConfigured` had been doing on the
    reference cluster for 57 hours to nobody's benefit. It only becomes a
    problem when there is something to deliver.
    """
    from ..config import get_settings

    if get_settings().server.webhook_url:
        return 0

    from ..repositories import get_monitor_repo
    from .episodes import list_open

    waiting = len(list_open())
    rows = get_monitor_repo().db.fetchone("SELECT count(*) AS n FROM actions WHERE status = 'proposed'")
    return waiting + int(rows["n"] if rows else 0)
