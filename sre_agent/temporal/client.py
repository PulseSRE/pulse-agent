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


async def start_incident_run(
    finding_id: str,
    resource: dict,
    fix_plan: dict,
    *,
    require_approval: bool = False,
    recurrence_window_seconds: int = 1800,
) -> dict:
    """Start the durable fix lifecycle for one finding.

    The workflow id is derived from the finding so a duplicate dispatch for the
    same finding is rejected by Temporal rather than by application bookkeeping
    — the monitor's own dedup (``_recent_fixes``, cooldowns) stays, but this
    makes "one fix workflow per finding" a platform guarantee.
    """
    from .incident_workflow import IncidentInput, IncidentWorkflow

    cfg = _settings()
    client = await _connect()
    workflow_id = f"incident-{finding_id}"
    handle = await client.start_workflow(
        IncidentWorkflow.run,
        IncidentInput(
            finding_id=finding_id,
            resource=resource,
            fix_plan=fix_plan,
            require_approval=require_approval,
            approval_timeout_seconds=cfg.approval_timeout,
            recurrence_window_seconds=recurrence_window_seconds,
        ),
        id=workflow_id,
        task_queue=cfg.task_queue,
    )
    logger.info("Started durable incident run %s", workflow_id)
    return {"workflow_id": workflow_id, "run_id": handle.result_run_id}

async def cancel_run(workflow_id: str, reason: str = "") -> None:
    """Ask a running workflow to stop.

    Cancellation is cooperative: Temporal delivers it at the next await point,
    so an in-flight activity finishes rather than being severed mid-write. For
    a workflow that mutates a cluster that is the behaviour you want — a fix
    interrupted between apply and verify is exactly the state this whole
    migration exists to avoid.
    """
    client = await _connect()
    handle = client.get_workflow_handle(workflow_id)
    await handle.cancel()
    logger.info("Cancellation requested for %s%s", workflow_id, f": {reason}" if reason else "")


async def list_runs(limit: int = 25) -> list[dict]:
    """Recent workflow runs, newest first — what the UI lists.

    Uses Temporal's own visibility store rather than a Pulse table: the
    platform already records every run, and a second source of truth would be
    one more thing to drift.
    """
    client = await _connect()
    out: list[dict] = []
    async for wf in client.list_workflows(page_size=limit):
        out.append(
            {
                "workflow_id": wf.id,
                "run_id": wf.run_id,
                "type": wf.workflow_type,
                "status": wf.status.name if wf.status else "UNKNOWN",
                "started_at": wf.start_time.isoformat() if wf.start_time else "",
                "closed_at": wf.close_time.isoformat() if wf.close_time else "",
            }
        )
        if len(out) >= limit:
            break
    return out

