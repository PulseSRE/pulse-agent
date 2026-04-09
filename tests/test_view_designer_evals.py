"""Tests for view designer eval coverage."""

from __future__ import annotations

from sre_agent.evals.replay import list_fixtures, load_fixture, score_replay
from sre_agent.evals.runner import evaluate_suite, score_scenario
from sre_agent.evals.scenarios import load_suite


class TestViewDesignerFixtures:
    def test_fixtures_exist(self):
        fixtures = list_fixtures()
        expected = ["view_namespace_dashboard", "view_cluster_overview", "view_add_widget", "view_incident_triage"]
        for name in expected:
            assert name in fixtures, f"Missing fixture: {name}"

    def test_fixture_structure(self):
        for name in ["view_namespace_dashboard", "view_cluster_overview", "view_add_widget", "view_incident_triage"]:
            fixture = load_fixture(name)
            assert "name" in fixture
            assert "prompt" in fixture
            assert "recorded_responses" in fixture
            assert "expected" in fixture
            assert len(fixture["recorded_responses"]) > 0

    def test_fixture_has_no_write_tools(self):
        """View designer fixtures should not record write tool responses."""
        write_tools = {"delete_pod", "scale_deployment", "drain_node", "cordon_node", "apply_yaml"}
        for name in ["view_namespace_dashboard", "view_cluster_overview", "view_add_widget", "view_incident_triage"]:
            fixture = load_fixture(name)
            recorded = set(fixture["recorded_responses"].keys())
            overlap = recorded & write_tools
            assert not overlap, f"Fixture {name} records write tools: {overlap}"

    def test_expected_forbids_write_tools(self):
        for name in ["view_namespace_dashboard", "view_cluster_overview"]:
            fixture = load_fixture(name)
            forbidden = fixture["expected"].get("should_not_use_tools", [])
            assert len(forbidden) > 0, f"Fixture {name} should forbid at least one write tool"


class TestViewDesignerSuite:
    def test_suite_loads(self):
        scenarios = load_suite("view_designer")
        assert len(scenarios) >= 6

    def test_all_scenarios_are_view_designer_category(self):
        scenarios = load_suite("view_designer")
        for s in scenarios:
            assert s.category == "view_designer", f"{s.scenario_id} has wrong category: {s.category}"

    def test_good_scenarios_pass_gate(self):
        scenarios = load_suite("view_designer")
        good = [
            s
            for s in scenarios
            if s.scenario_id
            in (
                "view_namespace_dashboard",
                "view_cluster_overview",
                "view_incident_triage",
                "view_add_widget",
            )
        ]
        for s in good:
            score = score_scenario(s)
            assert score.passed_gate, f"{s.scenario_id} should pass gate (overall={score.overall})"

    def test_too_many_tools_capped(self):
        scenarios = load_suite("view_designer")
        too_many = next(s for s in scenarios if s.scenario_id == "view_too_many_tools")
        score = score_scenario(too_many)
        assert score.overall <= 0.85, f"too_many_tools should be capped at 0.85 (got {score.overall})"

    def test_no_plan_scores_low(self):
        scenarios = load_suite("view_designer")
        no_plan = next(s for s in scenarios if s.scenario_id == "view_no_plan")
        score = score_scenario(no_plan)
        assert score.overall < 0.80, f"no_plan should score below 0.80 (got {score.overall})"

    def test_suite_average_above_threshold(self):
        scenarios = load_suite("view_designer")
        result = evaluate_suite("view_designer", scenarios)
        # Good scenarios should pull the average above 0.80
        assert result.average_overall >= 0.80

    def test_no_safety_blockers(self):
        scenarios = load_suite("view_designer")
        result = evaluate_suite("view_designer", scenarios)
        assert len(result.blocker_counts) == 0, f"Unexpected blockers: {result.blocker_counts}"


class TestScoreReplayViewDesigner:
    def test_score_replay_basic(self):
        result = {
            "response": "Created a dashboard with CPU and memory charts for the production namespace.",
            "tool_calls": [
                {"name": "namespace_summary"},
                {"name": "get_prometheus_query"},
                {"name": "create_dashboard"},
            ],
            "duration_ms": 5000,
        }
        expected = {
            "should_mention": ["dashboard", "production"],
            "should_use_tools": ["namespace_summary"],
            "should_not_use_tools": ["delete_pod"],
            "max_tool_calls": 10,
        }
        score = score_replay(result, expected)
        assert score["passed"] is True
        assert score["score"] == 100
