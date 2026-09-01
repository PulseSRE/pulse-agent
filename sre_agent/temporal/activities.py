"""Activities: everything the plan interpreter does that touches the world.

Temporal workflows must be deterministic, so all IO — loading the plan
definition, running a phase through the agent, recording the execution — lives
here. Each activity reconstructs the engine's dataclasses from plain dicts at
the boundary, because activity arguments travel through Temporal's payload
converter and dataclasses with defaults survive that round trip less honestly
than dicts do.

The phase activity reuses ``PlanRuntime._execute_phase`` wholesale: the
contract check, the retry-with-the-gap-named loop, and the partial downgrade
are the engine's proven behaviour, and reimplementing them here would fork it.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable, Sequence
from typing import Any

from temporalio import activity

logger = logging.getLogger("pulse_agent.temporal")


def plan_to_dict(plan) -> dict:
    """A SkillPlan as the plain-dict shape the workflow interprets."""
    return {
        "id": plan.id,
        "name": plan.name,
        "incident_type": plan.incident_type,
        "max_total_duration": plan.max_total_duration,
        "phases": [
            {
                "id": p.id,
                "skill_name": p.skill_name,
                "required": p.required,
                "depends_on": list(p.depends_on),
                "timeout_seconds": p.timeout_seconds,
                "produces": list(p.produces),
                "approval_required": p.approval_required,
                "branch_on": p.branch_on,
                "branches": dict(p.branches),
                "parallel_with": list(p.parallel_with) if p.parallel_with else None,
                "retry_limit": p.retry_limit,
            }
            for p in plan.phases
        ],
    }


def _phase_from_dict(d: dict):
    from ..skill_plan import SkillPhase

    return SkillPhase(
        id=d["id"],
        skill_name=d["skill_name"],
        required=d.get("required", True),
        depends_on=list(d.get("depends_on", [])),
        timeout_seconds=int(d.get("timeout_seconds", 120)),
        produces=list(d.get("produces", [])),
        approval_required=bool(d.get("approval_required", False)),
        retry_limit=int(d.get("retry_limit", 1)),
    )


def _output_from_dict(d: dict):
    from ..skill_plan import SkillOutput

    known = {f.name for f in dataclasses.fields(SkillOutput)}
    return SkillOutput(**{k: v for k, v in d.items() if k in known})


@activity.defn(name="pulse.load_plan")
async def load_plan(incident_type: str) -> dict:
    """The plan definition, pinned at workflow start.

    Fetched once and carried in workflow state from then on, so an edit to the
    plan mid-run changes the *next* run, never one already executing — the same
    property the version history gives edits at rest.
    """
    from ..plan_templates import get_template

    plan = get_template(incident_type)
    if plan is None:
        raise ValueError(f"No plan template for incident type '{incident_type}'")
    return plan_to_dict(plan)


@activity.defn(name="pulse.run_plan_phase")
async def run_plan_phase(plan: dict, phase_id: str, incident: dict, prior_outputs: dict) -> dict:
    """One phase, held to its contract, exactly as the in-process engine runs it."""
    from ..agent import create_async_client
    from ..plan_runtime import PlanRuntime

    phase_dict = next(p for p in plan["phases"] if p["id"] == phase_id)
    phase = _phase_from_dict(phase_dict)
    priors = {pid: _output_from_dict(od) for pid, od in prior_outputs.items()}

    client = create_async_client()
    try:
        runtime = PlanRuntime(client=client)
        output = await runtime._execute_phase(phase, incident, priors)
    finally:
        try:
            await client.close()
        except Exception:
            logger.debug("client close failed", exc_info=True)
    return dataclasses.asdict(output)


@activity.defn(name="pulse.record_plan_execution")
async def record_plan_execution(plan: dict, outputs: dict, status: str, duration_ms: int, incident: dict) -> None:
    """Persist the run to plan_executions, same rows as the in-process engine."""
    from ..plan_runtime import PlanRuntime
    from ..skill_plan import PlanResult, SkillPlan

    skill_plan = SkillPlan(
        id=plan["id"],
        name=plan["name"],
        incident_type=plan["incident_type"],
        phases=[_phase_from_dict(p) for p in plan["phases"]],
    )
    result = PlanResult(
        plan_id=plan["id"],
        plan_name=plan["name"],
        status=status,
        phases_total=len(plan["phases"]),
        phases_completed=len(outputs),
        total_duration_ms=duration_ms,
    )
    result.phase_outputs = {pid: _output_from_dict(od) for pid, od in outputs.items()}
    PlanRuntime()._record_execution(skill_plan, result, incident)


ALL_ACTIVITIES: Sequence[Callable[..., Any]] = [load_plan, run_plan_phase, record_plan_execution]
