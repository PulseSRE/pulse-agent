"""Which findings get investigated, and when.

Eligibility, budgets, cooldowns and concurrency. How an investigation runs and
what happens to its result is ``investigation_pipeline``, for both strategies.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..config import get_settings
from .confidence import _finding_key
from .investigation_pipeline import investigate
from .registry import SEVERITY_CRITICAL, SEVERITY_WARNING

try:
    from ..observability import (
        COST_BUDGET_EXHAUSTION_TOTAL,
        COST_BUDGET_REMAINING_USD,
        INVESTIGATION_BUDGET_REMAINING,
        INVESTIGATIONS_TOTAL,
    )

    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

if TYPE_CHECKING:
    from .cluster_monitor import ClusterMonitor

logger = logging.getLogger("pulse_agent.monitor")


@dataclass(frozen=True)
class InvestigationRefusal:
    """Why an investigation was not started. ``reason`` is a stable code, ``message`` is for people."""

    reason: str  # circuit_open | daily_budget | cost_budget | cooldown | concurrency
    message: str
    retry_after: int | None = None


def _circuit_refusal() -> InvestigationRefusal | None:
    from ..agent import _circuit_breaker

    if _circuit_breaker.is_open:
        logger.info("Skipping proactive investigations: agent circuit breaker open")
        return InvestigationRefusal(
            "circuit_open",
            "The agent's model calls are failing and paused (circuit breaker open); try again shortly.",
            retry_after=max(1, int(get_settings().agent.cb_timeout)),
        )
    return None


def _daily_refusal(monitor: ClusterMonitor) -> InvestigationRefusal | None:
    max_daily = get_settings().monitor.max_daily_investigations
    if time.time() - monitor._daily_investigation_reset > 86400:
        monitor._daily_investigation_count = 0
        monitor._daily_investigation_reset = time.time()
    if monitor._daily_investigation_count >= max_daily:
        logger.info(
            "Daily investigation budget exhausted (%d/%d)",
            monitor._daily_investigation_count,
            max_daily,
        )
        if _METRICS_AVAILABLE:
            INVESTIGATION_BUDGET_REMAINING.set(0)
        return InvestigationRefusal(
            "daily_budget",
            f"The daily investigation budget is used up ({monitor._daily_investigation_count}/{max_daily}).",
            retry_after=max(1, int(monitor._daily_investigation_reset + 86400 - time.time())),
        )
    return None


def today_cost(monitor: ClusterMonitor) -> float | None:
    """Spend over the last 24 hours, cached for 5 minutes; ``None`` without a cost budget.

    Reads the database when the cache is stale, so async callers run it in a
    thread first (``asyncio.to_thread``); the gate then reads the cache.
    """
    if get_settings().cost_budget_usd <= 0:
        return None
    now_t = time.time()
    cached = monitor._cost_budget_cache
    if cached and (now_t - cached[0]) < 300:
        return float(cached[1])
    from ..observability import estimate_cost_usd
    from ..repositories import get_analytics_repo

    totals = get_analytics_repo().fetch_token_totals(1)
    if totals and totals["total_incidents"] > 0:
        # Includes investigations now (they record their usage as
        # turns) and cache tokens, priced for the configured model.
        cost = estimate_cost_usd(
            int(totals["total_input"]),
            int(totals["total_output"]),
            int(totals.get("total_cache_read") or 0),
            int(totals.get("total_cache_write") or 0),
        )
    else:
        cost = 0.0
    monitor._cost_budget_cache = (now_t, cost)
    return cost


def _cost_refusal(monitor: ClusterMonitor) -> InvestigationRefusal | None:
    """Cost budget gate (cached — only queries DB every 5 minutes)."""
    _budget_usd = get_settings().cost_budget_usd
    if _budget_usd <= 0:
        return None
    try:
        spent = today_cost(monitor)
        if spent is None:
            return None
        if _METRICS_AVAILABLE:
            COST_BUDGET_REMAINING_USD.set(max(0, _budget_usd - spent))
        if spent >= _budget_usd:
            logger.warning(
                "Daily cost budget exceeded ($%.2f / $%.2f) — pausing investigations",
                spent,
                _budget_usd,
            )
            if _METRICS_AVAILABLE:
                COST_BUDGET_EXHAUSTION_TOTAL.inc()
            # No retry_after: spend is summed over a rolling 24 hours, so there
            # is no reset time to give; the figure is re-read every 5 minutes.
            return InvestigationRefusal(
                "cost_budget",
                f"The daily cost budget is spent (${spent:.2f} of ${_budget_usd:.2f} over the last 24 hours).",
            )
    except Exception:
        logger.debug("Cost budget check failed", exc_info=True)
    return None


def budget_refusal(monitor: ClusterMonitor) -> InvestigationRefusal | None:
    """The gates every monitor investigation passes.

    The agent's circuit breaker, the daily investigation budget and the daily
    cost budget. Investigations people ask for pass the circuit breaker and
    the cost budget too, but have their own daily allowance
    (``admit_requested_investigation``), so clicks cannot spend this one.
    """
    return _circuit_refusal() or _daily_refusal(monitor) or _cost_refusal(monitor)


def _charge(monitor: ClusterMonitor, key: str, now: float) -> None:
    """Count an investigation when it starts: against the cooldown and the daily budget."""
    monitor._recent_investigations[key] = now
    monitor._daily_investigation_count += 1
    if _METRICS_AVAILABLE:
        INVESTIGATIONS_TOTAL.inc()
        INVESTIGATION_BUDGET_REMAINING.set(
            max(0, get_settings().monitor.max_daily_investigations - monitor._daily_investigation_count)
        )


def _requested_daily_refusal(monitor: ClusterMonitor, requester: str) -> InvestigationRefusal | None:
    settings = get_settings().monitor
    now = time.time()
    if now - monitor._requested_daily_reset > 86400:
        monitor._requested_daily_count = 0
        monitor._requested_daily_by_user = {}
        monitor._requested_daily_reset = now
    retry = max(1, int(monitor._requested_daily_reset + 86400 - now))
    total, limit = monitor._requested_daily_count, settings.max_daily_requested_investigations
    if total >= limit:
        return InvestigationRefusal(
            "daily_budget",
            f"The daily allowance for requested investigations is used up ({total}/{limit}).",
            retry_after=retry,
        )
    mine, per_user = (
        monitor._requested_daily_by_user.get(requester, 0),
        settings.max_daily_requested_investigations_per_user,
    )
    if mine >= per_user:
        return InvestigationRefusal(
            "user_budget",
            f"You have used your daily allowance of requested investigations ({mine}/{per_user}).",
            retry_after=retry,
        )
    return None


# Admission and charge of requests happen together under this lock, so two
# requests cannot both pass a check that only one of them fits under.
_request_lock = threading.Lock()


@dataclass(frozen=True)
class RequestCharge:
    """What admitting a request charged, so it can be refunded if the run never starts."""

    key: str
    requester: str
    previous_cooldown: float | None


def admit_requested_investigation(
    monitor: ClusterMonitor, finding: dict, *, requester: str = "", running_elsewhere: int = 0
) -> InvestigationRefusal | RequestCharge:
    """Admit an investigation a person asked for and charge it, or say why not.

    The circuit breaker and cost budget are the scan's; the daily allowance is
    the requests' own (``max_daily_requested_investigations``, and
    ``..._per_user`` for each person), so requests cannot use up the budget
    the monitor keeps for critical findings. The per-condition cooldown is
    keyed like the scan's, so a person and the monitor do not investigate one
    condition twice inside it. Concurrency counts the monitor's running plans
    plus ``running_elsewhere`` (requested and inbox runs in progress). With
    ``max_concurrent_investigations`` above 1 a request may not take the last
    slot, which is kept for the monitor; at 1 requests share the single slot
    (still bounded by the daily allowances), or they could never run.
    Category, severity and noise eligibility are the scan's own choices about
    what to look at unasked, so they do not apply here.

    The check and the charge are one step under a lock, so parallel requests
    cannot overshoot an allowance. If the run then fails to start, call
    ``refund_requested`` with the returned charge. The counters are in memory
    and start again from zero when the agent restarts.
    """
    with _request_lock:
        refusal = _circuit_refusal() or _requested_daily_refusal(monitor, requester) or _cost_refusal(monitor)
        if refusal is not None:
            return refusal

        settings = get_settings()
        now = time.time()
        key = _finding_key(finding)
        cooldown = settings.monitor.investigation_cooldown
        last = monitor._recent_investigations.get(key)
        if last is not None and now - last < cooldown:
            wait = max(1, int(last + cooldown - now))
            return InvestigationRefusal(
                "cooldown",
                f"This was investigated {int(now - last)}s ago; it can be investigated again in {wait}s.",
                retry_after=wait,
            )

        monitor._investigation_tasks = [t for t in monitor._investigation_tasks if not t.done()]
        running = len(monitor._investigation_tasks) + running_elsewhere
        limit = settings.monitor.max_concurrent_investigations
        request_limit = limit - 1 if limit > 1 else limit
        if running >= request_limit:
            kept = " (one slot kept for the monitor)" if limit > 1 else ""
            return InvestigationRefusal(
                "concurrency",
                f"{running} investigations are already running (limit {limit}{kept}); try again when one finishes.",
                retry_after=30,
            )

        monitor._recent_investigations[key] = now
        monitor._requested_daily_count += 1
        monitor._requested_daily_by_user[requester] = monitor._requested_daily_by_user.get(requester, 0) + 1
        if _METRICS_AVAILABLE:
            INVESTIGATIONS_TOTAL.inc()
        return RequestCharge(key=key, requester=requester, previous_cooldown=last)


def refund_requested(monitor: ClusterMonitor, charge: RequestCharge) -> None:
    """Undo an admission whose run never started (the claim or the first save failed)."""
    with _request_lock:
        if charge.previous_cooldown is None:
            monitor._recent_investigations.pop(charge.key, None)
        else:
            monitor._recent_investigations[charge.key] = charge.previous_cooldown
        monitor._requested_daily_count = max(0, monitor._requested_daily_count - 1)
        mine = monitor._requested_daily_by_user.get(charge.requester, 0) - 1
        if mine > 0:
            monitor._requested_daily_by_user[charge.requester] = mine
        else:
            monitor._requested_daily_by_user.pop(charge.requester, None)


async def run_investigations(monitor: ClusterMonitor, findings: list[dict]) -> None:
    """Run proactive read-only investigations for critical findings."""
    if budget_refusal(monitor) is not None:
        return
    _settings = get_settings()

    max_per_scan = _settings.monitor.investigations_max_per_scan
    cooldown_seconds = _settings.monitor.investigation_cooldown
    allowed_categories = {
        item.strip() for item in _settings.monitor.investigation_categories.split(",") if item.strip()
    }

    investigations_run = 0
    now = time.time()
    for finding in findings:
        if investigations_run >= max_per_scan:
            break
        if finding.get("severity") not in (SEVERITY_CRITICAL, SEVERITY_WARNING):
            continue
        if finding.get("category") not in allowed_categories:
            continue

        noise_score = finding.get("noiseScore", 0.0)
        if noise_score >= monitor._noise_threshold:
            logger.info(
                "Skipping investigation for noisy finding: %s (noiseScore=%.2f)",
                finding.get("title", "")[:40],
                noise_score,
            )
            monitor._noise_suppressed += 1
            monitor._noise_suppressed_last_scan += 1
            continue

        key = _finding_key(finding)
        last_time = monitor._recent_investigations.get(key, 0.0)
        if now - last_time < cooldown_seconds:
            continue

        from .confidence import _finding_content_hash

        content_hash = _finding_content_hash(finding)
        prev_hash = monitor._investigation_fingerprints.get(key)
        if prev_hash == content_hash:
            logger.info(
                "Skipping investigation for unchanged finding: %s (hash=%s)",
                finding.get("title", "")[:40],
                content_hash,
            )
            continue

        try:
            from ..log_fingerprinter import fingerprint_finding

            fps = fingerprint_finding(finding)
            if fps:
                finding["_log_fingerprints"] = fps
                logger.info(
                    "Log fingerprints for %s: %s",
                    finding.get("title", "")[:40],
                    ", ".join(f"{fp['category']}({fp['count']})" for fp in fps[:3]),
                )
        except Exception:
            logger.debug("Log fingerprinting failed for finding", exc_info=True)

        try:
            from .plan_executor import template_for

            template = template_for(finding)
        except Exception:
            logger.debug("Plan template lookup failed", exc_info=True)
            template = None

        if template is not None:
            from ..inbox_investigation import active_count as _inbox_runs

            monitor._investigation_tasks = [t for t in monitor._investigation_tasks if not t.done()]
            running = len(monitor._investigation_tasks) + _inbox_runs()
            if running >= _settings.monitor.max_concurrent_investigations:
                logger.info(
                    "Skipping investigation for %s — %d investigations already running (incl. inbox runs)",
                    finding.get("title", "")[:40],
                    running,
                )
                continue

        # Counted when it starts, for both strategies: a failed or timed-out
        # investigation spent its tokens too. Freeform runs used to count only
        # on success, so one that kept failing was retried every scan, outside
        # the daily budget and the cooldown.
        monitor._investigation_fingerprints[key] = content_hash
        investigations_run += 1
        _charge(monitor, key, now)

        if template is None:
            # One read-only agent run; short enough to run inside the scan.
            await investigate(monitor, finding)
            continue

        # A plan runs several phases, so it runs beside the scan. Timeout,
        # failure reporting, saving and learning are the same as above:
        # investigation_pipeline.investigate does both.
        task = asyncio.create_task(
            monitor._try_plan_execution(finding),
            name=f"plan-{finding.get('id', 'unknown')[:12]}",
        )
        monitor._investigation_tasks.append(task)

        def _on_plan_done(t: asyncio.Task, f=finding) -> None:
            try:
                if t.cancelled() or not t.result():
                    logger.warning(
                        "Plan investigation did not complete for %s — see its investigation report",
                        f.get("title", "")[:40],
                    )
            except Exception:
                logger.warning("Plan investigation raised for %s", f.get("title", "")[:40], exc_info=True)

        task.add_done_callback(_on_plan_done)
        logger.info("Spawned plan investigation for %s (template=%s)", finding.get("title", "")[:40], template.name)
