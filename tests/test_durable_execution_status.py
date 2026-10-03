"""Execution completion is independent of the observed recovery verdict."""

from unittest.mock import patch

import pytest

from sre_agent.monitor.actions import get_action_detail
from sre_agent.repositories.monitor_repo import get_monitor_repo
from sre_agent.temporal import incident_activities, incident_workflow


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verdict,applied,status",
    [
        ("verified", True, "completed"),
        ("verified_then_recurred", True, "completed"),
        ("unverifiable", True, "completed"),
        ("rolled_back", True, "completed"),
        ("cancelled", True, "completed"),
        ("unverifiable", False, "failed"),
        ("failed", None, "dispatched"),
        ("verified", None, "dispatched"),
        ("verified", "true", "dispatched"),
    ],
)
async def test_persisted_execution_and_recovery_are_independent(verdict, applied, status):
    repo = get_monitor_repo()
    action_id = f"execution-status-{verdict}-{applied}"
    repo.save_action(
        {"id": action_id, "findingId": "synthetic", "status": "dispatched", "tool": "delete_pod"},
        "crashloop",
        "[]",
        0,
        "{}",
        1,
    )
    await incident_activities.record_outcome(action_id, verdict, "synthetic post-fix observation", applied)
    row = repo.get_action_by_id(action_id)
    assert row["status"] == status
    assert row["verification_status"] == verdict
    assert row["verification_timestamp"] > row["timestamp"]
    # This is the actual UI detail contract, not a parallel serializer: a
    # verified badge requires both a completed action and a verified verdict.
    detail = get_action_detail(action_id)
    assert detail["status"] == status
    assert detail["verificationStatus"] == verdict
    assert (detail["status"] == "completed" and detail["verificationStatus"] == "verified") is (
        applied is True and verdict == "verified"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("patched,applied", [(True, True), (True, None), (False, True)])
async def test_workflow_outcome_payload_requires_explicit_apply_evidence(monkeypatch, patched, applied):
    records = []

    async def execute(function, *pos, **kw):
        name = function.__name__
        if name == "capture_snapshot":
            return None
        if name == "apply_fix":
            result = {"tool": "delete_pod", "snapshot": None}
            if applied is not None:
                result["applied"] = applied
            return result
        if name == "verify_fix":
            return {"healthy": True, "evidence": "synthetic readiness"}
        if name == "check_recurrence":
            return {"recurred": False, "evidence": "synthetic current readiness"}
        if name == "record_outcome":
            records.append(kw["args"])
        return None

    async def sleep(_):
        return None

    monkeypatch.setattr(incident_workflow.workflow, "execute_activity", execute)
    monkeypatch.setattr(incident_workflow.workflow, "sleep", sleep)
    monkeypatch.setattr(incident_workflow.workflow, "patched", lambda name: patched)
    result = await incident_workflow.IncidentWorkflow().run(
        incident_workflow.IncidentInput("synthetic", action_id="action", require_approval=False)
    )
    assert result["verdict"] == "verified"
    assert records[0][:3] == ["action", "verified", "synthetic readiness; recheck: synthetic current readiness"]
    assert records[0][3:] == ([applied] if patched else [])


@pytest.mark.asyncio
async def test_apply_activity_reports_explicit_execution_evidence():
    from sre_agent.monitor.fix_planner import FixExecution

    execution = FixExecution(("delete_pod", "before", "after"), None, [])
    with (
        patch("sre_agent.monitor.fix_planner.execute_fix_with_snapshot", return_value=execution),
        patch("sre_agent.temporal.incident_activities.activity.heartbeat"),
    ):
        result = await incident_activities.apply_fix({"strategy": "restart_controller"})
    assert result["applied"] is True


@pytest.mark.asyncio
async def test_durable_verified_outcome_enters_actual_recovery_kpi():
    import time

    repo = get_monitor_repo()
    now = int(time.time() * 1000)
    repo.db.execute(
        "INSERT INTO findings (id, cluster, message, timestamp) VALUES (?, ?, ?, ?)",
        ("durable-kpi-finding", "synthetic", "synthetic", now - 10000),
    )
    repo.db.commit()
    repo.save_action(
        {"id": "durable-kpi-action", "findingId": "durable-kpi-finding", "status": "dispatched"},
        "crashloop",
        "[]",
        0,
        "{}",
        now - 5000,
    )
    before = repo.fetch_fix_rate(7)
    await incident_activities.record_outcome("durable-kpi-action", "verified", "synthetic observed readiness", True)
    after = repo.fetch_fix_rate(7)
    assert after["total"] == before["total"] + 1
    assert after["good"] == before["good"] + 1
    assert repo.fetch_time_to_resolution(7)["sample_count"] >= 1
