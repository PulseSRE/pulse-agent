"""An incident needing a recovery plan must still receive actual diagnosis."""

import pytest

from sre_agent.evals.replay import load_fixture
from sre_agent.evals.replay_config import build_replay_config
from sre_agent.skill_router import _hard_pre_route, classify_query, offline_routing


@pytest.mark.parametrize(
    "query",
    [
        "Namespace team-alpha cannot create new pods. Diagnose the quota issue and propose a recovery plan.",
        "Pods cannot create volumes; diagnose quota exhaustion and propose a recovery plan.",
        "Pods cannot create volumes! Diagnose the quota issue and provide a recovery plan.",
        "Don't create a plan. Diagnose why team-alpha cannot create new pods.",
        "Do not   build a plan; investigate why the namespace is exhausted.",
        "Never make a recovery plan; diagnose the quota problem first.",
    ],
)
def test_incident_and_negated_authoring_requests_do_not_route_to_plan_builder(query):
    with offline_routing():
        hard = _hard_pre_route(query)
        assert hard is None or hard.name != "plan_builder"
        assert classify_query(query).name != "plan_builder"


@pytest.mark.parametrize(
    "query",
    [
        "Create a plan for investigating an incident.",
        "Build me an investigation plan.",
        "Make us a safe recovery plan.",
        "Create a new phased rollback plan.",
    ],
)
def test_explicit_plan_authoring_keeps_its_capability(query):
    with offline_routing():
        assert _hard_pre_route(query).name == "plan_builder"
        assert classify_query(query).name == "plan_builder"


def test_actual_quota_replay_builds_diagnostic_config_with_recorded_tools():
    fixture = load_fixture("release_quota_exhaustion")
    with offline_routing():
        config = build_replay_config(fixture["prompt"], fixture["recorded_responses"])
    assert config["mode"] == "sre"
    offered = {tool["name"] for tool in config["tool_defs"]}
    assert {"list_resources", "list_pods"} <= offered
    assert "team-alpha" in config["tool_map"]["list_resources"].call(
        {"resource": "resourcequotas", "namespace": "team-alpha"}
    )
