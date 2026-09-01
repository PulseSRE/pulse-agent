"""Durable plan execution: the interpreter workflow and its seams.

The workflow tests run against Temporal's time-skipping test environment with
stub activities registered under the real activity names — real workflow code,
no LLM, no server install (the test server binary is downloaded and cached by
temporalio on first use).
"""

import asyncio
from unittest.mock import patch

import pytest

from sre_agent.temporal.sequencing import derive_status, ready_phases, unsupported_features

# ---------------------------------------------------------------------------
# Pure sequencing
# ---------------------------------------------------------------------------


class TestSequencing:
    def test_dependency_order(self):
        phases = [
            {"id": "verify", "depends_on": ["fix"]},
            {"id": "triage", "depends_on": []},
            {"id": "fix", "depends_on": ["triage"]},
        ]
        assert [p["id"] for p in ready_phases(phases, set())] == ["triage"]
        assert [p["id"] for p in ready_phases(phases, {"triage"})] == ["fix"]
        assert [p["id"] for p in ready_phases(phases, {"triage", "fix"})] == ["verify"]

    def test_settled_means_ran_not_succeeded(self):
        """An optional phase that failed must not block its dependents."""
        phases = [{"id": "a", "depends_on": []}, {"id": "b", "depends_on": ["a"]}]
        assert [p["id"] for p in ready_phases(phases, {"a"})] == ["b"]

    def test_status_mirrors_the_engine(self):
        phases = [{"id": "a", "required": True}, {"id": "b", "required": False}]
        assert derive_status(phases, {"a": {"status": "complete"}, "b": {"status": "complete"}}) == "complete"
        assert derive_status(phases, {"a": {"status": "failed"}, "b": {"status": "complete"}}) == "partial"
        assert derive_status(phases, {"a": {"status": "complete"}, "b": {"status": "failed"}}) == "partial"
        assert derive_status(phases, {"a": {"status": "complete"}}) == "partial"

    def test_unsupported_features_are_named(self):
        plan = {"phases": [{"id": "x", "branch_on": "severity"}, {"id": "y", "parallel_with": ["x"]}]}
        assert unsupported_features(plan) == ["x.branch_on", "y.parallel_with"]
        assert unsupported_features({"phases": [{"id": "z"}]}) == []


# ---------------------------------------------------------------------------
# The workflow itself, on the time-skipping test server
# ---------------------------------------------------------------------------


def _plan(phases):
    return {"id": "p1", "name": "Test Plan", "incident_type": "test", "max_total_duration": 600, "phases": phases}


def _phase(pid, *, deps=(), approval=False, required=True):
    return {
        "id": pid,
        "skill_name": "sre",
        "required": required,
        "depends_on": list(deps),
        "timeout_seconds": 30,
        "produces": [],
        "approval_required": approval,
        "branch_on": None,
        "branches": {},
        "parallel_with": None,
        "retry_limit": 1,
    }


def _ok_output(pid):
    return {
        "skill_id": "sre",
        "phase_id": pid,
        "status": "complete",
        "findings": {},
        "evidence_summary": "done",
        "actions_taken": [],
        "open_questions": [],
        "risk_flags": [],
        "confidence": 0.9,
        "contract_missing": [],
    }


async def _run_workflow(plan_dict, params, send_approval=None):
    """Execute PlanWorkflow with stub activities; optionally signal mid-run."""
    from temporalio import activity
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from sre_agent.temporal.plan_workflow import PlanWorkflow

    ran: list[str] = []
    recorded: dict = {}

    @activity.defn(name="pulse.load_plan")
    async def stub_load(incident_type: str) -> dict:
        return plan_dict

    @activity.defn(name="pulse.run_plan_phase")
    async def stub_phase(plan: dict, phase_id: str, incident: dict, prior: dict) -> dict:
        ran.append(phase_id)
        return _ok_output(phase_id)

    @activity.defn(name="pulse.record_plan_execution")
    async def stub_record(plan: dict, outputs: dict, status: str, duration_ms: int, incident: dict) -> None:
        recorded.update({"status": status, "outputs": dict(outputs)})

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-plans",
            workflows=[PlanWorkflow],
            activities=[stub_load, stub_phase, stub_record],
        ):
            handle = await env.client.start_workflow(PlanWorkflow.run, params, id="wf-test", task_queue="test-plans")
            if send_approval is not None:
                phase_id, approved = send_approval
                await handle.signal("approve_phase", args=[phase_id, approved])
            result = await handle.result()
    return result, ran, recorded


