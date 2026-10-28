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
    from .sequencing import (
        blocked_by,
        derive_status,
        derive_status_v2,
        ready_phases,
        resolve_branch,
        unsupported_features,
    )

#: Margin over a phase's own timeout: _execute_phase may retry once with the
#: contract gap named, so the activity gets room for both attempts plus the
#: judge. The engine's own per-attempt timeout still applies inside.
_PHASE_TIMEOUT_MARGIN = 2.5


#: How deep subplan nesting may go. A plan whose subplan (transitively)
#: names itself would otherwise spawn children forever; three levels is
#: more composition than any real runbook has needed.
MAX_SUBPLAN_DEPTH = 3

#: A phase activity heartbeats every 10s; three missed beats means the worker
#: running it is gone and the phase should be retried elsewhere.
_PHASE_HEARTBEAT_TIMEOUT = timedelta(seconds=30)


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


def _skipped_output(phase: dict, reason: str, kind: str) -> dict:
    """A phase that was not run, and why. ``kind`` is blocked or stopped."""
    return {**_escalation_output(phase, reason), "status": "skipped", "skip_kind": kind}


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
        # Phase activities in flight, so a cancelled run can wait for them to
        # finish (never sever a write) and record what they did.
        self._inflight: dict[str, workflow.ActivityHandle[dict]] = {}

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
        args = [plan, phase["id"], params.incident, self._outputs, skill_override]
        may_write = bool(phase.get("approval_required") and self._approvals.get(phase["id"]))
        if workflow.patched("explicit-plan-write-approval"):
            args.append(may_write)
        # The engine already retries a failed contract internally; activity
        # retry only covers a dead worker. A read-only phase can safely run
        # once more. An approved write phase cannot: the worker may have died
        # after the write, and a second attempt would apply it again on the
        # strength of an approval given for one run.
        attempts = 2
        if may_write and workflow.patched("no-retry-approved-writes"):
            attempts = 1
        retry = RetryPolicy(maximum_attempts=attempts, non_retryable_error_types=["ValueError"])
        if not workflow.patched("phase-heartbeat-and-cancel-record"):
            return await workflow.execute_activity(
                run_plan_phase,
                args=args,
                start_to_close_timeout=timeout,
                retry_policy=retry,
            )
        # The activity heartbeats, so a worker that died mid-phase is noticed
        # within the heartbeat timeout instead of the whole start-to-close.
        # Shielded: cancelling the run must not sever a phase mid-write; the
        # cancel handler waits for it and records what it did.
        handle = workflow.start_activity(
            run_plan_phase,
            args=args,
            start_to_close_timeout=timeout,
            heartbeat_timeout=_PHASE_HEARTBEAT_TIMEOUT,
            retry_policy=retry,
        )
        self._inflight[phase["id"]] = handle
        try:
            return await asyncio.shield(handle)
        finally:
            if handle.done():
                self._inflight.pop(phase["id"], None)

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
        if not workflow.patched("phase-heartbeat-and-cancel-record"):
            return await self._run_graph_v1(plan, phases, params, started)
        try:
            status = await self._run_graph_v2(plan, phases, params)
        except asyncio.CancelledError:
            status = await self._settle_after_cancel(phases)
        duration_ms = int((workflow.now() - started).total_seconds() * 1000)
        # Shielded so a cancelled run still leaves its plan_executions row;
        # a no-op shield when the run was not cancelled.
        await asyncio.shield(
            workflow.execute_activity(
                record_plan_execution,
                args=[plan, self._outputs, status, duration_ms, params.incident],
                schedule_to_close_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        )
        return {
            "status": status,
            "plan_id": plan["id"],
            "duration_ms": duration_ms,
            "phase_outputs": self._outputs,
        }

    async def _settle_after_cancel(self, phases: list[dict]) -> str:
        """Let in-flight phases finish, then call the run cancelled.

        Returning instead of re-raising completes the workflow with a result
        that says "cancelled" and carries what ran, the same choice the
        incident workflow makes. Re-raising ended the run CANCELED with no
        plan_executions row and no phases visible through the API.
        """
        for pid, handle in list(self._inflight.items()):
            try:
                self._outputs[pid] = await asyncio.shield(handle)
            except Exception as exc:
                phase = next(p for p in phases if p["id"] == pid)
                self._outputs[pid] = {
                    **_escalation_output(phase, f"Phase ended with cancellation: {exc}"),
                    "status": "failed",
                }
        self._inflight.clear()
        for phase in phases:
            if phase["id"] not in self._outputs:
                self._outputs[phase["id"]] = _skipped_output(phase, "Run cancelled before this phase", "stopped")
        self._current_phase = ""
        self._awaiting = ""
        return "cancelled"

    async def _gate_and_collect(self, plan: dict, phase: dict, params: PlanRunInput) -> dict | None:
        """Approval gate for one phase. Returns an output when it must not run."""
        if not phase.get("approval_required"):
            return None
        approved = await self._gate_on_approval(phase, params.approval_timeout_seconds)
        if approved:
            return None
        reason = (
            f"Phase '{phase['id']}' was not approved within the window"
            if phase["id"] not in self._approvals
            else f"Phase '{phase['id']}' was denied by a human"
        )
        return _escalation_output(phase, reason)

    async def _run_graph_v2(self, plan: dict, phases: list[dict], params: PlanRunInput) -> str:
        """The phase graph with the in-process engine's skip rules.

        A phase runs only when every dependency came back complete or partial;
        a failed required phase stops the plan; ``runs: always`` phases run at
        the end regardless. Waves still run concurrently and branches still
        re-target skills, as in v1.
        """
        always = [p for p in phases if p.get("runs") == "always"]
        main = [p for p in phases if p.get("runs") != "always"]
        stopped_by = ""

        while any(p["id"] not in self._outputs for p in main):
            ready = ready_phases(main, set(self._outputs))
            if not ready:
                break
            runnable: list[dict] = []
            for phase in ready:
                self._current_phase = phase["id"]
                blocked = blocked_by(phase, self._outputs)
                if blocked:
                    self._outputs[phase["id"]] = _skipped_output(
                        phase, f"Not run: {', '.join(blocked)} did not complete", "blocked"
                    )
                    continue
                refused = await self._gate_and_collect(plan, phase, params)
                if refused is not None:
                    self._outputs[phase["id"]] = refused
                    continue
                runnable.append(phase)

            if runnable:
                results = await asyncio.gather(*(self._run_phase(plan, phase, params) for phase in runnable))
                for phase, output in zip(runnable, results, strict=True):
                    self._outputs[phase["id"]] = output
                failed = [
                    p["id"]
                    for p in runnable
                    if p.get("required", True) and self._outputs[p["id"]].get("status") == "failed"
                ]
                if failed:
                    stopped_by = failed[0]
                    break

        for phase in main:
            if phase["id"] not in self._outputs:
                if stopped_by:
                    reason, kind = f"Plan stopped: required phase '{stopped_by}' failed", "stopped"
                else:
                    reason, kind = "Not run: its dependencies never ran", "blocked"
                self._outputs[phase["id"]] = _skipped_output(phase, reason, kind)

        for phase in always:
            self._current_phase = phase["id"]
            refused = await self._gate_and_collect(plan, phase, params)
            self._outputs[phase["id"]] = refused if refused is not None else await self._run_phase(plan, phase, params)

        self._current_phase = ""
        return derive_status_v2(phases, self._outputs)

    async def _run_graph_v1(self, plan: dict, phases: list[dict], params: PlanRunInput, started) -> dict:
        """The original interpreter loop, kept verbatim for histories recorded before v2."""
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

                if phase.get("approval_required") and workflow.patched("writes-need-settled-deps"):
                    # A gated phase is the one that writes. The in-process
                    # engine never reaches it unless every dependency came back
                    # complete or partial; here a failed diagnosis still
                    # unblocked remediation, and the human was asked to approve
                    # a fix for a cause nobody found.
                    unsettled = [
                        dep
                        for dep in phase.get("depends_on", [])
                        if self._outputs.get(dep, {}).get("status") not in ("complete", "partial")
                    ]
                    if workflow.patched("writes-need-met-contracts"):
                        # A diagnosis that came back partial because it did
                        # not produce its contract (no root cause) is not
                        # something to act on either.
                        unsettled += [
                            dep
                            for dep in phase.get("depends_on", [])
                            if dep not in unsettled
                            and self._outputs.get(dep, {}).get("status") == "partial"
                            and self._outputs.get(dep, {}).get("contract_missing")
                        ]
                    if unsettled:
                        self._outputs[phase["id"]] = {
                            **_escalation_output(
                                phase,
                                f"Phase '{phase['id']}' not run: {', '.join(unsettled)} did not complete, "
                                "so there is nothing verified to act on",
                            ),
                            "status": "failed",
                        }
                        continue

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
