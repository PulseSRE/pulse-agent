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

Determinism rules: no IO here, no settings reads, no clocks but Temporal's.
All decisions are the pure functions in ``sequencing``; everything else is an
activity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from .activities import load_plan, record_plan_execution, run_plan_phase
    from .sequencing import derive_status, ready_phases, unsupported_features

#: Margin over a phase's own timeout: _execute_phase may retry once with the
#: contract gap named, so the activity gets room for both attempts plus the
#: judge. The engine's own per-attempt timeout still applies inside.
_PHASE_TIMEOUT_MARGIN = 2.5


@dataclass
class PlanRunInput:
    incident_type: str
    incident: dict = field(default_factory=dict)
    #: Seconds an approval_required phase waits for a human before degrading
    #: to needs_escalation. Passed in by the trigger endpoint from settings —
    #: workflows cannot read config without breaking determinism.
    approval_timeout_seconds: int = 86400


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
        while len(self._outputs) < len(phases):
            ready = ready_phases(phases, set(self._outputs))
            if not ready:
                # Unsatisfiable dependencies: record what ran and stop.
                break

            for phase in ready:
                self._current_phase = phase["id"]

                if phase.get("approval_required"):
                    approved = await self._gate_on_approval(phase, params.approval_timeout_seconds)
                    if not approved:
                        reason = (
                            f"Phase '{phase['id']}' was not approved within the window"
                            if phase["id"] not in self._approvals
                            else f"Phase '{phase['id']}' was denied by a human"
                        )
                        self._outputs[phase["id"]] = _escalation_output(phase, reason)
                        continue

                timeout = timedelta(seconds=int(phase.get("timeout_seconds", 120) * _PHASE_TIMEOUT_MARGIN))
                output = await workflow.execute_activity(
                    run_plan_phase,
                    args=[plan, phase["id"], params.incident, self._outputs],
                    start_to_close_timeout=timeout,
                    # The engine already retries a failed contract internally;
                    # activity-level retry only covers a dead worker, and an
                    # agent phase is not safe to blindly re-run more than once.
                    retry_policy=RetryPolicy(maximum_attempts=2, non_retryable_error_types=["ValueError"]),
                )
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
