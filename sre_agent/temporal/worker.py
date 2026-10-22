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


#: What the worker is doing, for /readiness and the run endpoints. A run
#: accepted while no worker polls sits in RUNNING forever with nothing
#: executing it; on a fresh install the agent routinely starts before the
#: Temporal server is ready, which is how that happened.
_STATE: dict = {"state": "disabled", "detail": "", "since": 0.0}

#: Reconnect backoff bounds, seconds.
_RETRY_FIRST = 2.0
_RETRY_MAX = 60.0


def _set_state(state: str, detail: str = "") -> None:
    import time

    if _STATE["state"] != state or _STATE["detail"] != detail:
        _STATE.update(state=state, detail=detail, since=time.time())


def worker_state() -> dict:
    """``state`` is disabled, connecting, polling or stopped."""
    return dict(_STATE)


def worker_ready() -> tuple[bool, str]:
    """Whether a durable run started now would be executed, and if not why."""
    state = _STATE["state"]
    if state == "polling":
        return True, ""
    if state == "connecting":
        return False, (
            "The Temporal worker has not connected yet, so a run started now would wait with nothing executing it. "
            f"Last error: {_STATE['detail'] or 'none yet'}. Pulse retries in the background; check that the "
            "Temporal server is up and reachable at PULSE_AGENT_TEMPORAL_HOST."
        )
    return False, f"The Temporal worker is {state}."


async def _connect_with_retry(shutdown: asyncio.Event):
    """Connect, retrying with backoff until connected or shut down.

    It used to try once at startup and give up until the next pod restart.
    """
    from temporalio.client import Client

    from ..config import get_settings

    cfg = get_settings().temporal
    delay = _RETRY_FIRST
    logged = False
    while not shutdown.is_set():
        try:
            return await Client.connect(cfg.host, namespace=cfg.namespace)
        except Exception as e:
            _set_state("connecting", f"{type(e).__name__}: {str(e)[:200]}")
            if not logged:
                logger.warning(
                    "Temporal worker cannot connect to %s yet; retrying in the background. "
                    "Durable runs are refused until it connects. The in-process engine is unaffected.",
                    cfg.host,
                    exc_info=True,
                )
                logged = True
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=delay)
            except TimeoutError:
                pass
            delay = min(delay * 2, _RETRY_MAX)
    return None


async def run_worker(shutdown: asyncio.Event) -> None:
    """Poll the task queue until ``shutdown`` is set. Never raises on exit."""
    from temporalio.worker import Worker

    from ..config import get_settings
    from .activities import ALL_ACTIVITIES
    from .flywheel_activities import FLYWHEEL_ACTIVITIES
    from .flywheel_workflow import FlywheelWorkflow
    from .incident_activities import INCIDENT_ACTIVITIES
    from .incident_workflow import IncidentWorkflow
    from .plan_workflow import PlanWorkflow

    cfg = get_settings().temporal
    _set_state("connecting")
    client = await _connect_with_retry(shutdown)
    if client is None:
        _set_state("stopped", "shut down before connecting")
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
    try:
        async with worker:
            _set_state("polling", cfg.host)
            await shutdown.wait()
    except Exception as e:
        _set_state("stopped", f"{type(e).__name__}: {str(e)[:200]}")
        logger.error("Temporal worker stopped unexpectedly; durable runs are refused", exc_info=True)
        return
    _set_state("stopped", "agent shutting down")
    logger.info("Temporal worker stopped")
