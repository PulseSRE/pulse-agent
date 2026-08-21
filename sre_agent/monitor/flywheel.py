"""Daily and weekly maintenance tasks (flywheel)."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cluster_monitor import ClusterMonitor

logger = logging.getLogger("pulse_agent.monitor")


async def run_flywheel(monitor: ClusterMonitor) -> None:
    """Execute daily/weekly maintenance tasks."""
    import time

    now = time.time()

    if now - monitor._last_daily_run > 86400:
        monitor._last_daily_run = now

        # Candidates whose verification never arrived are dropped, not promoted.
        # Silence is not a successful outcome.
        try:
            from ..trajectory import get_learner

            learner = get_learner()
            learner.expire_stale()
            logger.info("Daily flywheel: trajectory learning %s", learner.stats())
        except Exception:
            logger.debug("Trajectory expiry failed", exc_info=True)

        try:
            from ..selector_learning import (
                identify_skill_gaps,
                prune_low_performers,
                recompute_channel_weights,
            )

            new_weights = await asyncio.to_thread(recompute_channel_weights, 7)
            if new_weights:
                from ..skill_loader import _get_selector

                _get_selector().set_weights(new_weights)
                logger.info("Daily flywheel: applied learned channel weights: %s", new_weights)

            gaps = await asyncio.to_thread(identify_skill_gaps, 30)
            if gaps:
                logger.info("Daily flywheel: %d skill gaps identified", len(gaps))

            flagged = await asyncio.to_thread(prune_low_performers, 30)
            if flagged:
                logger.warning("Daily flywheel: flagged low performers: %s", flagged)

        except Exception:
            logger.debug("Daily flywheel failed", exc_info=True)

    if now - monitor._last_weekly_run > 604800:
        monitor._last_weekly_run = now
        try:
            from ..skill_loader import _get_selector

            selector = _get_selector()
            selector.invalidate_skill_token_cache()
            logger.info("Weekly flywheel: invalidated embedding cache")

            from ..intelligence import get_intelligence_sections

            sections = await asyncio.to_thread(get_intelligence_sections)
            if sections:
                logger.info("Weekly flywheel: intelligence sections computed (%d sections)", len(sections))

        except Exception:
            logger.debug("Weekly flywheel failed", exc_info=True)
