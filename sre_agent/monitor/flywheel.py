"""Daily and weekly maintenance tasks (flywheel).

The tasks themselves are monitor-free functions so two schedulers can share
them: the in-process fallback below (timestamps on the monitor, piggybacked on
the scan loop) and the Temporal Schedules in ``temporal/flywheel_workflow``.
When the Temporal worker has registered its schedules, the inline path stands
down — the work must not run twice, and the durable timer is the better one:
it fires even while the agent pod is down and leaves a run history behind.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cluster_monitor import ClusterMonitor

logger = logging.getLogger("pulse_agent.monitor")


async def run_daily_tasks() -> dict:
    """The daily flywheel body. Returns a small summary for whoever ran it."""
    summary: dict = {}

    # Candidates whose verification never arrived are dropped, not promoted.
    # Silence is not a successful outcome.
    try:
        from ..trajectory import get_learner

        learner = get_learner()
        learner.expire_stale()
        summary["trajectory"] = learner.stats()
        logger.info("Daily flywheel: trajectory learning %s", summary["trajectory"])
    except Exception:
        logger.debug("Trajectory expiry failed", exc_info=True)

    try:
        from ..selector_learning import recompute_channel_weights

        new_weights = await asyncio.to_thread(recompute_channel_weights, 7)
        if new_weights:
            from ..skill_loader import _get_selector

            _get_selector().set_weights(new_weights)
            summary["channel_weights"] = new_weights
            logger.info("Daily flywheel: applied learned channel weights: %s", new_weights)

        # Skill gaps and low performers are no longer logged here — the
        # inbox generators (gen_skill_gaps, gen_skill_low_performers)
        # surface them as actionable items with auto-resolve, which a log
        # line never was.

    except Exception:
        logger.debug("Daily flywheel failed", exc_info=True)

    return summary


async def run_weekly_tasks() -> dict:
    """The weekly flywheel body."""
    summary: dict = {}
    try:
        from ..skill_loader import _get_selector

        selector = _get_selector()
        selector.invalidate_skill_token_cache()
        summary["embedding_cache"] = "invalidated"
        logger.info("Weekly flywheel: invalidated embedding cache")

        from ..intelligence import get_intelligence_sections

        sections = await asyncio.to_thread(get_intelligence_sections)
        if sections:
            summary["intelligence_sections"] = len(sections)
            logger.info("Weekly flywheel: intelligence sections computed (%d sections)", len(sections))

    except Exception:
        logger.debug("Weekly flywheel failed", exc_info=True)
    return summary


async def run_flywheel(monitor: ClusterMonitor) -> None:
    """In-process fallback scheduler, driven by the monitor's scan loop.

    Stands down entirely while Temporal Schedules own the cadence — the
    timestamps here are in-memory and reset on every pod boot, which is
    exactly the weakness the durable schedules exist to remove.
    """
    from ..temporal.worker import flywheel_scheduled

    if flywheel_scheduled():
        return

    import time

    now = time.time()

    if now - monitor._last_daily_run > 86400:
        monitor._last_daily_run = now
        await run_daily_tasks()

    if now - monitor._last_weekly_run > 604800:
        monitor._last_weekly_run = now
        await run_weekly_tasks()
