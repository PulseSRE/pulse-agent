"""Tests for multi-turn replay harness."""

from __future__ import annotations

from sre_agent.evals.replay import (
    list_fixtures,
    load_fixture,
    score_multi_turn,
)


class TestMultiTurnFixtures:
    def test_multi_turn_fixtures_exist(self):
        fixtures = list_fixtures()
        expected = ["multi_crashloop_followup", "multi_dashboard_iterate", "multi_scale_and_verify"]
        for name in expected:
            assert name in fixtures, f"Missing multi-turn fixture: {name}"

    def test_multi_turn_fixture_structure(self):
        for name in ["multi_crashloop_followup", "multi_dashboard_iterate", "multi_scale_and_verify"]:
            fixture = load_fixture(name)
            assert fixture.get("multi_turn") is True
            assert "turns" in fixture
            assert len(fixture["turns"]) >= 2
            for turn in fixture["turns"]:
                assert "prompt" in turn
                assert "recorded_responses" in turn

    def test_crashloop_has_3_turns(self):
        fixture = load_fixture("multi_crashloop_followup")
        assert len(fixture["turns"]) == 3

    def test_dashboard_iterate_has_3_turns(self):
        fixture = load_fixture("multi_dashboard_iterate")
        assert len(fixture["turns"]) == 3

    def test_scale_verify_has_3_turns(self):
        fixture = load_fixture("multi_scale_and_verify")
        assert len(fixture["turns"]) == 3


class TestScoreMultiTurn:
    def test_all_checks_pass(self):
        result = {
            "turns": [
                {"response": "Found database connection error in logs", "tool_calls": [{"name": "get_pod_logs"}]},
                {"response": "No postgres pods running", "tool_calls": [{"name": "describe_service"}]},
            ],
            "total_duration_ms": 5000,
        }
        expected = {
            "per_turn": [
                {"should_mention": ["database"], "should_use_tools": ["get_pod_logs"]},
                {"should_mention": ["postgres"], "should_use_tools": ["describe_service"]},
            ],
            "overall_should_mention": ["database", "postgres"],
            "max_total_tool_calls": 10,
        }
        score = score_multi_turn(result, expected)
        assert score["passed"] is True
        assert score["score"] == 100

    def test_missing_keyword_fails(self):
        result = {
            "turns": [
                {"response": "Everything looks fine", "tool_calls": [{"name": "list_pods"}]},
            ],
        }
        expected = {
            "per_turn": [{"should_mention": ["error", "crash"]}],
        }
        score = score_multi_turn(result, expected)
        assert score["passed"] is False
        assert score["score"] < 100

    def test_tool_order_check(self):
        result = {
            "turns": [
                {"response": "Checking logs", "tool_calls": [{"name": "get_pod_logs"}]},
                {"response": "Checking service", "tool_calls": [{"name": "describe_service"}]},
                {"response": "Found quota issue", "tool_calls": [{"name": "describe_deployment"}]},
            ],
        }
        expected = {
            "should_use_tools_in_order": ["get_pod_logs", "describe_service", "describe_deployment"],
        }
        score = score_multi_turn(result, expected)
        in_order_check = next(c for c in score["checks"] if "order" in c["check"])
        assert in_order_check["passed"] is True

    def test_tool_order_wrong_fails(self):
        result = {
            "turns": [
                {"response": "Checking deployment", "tool_calls": [{"name": "describe_deployment"}]},
                {"response": "Now logs", "tool_calls": [{"name": "get_pod_logs"}]},
            ],
        }
        expected = {
            "should_use_tools_in_order": ["get_pod_logs", "describe_deployment"],
        }
        score = score_multi_turn(result, expected)
        in_order_check = next(c for c in score["checks"] if "order" in c["check"])
        assert in_order_check["passed"] is False

    def test_tool_budget_exceeded(self):
        result = {
            "turns": [
                {"response": "ok", "tool_calls": [{"name": f"tool_{i}"} for i in range(10)]},
            ],
        }
        expected = {"max_total_tool_calls": 5}
        score = score_multi_turn(result, expected)
        assert score["passed"] is False

    def test_empty_expected(self):
        result = {
            "turns": [{"response": "hello", "tool_calls": []}],
        }
        score = score_multi_turn(result, {})
        assert score["passed"] is True
        assert score["score"] == 100

    def test_per_turn_should_not_use_tools(self):
        result = {
            "turns": [
                {"response": "Added widget", "tool_calls": [{"name": "add_widget_to_view"}]},
            ],
        }
        expected = {
            "per_turn": [{"should_not_use_tools": ["create_dashboard"]}],
        }
        score = score_multi_turn(result, expected)
        assert score["passed"] is True
