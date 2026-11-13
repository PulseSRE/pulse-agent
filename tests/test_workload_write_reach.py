"""The workload write tools are reachable from the sre skill and survive the 50-tool cap.

"raise the memory limit on X" used to reach only apply_yaml (a full manifest
rewrite) and "restart the statefulset X" only restart_deployment, which cannot
restart a StatefulSet (JTBD F5, E7).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sre_agent.skill_loader import MAX_TOOL_BUDGET, build_config_from_skill, get_skill, load_skills, select_tools
from sre_agent.tool_categories import MODE_CATEGORIES, TOOL_CATEGORIES

CASES = [
    ("raise the memory limit on payments-api", "patch_workload_container"),
    ("raise the memory limit on X", "patch_workload_container"),
    ("the api container keeps getting OOMKilled, bump its memory limit to 1Gi", "patch_workload_container"),
    ("give the startup probe on the checkout deployment more time", "patch_workload_container"),
    ("add the quay-creds pull secret to the worker statefulset", "patch_workload_container"),
    ("restart the statefulset postgres", "restart_workload"),
    ("restart the statefulset X", "restart_workload"),
    ("scale the kafka statefulset to 5 replicas", "scale_workload"),
    ("roll back the fluentd daemonset to the previous revision", "rollback_workload"),
]

_MCP = [{"name": f"mcp_extra_tool_{i}"} for i in range(40)]


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name

    def to_dict(self) -> dict:
        return {"name": self.name}


def test_tools_are_in_the_sre_skills_categories():
    load_skills()
    skill = get_skill("sre")
    assert skill is not None
    names: set[str] = set()
    for cat in skill.categories:
        names.update(TOOL_CATEGORIES.get(cat, {}).get("tools", []))
    mode_names: set[str] = set()
    for cat in MODE_CATEGORIES["sre"] or []:
        mode_names.update(TOOL_CATEGORIES[cat]["tools"])
    for tool in ("patch_workload_container", "scale_workload", "restart_workload", "rollback_workload"):
        assert tool in names and tool in mode_names


@pytest.mark.parametrize(("query", "tool"), CASES)
def test_query_routes_to_the_sre_skill(query, tool):
    from sre_agent.skill_router import classify_query

    load_skills()
    assert classify_query(query).name == "sre"


@pytest.mark.parametrize(("query", "tool"), CASES)
def test_sre_mode_keeps_the_tool_under_the_budget_cap(query, tool):
    from sre_agent.agent import TOOL_MAP

    mcp_tools = [_FakeTool(t["name"]) for t in _MCP]
    all_tools = list(TOOL_MAP.values()) + mcp_tools
    tool_map = {t.name: t for t in all_tools}
    with patch("sre_agent.mcp_client.list_mcp_tools", return_value=_MCP):
        _, _, offered = select_tools(query, all_tools, tool_map, "sre")
    assert len(offered) == MAX_TOOL_BUDGET
    assert tool in offered


@pytest.mark.parametrize(("query", "tool"), CASES)
def test_sre_skill_config_offers_the_tool(query, tool):
    from sre_agent.tool_predictor import PredictionResult

    load_skills()
    skill = get_skill("sre")
    assert skill is not None
    with (
        patch("sre_agent.tool_predictor.predict_tools", return_value=PredictionResult(tools=[], confidence="low")),
        patch("sre_agent.tool_predictor.llm_pick_tools", return_value=[]),
    ):
        config = build_config_from_skill(skill, query=query)
    assert tool in config["tool_map"]
    assert tool in config["write_tools"]
