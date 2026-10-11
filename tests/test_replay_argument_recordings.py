"""Synthetic replay snapshots must match the resource/scope actually requested."""

import json
import re

import pytest

from sre_agent.evals.replay import _unrecorded_calls, load_fixture
from sre_agent.evals.replay_config import RecordedTool, no_recording_message, shadow_tool_map


@pytest.fixture
def tools():
    fixture = load_fixture("integration_daemonset_gap")
    return shadow_tool_map({}, fixture["recorded_responses"])


@pytest.mark.parametrize("resource", ["daemonsets", "DaemonSet", "ds"])
@pytest.mark.parametrize("namespace", ["", "ALL", "monitoring"])
def test_daemonset_selector_returns_daemonsets_not_nodes(tools, resource, namespace):
    data = json.loads(tools["list_resources"].call({"resource": resource, "namespace": namespace, "group": "apps"}))
    assert {row["metadata"]["name"] for row in data["items"]} == {"node-exporter", "fluentd"}
    assert all(row["metadata"]["namespace"] == "monitoring" for row in data["items"])
    assert not tools["list_resources"].missing


@pytest.mark.parametrize(
    "inputs",
    [
        {"resource": "daemonsets", "namespace": "production"},
        {"resource": "daemonsets", "label_selector": "unknown=label"},
        {"resource": "daemonsets", "group": "unknown.example"},
        {"resource": "services"},
        {},
    ],
)
def test_unrecorded_arguments_are_explicitly_unavailable(tools, inputs):
    result = tools["list_resources"].call(inputs)
    assert result == no_recording_message("list_resources")
    assert _unrecorded_calls(tools) == ["list_resources"]
    # A later valid call must not hide the earlier coverage gap.
    assert "items" in json.loads(tools["list_resources"].call({"resource": "ds"}))
    assert _unrecorded_calls(tools) == ["list_resources"]


@pytest.mark.parametrize("name", ["list_pods", "get_events"])
def test_default_namespace_cannot_expose_monitoring_snapshots(tools, name):
    assert tools[name].call({}) == no_recording_message(name)
    assert tools[name].call({"namespace": "default"}) == no_recording_message(name)
    assert tools[name].call({"namespace": "production"}) == no_recording_message(name)
    assert "node-exporter" in tools[name].call({"namespace": "monitoring"})
    assert "node-exporter" in tools[name].call({"namespace": "ALL"})


def test_synthetic_snapshot_consistency_and_distinct_node_pod_event_evidence(tools):
    fixture = load_fixture("integration_daemonset_gap")
    assert fixture["fixture_origin"].startswith("synthetic")
    nodes = tools["list_resources"].call({"resource": "no", "namespace": "ALL"})
    node_names = set(re.findall(r"worker-\d", nodes))
    assert len(node_names) == 6
    daemonsets = json.loads(tools["list_resources"].call({"resource": "ds"}))["items"]
    by_name = {row["metadata"]["name"]: row for row in daemonsets}
    for app, wanted in [("node-exporter", 4), ("fluentd", 6)]:
        pod_rows = tools["list_pods"].call({"namespace": "monitoring", "label_selector": f"app={app}"})
        assert len(pod_rows.splitlines()) == wanted
        assert set(re.findall(r"worker-\d", pod_rows)) <= node_names
        assert by_name[app]["status"]["currentNumberScheduled"] == wanted
        assert by_name[app]["status"]["numberReady"] == wanted
    # All-node coverage and controller eligibility are distinct: exporter does
    # not tolerate worker-3's NoSchedule taint, so its desired count excludes it.
    assert by_name["node-exporter"]["status"]["desiredNumberScheduled"] == 5
    assert by_name["node-exporter"]["spec"]["template"]["spec"]["tolerations"] == []
    assert by_name["fluentd"]["status"]["desiredNumberScheduled"] == 6
    assert by_name["fluentd"]["spec"]["template"]["spec"]["tolerations"][0]["key"] == "dedicated"
    assert "FailedScheduling" in tools["list_resources"].call({"resource": "events", "namespace": "monitoring"})
    assert "node-exporter" in tools["list_resources"].call({"resource": "pods", "namespace": "monitoring"})


def test_legacy_recording_and_real_tool_shadowing_are_preserved():
    class RealTool:
        def call(self, _):
            raise AssertionError("live tool execution")

    shadowed = shadow_tool_map({"list_pods": RealTool()}, {"list_pods": "legacy snapshot"})
    assert shadowed["list_pods"].call({"namespace": "test"}) == "legacy snapshot"


@pytest.mark.parametrize("cases", [[], [{"input": {}}]])
def test_malformed_argument_recording_fails_closed(cases):
    tool = RecordedTool("list_resources", {"recording_kind": "argument_cases", "cases": cases})
    with pytest.raises(ValueError):
        tool.call({"resource": "nodes"})


def test_ambiguous_alias_cases_fail_closed():
    tool = RecordedTool(
        "list_resources",
        {
            "recording_kind": "argument_cases",
            "cases": [
                {"input": {"resource": "daemonsets"}, "response": "one"},
                {"input": {"resource": "ds"}, "response": "two"},
            ],
        },
    )
    with pytest.raises(ValueError, match="Ambiguous"):
        tool.call({"resource": "daemonset"})
