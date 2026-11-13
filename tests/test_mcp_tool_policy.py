"""MCP tools are offered read-only; every sidecar tool is classified."""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from sre_agent.mcp_client import MCPConnection, call_mcp_tool, list_mcp_connections, register_mcp_tools
from sre_agent.mcp_tool_policy import (
    ANNOTATED_WRITE_REASON,
    MCP_READ_TOOLS,
    MCP_WITHHELD_TOOLS,
    UNKNOWN_REASON,
    is_mcp_read_only,
    mcp_withheld_reason,
)
from sre_agent.tool_registry import TOOL_REGISTRY, WRITE_TOOL_NAMES, unregister_tool

_ROOT = Path(__file__).parent.parent
_INVENTORY = json.loads((_ROOT / "tests" / "fixtures" / "mcp_sidecar_tools.json").read_text())
_SIDECAR = {t["name"]: t for t in _INVENTORY["tools"]}


def test_inventory_matches_the_pinned_sidecar_build():
    workflow = (_ROOT / ".github" / "workflows" / "build-push.yml").read_text()
    ref = re.search(r"MCP_SERVER_REF:\s*([0-9a-f]{40})", workflow)
    assert ref, "MCP_SERVER_REF not found in build-push.yml"
    assert _INVENTORY["server_ref"] == ref.group(1), (
        "MCP_SERVER_REF moved: regenerate tests/fixtures/mcp_sidecar_tools.json with "
        "scripts/mcp_tool_inventory.py and classify any new tool in sre_agent/mcp_tool_policy.py"
    )
    assert len(_SIDECAR) > 100


def test_every_sidecar_tool_is_classified_exactly_once():
    read, withheld = MCP_READ_TOOLS, set(MCP_WITHHELD_TOOLS)
    assert not read & withheld
    unclassified = sorted(set(_SIDECAR) - read - withheld)
    assert not unclassified, f"classify these MCP tools in sre_agent/mcp_tool_policy.py: {unclassified}"
    stale = sorted((read | withheld) - set(_SIDECAR))
    assert not stale, f"no longer exposed by the sidecar: {stale}"


def test_sidecar_tools_not_annotated_read_only_are_withheld():
    # The read list may only narrow upstream's own annotations, never widen them.
    for name, tool in _SIDECAR.items():
        if tool["readOnlyHint"] is not True or tool["destructiveHint"] is True:
            assert name in MCP_WITHHELD_TOOLS, name


@pytest.mark.parametrize(
    "name",
    [
        "helm_install",
        "helm_uninstall",
        "resources_create_or_update",
        "resources_delete",
        "resources_scale",
        "pods_delete",
        "pods_run",
        "pods_exec",
        "nodes_debug_exec",
        "exec_dns_in_pod",
    ],
)
def test_named_writes_and_execs_are_withheld(name):
    assert name in _SIDECAR
    assert not is_mcp_read_only(name, {"readOnlyHint": True, "destructiveHint": False})


@pytest.mark.parametrize("name", ["get_router_config", "ovn_trace", "ovs_vsctl", "tcpdump", "get-iptables"])
def test_exec_tools_annotated_read_only_upstream_are_still_withheld(name):
    assert _SIDECAR[name]["readOnlyHint"] is True
    assert "pod" in MCP_WITHHELD_TOOLS[name]
    assert not is_mcp_read_only(name, {"readOnlyHint": True})


@pytest.mark.parametrize(
    "name",
    [
        "helm_list",
        "nodes_log",
        "nodes_stats_summary",
        "pods_top",
        "resources_get",
        "resources_list",
        "inspect_route",
        "probe_dns_local",
        "probe_http",
        "get_coredns_config",
    ],
)
def test_reads_stay_available(name):
    assert is_mcp_read_only(name, _annotations(name))


