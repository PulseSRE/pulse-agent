"""The learning curve, checked on every run.

The simulator drives scripted incidents through the real planner,
verification, recurrence and learning code (see sre_agent/evals/learning_sim).
These assertions are the claim "Pulse learns from what held" in executable
form: if a change breaks any loop end to end, a curve flattens and this fails.
"""

from __future__ import annotations

import pytest

from sre_agent.evals.learning_sim import SCENARIOS, simulate


@pytest.fixture(scope="module")
def curves():
    # Module-scoped, so it runs before the per-test autouse fixture that
    # points the app at the test database; do that here.
    from sre_agent.config import _reset_settings
    from tests.conftest import _TEST_DB_URL

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PULSE_AGENT_DATABASE_URL", _TEST_DB_URL)
        _reset_settings()
        try:
            yield {r.scenario.name: r for r in simulate()}
        finally:
            _reset_settings()


def test_a_fix_that_holds_keeps_being_applied_and_becomes_a_skill(curves):
    r = curves["transient-crashloop"]
    assert [i.verdict for i in r.incidents] == ["held"] * SCENARIOS["transient-crashloop"].incidents
    assert r.first_escalation is None
    assert r.skill is not None, "verified fixes produce a learned skill"
    assert r.skill["version"] > 1, "later verified cases deepen it rather than cloning it"
    assert r.skill["reviewed"] is False, "a person still approves it before it routes"


def test_a_fix_that_never_holds_stops_being_applied(curves):
    r = curves["config-crashloop"]
    assert r.fixes_that_did_not_hold == 3
    assert r.first_escalation == 4
    later = r.incidents[r.first_escalation - 1 :]
    assert all(i.verdict == "escalated" for i in later), "no more restarts once it has learned they do not hold"
    assert "held 0 of 3" in later[0].learned, "and it says why"
    assert r.skill is None, "nothing that failed was learned as a skill"


def test_a_recurring_fix_gets_one_alternative_then_a_person(curves):
    r = curves["memory-leak"]
    strategies = [i.strategy for i in r.incidents]
    assert "restart_controller" in strategies[:4], "the untried alternative was tried"
    assert r.first_escalation is not None and r.first_escalation <= 5
    assert r.fixes_that_did_not_hold <= 4
    assert all(i.verdict == "escalated" for i in r.incidents[r.first_escalation - 1 :])
    assert r.skill is None, "fixes that came back within the recurrence window teach nothing"


def test_each_incident_is_a_different_workload(curves):
    """Per-resource attempt caps cannot explain the curve; only cross-incident learning can."""
    for r in curves.values():
        workloads = [i.workload for i in r.incidents]
        assert len(set(workloads)) == len(workloads)
