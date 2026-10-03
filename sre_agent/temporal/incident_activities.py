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
    from ..monitor.fix_planner import FixPlan, FixPreconditionError, execute_fix_with_snapshot

    activity.heartbeat("starting")
    fix_plan = FixPlan(
        strategy=plan["strategy"],
        cause_category=plan.get("cause_category", ""),
        confidence=float(plan.get("confidence", 0)),
        description=plan.get("description", ""),
        params=plan.get("params", {}),
    )
    from kubernetes.client.rest import ApiException
    from temporalio.exceptions import ApplicationError

    try:
        execution = execute_fix_with_snapshot(fix_plan)
    except FixPreconditionError as exc:
        raise ApplicationError(str(exc), type="FixNotApplied", non_retryable=True) from exc
    except ApiException as exc:
        if exc.status in (400, 403, 404, 409, 422):
            raise ApplicationError(
                "Mutation refused by API preconditions or permissions", type="FixNotApplied", non_retryable=True
            ) from exc
        raise
    tool, before, after = execution.result
    if not execution.applied:
        from temporalio.exceptions import ApplicationError

        raise ApplicationError(f"Fix was not applied: {after}", type="FixNotApplied", non_retryable=True)
    activity.heartbeat("applied")
    return {
        "tool": tool,
        "before": before,
        "after": after,
        "snapshot": execution.snapshot,
        "verify_resource": execution.verify_resources[0] if execution.verify_resources else None,
    }


@activity.defn(name="pulse.incident.verify")
async def verify_fix(resource: dict) -> dict:
    """Affirmative post-check: is the resource actually healthy now?

    Raises on "not yet healthy" so Temporal's retry policy provides the grace
    window a rollout needs — the same idea as the monitor's 3-scan window, but
    expressed as backoff the platform owns instead of scan-cycle bookkeeping.
    """
    from ..monitor.health_gate import PASS, check_resource

    gate = check_resource(resource.get("kind", "Pod"), resource.get("name", ""), resource.get("namespace", ""))
    if gate.status != PASS:
        raise RuntimeError(f"Recovery not verified ({gate.status}): {gate.detail}")
    return {"healthy": True, "evidence": gate.detail, "baseline": gate.observations}


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
    from ..monitor.health_gate import FAIL, PASS, check_resource

    gate = check_resource(resource.get("kind", "Pod"), resource.get("name", ""), resource.get("namespace", ""))
    if gate.status not in (PASS, FAIL):
        raise RuntimeError(f"Recovery recheck is unverifiable: {gate.detail}")
    if resource.get("kind", "Pod") == "Pod" and gate.status == PASS:
        uid = resource.get("uid_at_fix")
        baseline = resource.get("restarts_at_fix")
        if not isinstance(uid, str) or not uid or type(baseline) is not int or baseline < 0:
            raise RuntimeError("Pod recurrence baseline is missing; sustained recovery cannot be confirmed")
        if gate.observations.get("uid") != uid:
            raise RuntimeError("Pod identity changed; original recurrence comparison is unverifiable")
        return {"recurred": gate.observations["restarts"] > baseline, "evidence": gate.detail}
    return {"recurred": gate.status == FAIL, "evidence": gate.detail}


@activity.defn(name="pulse.incident.record_outcome")
async def record_outcome(finding_id: str, verdict: str, evidence: str) -> None:
    """Persist the final verdict where fix history already lives."""
    from temporalio.exceptions import ApplicationError

    from ..monitor.findings import _ts
    from ..repositories.monitor_repo import get_monitor_repo

    repo = get_monitor_repo()
    action_id = finding_id
    if repo.get_action_by_id(action_id) is None:
        # Legacy workflow inputs carried a finding ID instead of an action ID.
        # Only one dispatched action is an unambiguous link; never choose a latest row.
        rows = repo.db.fetchall("SELECT id FROM actions WHERE finding_id = ? AND status = 'dispatched'", (finding_id,))
        if len(rows) != 1:
            raise ApplicationError("Outcome has no unique linked action row", type="MissingAction")
        action_id = rows[0]["id"]
    # Use the repository directly: the presentation helper swallows DB errors,
    # which would let the durable activity claim persistence without a write.
    repo.update_action_verification(action_id, verdict, evidence, _ts())


INCIDENT_ACTIVITIES: Sequence[Callable[..., Any]] = [
    capture_snapshot,
    apply_fix,
    verify_fix,
    restore_snapshot,
    check_recurrence,
    record_outcome,
]

__all__ = ["INCIDENT_ACTIVITIES", *[f.__name__ for f in INCIDENT_ACTIVITIES]]
