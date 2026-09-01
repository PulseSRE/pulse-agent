"""Activities for the incident lifecycle workflow.

Each one wraps machinery Pulse already has — snapshot capture, fix execution,
verification probes, recurrence checking — so the workflow orchestrates proven
code rather than reimplementing it. The point of the workflow is the
*sequencing guarantees*, not new fix logic.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from temporalio import activity

logger = logging.getLogger("pulse_agent.temporal.incident")


@activity.defn(name="pulse.incident.snapshot")
async def capture_snapshot(resource: dict) -> dict | None:
    """Capture a restorable copy of the resource before anything mutates it.

    This is the compensation data for the saga below: without it a failed fix
    can only be described, not undone.
    """
    from ..snapshot import capture

    return capture(
        kind=resource.get("kind", "Pod"),
        name=resource.get("name", ""),
        namespace=resource.get("namespace", ""),
    )


@activity.defn(name="pulse.incident.apply_fix")
async def apply_fix(plan: dict) -> dict:
    """Execute a targeted fix. Returns the tool used and before/after state.

    Heartbeats so a worker that dies mid-fix is detected in seconds rather
    than at the activity timeout — the difference between a fast retry and a
    stalled incident.
    """
    from ..monitor.fix_planner import FixPlan, execute_fix

    activity.heartbeat("starting")
    fix_plan = FixPlan(
        strategy=plan["strategy"],
        cause_category=plan.get("cause_category", ""),
        confidence=float(plan.get("confidence", 0)),
        description=plan.get("description", ""),
        params=plan.get("params", {}),
    )
    tool, before, after = execute_fix(fix_plan)
    activity.heartbeat("applied")
    return {"tool": tool, "before": before, "after": after}


@activity.defn(name="pulse.incident.verify")
async def verify_fix(resource: dict) -> dict:
    """Affirmative post-check: is the resource actually healthy now?

    Raises on "not yet healthy" so Temporal's retry policy provides the grace
    window a rollout needs — the same idea as the monitor's 3-scan window, but
    expressed as backoff the platform owns instead of scan-cycle bookkeeping.
    """
    from ..k8s_client import get_core_client, safe

    ns, name = resource.get("namespace", ""), resource.get("name", "")
    pod = safe(lambda: get_core_client().read_namespaced_pod(name, ns))
    if isinstance(pod, str):
        # Gone entirely: for a controller-managed pod that is a successful
        # replacement, not a failure.
        return {"healthy": True, "evidence": f"pod {name} no longer present ({pod})"}

    phase = getattr(pod.status, "phase", "")
    restarts = 0
    if pod.status and pod.status.container_statuses:
        restarts = pod.status.container_statuses[0].restart_count
    if phase != "Running":
        raise RuntimeError(f"pod {name} is {phase}, not Running yet")
    return {"healthy": True, "evidence": f"pod {name} Running, restarts={restarts}"}


@activity.defn(name="pulse.incident.compensate")
async def restore_snapshot(snapshot: dict | None) -> str:
    """Undo the fix by restoring the pre-write snapshot — the saga's rollback."""
    if not snapshot:
        return "no snapshot captured; nothing to restore"
    from ..snapshot import restore

    return restore(snapshot)


@activity.defn(name="pulse.incident.check_recurrence")
async def check_recurrence(resource: dict) -> dict:
    """Did the problem come back after the settling window?

    The monitor answers this by re-reading the database on a later scan, which
    a restart can miss. Here it is a plain read after a durable timer.
    """
    from ..k8s_client import get_core_client, safe

    ns, name = resource.get("namespace", ""), resource.get("name", "")
    pod = safe(lambda: get_core_client().read_namespaced_pod(name, ns))
    if isinstance(pod, str):
        return {"recurred": False, "evidence": "resource absent at recheck"}

    restarts = 0
    if pod.status and pod.status.container_statuses:
        restarts = pod.status.container_statuses[0].restart_count
    phase = getattr(pod.status, "phase", "")
    recurred = phase != "Running" or restarts > int(resource.get("restarts_at_fix", 0))
    return {
        "recurred": recurred,
        "evidence": f"phase={phase} restarts={restarts}",
    }


@activity.defn(name="pulse.incident.record_outcome")
async def record_outcome(finding_id: str, verdict: str, evidence: str) -> None:
    """Persist the final verdict where fix history already lives."""
    try:
        from ..monitor.actions import update_action_verification

        update_action_verification(finding_id, verdict, evidence)
    except Exception:
        logger.warning("Could not record outcome for %s", finding_id, exc_info=True)


INCIDENT_ACTIVITIES: Sequence[Callable[..., Any]] = [
    capture_snapshot,
    apply_fix,
    verify_fix,
    restore_snapshot,
    check_recurrence,
    record_outcome,
]

__all__ = ["INCIDENT_ACTIVITIES", *[f.__name__ for f in INCIDENT_ACTIVITIES]]
