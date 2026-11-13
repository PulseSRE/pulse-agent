"""grant_access and revoke_access are reachable from the sre skill and survive the 50-tool cap.

"give alice edit on payments" used to route to the sre skill, whose apply_yaml
refuses RoleBindings, and "remove bob from the payments rolebinding" to the
security skill, which cannot write at all (JTBD J2, J3, W1).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sre_agent.skill_loader import MAX_TOOL_BUDGET, build_config_from_skill, get_skill, load_skills, select_tools
from sre_agent.tool_categories import MODE_CATEGORIES, TOOL_CATEGORIES

CASES = [
    ("give alice edit on payments", "grant_access"),
    ("grant alice the edit role in payments", "grant_access"),
    ("give the ci service account view access to staging", "grant_access"),
    ("add alice as admin of namespace checkout", "grant_access"),
    ("grant cluster-admin to carol", "grant_access"),
    ("remove bob's access", "revoke_access"),
    ("revoke bob's access to payments", "revoke_access"),
    ("remove bob from the payments rolebinding", "revoke_access"),
    ("offboard bob, he left the company", "revoke_access"),
]

# Read questions keep their routes: these must not be taken for grants.
NOT_GRANTS = [
    ("give me a view of the pods in payments", "view_designer"),
    ("scan rbac for overpermissive bindings", "security"),
    ("show RBAC changes from the last 24 hours, who granted cluster-admin?", "security"),
]

_MCP = [{"name": f"mcp_extra_tool_{i}"} for i in range(40)]


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name

    def to_dict(self) -> dict:
        return {"name": self.name}


def test_tools_are_in_the_sre_skills_categories_only():
    load_skills()
    skill = get_skill("sre")
    assert skill is not None and skill.write_tools
    names: set[str] = set()
    for cat in skill.categories:
        names.update(TOOL_CATEGORIES.get(cat, {}).get("tools", []))
    mode_names: set[str] = set()
    for cat in MODE_CATEGORIES["sre"] or []:
        mode_names.update(TOOL_CATEGORIES[cat]["tools"])
    for tool in ("grant_access", "revoke_access"):
        assert tool in names and tool in mode_names
        # The security category feeds a skill that cannot write.
        assert tool not in TOOL_CATEGORIES["security"]["tools"]


@pytest.mark.parametrize(("query", "tool"), CASES)
def test_query_routes_to_the_sre_skill(query, tool):
    from sre_agent.skill_router import classify_query

    load_skills()
    assert classify_query(query).name == "sre"


@pytest.mark.parametrize(("query", "skill"), NOT_GRANTS)
def test_read_questions_keep_their_route(query, skill):
    from sre_agent.skill_router import classify_query

    load_skills()
    assert classify_query(query).name == skill


def test_security_hands_grants_and_revokes_to_sre():
    from sre_agent.skill_router import check_handoff

    load_skills()
    security = get_skill("security")
    for query in (
        "revoke the overpermissive binding for bob",
        "grant the auditor read access",
        "remove access for bob",
    ):
        target = check_handoff(security, query)
        assert target is not None and target.name == "sre"


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
def test_sre_skill_config_offers_the_tool_as_a_write(query, tool):
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
