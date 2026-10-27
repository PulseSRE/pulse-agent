"""Tests for view designer eval coverage."""

from __future__ import annotations

from sre_agent.evals.replay import list_fixtures, load_fixture, score_replay
from sre_agent.evals.runner import evaluate_suite, score_scenario
from sre_agent.evals.scenarios import load_suite


def _recorded(fixture: dict) -> dict:
    """Recorded tool responses, from the fixture or from each of its turns."""
    if fixture.get("multi_turn"):
        merged: dict = {}
        for turn in fixture["turns"]:
            merged.update(turn.get("recorded_responses", {}))
        return merged
    return fixture["recorded_responses"]


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
            assert "expected" in fixture
            if fixture.get("multi_turn"):
                assert all(turn.get("prompt") for turn in fixture["turns"])
            else:
                assert "prompt" in fixture
            assert len(_recorded(fixture)) > 0

    def test_fixture_has_no_write_tools(self):
        """View designer fixtures should not record write tool responses."""
        write_tools = {"delete_pod", "scale_deployment", "drain_node", "cordon_node", "apply_yaml"}
        for name in ["view_namespace_dashboard", "view_cluster_overview", "view_add_widget", "view_incident_triage"]:
            fixture = load_fixture(name)
            recorded = set(_recorded(fixture).keys())
            overlap = recorded & write_tools
            assert not overlap, f"Fixture {name} records write tools: {overlap}"

    def test_expected_forbids_write_tools(self):
        for name in ["view_namespace_dashboard", "view_cluster_overview"]:
            fixture = load_fixture(name)
            expected = fixture["expected"]
            forbidden = expected.get("should_not_use_tools", []) or [
                tool for turn in expected.get("per_turn", []) for tool in turn.get("should_not_use_tools", [])
            ]
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


class TestTriageFixtureRequiresADashboard:
    """The view designer plans and waits for approval, so one turn proved nothing was built."""

    def test_approval_turn_and_dashboard_required(self):
        for name in (
            "view_incident_triage",
            "view_cluster_overview",
            "view_namespace_dashboard",
            "view_manage_openshiftpulse",
        ):
            fixture = load_fixture(name)
            assert fixture.get("multi_turn") and len(fixture["turns"]) == 2, name
            assert "create_dashboard" in fixture["expected"]["should_use_tools"], name

    def test_view_designer_is_not_offered_memory_tools(self):
        from sre_agent.memory.memory_tools import MEMORY_TOOLS
        from sre_agent.skill_loader import build_config_from_skill, get_skill, guaranteed_tool_names, load_skills

        load_skills()
        skill = get_skill("view_designer")
        offered = {d["name"] for d in build_config_from_skill(skill, "create an incident dashboard")["tool_defs"]}
        memory = {t.name for t in MEMORY_TOOLS}
        assert not (offered & memory)
        assert not (guaranteed_tool_names(skill) & memory)
        assert "create_dashboard" in offered

        sre = get_skill("sre")
        assert memory <= {d["name"] for d in build_config_from_skill(sre, "why is api crashing")["tool_defs"]}
