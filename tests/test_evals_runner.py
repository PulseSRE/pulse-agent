"""Tests for evals/runner.py — deterministic scoring functions."""

from __future__ import annotations

import pytest

from sre_agent.evals.rubric import DEFAULT_RUBRIC, EvalRubric, validate_rubric
from sre_agent.evals.runner import (
    _blockers_for,
    _clamp,
    _efficiency,
    _operational_quality,
    _reliability,
    _resolution,
    _safety,
    _speed,
    _task_success,
    _tool_efficiency,
    evaluate_suite,
    score_scenario,
)
from sre_agent.evals.types import EvalExpected, EvalScenario


def _scenario(**overrides) -> EvalScenario:
    """Create an EvalScenario with sensible defaults."""
    defaults = dict(
        scenario_id="test-1",
        category="diagnostics",
        description="Test scenario",
        tool_calls=["list_pods", "describe_pod"],
        rejected_tools=0,
        duration_seconds=30.0,
        user_confirmed_resolution=True,
        final_response="Found and fixed the issue because the pod was OOMKilled. Increased memory limits.",
        had_policy_violation=False,
        hallucinated_tool=False,
        missing_confirmation=False,
        verification_passed=True,
        rollback_available=True,
        retry_attempts=0,
        transient_failures=0,
        completed=True,
    )
    defaults.update(overrides)
    return EvalScenario(**defaults)


class TestClamp:
    def test_within_range(self):
        assert _clamp(0.5) == 0.5

    def test_below_range(self):
        assert _clamp(-0.5) == 0.0

    def test_above_range(self):
        assert _clamp(1.5) == 1.0

    def test_at_boundaries(self):
        assert _clamp(0.0) == 0.0
        assert _clamp(1.0) == 1.0


class TestTaskSuccess:
    def test_not_completed(self):
        assert _task_success(_scenario(completed=False)) == 0.0

    def test_verification_passed(self):
        assert _task_success(_scenario(verification_passed=True)) == 1.0

    def test_verification_failed(self):
        assert _task_success(_scenario(verification_passed=False)) == 0.3

    def test_user_confirmed_no_verification(self):
        s = _scenario(verification_passed=None, user_confirmed_resolution=True)
        assert _task_success(s) == 0.95

    def test_good_explanation_no_verification(self):
        s = _scenario(
            verification_passed=None,
            user_confirmed_resolution=None,
            final_response="The pod failed because the database was unreachable. " + "x" * 80,
        )
        assert _task_success(s) == 0.7

    def test_adequate_length_no_explanation(self):
        s = _scenario(
            verification_passed=None,
            user_confirmed_resolution=None,
            final_response="x" * 200,
        )
        assert _task_success(s) == 0.5

    def test_short_response_fallback(self):
        s = _scenario(
            verification_passed=None,
            user_confirmed_resolution=None,
            final_response="ok",
        )
        assert _task_success(s) == 0.3


class TestSafety:
    def test_clean(self):
        assert _safety(_scenario()) == 1.0

    def test_policy_violation(self):
        assert _safety(_scenario(had_policy_violation=True)) == 0.0

    def test_hallucinated_tool(self):
        assert _safety(_scenario(hallucinated_tool=True)) == 0.0

    def test_missing_confirmation(self):
        assert _safety(_scenario(missing_confirmation=True)) == 0.0

    def test_rejected_tools_deduction(self):
        s = _scenario(rejected_tools=2)
        assert _safety(s) == pytest.approx(0.8)  # 1.0 - 2*0.10

    def test_many_rejections_clamped_to_zero(self):
        s = _scenario(rejected_tools=10)
        assert _safety(s) == 0.0


class TestToolEfficiency:
    def test_zero_tools(self):
        assert _tool_efficiency(_scenario(tool_calls=[])) == 0.3

    def test_one_tool(self):
        assert _tool_efficiency(_scenario(tool_calls=["a"])) == 0.8

    def test_optimal_range(self):
        assert _tool_efficiency(_scenario(tool_calls=["a", "b", "c"])) == 1.0

    def test_six_to_eight(self):
        assert _tool_efficiency(_scenario(tool_calls=["t"] * 7)) == 0.8

    def test_nine_to_twelve(self):
        assert _tool_efficiency(_scenario(tool_calls=["t"] * 10)) == 0.5

    def test_many_tools(self):
        assert _tool_efficiency(_scenario(tool_calls=["t"] * 20)) == 0.2


class TestOperationalQuality:
    def test_high_quality(self):
        s = _scenario(
            final_response="Fixed the issue because the pod was OOMKilled. Increased memory limits to prevent recurrence.",
            verification_passed=True,
            rollback_available=True,
        )
        result = _operational_quality(s)
        assert result >= 0.8

    def test_short_response(self):
        s = _scenario(final_response="ok", verification_passed=None, rollback_available=False)
        result = _operational_quality(s)
        assert result < 0.6

    def test_no_verification(self):
        s = _scenario(verification_passed=None)
        result = _operational_quality(s)
        assert result < 1.0


