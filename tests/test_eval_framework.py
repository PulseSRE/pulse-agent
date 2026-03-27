"""Tests for deterministic eval framework."""

from __future__ import annotations

from sre_agent.evals.runner import evaluate_suite, score_scenario
from sre_agent.evals.scenarios import load_suite


def test_load_core_suite():
    scenarios = load_suite("core")
    assert len(scenarios) >= 6
    assert scenarios[0].scenario_id


def test_blocker_scenarios_fail_gate():
    scenarios = load_suite("core")
    blocked = [s for s in scenarios if s.expected and s.expected.should_block_release]
    assert blocked
    for scenario in blocked:
        score = score_scenario(scenario)
        assert score.passed_gate is False
        for blocker in scenario.expected.required_blockers:
            assert blocker in score.blockers


def test_suite_gate_fails_when_blockers_present():
    scenarios = load_suite("core")
    result = evaluate_suite("core", scenarios)
    assert result.gate_passed is False
    assert result.blocker_counts
    assert result.scenario_count == len(scenarios)


def test_release_suite_has_broad_coverage():
    scenarios = load_suite("release")
    assert len(scenarios) >= 10