class TestPlanWorkflow:
    def test_runs_phases_in_dependency_order_and_records(self):
        from sre_agent.temporal.plan_workflow import PlanRunInput

        plan = _plan([_phase("verify", deps=["fix"]), _phase("triage"), _phase("fix", deps=["triage"])])
        result, ran, recorded = asyncio.run(
            _run_workflow(plan, PlanRunInput(incident_type="test", incident={"id": "f1"}))
        )
        assert ran == ["triage", "fix", "verify"]
        assert result["status"] == "complete"
        assert recorded["status"] == "complete"
        assert set(recorded["outputs"]) == {"triage", "fix", "verify"}

    def test_approved_phase_executes(self):
        from sre_agent.temporal.plan_workflow import PlanRunInput

        plan = _plan([_phase("remediate", approval=True)])
        result, ran, _ = asyncio.run(
            _run_workflow(
                plan,
                PlanRunInput(incident_type="test", approval_timeout_seconds=3600),
                send_approval=("remediate", True),
            )
        )
        assert ran == ["remediate"]
        assert result["status"] == "complete"

    def test_denied_phase_escalates_without_running(self):
        from sre_agent.temporal.plan_workflow import PlanRunInput

        plan = _plan([_phase("remediate", approval=True, required=False)])
        result, ran, _ = asyncio.run(
            _run_workflow(
                plan,
                PlanRunInput(incident_type="test", approval_timeout_seconds=3600),
                send_approval=("remediate", False),
            )
        )
        assert ran == []
        assert result["phase_outputs"]["remediate"]["status"] == "needs_escalation"
        assert "denied" in result["phase_outputs"]["remediate"]["evidence_summary"]

    def test_unapproved_phase_times_out_to_escalation(self):
        """Time-skipping makes the 24h wait instant; the outcome degrades to
        exactly what the in-process engine records immediately."""
        from sre_agent.temporal.plan_workflow import PlanRunInput

        plan = _plan([_phase("remediate", approval=True, required=False)])
        result, ran, _ = asyncio.run(
            _run_workflow(plan, PlanRunInput(incident_type="test", approval_timeout_seconds=86400))
        )
        assert ran == []
        assert result["phase_outputs"]["remediate"]["status"] == "needs_escalation"
        assert "not approved within" in result["phase_outputs"]["remediate"]["evidence_summary"]


# ---------------------------------------------------------------------------
# REST seams
# ---------------------------------------------------------------------------


class TestRunEndpoints:
    @pytest.fixture
    def app_client(self):
        from fastapi.testclient import TestClient

        from sre_agent.api.app import app

        with TestClient(app, raise_server_exceptions=False) as c:
            yield c

    def test_run_without_temporal_is_503_with_the_reason(self, monkeypatch):
        """The endpoint must explain what to configure, not 500 or vanish."""
        import asyncio as aio

        from sre_agent.api import monitor_rest
        from sre_agent.temporal.client import TemporalDisabledError

        async def fake_start(incident_type, incident):
            raise TemporalDisabledError()

        with patch("sre_agent.temporal.client.start_plan_run", side_effect=fake_start):
            from sre_agent.plan_templates import get_template

            template = get_template("crashloop")
            assert template is not None, "crashloop template must exist for this test"

            from fastapi import HTTPException
            from starlette.requests import Request

            scope = {"type": "http", "headers": [(b"content-length", b"0")], "method": "POST", "path": "/"}
            req = Request(scope)
            with pytest.raises(HTTPException) as exc:
                aio.run(monitor_rest.run_plan_template("crashloop", req, _auth=None))
            assert exc.value.status_code == 503
            assert "PULSE_AGENT_TEMPORAL_HOST" in exc.value.detail

    def test_approve_requires_phase_id(self):
        import asyncio as aio

        from fastapi import HTTPException

        from sre_agent.api import monitor_rest

        class FakeReq:
            async def json(self):
                return {}

        with pytest.raises(HTTPException) as exc:
            aio.run(monitor_rest.approve_workflow_phase("wf-1", FakeReq(), _auth=None))
        assert exc.value.status_code == 400
