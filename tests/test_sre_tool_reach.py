"""Tools the sre skill is told to use must be reachable from it, and survive the budget cut.

`get_tls_certificates` was only in the security category and `list_deprecated_apis`
only a self-describe tool, so a cert-expiry or removed-API question answered by
the sre skill (the upgrade readiness panel's "Ask Pulse", a Route failing TLS)
could not call them, although `skills/sre/skill.md` tells the model to call
`list_deprecated_apis`.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sre_agent.skill_loader import MAX_TOOL_BUDGET, build_config_from_skill, get_skill, load_skills, select_tools
from sre_agent.tool_categories import MODE_CATEGORIES, TOOL_CATEGORIES

CERT_QUERY = "Which route certificates are expiring soon? Check the TLS certs before they break the app."
REMOVED_API_QUERY = "Which APIs will be removed in the next upgrade, and is anything still using them?"
PENDING_PLANS_QUERY = "Which operator updates are waiting for approval?"
APPROVE_PLAN_QUERY = "Approve the pending InstallPlan for the elasticsearch operator in openshift-operators"


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name

    def to_dict(self) -> dict:
        return {"name": self.name}


# Enough MCP tools to push the sre mode well past MAX_TOOL_BUDGET, as a cluster
# with every MCP toolset enabled does (36 MCP tools at the time of writing).
_MCP = [{"name": f"mcp_extra_tool_{i}"} for i in range(40)]


def _sre_category_tools() -> set[str]:
    names: set[str] = set()
    for cat in MODE_CATEGORIES["sre"] or []:
        names.update(TOOL_CATEGORIES[cat]["tools"])
    return names


def test_tools_are_in_sre_categories():
    names = _sre_category_tools()
    assert "get_tls_certificates" in names
    assert "list_deprecated_apis" in names
    load_skills()
    skill = get_skill("sre")
    assert skill is not None
    skill_names: set[str] = set()
    for cat in skill.categories:
        skill_names.update(TOOL_CATEGORIES.get(cat, {}).get("tools", []))
    assert {"get_tls_certificates", "list_deprecated_apis"} <= skill_names
    assert {"list_pending_installplans", "approve_installplan"} <= names
    assert {"list_pending_installplans", "approve_installplan"} <= skill_names


def test_security_still_reaches_tls_certificates():
    assert "get_tls_certificates" in TOOL_CATEGORIES["security"]["tools"]


@pytest.mark.parametrize(
    ("query", "tool"),
    [
        (CERT_QUERY, "get_tls_certificates"),
        (REMOVED_API_QUERY, "list_deprecated_apis"),
        (PENDING_PLANS_QUERY, "list_pending_installplans"),
        (APPROVE_PLAN_QUERY, "approve_installplan"),
        (APPROVE_PLAN_QUERY, "list_pending_installplans"),
    ],
)
def test_sre_mode_keeps_the_tool_under_the_budget_cap(query, tool):
    from sre_agent.agent import TOOL_MAP

    mcp_tools = [_FakeTool(t["name"]) for t in _MCP]
    all_tools = list(TOOL_MAP.values()) + mcp_tools
    tool_map = {t.name: t for t in all_tools}
    with patch("sre_agent.mcp_client.list_mcp_tools", return_value=_MCP):
        _, _, offered = select_tools(query, all_tools, tool_map, "sre")
    # The cap really applied: this checks ranking, not an under-budget pass-through.
    assert len(offered) == MAX_TOOL_BUDGET
    assert tool in offered


@pytest.mark.parametrize(
    ("query", "tool"),
    [
        (CERT_QUERY, "get_tls_certificates"),
        (REMOVED_API_QUERY, "list_deprecated_apis"),
        (PENDING_PLANS_QUERY, "list_pending_installplans"),
        (APPROVE_PLAN_QUERY, "approve_installplan"),
        (APPROVE_PLAN_QUERY, "list_pending_installplans"),
    ],
)
def test_sre_skill_config_offers_the_tool(query, tool):
    """The orchestrated /ws/agent path: the sre skill scoped to its categories."""
    from sre_agent.tool_predictor import PredictionResult

    load_skills()
    skill = get_skill("sre")
    assert skill is not None
    # No learned prediction and no LLM pick: the predictor falls back to the
    # skill's categories, which now hold the tool.
    with (
        patch("sre_agent.tool_predictor.predict_tools", return_value=PredictionResult(tools=[], confidence="low")),
        patch("sre_agent.tool_predictor.llm_pick_tools", return_value=[]),
    ):
        config = build_config_from_skill(skill, query=query)
    assert tool in config["tool_map"]


def test_slo_skill_points_to_service_levels():
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "sre_agent/skills/slo-management/skill.md").read_text()
    assert "Settings > SLOs" not in text
    assert "Service Levels" in text and "/slo" in text
