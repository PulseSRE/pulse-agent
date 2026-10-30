"""One investigation pipeline: plan and freeform diagnoses are completed the same way.

Each test here describes a way the two paths had drifted apart; each fails on
the code before ``monitor/investigation_pipeline.py``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from sre_agent.monitor import investigation_pipeline as pipeline
from sre_agent.skill_plan import PlanResult, SkillOutput

FINDING = {
    "id": "f-pipe",
    "category": "crashloop",
    "severity": "critical",
    "title": "Pod api crashing",
    "resources": [{"kind": "Pod", "name": "api", "namespace": "shop"}],
}


class _Monitor:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._last_security_followup = 0.0

    async def _broadcast_raw(self, message: dict) -> None:
        self.sent.append(message)

    @property
    def reports(self) -> list[dict]:
        return [m for m in self.sent if m.get("type") == "investigation_report"]


def _template(timeout: int = 1):
    return SimpleNamespace(
        id="crashloop-resolution",
        name="Crashloop Resolution",
        phases=[SimpleNamespace(id="diagnose", skill_name="sre", produces=["root_cause"], timeout_seconds=timeout)],
    )


@pytest.fixture
def quiet(monkeypatch):
    """No database, inbox or postmortem writes; record what is saved and held."""
    saved: list[dict] = []
    held: list = []
    remembered: list = []

    async def _save(report, finding):
        saved.append(report)

    monkeypatch.setattr(pipeline, "_save", _save)
    monkeypatch.setattr(pipeline, "_save_postmortem", lambda *a: None)
    monkeypatch.setattr("sre_agent.inbox.attach_investigation_to_finding", lambda *a: None)
    learner = MagicMock()
    learner.record.side_effect = held.append
    monkeypatch.setattr("sre_agent.trajectory.get_learner", lambda: learner)
    manager = MagicMock()
    manager.store_incident.side_effect = lambda incident, confirmed: remembered.append(incident)
    monkeypatch.setattr("sre_agent.memory.get_manager", lambda: manager)
    return SimpleNamespace(saved=saved, held=held, remembered=remembered)


# ---------------------------------------------------------------------------
# A plan that fails is reported, like a freeform investigation that fails
# ---------------------------------------------------------------------------


def test_a_plan_that_times_out_is_reported_as_failed(monkeypatch, quiet):
    """Plans ran with no overall timeout and reported nothing when they failed."""
    monkeypatch.setenv("PULSE_AGENT_INVESTIGATION_TIMEOUT", "1")
    from sre_agent.config import _reset_settings

    _reset_settings()

    async def _hang(*a, **k):
        await asyncio.sleep(10)

    monitor = _Monitor()
    with patch("sre_agent.plan_runtime.PlanRuntime.execute", side_effect=_hang):
        outcome = asyncio.run(pipeline.investigate(monitor, FINDING, _template(timeout=1)))

    assert outcome.status == "failed" and outcome.strategy == "plan"
    [report] = monitor.reports
    assert report["status"] == "failed"
    assert report["error"] == "Investigation timed out after 1s"
    assert report["planId"] == "crashloop-resolution"
    assert quiet.saved and quiet.saved[0]["id"] == report["id"]
    assert not quiet.held


def test_a_plan_that_raises_is_reported_as_failed(quiet):
    monitor = _Monitor()
    with patch("sre_agent.plan_runtime.PlanRuntime.execute", side_effect=RuntimeError("Connection error.")):
        asyncio.run(pipeline.investigate(monitor, FINDING, _template()))
    [report] = monitor.reports
    assert report["status"] == "failed" and report["error"] == "Connection error."
    # The progress indicator is cleared even when the plan fails.
    assert monitor.sent[-2]["type"] == "skill_activity" and monitor.sent[-2]["status"] == "idle"


def test_a_plan_with_no_phase_output_is_a_failed_report_not_silence():
    outcome = pipeline.outcome_from_plan(_template(), PlanResult(plan_id="p", plan_name="p", status="failed"))
    assert outcome.status == "failed"
    assert outcome.to_report(FINDING)["error"] == "The plan produced no phase output"


# ---------------------------------------------------------------------------
# Completion is the same for both strategies
# ---------------------------------------------------------------------------


def _plan_result(confidence: float = 0.9) -> PlanResult:
    diagnose = SkillOutput(
        skill_id="sre",
        phase_id="diagnose",
        findings={"root_cause": "DB_HOST removed by the rollout"},
        evidence_summary="DB_HOST unset since 10:02",
        actions_taken=["get_pod_logs", "get_events"],
        confidence=confidence,
        evidence=[{"observation": "DB_HOST unset", "source": "get_pod_logs", "stance": "supports"}],
    )
    return PlanResult(plan_id="p", plan_name="p", status="complete", phase_outputs={"diagnose": diagnose})


def _freeform_result(confidence: float = 0.9, cause: str = "DB_HOST removed by the rollout") -> dict:
    return {
        "summary": "DB_HOST unset since 10:02",
        "suspected_cause": cause,
        "recommended_fix": "restore it",
        "confidence": confidence,
        "evidence_detail": [{"observation": "DB_HOST unset", "source": "get_pod_logs", "stance": "supports"}],
        "tools_called": ["get_pod_logs", "get_events"],
    }


def test_both_strategies_produce_the_same_report_shape(quiet):
    plan = pipeline.outcome_from_plan(_template(), _plan_result()).to_report(FINDING)
    free = pipeline.outcome_from_freeform(_freeform_result()).to_report(FINDING)
    assert set(plan) - {"planId"} == set(free)
    for key in ("suspected_cause", "confidence", "evidenceDetail", "status"):
        assert plan[key] == free[key], key


@pytest.mark.parametrize("strategy", ["plan", "freeform"])
def test_both_strategies_reach_memory_learning_and_the_context_bus(monkeypatch, quiet, strategy):
    """Plans never reached memory or the context bus; freeform did."""
    monkeypatch.setenv("PULSE_AGENT_MEMORY", "1")
    from sre_agent.config import _reset_settings

    _reset_settings()
    bus = MagicMock()
    monkeypatch.setattr("sre_agent.context_bus.get_context_bus", lambda: bus)
    outcome = (
        pipeline.outcome_from_plan(_template(), _plan_result())
        if strategy == "plan"
        else pipeline.outcome_from_freeform(_freeform_result())
    )
    monitor = _Monitor()
    asyncio.run(pipeline.complete_investigation(monitor, FINDING, outcome))

    assert len(monitor.reports) == 1 and quiet.saved
    bus.publish.assert_called_once()
    [candidate] = quiet.held
    assert candidate.root_cause == "DB_HOST removed by the rollout"
    [incident] = quiet.remembered
    # The tools it used, not the placeholder ["proactive_investigation"].
    assert incident["tool_sequence"] == ["get_pod_logs", "get_events"]


def test_a_confident_outcome_without_a_cause_is_not_held_for_learning(quiet):
    """Freeform learned a missing cause as "unknown"."""
    outcome = pipeline.outcome_from_freeform(_freeform_result(confidence=0.95, cause=""))
    asyncio.run(pipeline.complete_investigation(_Monitor(), FINDING, outcome))
    assert not quiet.held
    unknown = pipeline.outcome_from_freeform(_freeform_result(confidence=0.95, cause="unknown"))
    assert unknown.suspected_cause == ""


def test_a_failed_outcome_is_saved_but_not_learned_or_remembered(quiet):
    outcome = pipeline.failed_outcome("freeform", "boom")
    monitor = _Monitor()
    asyncio.run(pipeline.complete_investigation(monitor, FINDING, outcome))
    assert monitor.reports[0]["status"] == "failed"
    assert quiet.saved and not quiet.held and not quiet.remembered


def test_a_plan_gets_at_least_the_sum_of_its_phase_timeouts(monkeypatch):
    monkeypatch.setenv("PULSE_AGENT_INVESTIGATION_TIMEOUT", "120")
    from sre_agent.config import _reset_settings

    _reset_settings()
    template = SimpleNamespace(phases=[SimpleNamespace(timeout_seconds=90), SimpleNamespace(timeout_seconds=120)])
    assert pipeline.investigation_timeout(template) == 210
    assert pipeline.investigation_timeout(None) == 120


# ---------------------------------------------------------------------------
# Scheduling: a failed freeform investigation counts against the budget
# ---------------------------------------------------------------------------


def test_a_failing_freeform_investigation_is_not_retried_every_scan(monkeypatch, quiet):
    """It used to count only on success, so a failing one re-ran every scan outside the budget."""
    monkeypatch.setenv("PULSE_AGENT_INVESTIGATIONS_MAX_PER_SCAN", "1")
    from sre_agent.config import _reset_settings
    from sre_agent.monitor.cluster_monitor import ClusterMonitor

    _reset_settings()
    monitor = ClusterMonitor()
    calls = {"n": 0}

    async def _fail(finding, *, client=None):
        calls["n"] += 1
        raise RuntimeError("Connection error.")

    finding = {**FINDING, "noiseScore": 0.0}
    with (
        patch("sre_agent.monitor.investigation_pipeline._run_proactive_investigation", side_effect=_fail),
        patch("sre_agent.agent._circuit_breaker") as cb,
        patch("sre_agent.plan_templates.match_template", return_value=None),
    ):
        cb.is_open = False
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(monitor.run_investigations([finding]))
            loop.run_until_complete(monitor.run_investigations([finding]))
        finally:
            loop.close()

    assert calls["n"] == 1, "the second scan should respect the cooldown"
    assert monitor._daily_investigation_count == 1


# ---------------------------------------------------------------------------
# Every entry point goes through the pipeline
# ---------------------------------------------------------------------------


def test_a_handoff_investigation_is_reported_not_discarded(monkeypatch, quiet):
    """process_handoffs ran the investigation and threw the result away."""
    from sre_agent.monitor import handoff_processor

    class _Repo:
        async def async_get_pending_handoffs(self, cutoff):
            return [
                {
                    "id": 1,
                    "namespace": "shop",
                    "details": {"target": "sre_agent", "namespace": "shop", "kind": "Pod", "name": "api"},
                }
            ]

        async def async_delete_processed_handoffs(self, ids):
            return None

    clients: list = []

    async def _freeform(finding, *, client=None):
        clients.append(client)
        return _freeform_result()

    monkeypatch.setattr(handoff_processor, "get_monitor_repo", lambda: _Repo())
    monkeypatch.setattr(pipeline, "_run_proactive_investigation", _freeform)
    monitor = _Monitor()
    monitor._client = object()
    asyncio.run(handoff_processor.process_handoffs(monitor))

    [report] = monitor.reports
    assert report["status"] == "completed" and report["suspected_cause"]
    assert quiet.saved and quiet.held
    # Never the monitor's shared client under a timeout.
    assert clients == [None]


def test_inbox_triage_runs_the_pipeline_without_a_monitor(monkeypatch, quiet):
    attached: list = []
    monkeypatch.setattr("sre_agent.inbox.attach_investigation_to_finding", lambda *a: attached.append(a))

    async def _freeform(finding, *, client=None):
        return _freeform_result()

    monkeypatch.setattr(pipeline, "_run_proactive_investigation", _freeform)
    outcome = asyncio.run(pipeline.investigate(None, FINDING, attach_to_inbox=False))

    assert outcome.completed and outcome.report_id.startswith("i-")
    assert quiet.saved[0]["id"] == outcome.report_id
    assert not attached, "the inbox updates its own item"
    assert quiet.held, "inbox investigations reach the learning gate too"
