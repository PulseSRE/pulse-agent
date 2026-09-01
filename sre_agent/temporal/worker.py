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


async def run_worker(shutdown: asyncio.Event) -> None:
    """Poll the task queue until ``shutdown`` is set. Never raises on exit."""
    from temporalio.client import Client
    from temporalio.worker import Worker

    from ..config import get_settings
    from .activities import ALL_ACTIVITIES
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

    worker = Worker(
        client,
        task_queue=cfg.task_queue,
        workflows=[PlanWorkflow],
        activities=ALL_ACTIVITIES,
    )
    logger.info("Temporal worker polling %s (queue=%s)", cfg.host, cfg.task_queue)
    async with worker:
        await shutdown.wait()
    logger.info("Temporal worker stopped")