@pytest.mark.parametrize(
    "annotations,reason",
    [
        (None, UNKNOWN_REASON),
        ({}, UNKNOWN_REASON),
        ({"readOnlyHint": "true"}, UNKNOWN_REASON),
        ("read", UNKNOWN_REASON),
        ({"readOnlyHint": False}, ANNOTATED_WRITE_REASON),
        ({"readOnlyHint": True, "destructiveHint": True}, ANNOTATED_WRITE_REASON),
        ({"readOnlyHint": True}, None),
    ],
)
def test_unknown_tools_fail_closed(annotations, reason):
    assert mcp_withheld_reason("custom_server_tool", annotations) == reason


def test_listed_read_is_withheld_when_server_marks_it_mutating():
    assert mcp_withheld_reason("resources_get", {"readOnlyHint": False}) == ANNOTATED_WRITE_REASON


def _annotations(name: str) -> dict:
    tool = _SIDECAR[name]
    return {"readOnlyHint": tool["readOnlyHint"], "destructiveHint": tool["destructiveHint"]}


def _sidecar_conn() -> MCPConnection:
    names = list(_SIDECAR)
    return MCPConnection(
        name="openshift-mcp-test",
        url="none",
        transport="sse",
        toolsets=[],
        connected=True,
        tools=names,
        tool_schemas={n: {"annotations": _annotations(n)} for n in names},
    )


def test_registering_the_sidecar_offers_reads_only():
    conn = _sidecar_conn()
    native = {n for n in MCP_READ_TOOLS if n in TOOL_REGISTRY}  # MCP never replaces a native tool
    try:
        register_mcp_tools(conn)
        registered = set(conn.tools)
        assert registered == MCP_READ_TOOLS - native
        assert not registered & set(MCP_WITHHELD_TOOLS)
        assert set(conn.withheld_tools) == set(MCP_WITHHELD_TOOLS)
        for name in MCP_WITHHELD_TOOLS:
            assert name not in TOOL_REGISTRY
            assert name not in WRITE_TOOL_NAMES
        # Every MCP tool that is offered is a read: none needs confirmation.
        assert not registered & WRITE_TOOL_NAMES
    finally:
        for name in [*conn.tools, *conn.withheld_tools]:
            existing = TOOL_REGISTRY.get(name)
            if getattr(existing, "connection", None) is conn:
                unregister_tool(name)


def test_withheld_tools_are_reported_for_the_toolbox():
    from sre_agent import mcp_client

    conn = _sidecar_conn()
    try:
        register_mcp_tools(conn)
        with patch.dict(mcp_client._connections, {"sre": conn}, clear=True):
            [entry] = list_mcp_connections()
        withheld = {w["name"]: w["reason"] for w in entry["withheld_tools"]}
        assert withheld["helm_install"] == MCP_WITHHELD_TOOLS["helm_install"]
        assert "pods_exec" in withheld
        assert "helm_install" not in entry["tools"]
    finally:
        for name in conn.tools:
            unregister_tool(name)


def test_reregistration_keeps_withheld_tools_out():
    conn = _sidecar_conn()
    try:
        register_mcp_tools(conn)
        register_mcp_tools(conn)
        assert "helm_install" in conn.withheld_tools
        assert "helm_install" not in TOOL_REGISTRY
    finally:
        for name in conn.tools:
            unregister_tool(name)


def test_withheld_tool_cannot_be_called_directly():
    conn = MCPConnection(name="x", url="none", transport="sse", toolsets=[], connected=True)
    with patch("sre_agent.mcp_client._mcp_post") as post:
        out = call_mcp_tool(conn, "pods_exec", {"name": "p", "command": ["sh"]})
    post.assert_not_called()
    assert "disabled" in out


def test_no_skill_is_offered_an_mcp_write():
    from sre_agent.skill_loader import build_config_from_skill, list_skills

    conn = _sidecar_conn()
    try:
        register_mcp_tools(conn)
        listed = [{"name": n} for n in _SIDECAR]
        with patch("sre_agent.mcp_client.list_mcp_tools", return_value=listed):
            for skill in list_skills():
                cfg = build_config_from_skill(skill)
                offered = set(cfg["tool_map"])
                assert not offered & set(MCP_WITHHELD_TOOLS), skill.name
    finally:
        for name in conn.tools:
            unregister_tool(name)
