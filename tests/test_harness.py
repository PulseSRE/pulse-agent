"""Tests for harness.py — tool selection, prompt caching, cluster context."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from sre_agent.harness import (
    ALWAYS_INCLUDE,
    COMPONENT_HINT,
    TOOL_CATEGORIES,
    build_cached_system_prompt,
    get_cluster_context,
    select_tools,
)


def _make_tool(name: str):
    """Create a mock tool with a name and to_dict method."""
    tool = SimpleNamespace(name=name)
    tool.to_dict = lambda: {"name": name, "description": f"Tool {name}"}
    return tool


class TestToolCategories:
    def test_all_categories_have_keywords_and_tools(self):
        for cat, config in TOOL_CATEGORIES.items():
            assert "keywords" in config, f"{cat} missing keywords"
            assert "tools" in config, f"{cat} missing tools"
            assert len(config["keywords"]) > 0
            assert len(config["tools"]) > 0

    def test_always_include_set(self):
        assert "list_namespaces" in ALWAYS_INCLUDE
        assert "get_cluster_version" in ALWAYS_INCLUDE


class TestSelectTools:
    def _all_tools(self):
        """Build a mock tool list covering all categories + always-include."""
        names = set(ALWAYS_INCLUDE)
        for config in TOOL_CATEGORIES.values():
            names.update(config["tools"])
        tools = [_make_tool(n) for n in sorted(names)]
        tool_map = {t.name: t for t in tools}
        return tools, tool_map

    def test_diagnostics_query(self):
        all_tools, tool_map = self._all_tools()
        defs, selected = select_tools("check cluster health", all_tools, tool_map)
        assert "list_pods" in selected
        assert "get_events" in selected
        assert len(defs) == len(all_tools)  # All tools always returned

    def test_security_query(self):
        all_tools, tool_map = self._all_tools()
        _defs, selected = select_tools("run a security audit of rbac", all_tools, tool_map)
        assert "scan_rbac_risks" in selected
        assert "scan_pod_security" in selected

    def test_generic_query_returns_all(self):
        all_tools, tool_map = self._all_tools()
        _defs, selected = select_tools("hello world", all_tools, tool_map)
        assert len(selected) == len(all_tools)

    def test_always_include_present(self):
        all_tools, tool_map = self._all_tools()
        _defs, selected = select_tools("check pod status", all_tools, tool_map)
        for name in ALWAYS_INCLUDE:
            if name in tool_map:
                assert name in selected

    def test_diagnostics_includes_workloads(self):
        """When diagnostics is a top category, workload tools should be included."""
        all_tools, tool_map = self._all_tools()
        _defs, selected = select_tools("what's wrong with my cluster health", all_tools, tool_map)
        # diagnostics should pull in workloads
        assert "scale_deployment" in selected or "list_deployments" in selected

    def test_fleet_query(self):
        all_tools, tool_map = self._all_tools()
        _defs, selected = select_tools("compare across all clusters fleet", all_tools, tool_map)
        assert "fleet_list_clusters" in selected

    def test_storage_query(self):
        all_tools, tool_map = self._all_tools()
        _defs, selected = select_tools("check pvc storage volumes", all_tools, tool_map)
        assert "get_persistent_volume_claims" in selected

    def test_empty_tool_list(self):
        defs, selected = select_tools("anything", [], {})
        assert defs == []
        assert selected == {}

    def test_nonexistent_tools_filtered_out(self):
        """Tools named in categories but not in all_tools should be excluded."""
        tools = [_make_tool("list_namespaces")]
        tool_map = {"list_namespaces": tools[0]}
        _defs, selected = select_tools("check health status", tools, tool_map)
        # Only the available tool should be in selected
        assert len(selected) <= 1


class TestBuildCachedSystemPrompt:
    def test_base_only(self):
        blocks = build_cached_system_prompt("You are an SRE agent.")
        assert len(blocks) == 1
        assert blocks[0]["type"] == "text"
        assert blocks[0]["text"] == "You are an SRE agent."
        assert "cache_control" in blocks[0]

    def test_with_cluster_context(self):
        blocks = build_cached_system_prompt("Base prompt", cluster_context="Nodes: 3/3 Ready")
        assert len(blocks) == 2
        assert blocks[0]["cache_control"]["type"] == "ephemeral"
        assert blocks[1]["text"] == "Nodes: 3/3 Ready"
        assert "cache_control" not in blocks[1]

    def test_empty_cluster_context_ignored(self):
        blocks = build_cached_system_prompt("Base", cluster_context="")
        assert len(blocks) == 1


class TestGetClusterContext:
    def test_caches_result(self):
        import sre_agent.harness as h

        h._cluster_context_cache = ""
        h._cluster_context_ts = 0

        with patch("sre_agent.harness.gather_cluster_context", return_value="cached data") as mock_gather:
            result1 = get_cluster_context(max_age=60)
            result2 = get_cluster_context(max_age=60)
        assert result1 == "cached data"
        assert result2 == "cached data"
        assert mock_gather.call_count == 1

    def test_refreshes_when_stale(self):
        import sre_agent.harness as h

        h._cluster_context_cache = "old"
        h._cluster_context_ts = 0  # ancient timestamp

        with patch("sre_agent.harness.gather_cluster_context", return_value="new data"):
            result = get_cluster_context(max_age=60)
        assert result == "new data"

    def test_keeps_stale_on_error(self):
        import sre_agent.harness as h

        h._cluster_context_cache = "stale"
        h._cluster_context_ts = 0

        with patch("sre_agent.harness.gather_cluster_context", side_effect=RuntimeError("k8s down")):
            result = get_cluster_context(max_age=60)
        assert result == "stale"


class TestComponentHint:
    def test_hint_mentions_dashboards(self):
        assert "dashboard" in COMPONENT_HINT.lower()

    def test_hint_mentions_safety(self):
        assert "dry_run" in COMPONENT_HINT
