"""The flywheel on Temporal Schedules, and the inline fallback standing down.

The daily/weekly maintenance cadence used to live in in-memory timestamps
reset on every pod boot. The properties pinned here: the schedule registration
is idempotent, its failure leaves the inline path running (losing the cadence
entirely would be strictly worse than the old behaviour), and once schedules
own the cadence the inline path must not run the same work a second time.

The time-skipping test server does not implement CreateSchedule, so the
registration is tested against a fake client at the seam; the workflow itself
runs on the real test server.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest


class TestEnsureFlywheelSchedules:
    def setup_method(self):
        import sre_agent.temporal.worker as worker

        worker._FLYWHEEL_SCHEDULED = False

    def test_creates_both_cadences_and_reports_scheduled(self):
        from sre_agent.temporal import worker

        client = AsyncMock()
        asyncio.run(worker._ensure_flywheel_schedules(client))

        ids = [call.args[0] for call in client.create_schedule.call_args_list]
        assert ids == ["pulse-flywheel-daily", "pulse-flywheel-weekly"]
        assert worker.flywheel_scheduled() is True

    def test_already_existing_schedules_count_as_success(self):
        """Two agent pods racing, or a schedule from the previous deploy —
        the schedule existing is the entire requirement."""
        from temporalio.client import ScheduleAlreadyRunningError

        from sre_agent.temporal import worker

        client = AsyncMock()
        client.create_schedule.side_effect = ScheduleAlreadyRunningError()
        asyncio.run(worker._ensure_flywheel_schedules(client))
        assert worker.flywheel_scheduled() is True

    def test_failure_leaves_the_inline_fallback_in_charge(self):
        """A server too old for Schedules must not silence the flywheel."""
        from sre_agent.temporal import worker

        client = AsyncMock()
        client.create_schedule.side_effect = RuntimeError("CreateSchedule is unimplemented")
        asyncio.run(worker._ensure_flywheel_schedules(client))
        assert worker.flywheel_scheduled() is False


class TestInlineFallbackStandsDown:
    def test_inline_flywheel_skips_when_schedules_own_the_cadence(self):
        """The work must not run on two cadences at once."""
        from sre_agent.monitor import flywheel

        class FakeMonitor:
            _last_daily_run = 0.0
            _last_weekly_run = 0.0

        with (
            patch("sre_agent.temporal.worker.flywheel_scheduled", return_value=True),
            patch.object(flywheel, "run_daily_tasks", new=AsyncMock()) as daily,
        ):
            asyncio.run(flywheel.run_flywheel(FakeMonitor()))
        assert not daily.called
        assert FakeMonitor._last_daily_run == 0.0, "the inline timestamps must not advance either"

    def test_inline_flywheel_runs_when_schedules_are_absent(self):
        from sre_agent.monitor import flywheel

        class FakeMonitor:
            _last_daily_run = 0.0
            _last_weekly_run = 0.0

        with (
            patch("sre_agent.temporal.worker.flywheel_scheduled", return_value=False),
            patch.object(flywheel, "run_daily_tasks", new=AsyncMock(return_value={})) as daily,
            patch.object(flywheel, "run_weekly_tasks", new=AsyncMock(return_value={})) as weekly,
        ):
            asyncio.run(flywheel.run_flywheel(FakeMonitor()))
        assert daily.called and weekly.called


class TestFlywheelActivity:
    def test_unknown_cadence_is_refused(self):
        from sre_agent.temporal.flywheel_activities import run_flywheel_cadence

        with pytest.raises(ValueError, match="cadence"):
            asyncio.run(run_flywheel_cadence("hourly"))

    def test_daily_and_weekly_route_to_their_tasks(self):
        from sre_agent.monitor import flywheel
        from sre_agent.temporal.flywheel_activities import run_flywheel_cadence

        with (
            patch.object(flywheel, "run_daily_tasks", new=AsyncMock(return_value={"trajectory": "ok"})),
            patch.object(flywheel, "run_weekly_tasks", new=AsyncMock(return_value={"embedding_cache": "invalidated"})),
        ):
            daily = asyncio.run(run_flywheel_cadence("daily"))
            weekly = asyncio.run(run_flywheel_cadence("weekly"))
        assert daily == {"cadence": "daily", "trajectory": "ok"}
        assert weekly == {"cadence": "weekly", "embedding_cache": "invalidated"}


class TestFlywheelWorkflow:
    def test_runs_the_cadence_as_one_activity(self):
        from temporalio import activity
        from temporalio.testing import WorkflowEnvironment
        from temporalio.worker import Worker

        from sre_agent.temporal.flywheel_workflow import FlywheelWorkflow

        seen: list[str] = []

        @activity.defn(name="pulse.flywheel.run")
        async def stub(cadence: str) -> dict:
            seen.append(cadence)
            return {"cadence": cadence}

        async def go():
            async with await WorkflowEnvironment.start_time_skipping() as env:
                async with Worker(env.client, task_queue="tq-fly", workflows=[FlywheelWorkflow], activities=[stub]):
                    return await env.client.execute_workflow(
                        FlywheelWorkflow.run, "daily", id="wf-fly", task_queue="tq-fly"
                    )

        result = asyncio.run(go())
        assert result == {"cadence": "daily"}
        assert seen == ["daily"]
