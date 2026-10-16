"""The view designer can reach every widget it advertises, and evals can tell.

Three gaps these guard: the skill listed widget kinds that do not exist
(donut_chart, summary_bar); its prompt never said when to use most of the
widgets it is allowed, so recorded runs only ever produced the defaults that
data tools emit; and the node_map registry example omitted fields the UI
dereferences, so specs copied from it crashed the widget.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from sre_agent.component_registry import COMPONENT_REGISTRY
from sre_agent.evals.replay import ReplayHarness, score_replay
from sre_agent.evals.replay_cli import _expected_for
from sre_agent.quality_engine import VALID_KINDS

SKILL_MD = Path(__file__).resolve().parents[1] / "sre_agent" / "skills" / "view-designer" / "skill.md"


def _skill() -> tuple[dict, str]:
    _, frontmatter, body = SKILL_MD.read_text().split("---", 2)
    return yaml.safe_load(frontmatter), body


class TestAdvertisedWidgets:
    def test_every_supported_kind_exists(self):
        meta, _ = _skill()
        unknown = [k for k in meta["supported_components"] if k not in VALID_KINDS]
        assert unknown == [], f"skill advertises kinds the agent rejects: {unknown}"

    def test_every_supported_kind_has_prompt_guidance(self):
        """A kind the prompt never describes is a kind the model never picks."""
        meta, body = _skill()
        missing = [k for k in meta["supported_components"] if f"`{k}`" not in body]
        assert missing == [], f"no guidance in skill.md for: {missing}"

    def test_relationship_tree_tool_is_always_offered(self):
        meta, _ = _skill()
        assert "get_resource_relationships" in meta["requires_tools"]


class TestRegistryExamples:
    def test_node_map_example_has_fields_the_ui_reads(self):
        # pulse-ui AgentNodeMap/NodeHexMap read these on every node.
        node = COMPONENT_REGISTRY["node_map"].example["nodes"][0]
        assert isinstance(node["roles"], list)
        assert isinstance(node["podCount"], int)
        assert isinstance(node["podCap"], int)
        assert node["status"] in {"ready", "not-ready", "pressure", "cordoned"}


def _result(emitted: list[str]) -> dict:
    return {"response": "done", "tool_calls": [{"name": "emit_component"}], "emitted_kinds": emitted}


class TestShouldEmitKinds:
    def test_passes_when_kind_emitted(self):
        score = score_replay(_result(["bar_list", "progress_list"]), {"should_emit_kinds": ["bar_list"]})
        assert score["passed"] is True

    def test_fails_when_kind_missing(self):
        score = score_replay(_result(["chart"]), {"should_emit_kinds": ["bar_list"]})
        assert score["passed"] is False
        assert any(c["check"] == "emitted widget 'bar_list'" and not c["passed"] for c in score["checks"])

    def test_dry_run_drops_the_check(self):
        """The dry-run mock synthesizes emit_component input, so it cannot judge widget choice."""
        trimmed = _expected_for({"should_emit_kinds": ["tabs"], "should_use_tools": ["emit_component"]}, True)
        assert "should_emit_kinds" not in trimmed
        assert trimmed["should_use_tools"] == ["emit_component"]


async def _agent_emitting(**kwargs):
    for kind in ("bar_list", "progress_list"):
        await kwargs["on_tool_use"]("emit_component")
        await kwargs["on_tool_result"](
            {"tool_name": "emit_component", "input": {"kind": kind, "spec_json": "{}"}, "status": "success"}
        )
    await kwargs["on_tool_use"]("list_pods")
    await kwargs["on_tool_result"]({"tool_name": "list_pods", "input": {"namespace": "x"}, "status": "success"})
    return "built"


@pytest.mark.asyncio
async def test_replay_records_kinds_requested_through_emit_component():
    with patch("sre_agent.evals.replay.run_agent_streaming", side_effect=_agent_emitting):
        result = await ReplayHarness({}, stub_config=True).run_async(None, "synthetic")
    assert result["emitted_kinds"] == ["bar_list", "progress_list"]


class TestExplicitDashboardRouting:
    """An explicit "build a dashboard" request reaches the view designer even
    when it describes an incident. These prompts used to route to the SRE skill
    via its pod-crash trigger, where create_dashboard is not offered."""

    @pytest.mark.parametrize(
        "query",
        [
            "Create an incident triage dashboard for the api-server pod that's been crashing",
            "Create an incident investigation dashboard for the payment-api pod that's crashlooping.",
            "Build me a capacity dashboard for production",
            "Pods look bad. Build a dashboard for them.",
        ],
    )
    def test_routes_to_view_designer(self, query):
        from sre_agent.skill_router import classify_query, offline_routing

        with offline_routing():
            assert classify_query(query).name == "view_designer"

    @pytest.mark.parametrize(
        "query",
        [
            "why is my pod crashlooping?",
            "The build failed, view the logs",
            "I want to make sure the pods view is right before deploy",
            "create a plan to fix the crashlooping pod",
        ],
    )
    def test_incidental_words_do_not_trigger(self, query):
        from sre_agent.skill_router import is_dashboard_request

        assert not is_dashboard_request(query)
