"""The fix lifecycle as one durable workflow — Pulse's showcase Temporal use.

Today this lifecycle is spread across five mechanisms, each durable only as
far as the pod lives:

===================  ==========================================  ======================
Stage                How Pulse does it now                       What it becomes here
===================  ==========================================  ======================
approval wait        a future with a DB-polled timeout; 281 of   a signal the workflow
                     368 actions on dev05 expired unseen         waits days for
snapshot             captured, stored, and *usually* used        the saga's compensation
verification         probed on later monitor scan cycles         retry policy backoff
grace window         "3 scans", lost if the pod restarts         the retry itself
recurrence window    a 1800s DB re-read on a future scan         a durable timer
===================  ==========================================  ======================

The interesting property is not that each piece works — they already do — but
that the *sequence* becomes atomic. A pod restart between "applied the fix" and
"verified it" today loses the verification entirely, leaving a mutation nobody
confirmed. Here the workflow resumes at the next step.

Temporal features this exercises, all in one coherent business process:
durable execution, signals (approval), queries (live status), durable timers
(the recurrence window), saga compensation (snapshot restore on failed
verification), activity heartbeats (long fixes), and retry-with-backoff as a
grace window.

Determinism: see plan_workflow's docstring — the same patch discipline applies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from .incident_activities import (
        apply_fix,
        capture_snapshot,
        check_recurrence,
        record_outcome,
        restore_snapshot,
        verify_fix,
    )


@dataclass
class IncidentInput:
    finding_id: str
    resource: dict = field(default_factory=dict)
    fix_plan: dict = field(default_factory=dict)
    #: Skip the human gate. Mirrors trust level 3+ auto-fix, where the monitor
    #: acts unattended — the workflow shape is identical either way.
    require_approval: bool = True
    approval_timeout_seconds: int = 86400
    #: How long to wait before asking whether the fix held. The monitor's
    #: PULSE_AGENT_RECURRENCE_WINDOW, as a durable timer.
    recurrence_window_seconds: int = 1800


@workflow.defn(name="PulseIncidentWorkflow")
class IncidentWorkflow:
    def __init__(self) -> None:
        self._approved: bool | None = None
        self._stage: str = "starting"
        self._compensated: bool = False

    @workflow.signal
    def approve(self, approved: bool = True) -> None:
        self._approved = approved

    @workflow.query
    def status(self) -> dict:
        """Live status without touching the database — the workflow is the truth."""
        return {"stage": self._stage, "approved": self._approved, "compensated": self._compensated}

    @workflow.run
    async def run(self, params: IncidentInput) -> dict:
        # ── 1. Human gate ────────────────────────────────────────────────────
        if params.require_approval:
            self._stage = "awaiting_approval"
            try:
                await workflow.wait_condition(
                    lambda: self._approved is not None,
                    timeout=timedelta(seconds=params.approval_timeout_seconds),
                )
            except TimeoutError:
                self._stage = "expired"
                return {"verdict": "expired", "evidence": "no human responded within the window"}
            if not self._approved:
                self._stage = "denied"
                return {"verdict": "denied", "evidence": "a human declined the fix"}

        # ── 2. Snapshot before mutating — the compensation data ──────────────
        self._stage = "snapshotting"
        snapshot = await workflow.execute_activity(
            capture_snapshot,
            params.resource,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

        # ── 3. Apply ─────────────────────────────────────────────────────────
        self._stage = "applying"
        try:
            applied = await workflow.execute_activity(
                apply_fix,
                params.fix_plan,
                start_to_close_timeout=timedelta(minutes=5),
                heartbeat_timeout=timedelta(seconds=30),
                # A fix that mutates the cluster is not safe to retry blindly;
                # one attempt, and a failure is handled below.
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except ActivityError as exc:
            # An apply that fails must still produce a verdict. Letting the
            # workflow die here leaves the dispatched action with no outcome
            # forever — strictly worse than the inline path, which records a
            # failure. Found by running this against the live server.
            self._stage = "apply_failed"
            # The failure may have landed after a partial mutation, so undo
            # defensively; restore is a no-op when there is nothing to undo.
            restored = await workflow.execute_activity(
                restore_snapshot,
                snapshot,
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            self._compensated = True
            reason = f"fix could not be applied: {exc.cause or exc}"
            await workflow.execute_activity(
                record_outcome,
                args=[params.finding_id, "failed", f"{reason}; {restored}"],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            return {"verdict": "failed", "evidence": reason, "compensated": True}

        # ── 4. Verify, with backoff as the rollout grace window ───────────────
        self._stage = "verifying"
        try:
            verified = await workflow.execute_activity(
                verify_fix,
                params.resource,
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=10),
                    backoff_coefficient=2.0,
                    maximum_interval=timedelta(seconds=60),
                    maximum_attempts=6,
                ),
            )
        except ActivityError:
            # ── Saga compensation: the fix did not hold, so undo it ──────────
            self._stage = "compensating"
            restored = await workflow.execute_activity(
                restore_snapshot,
                snapshot,
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            self._compensated = True
            self._stage = "rolled_back"
            await workflow.execute_activity(
                record_outcome,
                args=[params.finding_id, "rolled_back", f"verification failed; {restored}"],
                start_to_close_timeout=timedelta(seconds=30),
            )
            return {
                "verdict": "rolled_back",
                "evidence": restored,
                "applied": applied,
                "compensated": True,
            }

        # ── 5. Durable timer: does the verdict still hold later? ─────────────
        # A "verified" verdict has a time horizon. The monitor re-reads the
        # database on a future scan to find out, which a restart can miss.
        self._stage = "settling"
        await workflow.sleep(timedelta(seconds=params.recurrence_window_seconds))

        self._stage = "rechecking"
        recheck = await workflow.execute_activity(
            check_recurrence,
            params.resource,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

        verdict = "verified_then_recurred" if recheck.get("recurred") else "verified"
        evidence = f"{verified.get('evidence', '')}; recheck: {recheck.get('evidence', '')}"
        self._stage = verdict

        await workflow.execute_activity(
            record_outcome,
            args=[params.finding_id, verdict, evidence],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        return {"verdict": verdict, "evidence": evidence, "applied": applied, "compensated": False}
