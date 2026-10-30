"""Which findings get investigated, and when.

Eligibility, budgets, cooldowns and concurrency. How an investigation runs and
what happens to its result is ``investigation_pipeline``, for both strategies.
"""

from __future__ import annotations

import asyncio
import logging
import time
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


async def run_investigations(monitor: ClusterMonitor, findings: list[dict]) -> None:
    """Run proactive read-only investigations for critical findings."""
    from ..agent import _circuit_breaker

    if _circuit_breaker.is_open:
        logger.info("Skipping proactive investigations: agent circuit breaker open")
        return

    _settings = get_settings()
    max_daily = _settings.monitor.max_daily_investigations
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
        return

    # Cost budget gate (cached — only queries DB every 5 minutes)
    _budget_usd = _settings.cost_budget_usd
    if _budget_usd > 0:
        try:
            now_t = time.time()
            cached = monitor._cost_budget_cache
            if cached and (now_t - cached[0]) < 300:
                today_cost = cached[1]
            else:
                from ..observability import estimate_cost_usd
                from ..repositories import get_analytics_repo

                totals = get_analytics_repo().fetch_token_totals(1)
                if totals and totals["total_incidents"] > 0:
                    # Includes investigations now (they record their usage as
                    # turns) and cache tokens, priced for the configured model.
                    today_cost = estimate_cost_usd(
                        int(totals["total_input"]),
                        int(totals["total_output"]),
                        int(totals.get("total_cache_read") or 0),
                        int(totals.get("total_cache_write") or 0),
                    )
                else:
                    today_cost = 0.0
                monitor._cost_budget_cache = (now_t, today_cost)
            if _METRICS_AVAILABLE:
                COST_BUDGET_REMAINING_USD.set(max(0, _budget_usd - today_cost))
            if today_cost >= _budget_usd:
                logger.warning(
                    "Daily cost budget exceeded ($%.2f / $%.2f) — pausing investigations",
                    today_cost,
                    _budget_usd,
                )
                if _METRICS_AVAILABLE:
                    COST_BUDGET_EXHAUSTION_TOTAL.inc()
                return
        except Exception:
            logger.debug("Cost budget check failed", exc_info=True)

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
            monitor._investigation_tasks = [t for t in monitor._investigation_tasks if not t.done()]
            if len(monitor._investigation_tasks) >= _settings.monitor.max_concurrent_investigations:
                logger.info(
                    "Skipping investigation for %s — %d tasks already running",
                    finding.get("title", "")[:40],
                    len(monitor._investigation_tasks),
                )
                continue

        # Counted when it starts, for both strategies: a failed or timed-out
        # investigation spent its tokens too. Freeform runs used to count only
        # on success, so one that kept failing was retried every scan, outside
        # the daily budget and the cooldown.
        monitor._recent_investigations[key] = now
        monitor._investigation_fingerprints[key] = content_hash
        investigations_run += 1
        monitor._daily_investigation_count += 1
        if _METRICS_AVAILABLE:
            INVESTIGATIONS_TOTAL.inc()
            INVESTIGATION_BUDGET_REMAINING.set(max(0, max_daily - monitor._daily_investigation_count))

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
