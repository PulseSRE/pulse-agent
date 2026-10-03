"""Simulation records the confirmation callback rather than assuming its gate ran."""

import json

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
