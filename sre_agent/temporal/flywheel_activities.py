"""Activity wrapper for the flywheel tasks.

The tasks live in ``monitor/flywheel.py`` as monitor-free functions; this is
only the activity boundary. One activity taking the cadence as data (rather
than one activity per cadence) keeps the workflow's command sequence identical
for daily and weekly firings.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from temporalio import activity

logger = logging.getLogger("pulse_agent.temporal")


@activity.defn(name="pulse.flywheel.run")
async def run_flywheel_cadence(cadence: str) -> dict:
    from ..monitor.flywheel import run_daily_tasks, run_weekly_tasks

    if cadence == "weekly":
        summary = await run_weekly_tasks()
    elif cadence == "daily":
        summary = await run_daily_tasks()
    else:
        raise ValueError(f"Unknown flywheel cadence: {cadence!r}")
    logger.info("Flywheel (%s) via schedule: %s", cadence, summary)
    return {"cadence": cadence, **summary}


FLYWHEEL_ACTIVITIES: Sequence[Callable[..., Any]] = [run_flywheel_cadence]
