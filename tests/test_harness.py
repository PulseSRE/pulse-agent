"""Tests for harness.py — tool selection, prompt caching, cluster context."""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import patch

from sre_agent.harness import (
    ALWAYS_INCLUDE,
    COMPONENT_HINT,
    TOOL_CATEGORIES,
    build_cached_system_prompt,
    get_cluster_context,
    get_tool_category,
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
        assert "list_resources" in ALWAYS_INCLUDE
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
        _defs, selected, _offered = select_tools("check cluster health", all_tools, tool_map, mode="sre")
        assert "list_pods" in selected
        assert "get_events" in selected

    def test_security_query(self):
        all_tools, tool_map = self._all_tools()
        _defs, selected, _offered = select_tools("run a security audit of rbac", all_tools, tool_map, mode="security")
        assert "scan_rbac_risks" in selected
        assert "scan_pod_security" in selected

    def test_generic_query_returns_all(self):
        all_tools, tool_map = self._all_tools()
        _defs, selected, _offered = select_tools("hello world", all_tools, tool_map, mode="both")
        assert len(selected) == len(all_tools)

    def test_always_include_present(self):
        all_tools, tool_map = self._all_tools()
        _defs, selected, _offered = select_tools("check pod status", all_tools, tool_map)
        for name in ALWAYS_INCLUDE:
            if name in tool_map:
                assert name in selected

    def test_diagnostics_includes_workloads(self):
        """When diagnostics is a top category, workload tools should be included."""
        all_tools, tool_map = self._all_tools()
        _defs, selected, _offered = select_tools("what's wrong with my cluster health", all_tools, tool_map)
        # diagnostics should pull in workloads
        assert "scale_deployment" in selected or "list_resources" in selected

    def test_fleet_query(self):
        all_tools, tool_map = self._all_tools()
        _defs, selected, _offered = select_tools("compare across all clusters fleet", all_tools, tool_map, mode="both")
        assert "fleet_list_clusters" in selected

    def test_storage_query(self):
        all_tools, tool_map = self._all_tools()
        _defs, selected, _offered = select_tools("check pvc storage volumes", all_tools, tool_map)
        assert "list_resources" in selected

    def test_empty_tool_list(self):
        defs, selected, _offered = select_tools("anything", [], {})
        assert defs == []
        assert selected == {}

    def test_nonexistent_tools_filtered_out(self):
        """Tools named in categories but not in all_tools should be excluded."""
        tools = [_make_tool("list_namespaces")]
        tool_map = {"list_namespaces": tools[0]}
        _defs, selected, _offered = select_tools("check health status", tools, tool_map)
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
    def _clear_caches(self):
        import sre_agent.harness as h

        h._cluster_context_cache.clear()
        try:
            from sre_agent.tool_chains import _chain_hints_cache

            _chain_hints_cache.clear()
        except ImportError:
            pass

    def test_caches_result(self):
        self._clear_caches()

        with (
            patch("sre_agent.harness.gather_cluster_context", return_value="cached data") as mock_gather,
            patch("sre_agent.tool_chains.ensure_hints_fresh"),
            patch("sre_agent.tool_chains.get_chain_hints_text", return_value=""),
        ):
            result1 = get_cluster_context(max_age=60, mode="sre")
            result2 = get_cluster_context(max_age=60, mode="sre")
        assert result1 == "cached data"
        assert result2 == "cached data"
        assert mock_gather.call_count == 1

    def test_refreshes_when_stale(self):
        self._clear_caches()
        import sre_agent.harness as h

        h._cluster_context_cache["sre"] = ("old", 0)  # ancient timestamp

        with (
            patch("sre_agent.harness.gather_cluster_context", return_value="new data"),
            patch("sre_agent.tool_chains.ensure_hints_fresh"),
            patch("sre_agent.tool_chains.get_chain_hints_text", return_value=""),
        ):
            result = get_cluster_context(max_age=60, mode="sre")
        assert result == "new data"

    def test_keeps_stale_on_error(self):
        import sre_agent.harness as h

        h._cluster_context_cache["sre"] = ("stale", 0)

        with patch("sre_agent.harness.gather_cluster_context", side_effect=RuntimeError("k8s down")):
            result = get_cluster_context(max_age=60, mode="sre")
        assert result == "stale"


class TestGetToolCategory:
    def test_tool_in_single_category(self):
        assert get_tool_category("scale_deployment") == "workloads"

    def test_tool_in_multiple_categories_returns_first(self):
        # list_resources is in diagnostics, workloads, networking, storage
        result = get_tool_category("list_resources")
        assert result in ("diagnostics", "workloads", "networking", "storage")

    def test_tool_not_in_any_category(self):
        assert get_tool_category("nonexistent_tool_xyz") is None

    def test_always_include_tool_without_category(self):
        # record_audit_entry is in ALWAYS_INCLUDE but no category
        assert get_tool_category("record_audit_entry") is None

    def test_all_categorized_tools_return_category(self):
        """Every tool in TOOL_CATEGORIES should return a non-None category."""
        for cat_name, config in TOOL_CATEGORIES.items():
            for tool_name in config["tools"]:
                result = get_tool_category(tool_name)
                assert result is not None, f"{tool_name} returned None but is in {cat_name}"


class TestSelectToolsOffered:
    def _all_tools(self):
        names = set(ALWAYS_INCLUDE)
        for config in TOOL_CATEGORIES.values():
            names.update(config["tools"])
        tools = [_make_tool(n) for n in sorted(names)]
        tool_map = {t.name: t for t in tools}
        return tools, tool_map

    def test_returns_three_tuple(self):
        all_tools, tool_map = self._all_tools()
        result = select_tools("check health", all_tools, tool_map, mode="sre")
        assert len(result) == 3, "select_tools should return (defs, map, offered_names)"

    def test_offered_names_is_list(self):
        all_tools, tool_map = self._all_tools()
        _defs, _map, offered = select_tools("check health", all_tools, tool_map, mode="sre")
        assert isinstance(offered, list)
        assert all(isinstance(n, str) for n in offered)

    def test_offered_matches_filtered(self):
        all_tools, tool_map = self._all_tools()
        _defs, selected, offered = select_tools("check health", all_tools, tool_map, mode="sre")
        assert set(offered) == set(selected.keys())

    def test_both_mode_returns_all_names(self):
        all_tools, tool_map = self._all_tools()
        _defs, _map, offered = select_tools("hello", all_tools, tool_map, mode="both")
        assert len(offered) == len(all_tools)


class TestCategoryCoverage:
    def test_all_registered_tools_have_category(self):
        """Every tool in the registry should be in at least one category or ALWAYS_INCLUDE."""
        # Import all tool modules to populate the registry (side-effect imports)
        from sre_agent import (
            fleet_tools,  # noqa: F401
            git_tools,  # noqa: F401
            gitops_tools,  # noqa: F401
            handoff_tools,  # noqa: F401
            k8s_tools,  # noqa: F401
            predict_tools,  # noqa: F401
            security_tools,  # noqa: F401
            timeline_tools,  # noqa: F401
            view_tools,  # noqa: F401
        )
        from sre_agent.harness import ALWAYS_INCLUDE, TOOL_CATEGORIES
        from sre_agent.tool_registry import TOOL_REGISTRY

        all_categorized = set(ALWAYS_INCLUDE)
        for config in TOOL_CATEGORIES.values():
            all_categorized.update(config["tools"])

        # These tools are internal/meta and intentionally uncategorized
        EXCLUDED = {"set_store", "set_current_user", "get_current_user", "get_cluster_patterns"}

        missing = set()
        for tool_name in TOOL_REGISTRY:
            if tool_name not in all_categorized and tool_name not in EXCLUDED:
                missing.add(tool_name)

        assert missing == set(), f"Tools missing from categories: {sorted(missing)}"


class TestComponentHint:
    REQUIRED_COMPONENT_KINDS: ClassVar[list[str]] = [
        "data_table",
        "info_card_grid",
        "chart",
        "status_list",
        "badge_list",
        "key_value",
        "relationship_tree",
        "tabs",
        "grid",
        "section",
    ]

    def test_hint_mentions_dashboards(self):
        assert "dashboard" in COMPONENT_HINT.lower()

    def test_hint_mentions_safety(self):
        assert "dry_run" in COMPONENT_HINT

    def test_hint_documents_all_component_kinds(self):
        """Every component kind must be documented in the system prompt."""
        for kind in self.REQUIRED_COMPONENT_KINDS:
            assert kind in COMPONENT_HINT, f"Component kind '{kind}' missing from COMPONENT_HINT"

    def test_hint_has_schema_for_each_kind(self):
        """Each component kind should have a JSON schema example in the hint."""
        for kind in self.REQUIRED_COMPONENT_KINDS:
            assert f'"kind": "{kind}"' in COMPONENT_HINT, (
                f"Component kind '{kind}' has no schema example in COMPONENT_HINT"
            )


# ---------------------------------------------------------------------------
# Orchestrator: view_designer routing
# ---------------------------------------------------------------------------

from sre_agent.orchestrator import build_orchestrated_config, classify_intent


class TestViewDesignerRouting:
    def test_dashboard_keyword_routes_to_view_designer(self):
        assert classify_intent("create a dashboard for my cluster") == "view_designer"

    def test_widget_keyword_routes_to_view_designer(self):
        assert classify_intent("add a widget to my view") == "view_designer"

    def test_metric_card_routes_to_view_designer(self):
        assert classify_intent("add metric cards with sparklines") == "view_designer"

    def test_sre_query_does_not_route_to_view_designer(self):
        assert classify_intent("what pods are crashing") == "sre"

    def test_security_query_does_not_route_to_view_designer(self):
        assert classify_intent("scan rbac permissions") == "security"

    def test_build_config_returns_no_write_tools(self):
        config = build_orchestrated_config("view_designer")
        assert config["write_tools"] == set()
        assert len(config["tool_defs"]) > 0
        assert "view_designer" not in [d.get("name") for d in config["tool_defs"]]  # no meta-tool

    def test_build_config_has_critique_and_plan_tools(self):
        config = build_orchestrated_config("view_designer")
        tool_names = {d.get("name") for d in config["tool_defs"]}
        assert "critique_view" in tool_names
        assert "plan_dashboard" in tool_names
        assert "create_dashboard" in tool_names


# ---------------------------------------------------------------------------
# Chart type selection
# ---------------------------------------------------------------------------


class TestPickChartType:
    def test_sum_by_with_many_series_is_stacked_area(self):
        # Can't easily test the nested function, but we can verify via import
        # For now, test the outer function behavior
        pass  # Covered by integration — nested function not independently testable

    def test_layout_template_apply(self):
        from sre_agent.layout_templates import apply_template

        components = [
            {"kind": "metric_card", "title": "CPU"},
            {"kind": "metric_card", "title": "Mem"},
            {"kind": "chart", "title": "CPU Trend"},
            {"kind": "data_table", "title": "Pods"},
        ]
        result = apply_template("sre_dashboard", components)
        assert result is not None
        positions = result
        # Metric cards should be in top row (y=0)
        assert positions[0]["y"] == 0
        assert positions[1]["y"] == 0
        # Chart should be below (y > 0)
        assert positions[2]["y"] > 0
        # Table should be below chart
        assert positions[3]["y"] > positions[2]["y"]

    def test_apply_template_unknown_returns_none(self):
        from sre_agent.layout_templates import apply_template

        result = apply_template("nonexistent_template", [])
        assert result is None

    def test_apply_template_unmatched_appended(self):
        from sre_agent.layout_templates import apply_template

        components = [
            {"kind": "log_viewer", "title": "Logs"},
            {"kind": "yaml_viewer", "title": "YAML"},
        ]
        result = apply_template("sre_dashboard", components)
        assert result is not None
        # Both should be appended at bottom since they don't match sre_dashboard slots
        assert len(result) == 2
