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


class TestVersioningSafety:
    """Guards for the one correctness gap that bites *after* rollout.

    A running workflow replays its history against current code, and approval
    waits are meant to last days — so the seams that make a logic change safe
    have to exist before anything depends on them, not after the first
    nondeterminism error in production.
    """

    def test_worker_stamps_a_build_id(self):
        """Which build produced a history must be answerable."""
        import inspect

        from sre_agent.temporal import worker

        src = inspect.getsource(worker.run_worker)
        assert "build_id=" in src, "worker must stamp a build id for worker versioning"
        assert "openshift-sre-agent" in src, "build id should derive from the agent version"

    def test_workflow_documents_the_patch_discipline(self):
        """The rule lives where the change happens, not only in a doc."""
        from sre_agent.temporal import plan_workflow

        doc = plan_workflow.__doc__ or ""
        assert "workflow.patched" in doc
        assert "build_id" in doc

    def test_patched_is_available_from_the_sdk(self):
        """The mechanism the discipline depends on actually exists here."""
        from temporalio import workflow

        assert hasattr(workflow, "patched")
        assert hasattr(workflow, "deprecate_patch")


# ---------------------------------------------------------------------------
# The incident lifecycle workflow — Pulse's showcase Temporal use
# ---------------------------------------------------------------------------


async def _run_incident(params, *, verify_fails=False, send_approval=None):
    """Run IncidentWorkflow with stub activities on the time-skipping server."""
    from temporalio import activity
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from sre_agent.temporal.incident_workflow import IncidentWorkflow

    calls: list[str] = []

    @activity.defn(name="pulse.incident.snapshot")
    async def stub_snapshot(resource: dict) -> dict:
        calls.append("snapshot")
        return {"kind": "Pod", "name": resource.get("name", "x")}

    @activity.defn(name="pulse.incident.apply_fix")
    async def stub_apply(plan: dict) -> dict:
        calls.append("apply")
        return {"tool": "delete_pod", "before": "crashloop", "after": "recreated"}

    @activity.defn(name="pulse.incident.verify")
    async def stub_verify(resource: dict) -> dict:
        calls.append("verify")
        if verify_fails:
            raise RuntimeError("pod is CrashLoopBackOff, not Running yet")
        return {"healthy": True, "evidence": "pod Running"}

    @activity.defn(name="pulse.incident.compensate")
    async def stub_compensate(snapshot: dict | None) -> str:
        calls.append("compensate")
        return "restored from snapshot"

    @activity.defn(name="pulse.incident.check_recurrence")
    async def stub_recheck(resource: dict) -> dict:
        calls.append("recheck")
        return {"recurred": bool(resource.get("_recurs")), "evidence": "phase=Running"}

    @activity.defn(name="pulse.incident.record_outcome")
    async def stub_record(finding_id: str, verdict: str, evidence: str) -> None:
        calls.append(f"record:{verdict}")

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-incidents",
            workflows=[IncidentWorkflow],
            activities=[stub_snapshot, stub_apply, stub_verify, stub_compensate, stub_recheck, stub_record],
        ):
            handle = await env.client.start_workflow(
                IncidentWorkflow.run, params, id="wf-incident", task_queue="test-incidents"
            )
            if send_approval is not None:
                await handle.signal("approve", send_approval)
            result = await handle.result()
    return result, calls


class TestIncidentWorkflow:
    """The lifecycle guarantees the in-process monitor cannot make."""

    def test_happy_path_snapshots_applies_verifies_then_settles(self):
        from sre_agent.temporal.incident_workflow import IncidentInput

        result, calls = asyncio.run(
            _run_incident(
                IncidentInput(
                    finding_id="f1",
                    resource={"name": "api-1", "namespace": "dev"},
                    fix_plan={"strategy": "restart_controller"},
                    require_approval=False,
                    recurrence_window_seconds=1800,
                ),
            )
        )
        # The snapshot precedes the mutation — without that ordering there is
        # nothing to compensate with.
        assert calls == ["snapshot", "apply", "verify", "recheck", "record:verified"]
        assert result["verdict"] == "verified"
        assert result["compensated"] is False

    def test_failed_verification_rolls_the_fix_back(self):
        """The saga: a fix that does not hold is undone, not just reported."""
        from sre_agent.temporal.incident_workflow import IncidentInput

        result, calls = asyncio.run(
            _run_incident(
                IncidentInput(
                    finding_id="f2",
                    resource={"name": "api-2", "namespace": "dev"},
                    fix_plan={"strategy": "restart_controller"},
                    require_approval=False,
                ),
                verify_fails=True,
            )
        )
        assert "compensate" in calls, "a failed verification must restore the snapshot"
        assert calls[-1] == "record:rolled_back"
        assert result["verdict"] == "rolled_back"
        assert result["compensated"] is True

    def test_recurrence_after_the_durable_timer_downgrades_the_verdict(self):
        """A 'verified' verdict has a time horizon; time-skipping makes the
        30-minute settling window instant."""
        from sre_agent.temporal.incident_workflow import IncidentInput

        result, calls = asyncio.run(
            _run_incident(
                IncidentInput(
                    finding_id="f3",
                    resource={"name": "api-3", "namespace": "dev", "_recurs": True},
                    fix_plan={"strategy": "restart_controller"},
                    require_approval=False,
                    recurrence_window_seconds=1800,
                ),
            )
        )
        assert result["verdict"] == "verified_then_recurred"
        assert "record:verified_then_recurred" in calls

    def test_approval_gate_waits_and_proceeds_on_yes(self):
        from sre_agent.temporal.incident_workflow import IncidentInput

        result, calls = asyncio.run(
            _run_incident(
                IncidentInput(
                    finding_id="f4",
                    resource={"name": "api-4", "namespace": "prod"},
                    fix_plan={"strategy": "restart_controller"},
                    require_approval=True,
                    approval_timeout_seconds=3600,
                ),
                send_approval=True,
            )
        )
        assert calls[0] == "snapshot"
        assert result["verdict"] == "verified"

    def test_denied_fix_never_touches_the_cluster(self):
        from sre_agent.temporal.incident_workflow import IncidentInput

        result, calls = asyncio.run(
            _run_incident(
                IncidentInput(
                    finding_id="f5",
                    resource={"name": "api-5", "namespace": "prod"},
                    fix_plan={"strategy": "restart_controller"},
                    require_approval=True,
                    approval_timeout_seconds=3600,
                ),
                send_approval=False,
            )
        )
        assert calls == [], "a denied fix must not snapshot or apply anything"
        assert result["verdict"] == "denied"

    def test_unanswered_approval_expires_without_acting(self):
        """dev05's real failure mode: 281 of 368 actions expired unseen. Here
        that is an explicit verdict after a durable 24h wait, not a lost future."""
        from sre_agent.temporal.incident_workflow import IncidentInput

        result, calls = asyncio.run(
            _run_incident(
                IncidentInput(
                    finding_id="f6",
                    resource={"name": "api-6", "namespace": "prod"},
                    fix_plan={"strategy": "restart_controller"},
                    require_approval=True,
                    approval_timeout_seconds=86400,
                ),
            )
        )
        assert calls == []
        assert result["verdict"] == "expired"
