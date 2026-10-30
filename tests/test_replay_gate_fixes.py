"""Two causes of the live investigation replay being red on main.

1. Monitor plan phases were offered write tools they could never use (no
   confirmation callback), and an image-pull diagnosis reached for one.
2. A node-pressure diagnosis could stop at "the disk is full" without saying
   what filled it.
"""

from __future__ import annotations

from unittest.mock import MagicMock


def _config():
    return {
        "system_prompt": "s",
        "tool_defs": [{"name": "get_events"}, {"name": "test_connectivity"}, {"name": "delete_pod"}],
        "tool_map": {"get_events": MagicMock(), "test_connectivity": MagicMock(), "delete_pod": MagicMock()},
        "write_tools": {"delete_pod"},
    }


def test_a_phase_that_cannot_approve_writes_is_not_offered_them():
    from sre_agent.plan_runtime import _without_unapprovable_writes
    from sre_agent.tool_discovery import discover_tools

    discover_tools()
    config = _without_unapprovable_writes(_config(), can_approve=False)
    # test_connectivity is a registered write even though the skill did not list it.
    assert [d["name"] for d in config["tool_defs"]] == ["get_events"]
    assert set(config["tool_map"]) == {"get_events"}
    assert config["write_tools"] == set()


def test_a_phase_that_can_approve_writes_keeps_them():
    from sre_agent.plan_runtime import _without_unapprovable_writes

    config = _config()
    assert _without_unapprovable_writes(config, can_approve=True) is config


def test_monitor_plan_phases_run_without_write_tools(monkeypatch):
    """PlanRuntime with no on_confirm (how the monitor builds it) hands the agent no writes."""
    import asyncio
    from types import SimpleNamespace

    import sre_agent.agent as agent_mod
    from sre_agent.plan_runtime import PlanRuntime
    from sre_agent.tool_discovery import discover_tools

    discover_tools()
    seen: dict = {}

    async def fake_run(client, messages, system_prompt, tool_defs, tool_map, write_tools, **kwargs):
        seen["tools"] = set(tool_map)
        seen["write_tools"] = set(write_tools)
        return '```json\n{"findings": {}, "evidence": [], "confidence": 0.1}\n```'

    monkeypatch.setattr(agent_mod, "run_agent_streaming", fake_run)
    monkeypatch.setattr("sre_agent.skill_loader.build_config_from_skill", lambda skill, query="": _config())
    monkeypatch.setattr("sre_agent.skill_loader.get_skill", lambda name: SimpleNamespace(name=name))

    class _Client:
        async def close(self):
            return None

    phase = SimpleNamespace(id="diagnose", skill_name="sre", produces=[], timeout_seconds=30)
    asyncio.run(PlanRuntime(client=_Client())._run_phase_once(phase, {"id": "f1"}, {}))
    assert seen["tools"] == {"get_events"}
    assert seen["write_tools"] == set()


def test_node_diagnosis_must_name_what_is_using_the_resource():
    from sre_agent.plan_templates import match_template

    template = match_template(category="nodes")
    diagnostics = next(p for p in template.phases if p.id == "node_diagnostics")
    assert "top_consumer" in diagnostics.produces
    assert "root_cause" in diagnostics.produces
