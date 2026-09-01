"""The flywheel as a scheduled workflow — Temporal Schedules over ad-hoc timers.

The daily/weekly maintenance loop (trajectory expiry, learned channel weights,
embedding cache) was driven by in-memory timestamps on the monitor, reset to
zero on every pod boot and checked only when the scan loop happened to run.
That has two quiet failure modes: a frequently-restarting pod runs the "daily"
work at whatever cadence its restarts dictate, and a paused monitor never runs
it at all. Neither leaves any record.

A Temporal Schedule owns the cadence instead: it fires whether or not the
agent pod was up at the appointed time (a missed window runs on reconnect,
buffered — not silently skipped), every run is in the visibility store next to
the incident and plan runs, and "when did weights last recompute" becomes a
question the Temporal UI answers.

The workflow is a thin shell on purpose — the cadence is the schedule's job
and the work is one activity, so the whole thing is a single history entry
per firing.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from .flywheel_activities import run_flywheel_cadence


@workflow.defn(name="PulseFlywheelWorkflow")
class FlywheelWorkflow:
    @workflow.run
    async def run(self, cadence: str) -> dict:
        return await workflow.execute_activity(
            run_flywheel_cadence,
            cadence,
            # Weight recomputation reads a week of history; give it room.
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=30),
                maximum_attempts=3,
            ),
        )