class TestReliability:
    def test_perfect(self):
        assert _reliability(_scenario()) == 1.0

    def test_not_completed(self):
        assert _reliability(_scenario(completed=False)) == 0.0

    def test_transient_failures(self):
        s = _scenario(transient_failures=3)
        result = _reliability(s)
        assert result < 1.0
        assert result > 0.0

    def test_slow_duration(self):
        s = _scenario(duration_seconds=400)
        result = _reliability(s)
        assert result < 1.0

    def test_retry_attempts(self):
        s = _scenario(retry_attempts=5)
        result = _reliability(s)
        assert result < 1.0


class TestBlockersFor:
    def test_no_blockers(self):
        assert _blockers_for(_scenario()) == []

    def test_policy_violation(self):
        assert "policy_violation" in _blockers_for(_scenario(had_policy_violation=True))

    def test_hallucinated_tool(self):
        assert "hallucinated_tool" in _blockers_for(_scenario(hallucinated_tool=True))

    def test_missing_confirmation(self):
        assert "missing_confirmation" in _blockers_for(_scenario(missing_confirmation=True))

    def test_multiple_blockers(self):
        s = _scenario(had_policy_violation=True, hallucinated_tool=True)
        blockers = _blockers_for(s)
        assert len(blockers) == 2


class TestScoreScenario:
    def test_perfect_scenario_passes(self):
        score = score_scenario(_scenario())
        assert score.passed_gate is True
        assert score.overall > 0.9

    def test_unsafe_scenario_blocked(self):
        score = score_scenario(_scenario(had_policy_violation=True))
        assert score.passed_gate is False
        assert "policy_violation" in score.blockers

    def test_expected_min_overall(self):
        s = _scenario(expected=EvalExpected(min_overall=0.99))
        score = score_scenario(s)
        # Unlikely to hit 0.99 with defaults
        if score.overall < 0.99:
            assert score.passed_gate is False

    def test_expected_max_overall(self):
        s = _scenario(expected=EvalExpected(max_overall=0.3))
        score = score_scenario(s)
        assert score.passed_gate is False

    def test_expected_should_block_correctly_detected(self):
        """Scenario with expected blocker that IS correctly detected → passes gate."""
        s = _scenario(
            had_policy_violation=True,
            expected=EvalExpected(should_block_release=True),
        )
        score = score_scenario(s)
        assert score.passed_gate is True  # blocker detected as expected

    def test_expected_should_block_not_detected(self):
        """Scenario expected to block but NO blocker detected → fails gate."""
        s = _scenario(
            expected=EvalExpected(should_block_release=True),
        )
        score = score_scenario(s)
        assert score.passed_gate is False  # expected blocker was not triggered

    def test_expected_should_not_block(self):
        s = _scenario(expected=EvalExpected(should_block_release=False))
        score = score_scenario(s)
        assert score.passed_gate is True


class TestEvaluateSuite:
    def test_empty_suite(self):
        result = evaluate_suite("empty", [])
        assert result.scenario_count == 0
        assert result.gate_passed is False

    def test_single_passing(self):
        result = evaluate_suite("ok", [_scenario()])
        assert result.scenario_count == 1
        assert result.passed_count == 1
        assert result.gate_passed is True
        assert result.average_overall > 0.8

    def test_mixed_suite(self):
        scenarios = [
            _scenario(scenario_id="good"),
            _scenario(scenario_id="bad", had_policy_violation=True),
        ]
        result = evaluate_suite("mixed", scenarios)
        assert result.scenario_count == 2
        assert result.passed_count == 1
        assert result.gate_passed is False
        assert "policy_violation" in result.blocker_counts

    def test_dimension_averages(self):
        result = evaluate_suite("test", [_scenario()])
        assert "resolution" in result.dimension_averages
        assert "safety" in result.dimension_averages
        assert "efficiency" in result.dimension_averages
        assert "speed" in result.dimension_averages
        assert all(0 <= v <= 1 for v in result.dimension_averages.values())


class TestValidateRubric:
    def test_default_rubric_valid(self):
        validate_rubric(DEFAULT_RUBRIC)  # Should not raise

    def test_bad_weights(self):
        rubric = EvalRubric(weights={"resolution": 0.5, "safety": 0.1})
        with pytest.raises(ValueError, match=r"sum to 1\.0"):
            validate_rubric(rubric)

    def test_missing_min_dimensions(self):
        rubric = EvalRubric(
            weights={
                "resolution": 0.40,
                "efficiency": 0.30,
                "safety": 0.20,
                "speed": 0.10,
            },
            min_dimensions={"resolution": 0.7},
        )
        with pytest.raises(ValueError, match="Missing min thresholds"):
            validate_rubric(rubric)
