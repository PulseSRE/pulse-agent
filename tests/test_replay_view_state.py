"""Mutation-linked replay state changes only after a supported successful action."""

import copy
import json

import pytest

from sre_agent.evals.replay import load_fixture
from sre_agent.evals.replay_config import no_recording_message, shadow_tool_map

QUERY = 'sum(container_memory_working_set_bytes{namespace!=""}) by (namespace)'


def tools(recordings=None):
    return shadow_tool_map({}, recordings or load_fixture("view_add_widget")["recorded_responses"])


def view(mapping):
    return json.loads(mapping["get_view_details"].call({"view_id": "view-001"}))


def generate_component(mapping, title="Memory Usage by Namespace"):
    return mapping["get_prometheus_query"].call({"query": QUERY, "title": title, "time_range": "1h"})


def test_actual_fixture_mutation_updates_once_after_supported_component():
    mapping = tools()
    assert len(view(mapping)["widgets"]) == 5
    assert len(view(mapping)["widgets"]) == 5
    assert mapping["add_widget_to_view"].call({"view_id": "view-001"}) == no_recording_message("add_widget_to_view")
    generate_component(mapping)
    assert len(view(mapping)["widgets"]) == 5  # data generation is not persistence
    assert "6 widgets" in mapping["add_widget_to_view"].call({"view_id": "view-001"})
    after = view(mapping)
    assert len(after["widgets"]) == 6
    assert after["widgets"][-1]["query"] == QUERY
    assert after["widgets"][-1]["title"] == "Memory Usage by Namespace"
    assert view(mapping) == after  # repeated reads never advance state
    assert mapping["add_widget_to_view"].call({"view_id": "view-001"}) == no_recording_message("add_widget_to_view")
    assert view(mapping) == after
    assert len(view(tools())["widgets"]) == 5  # per-session isolation


@pytest.mark.parametrize(
    "inputs",
    [
        {"view_id": "wrong-view"},
        {"view_id": "view-001", "widget": "invented"},
    ],
)
def test_unmatched_action_preserves_before_state(inputs):
    mapping = tools()
    generate_component(mapping)
    assert mapping["add_widget_to_view"].call(inputs) == no_recording_message("add_widget_to_view")
    assert len(view(mapping)["widgets"]) == 5


def test_unknown_query_cannot_manufacture_component_or_saved_widget():
    mapping = tools()
    assert mapping["get_prometheus_query"].call({"query": "up"}) == no_recording_message("get_prometheus_query")
    assert mapping["add_widget_to_view"].call({"view_id": "view-001"}) == no_recording_message("add_widget_to_view")
    assert len(view(mapping)["widgets"]) == 5


@pytest.mark.parametrize("mode", ["failed", "ambiguous", "missing-success"])
def test_failed_or_invalid_transition_preserves_state(mode):
    recorded = {
        "read": {
            "recording_kind": "argument_cases",
            "initial_state": {"applied": False},
            "cases": [
                {"input": {}, "requires_state": {"applied": False}, "response": "before"},
                {"input": {}, "requires_state": {"applied": True}, "response": "after"},
            ],
        },
        "write": {
            "recording_kind": "argument_cases",
            "cases": [
                {"input": {}, "response": "ack", "succeeded": True, "set_state": {"applied": True}},
            ],
        },
    }
    case = recorded["write"]["cases"][0]
    if mode == "failed":
        case.update(succeeded=False, response="Frontend rejected action")
    elif mode == "ambiguous":
        recorded["write"]["cases"].append(copy.deepcopy(case))
    else:
        case.pop("succeeded")
    mapping = tools(recorded)
    if mode == "failed":
        assert mapping["write"].call({}) == "Frontend rejected action"
    else:
        with pytest.raises(ValueError):
            mapping["write"].call({})
    assert mapping["read"].call({}) == "before"


def test_recording_data_unchanged_and_legacy_values_preserved():
    recorded = load_fixture("view_add_widget")["recorded_responses"]
    original = copy.deepcopy(recorded)
    mapping = tools(recorded)
    generate_component(mapping)
    mapping["add_widget_to_view"].call({"view_id": "view-001"})
    assert recorded == original
    legacy = shadow_tool_map({}, {"get_view_details": "old static recording"})
    assert legacy["get_view_details"].call({"view_id": "anything"}) == "old static recording"


