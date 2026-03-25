"""Tests for self-evaluation scoring."""

import pytest
from sre_agent.memory.evaluation import evaluate_interaction


class TestEvaluation:
    def test_perfect_score(self):
        result = evaluate_interaction(
            tool_calls=[{"name": "list_pods"}, {"name": "get_pod_logs"}],
            rejected_count=0,
            user_confirmed_resolution=True,
            duration_seconds=30,
            final_response="Found and fixed the issue",
        )
        assert result.score > 0.9
        assert result.resolved is True

    def test_zero_score(self):
        result = evaluate_interaction(
            tool_calls=[{"name": f"t{i}"} for i in range(20)],
            rejected_count=4,
            user_confirmed_resolution=False,
            duration_seconds=600,
            final_response="",
        )
        assert result.score < 0.15
        assert result.resolved is False

    def test_unknown_resolution(self):
        result = evaluate_interaction(
            tool_calls=[{"name": "list_pods"}],
            rejected_count=0,
            user_confirmed_resolution=None,
            duration_seconds=30,
            final_response="Here are the pods running in the default namespace with their status, restarts, and ages. Everything looks healthy across the board.",
        )
        assert 0.3 < result.score <= 0.8
        assert result.breakdown["resolution"] == 0.5

    def test_short_response_lower_score(self):
        result = evaluate_interaction(
            tool_calls=[], rejected_count=0,
            user_confirmed_resolution=None,
            duration_seconds=10, final_response="ok",
        )
        assert result.breakdown["resolution"] == 0.3

    def test_efficiency_optimal(self):
        result = evaluate_interaction(
            tool_calls=[{"name": f"t{i}"} for i in range(3)],
            rejected_count=0, user_confirmed_resolution=True,
            duration_seconds=30, final_response="done",
        )
        assert result.breakdown["efficiency"] == 1.0

    def test_efficiency_many_tools(self):
        result = evaluate_interaction(
            tool_calls=[{"name": f"t{i}"} for i in range(18)],
            rejected_count=0, user_confirmed_resolution=True,
            duration_seconds=30, final_response="done",
        )
        assert result.breakdown["efficiency"] == 0.2

    def test_safety_rejections(self):
        result = evaluate_interaction(
            tool_calls=[], rejected_count=2,
            user_confirmed_resolution=True,
            duration_seconds=30, final_response="done",
        )
        assert result.breakdown["safety"] == 0.4

    def test_speed_slow(self):
        result = evaluate_interaction(
            tool_calls=[], rejected_count=0,
            user_confirmed_resolution=True,
            duration_seconds=400, final_response="done",
        )
        assert result.breakdown["speed"] == 0.0

    def test_speed_medium(self):
        result = evaluate_interaction(
            tool_calls=[], rejected_count=0,
            user_confirmed_resolution=True,
            duration_seconds=180, final_response="done",
        )
        assert 0.0 < result.breakdown["speed"] < 1.0

    def test_weights_sum_to_one(self):
        weights = {"resolution": 0.4, "efficiency": 0.3, "safety": 0.2, "speed": 0.1}
        assert abs(sum(weights.values()) - 1.0) < 0.001
