"""The worker that executes plan workflows and their activities.

Runs inside the agent pod as a lifespan task when PULSE_AGENT_TEMPORAL_HOST is
set — no separate Deployment to operate for v1, and the worker shares the
agent's credentials, tools and database exactly as the in-process engine does.
Splitting it into its own Deployment later is an operator change, not a code
change: this module is already the entrypoint.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("pulse_agent.temporal")

# True once this process has confirmed the flywheel schedules exist on the
# Temporal server. monitor/flywheel.py checks it to stand its inline
# scheduler down — the work must not run on two cadences at once.
_FLYWHEEL_SCHEDULED = False


def flywheel_scheduled() -> bool:
    return _FLYWHEEL_SCHEDULED


async def _ensure_flywheel_schedules(client) -> None:
    """Create the daily/weekly flywheel schedules if they do not exist.

    Idempotent by construction: creation races (two agent pods booting, a
    schedule left from a previous deploy) land in ScheduleAlreadyRunningError,
    which is success — the schedule exists, which is all this asks for.

    SKIP on overlap and a 1-hour catchup window: maintenance that is already
    running does not need a second copy, and a firing missed while the whole
    Temporal server was down is worth running late — but a pod that was down
    for a day should not replay a backlog of stale firings.
    """
    from datetime import timedelta

    from temporalio.client import (
        Schedule,
        ScheduleActionStartWorkflow,
        ScheduleAlreadyRunningError,
        ScheduleIntervalSpec,
        ScheduleOverlapPolicy,
        SchedulePolicy,
        ScheduleSpec,
    )

    from ..config import get_settings
    from .flywheel_workflow import FlywheelWorkflow

    cfg = get_settings().temporal
    global _FLYWHEEL_SCHEDULED

    for schedule_id, cadence, every in (
        ("pulse-flywheel-daily", "daily", timedelta(days=1)),
        ("pulse-flywheel-weekly", "weekly", timedelta(days=7)),
    ):
        try:
            await client.create_schedule(
                schedule_id,
                Schedule(
                    action=ScheduleActionStartWorkflow(
                        FlywheelWorkflow.run,
                        cadence,
                        id=f"{schedule_id}-run",
                        task_queue=cfg.task_queue,
                    ),
                    spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=every)]),
                    policy=SchedulePolicy(
                        overlap=ScheduleOverlapPolicy.SKIP,
                        catchup_window=timedelta(hours=1),
                    ),
                ),
            )
            logger.info("Created flywheel schedule %s (every %s)", schedule_id, every)
        except ScheduleAlreadyRunningError:
            logger.debug("Flywheel schedule %s already exists", schedule_id)
        except Exception:
            # A server too old for Schedules, or a transient failure: the
            # inline flywheel keeps running, which is the correct fallback.
            logger.warning("Could not ensure flywheel schedule %s", schedule_id, exc_info=True)
            return

    _FLYWHEEL_SCHEDULED = True


async def run_worker(shutdown: asyncio.Event) -> None:
    """Poll the task queue until ``shutdown`` is set. Never raises on exit."""
    from temporalio.client import Client
    from temporalio.worker import Worker

    from ..config import get_settings
    from .activities import ALL_ACTIVITIES
    from .flywheel_activities import FLYWHEEL_ACTIVITIES
    from .flywheel_workflow import FlywheelWorkflow
    from .incident_activities import INCIDENT_ACTIVITIES
    from .incident_workflow import IncidentWorkflow
    from .plan_workflow import PlanWorkflow

    cfg = get_settings().temporal
    try:
        client = await Client.connect(cfg.host, namespace=cfg.namespace)
    except Exception:
        logger.warning(
            "Temporal worker could not connect to %s — durable plan runs are unavailable "
            "until the next restart. The in-process engine is unaffected.",
            cfg.host,
            exc_info=True,
        )
        return

    # A build id stamps every task this worker completes, so a code change that
    # alters workflow logic can be rolled out under a new id without old
    # in-flight histories replaying against new code (Temporal worker
    # versioning). Derived from the agent version — the same string the CR
    # pins — so "which build ran this" is answerable from the workflow history.
    try:
        from importlib.metadata import version as _pkg_version

        _pulse_version = _pkg_version("openshift-sre-agent")
    except Exception:
        _pulse_version = "unknown"

    worker = Worker(
        client,
        task_queue=cfg.task_queue,
        workflows=[PlanWorkflow, IncidentWorkflow, FlywheelWorkflow],
        activities=[*ALL_ACTIVITIES, *INCIDENT_ACTIVITIES, *FLYWHEEL_ACTIVITIES],
        build_id=f"pulse-{_pulse_version}",
    )
    await _ensure_flywheel_schedules(client)

    logger.info("Temporal worker polling %s (queue=%s)", cfg.host, cfg.task_queue)
    async with worker:
        await shutdown.wait()
    logger.info("Temporal worker stopped")