@pytest.mark.parametrize(
    "query,expected",
    [
        ('sum by (namespace) (container_memory_working_set_bytes{container!="",container!="POD",image!=""})', 6),
        ('sum(container_memory_working_set_bytes{namespace="production"})', 1),
        ('container_memory_working_set_bytes{namespace=~"production|staging"}', 2),
        ('sum(container_memory_working_set_bytes{namespace!="production"}) by (namespace)', 5),
    ],
)
def test_semantic_memory_queries_preserve_metadata_and_apply_filters(query, expected):
    mapping = tools()
    title = 'My "custom" memory chart'
    description = "User supplied caption; arbitrary presentation is supported"
    response = json.loads(
        mapping["get_prometheus_query"].call(
            {
                "query": query,
                "time_range": "3h",
                "title": title,
                "description": description,
            }
        )
    )
    assert len(response["series"]) == expected
    if 'namespace="production"' in query:
        assert response["series"][0]["bytes"] == round(4.2 * 1024**3)
    mapping["add_widget_to_view"].call({"view_id": "view-001"})
    widget = view(mapping)["widgets"][-1]
    assert widget["query"] == query
    assert widget["time_range"] == "3h"
    assert widget["title"] == title
    assert widget["description"] == description


@pytest.mark.parametrize(
    "query",
    ["up", "rate(container_memory_working_set_bytes[5m])", 'container_memory_working_set_bytes{unknown="value"}'],
)
def test_unsupported_query_does_not_create_successful_outcome(query):
    from sre_agent.evals.replay import _fixture_state, score_replay
    from sre_agent.evals.replay_cli import _apply_judge_gate

    mapping = tools()
    assert mapping["get_prometheus_query"].call({"query": query}) == no_recording_message("get_prometheus_query")
    result = {
        "response": "I cannot complete it",
        "tool_calls": [{"name": "get_prometheus_query"}],
        "fixture_state": _fixture_state(mapping),
    }
    score = score_replay(result, load_fixture("view_add_widget")["expected"])
    assert not _apply_judge_gate(score, {"total": 100}, 60)["passed"]
    assert result["fixture_state"] == {"memory_widget_added": False, "view_widget_count": 5}


def test_actual_success_requires_add_and_final_saved_state():
    from sre_agent.evals.replay import _fixture_state, score_replay
    from sre_agent.evals.replay_cli import _apply_judge_gate

    mapping = tools()
    generate_component(mapping)
    calls = [{"name": "get_prometheus_query"}, {"name": "add_widget_to_view"}]
    expected = load_fixture("view_add_widget")["expected"]
    # Merely requesting the add cannot pass without the recorded state change.
    before = {"response": "Memory added", "tool_calls": calls, "fixture_state": _fixture_state(mapping)}
    assert not _apply_judge_gate(score_replay(before, expected), {"total": 100}, 60)["passed"]
    mapping["add_widget_to_view"].call({"view_id": "view-001"})
    after = {**before, "fixture_state": _fixture_state(mapping)}
    assert _apply_judge_gate(score_replay(after, expected), {"total": 100}, 60)["passed"]


def test_verification_and_discovery_have_real_tool_argument_shapes():
    mapping = tools()
    assert "container_memory_working_set_bytes" in mapping["discover_metrics"].call({"category": "memory"})
    assert "PASS" in mapping["verify_query"].call({"query": QUERY})
    assert mapping["verify_query"].call({"query": QUERY, "namespace": "production"}) == no_recording_message(
        "verify_query"
    )
    assert len(view(mapping)["widgets"]) == 5


@pytest.mark.parametrize(
    "state", [{}, {"memory_widget_added": "true", "view_widget_count": 6}, {"memory_widget_added": True}]
)
def test_missing_or_malformed_completion_cannot_be_overridden_by_judge(state):
    from sre_agent.evals.replay import score_replay
    from sre_agent.evals.replay_cli import _apply_judge_gate

    result = {
        "response": "Memory added",
        "tool_calls": [{"name": "get_prometheus_query"}, {"name": "add_widget_to_view"}],
        "fixture_state": state,
    }
    assert not _apply_judge_gate(score_replay(result, load_fixture("view_add_widget")["expected"]), {"total": 100}, 60)[
        "passed"
    ]


def test_successful_state_without_add_tool_evidence_cannot_pass():
    from sre_agent.evals.replay import score_replay
    from sre_agent.evals.replay_cli import _apply_judge_gate

    result = {
        "response": "Memory added",
        "tool_calls": [{"name": "get_prometheus_query"}],
        "fixture_state": {"memory_widget_added": True, "view_widget_count": 6},
    }
    assert not _apply_judge_gate(score_replay(result, load_fixture("view_add_widget")["expected"]), {"total": 100}, 60)[
        "passed"
    ]


def test_nullable_unused_chart_metadata_and_range_follow_production_defaults():
    mapping = tools()
    response = json.loads(
        mapping["get_prometheus_query"].call({"query": QUERY, "title": None, "description": None, "time_range": None})
    )
    assert response["component"]["title"] == "Memory Usage by namespace"
    assert response["component"]["description"] is None
    assert response["time_range"] == "1h"
