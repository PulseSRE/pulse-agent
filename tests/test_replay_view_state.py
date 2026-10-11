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
    recorded = copy.deepcopy(load_fixture("view_add_widget")["recorded_responses"])
    case = recorded["add_widget_to_view"]["cases"][0]
    if mode == "failed":
        case.update(succeeded=False, response="Frontend rejected action")
    elif mode == "ambiguous":
        recorded["add_widget_to_view"]["cases"].append(copy.deepcopy(case))
    else:
        case.pop("succeeded")
    mapping = tools(recorded)
    generate_component(mapping)
    if mode == "failed":
        assert mapping["add_widget_to_view"].call({"view_id": "view-001"}) == "Frontend rejected action"
    else:
        with pytest.raises(ValueError):
            mapping["add_widget_to_view"].call({"view_id": "view-001"})
    assert len(view(mapping)["widgets"]) == 5


def test_recording_data_unchanged_and_legacy_values_preserved():
    recorded = load_fixture("view_add_widget")["recorded_responses"]
    original = copy.deepcopy(recorded)
    mapping = tools(recorded)
    generate_component(mapping)
    mapping["add_widget_to_view"].call({"view_id": "view-001"})
    assert recorded == original
    legacy = shadow_tool_map({}, {"get_view_details": "old static recording"})
    assert legacy["get_view_details"].call({"view_id": "anything"}) == "old static recording"
