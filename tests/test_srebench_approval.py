"""Simulation records the confirmation callback rather than assuming its gate ran."""

import json

import pytest

from sre_agent.evals.srebench_adapter import _SimBackedTool


class ObservingBackend:
    def __init__(self):
        self.missing_confirmation = False

    def call(self, tool_name, **args):
        if args.get("confirmed") is not True:
            self.missing_confirmation = True
            return {"error": "confirmation required"}
        return {"ok": True}


def test_gate_bypass_cannot_forge_approval_in_input():
    backend = ObservingBackend()
    tool = _SimBackedTool({"name": "delete_pod"}, backend, True)
    assert "error" in json.loads(tool.call({"name": "pod", "confirmed": True}))
    assert backend.missing_confirmation


def test_observed_callback_authorizes_exactly_one_matching_call():
    backend = ObservingBackend()
    tool = _SimBackedTool({"name": "delete_pod"}, backend, True)
    tool.approve({"name": "pod"})
    assert json.loads(tool.call({"name": "pod"})) == {"ok": True}
    assert not backend.missing_confirmation
    assert "error" in json.loads(tool.call({"name": "pod"}))
    assert backend.missing_confirmation


def test_approval_for_another_resource_does_not_authorize_write():
    backend = ObservingBackend()
    tool = _SimBackedTool({"name": "delete_pod"}, backend, True)
    tool.approve({"name": "approved-pod"})
    assert "error" in json.loads(tool.call({"name": "other-pod"}))
    assert backend.missing_confirmation


@pytest.mark.parametrize("tool_name", ["describe_pod", "get_pod_logs", "delete_pod"])
def test_native_pod_identity_reaches_canonical_simulator(tool_name):
    from unittest.mock import Mock

    backend = Mock()
    backend.call.return_value = {"name": "api-123", "namespace": "production"}
    write = tool_name == "delete_pod"
    tool = _SimBackedTool({"name": tool_name}, backend, write)
    inputs = {"namespace": "production", "pod_name": "api-123"}
    if write:
        tool.approve(inputs)
    result = json.loads(tool.call(inputs))
    assert result["name"] == "api-123"
    expected = {"namespace": "production", "name": "api-123"}
    if write:
        expected["confirmed"] = True
    backend.call.assert_called_once_with(tool_name, **expected)
    assert inputs == {"namespace": "production", "pod_name": "api-123"}


def test_conflicting_pod_identity_cannot_retarget_simulation():
    from unittest.mock import Mock

    backend = Mock()
    tool = _SimBackedTool({"name": "delete_pod"}, backend, True)
    inputs = {"name": "wrong", "pod_name": "requested"}
    tool.approve(inputs)
    with pytest.raises(ValueError, match="conflicting pod identity"):
        tool.call(inputs)
    backend.call.assert_not_called()
