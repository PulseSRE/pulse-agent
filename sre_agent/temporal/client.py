"""Client facade: how the rest of Pulse talks to Temporal.

REST handlers call these four functions and nothing else, so tests can patch
one seam and the temporalio import stays behind the feature flag. Every
function raises ``TemporalDisabledError`` when no host is configured — the caller
turns that into a 503 with the reason, rather than half the API silently not
existing.
"""

from __future__ import annotations

import logging
import uuid

logger = logging.getLogger("pulse_agent.temporal")


class TemporalDisabledError(RuntimeError):
    """Raised when Temporal endpoints are used with no host configured."""

    def __init__(self) -> None:
        super().__init__(
            "Durable plan execution is not configured. Set PULSE_AGENT_TEMPORAL_HOST "
            "to a Temporal frontend (e.g. temporal-frontend.temporal.svc:7233) to enable it."
        )


def _settings():
    from ..config import get_settings

    return get_settings().temporal


async def _connect():
    cfg = _settings()
    if not cfg.host:
        raise TemporalDisabledError()
    from temporalio.client import Client

    return await Client.connect(cfg.host, namespace=cfg.namespace)


async def start_plan_run(incident_type: str, incident: dict) -> dict:
    """Start a durable run of one plan. Returns ids the UI polls with."""
    from .plan_workflow import PlanRunInput, PlanWorkflow

    cfg = _settings()
    client = await _connect()
    workflow_id = f"plan-{incident_type}-{uuid.uuid4().hex[:10]}"
    handle = await client.start_workflow(
        PlanWorkflow.run,
        PlanRunInput(
            incident_type=incident_type,
            incident=incident,
            approval_timeout_seconds=cfg.approval_timeout,
        ),
        id=workflow_id,
        task_queue=cfg.task_queue,
    )
    logger.info("Started durable plan run %s (%s)", workflow_id, incident_type)
    return {"workflow_id": workflow_id, "run_id": handle.result_run_id}


async def describe_plan_run(workflow_id: str) -> dict:
    """Status plus live progress for one run.

    Progress comes from the workflow's own query, so it reflects exactly what
    the workflow believes — including which phase is waiting on a human.
    """
    client = await _connect()
    handle = client.get_workflow_handle(workflow_id)
    desc = await handle.describe()
    status = desc.status.name if desc.status else "UNKNOWN"

    out: dict = {"workflow_id": workflow_id, "status": status}
    if status == "RUNNING":
        try:
            out["progress"] = await handle.query("progress")
        except Exception:
            logger.debug("progress query failed for %s", workflow_id, exc_info=True)
    elif status == "COMPLETED":
        out["result"] = await handle.result()
    return out


async def approve_plan_phase(workflow_id: str, phase_id: str, approved: bool) -> None:
    """Deliver a human's verdict to a waiting run."""
    client = await _connect()
    handle = client.get_workflow_handle(workflow_id)
    await handle.signal("approve_phase", args=[phase_id, approved])
