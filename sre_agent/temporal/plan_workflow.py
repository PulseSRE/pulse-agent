"""The plan interpreter workflow.

One workflow executes *any* plan definition — including plans created in the
UI at runtime — by interpreting its phase graph. Extensibility is the point:
a new plan is data, not a deploy.

What Temporal buys over the in-process engine, concretely:

- **The run survives the pod.** Every phase result is in workflow history; a
  worker restart resumes from the last completed activity instead of losing
  the run unrecorded.
- **Approval actually waits.** ``approval_required`` phases block on a signal
  a human can send hours or days later. The in-process engine marks these
  ``needs_escalation`` and moves on, because it cannot afford to wait — here
  waiting is free, and only the *timeout* degrades to the old behaviour.
- **Waves run concurrently and branches re-target skills**, mirroring the
  in-process engine: every phase whose dependencies are settled runs in the
  same wave (``asyncio.gather`` over activities — deterministic, since the
  workflow scheduler orders it), and ``branch_on`` picks a phase's skill from
  a dependency's findings via the pure ``resolve_branch``.
- **A phase can be a whole plan.** ``subplan: <incident_type>`` runs that plan
  as a *child workflow* — its own phase graph, approval gates and history,
  linked to the parent run. Nesting is capped at ``MAX_SUBPLAN_DEPTH``.

Determinism rules: no IO here, no settings reads, no clocks but Temporal's.
All decisions are the pure functions in ``sequencing``; everything else is an
activity.

**Changing this file is a versioned operation.** A running workflow replays its
history against whatever code the worker has now, and approval waits are meant
to last days — so an edit that changes the *sequence of commands* (which
activities run, in what order, which timers or waits exist) will break replay
for every in-flight run. Two mechanisms, both already wired:

- ``workflow.patched("some-change-id")`` guards a behaviour change so old
  histories take the old branch and new ones take the new branch. Use it for
  any change to command sequence; ``workflow.deprecate_patch`` retires the
  guard once no old runs remain.
- The worker stamps a ``build_id`` derived from the agent version
  (``temporal/worker.py``), so which build produced a history is answerable,
  and Temporal's worker versioning can pin old runs to old workers.

Edits that are always safe without a patch: comments, logging, renaming locals,
and anything inside an *activity* (activities are re-executed, not replayed).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from .activities import load_plan, record_plan_execution, run_plan_phase
    from .sequencing import derive_status, ready_phases, resolve_branch, unsupported_features

#: Margin over a phase's own timeout: _execute_phase may retry once with the
#: contract gap named, so the activity gets room for both attempts plus the
#: judge. The engine's own per-attempt timeout still applies inside.
_PHASE_TIMEOUT_MARGIN = 2.5


#: How deep subplan nesting may go. A plan whose subplan (transitively)
#: names itself would otherwise spawn children forever; three levels is
#: more composition than any real runbook has needed.
MAX_SUBPLAN_DEPTH = 3


@dataclass
class PlanRunInput:
    incident_type: str
    incident: dict = field(default_factory=dict)
    #: Seconds an approval_required phase waits for a human before degrading
    #: to needs_escalation. Passed in by the trigger endpoint from settings —
    #: workflows cannot read config without breaking determinism.
    approval_timeout_seconds: int = 86400
    #: Subplan nesting depth of this run; 0 for a run a user started.
    depth: int = 0


def _escalation_output(phase: dict, reason: str) -> dict:
    """The same shape the in-process engine records for an unapproved phase."""
    return {
        "skill_id": phase["skill_name"],
        "phase_id": phase["id"],
        "status": "needs_escalation",
        "findings": {},
        "evidence_summary": reason,
        "actions_taken": [],
        "open_questions": [],
        "risk_flags": [],
        "confidence": 0.0,
        "contract_missing": [],
    }


@workflow.defn(name="PulsePlanWorkflow")
class PlanWorkflow:
    def __init__(self) -> None:
        # phase_id -> approved? A signal may arrive before its phase is
        # reached; keying by phase makes early approval just work.
        self._approvals: dict[str, bool] = {}
        self._awaiting: str = ""
        self._current_phase: str = ""
        self._outputs: dict[str, dict] = {}

    @workflow.signal
    def approve_phase(self, phase_id: str, approved: bool = True) -> None:
        self._approvals[phase_id] = approved

    @workflow.query
    def progress(self) -> dict:
        """Live progress for the UI: what ran, what it produced, what waits."""
        return {
            "current_phase": self._current_phase,
            "awaiting_approval": self._awaiting,
            "phases": {
                pid: {"status": out.get("status"), "confidence": out.get("confidence", 0)}
                for pid, out in self._outputs.items()
            },
        }

    async def _gate_on_approval(self, phase: dict, timeout_seconds: int) -> bool:
        """True when a human approved the phase, False on denial or timeout."""
        self._awaiting = phase["id"]
        try:
            await workflow.wait_condition(
                lambda: phase["id"] in self._approvals,
                timeout=timedelta(seconds=timeout_seconds),
            )
        except TimeoutError:
            return False
        finally:
            self._awaiting = ""
        return bool(self._approvals.get(phase["id"]))

    async def _run_phase(self, plan: dict, phase: dict, params: PlanRunInput) -> dict:
        """Execute one phase: an activity, or a whole plan as a child workflow."""
        subplan = phase.get("subplan")
        if subplan:
            # A child workflow, not an activity: the sub-plan keeps its own
            # phase graph, approval gates and history, linked to this run as
            # parent — composition without flattening.
            if params.depth + 1 >= MAX_SUBPLAN_DEPTH:
                return {
                    **_escalation_output(phase, f"Sub-plan nesting deeper than {MAX_SUBPLAN_DEPTH} refused"),
                    "status": "failed",
                }
            child = await workflow.execute_child_workflow(
                PlanWorkflow.run,
                PlanRunInput(
                    incident_type=subplan,
                    incident=params.incident,
                    approval_timeout_seconds=params.approval_timeout_seconds,
                    depth=params.depth + 1,
                ),
                id=f"{workflow.info().workflow_id}--{phase['id']}",
            )
            status = child.get("status", "failed")
            return {
                "skill_id": f"plan:{subplan}",
                "phase_id": phase["id"],
                "status": status if status in ("complete", "partial") else "failed",
                "findings": {"subplan": subplan, "subplan_status": status},
                "evidence_summary": (
                    f"Sub-plan '{subplan}' finished {status} in {child.get('duration_ms', 0)}ms "
                    f"({len(child.get('phase_outputs', {}))} phases)"
                ),
                "actions_taken": [],
                "open_questions": [],
                "risk_flags": [],
                "confidence": 1.0 if status == "complete" else 0.5,
                "contract_missing": [],
            }

        # The branch decision is made HERE, from outputs already in workflow
        # history, and only its result crosses to the activity — replay can
        # never re-decide a branch differently from what ran.
        skill_override = resolve_branch(phase, self._outputs)
        timeout = timedelta(seconds=int(phase.get("timeout_seconds", 120) * _PHASE_TIMEOUT_MARGIN))
        return await workflow.execute_activity(
            run_plan_phase,
            args=[plan, phase["id"], params.incident, self._outputs, skill_override],
            start_to_close_timeout=timeout,
            # The engine already retries a failed contract internally;
            # activity-level retry only covers a dead worker, and an
            # agent phase is not safe to blindly re-run more than once.
            retry_policy=RetryPolicy(maximum_attempts=2, non_retryable_error_types=["ValueError"]),
        )

    @workflow.run
    async def run(self, params: PlanRunInput) -> dict:
        started = workflow.now()

        plan = await workflow.execute_activity(
            load_plan,
            params.incident_type,
            schedule_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=3, non_retryable_error_types=["ValueError"]),
        )

        blockers = unsupported_features(plan)
        if blockers:
            # Refuse rather than run the plan half-faithfully — the in-process
            # engine still executes branching/parallel plans.
            raise ApplicationError(
                f"Plan '{params.incident_type}' uses features the durable interpreter "
                f"does not support yet: {', '.join(blockers)}",
                non_retryable=True,
            )

        phases: list[dict] = plan["phases"]
        # Waves run concurrently and branches re-target skills since the
        # "wave-parallel-branching-subplans" patch; histories recorded before
        # it replay the original one-phase-at-a-time loop unchanged.
        wave_parallel = workflow.patched("wave-parallel-branching-subplans")
        while len(self._outputs) < len(phases):
            ready = ready_phases(phases, set(self._outputs))
            if not ready:
                # Unsatisfiable dependencies: record what ran and stop.
                break

            runnable: list[dict] = []
            for phase in ready:
                self._current_phase = phase["id"]

                if phase.get("approval_required"):
                    # Gates are walked in declared order; approvals are keyed
                    # by phase and may arrive before their gate is reached, so
                    # a human approving a whole wave at once blocks nothing.
                    approved = await self._gate_on_approval(phase, params.approval_timeout_seconds)
                    if not approved:
                        reason = (
                            f"Phase '{phase['id']}' was not approved within the window"
                            if phase["id"] not in self._approvals
                            else f"Phase '{phase['id']}' was denied by a human"
                        )
                        self._outputs[phase["id"]] = _escalation_output(phase, reason)
                        continue

                if wave_parallel:
                    runnable.append(phase)
                else:
                    self._outputs[phase["id"]] = await self._run_phase(plan, phase, params)

            if runnable:
                # The whole ready wave at once, mirroring the in-process
                # engine's gather. Deterministic: the workflow event loop
                # schedules these, and each result lands keyed by phase id.
                results = await asyncio.gather(*(self._run_phase(plan, phase, params) for phase in runnable))
                for phase, output in zip(runnable, results, strict=True):
                    self._outputs[phase["id"]] = output

        self._current_phase = ""
        status = derive_status(phases, self._outputs)
        duration_ms = int((workflow.now() - started).total_seconds() * 1000)

        await workflow.execute_activity(
            record_plan_execution,
            args=[plan, self._outputs, status, duration_ms, params.incident],
            schedule_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

        return {
            "status": status,
            "plan_id": plan["id"],
            "duration_ms": duration_ms,
            "phase_outputs": self._outputs,
        }
