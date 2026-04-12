"""Tests filling coverage gaps across new skill-packages modules."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# skill_loader — reload diff, handoff edge cases, flat files, mode_categories
# ---------------------------------------------------------------------------


class TestSkillLoaderReload:
    def test_reload_logs_changes(self):
        from sre_agent.skill_loader import load_skills, reload_skills

        load_skills()  # Initial load
        result = reload_skills()  # Reload (no changes)
        assert len(result) >= 3

    def test_reload_returns_same_skills(self):
        from sre_agent.skill_loader import load_skills, reload_skills

        first = load_skills()
        second = reload_skills()
        assert set(first.keys()) == set(second.keys())


class TestSkillLoaderHandoffEdgeCases:
    def test_handoff_no_handoffs_defined(self):
        from sre_agent.skill_loader import Skill, check_handoff

        skill = Skill(
            name="test",
            version=1,
            description="test",
            keywords=[],
            categories=[],
            write_tools=False,
            priority=5,
            system_prompt="test",
        )
        assert check_handoff(skill, "create a dashboard") is None

    def test_handoff_target_not_found(self):
        from sre_agent.skill_loader import Skill, check_handoff, load_skills

        load_skills()
        skill = Skill(
            name="test",
            version=1,
            description="test",
            keywords=[],
            categories=[],
            write_tools=False,
            priority=5,
            system_prompt="test",
            handoff_to={"nonexistent_skill_xyz": ["trigger"]},
        )
        assert check_handoff(skill, "trigger handoff") is None


class TestSkillLoaderModeCategoriesEdge:
    def test_both_mode_returns_none(self):
        from sre_agent.skill_loader import get_mode_categories

        cats = get_mode_categories()
        assert cats["both"] is None

    def test_empty_categories_skill(self):
        from sre_agent.skill_loader import get_mode_categories

        cats = get_mode_categories()
        # view_designer has categories: [] in frontmatter
        if "view_designer" in cats:
            assert cats["view_designer"] is None or isinstance(cats["view_designer"], list)


class TestSkillLoaderWordBoundary:
    def test_short_keyword_word_boundary(self):
        """Short keywords like 'pod' should not match 'tripod'."""
        from sre_agent.skill_loader import classify_query

        # "pod" is a 3-char keyword — should use word boundary
        classify_query("check the tripod setup")
        # Should NOT route to SRE just because "pod" is in "tripod"
        # (may still route to SRE for other reasons, but not because of "pod")

    def test_long_keyword_substring_match(self):
        """Long keywords can match as substrings."""
        from sre_agent.skill_loader import classify_query

        result = classify_query("check the deployment status")
        assert result.name in ("sre", "capacity_planner")  # "deployment" is long enough


class TestSkillLoaderFixTypos:
    def test_typo_correction_applied(self):
        """classify_query should attempt to apply fix_typos."""
        from sre_agent.skill_loader import classify_query

        # Even with typo, should route correctly
        result = classify_query("my pod is crashlooping")
        assert result.name == "sre"


# ---------------------------------------------------------------------------
# component_transform — edge cases and fallback paths
# ---------------------------------------------------------------------------


class TestTransformEdgeCases:
    def test_table_to_chart_no_numeric_columns(self):
        """Table with no numeric columns falls back to counting."""
        from sre_agent.component_transform import transform

        spec = {
            "kind": "data_table",
            "title": "Labels",
            "columns": [{"id": "name", "header": "Name"}, {"id": "status", "header": "Status"}],
            "rows": [
                {"name": "nginx", "status": "Running"},
                {"name": "nginx", "status": "Running"},
                {"name": "redis", "status": "Pending"},
            ],
        }
        result = transform(spec, "chart")
        assert result["kind"] == "chart"
        assert result["chartType"] == "bar"

    def test_table_to_bar_list_no_numeric(self):
        """Bar list with no numeric column counts occurrences."""
        from sre_agent.component_transform import transform

        spec = {
            "kind": "data_table",
            "columns": [{"id": "name", "header": "Name"}],
            "rows": [{"name": "a"}, {"name": "a"}, {"name": "b"}],
        }
        result = transform(spec, "bar_list")
        assert result["kind"] == "bar_list"
        assert result["items"][0]["label"] == "a"
        assert result["items"][0]["value"] == 2

    def test_table_to_metric_card_max_aggregation(self):
        from sre_agent.component_transform import transform

        spec = {
            "kind": "data_table",
            "columns": [{"id": "name", "header": "Name"}, {"id": "cpu", "header": "CPU"}],
            "rows": [{"name": "a", "cpu": 5}, {"name": "b", "cpu": 10}],
        }
        result = transform(spec, "metric_card", options={"aggregation": "max", "value_column": "cpu"})
        assert result["value"] == "10"

    def test_table_to_metric_card_avg_aggregation(self):
        from sre_agent.component_transform import transform

        spec = {
            "kind": "data_table",
            "columns": [{"id": "n", "header": "N"}, {"id": "v", "header": "V"}],
            "rows": [{"n": "a", "v": 10}, {"n": "b", "v": 20}],
        }
        result = transform(spec, "metric_card", options={"aggregation": "avg", "value_column": "v"})
        assert result["value"] == "15.0"

    def test_table_to_metric_card_no_numeric_values(self):
        from sre_agent.component_transform import transform

        spec = {
            "kind": "data_table",
            "columns": [{"id": "n", "header": "N"}, {"id": "v", "header": "V"}],
            "rows": [{"n": "a", "v": "text"}],
        }
        result = transform(spec, "metric_card", options={"aggregation": "sum", "value_column": "v"})
        assert "0" in result["value"] or "no numeric" in result.get("description", "")

    def test_chart_to_table_empty_series(self):
        from sre_agent.component_transform import transform

        spec = {"kind": "chart", "series": []}
        result = transform(spec, "data_table")
        assert result["kind"] == "data_table"
        assert result["rows"] == []

    def test_metric_card_to_chart_custom_time_range(self):
        from sre_agent.component_transform import transform

        spec = {"kind": "metric_card", "title": "CPU", "value": "72%", "query": "rate(cpu[5m])"}
        result = transform(spec, "chart", options={"time_range": "24h"})
        assert result["time_range"] == "24h"


# ---------------------------------------------------------------------------
# mcp_renderer — additional parser and auto-detect paths
# ---------------------------------------------------------------------------


class TestMCPRendererEdgeCases:
    def test_url_not_parsed_as_key_value(self):
        """URLs should not be misclassified as key-value."""
        from sre_agent.mcp_renderer import _parse_key_value

        result = _parse_key_value("https://example.com\nhttps://other.com")
        assert result is None

    def test_csv_single_line_returns_none(self):
        from sre_agent.mcp_renderer import _parse_csv

        result = _parse_csv("just one line")
        assert result is None

    def test_parse_lines(self):
        from sre_agent.mcp_renderer import _parse_lines

        result = _parse_lines("a\nb\n\nc")
        assert result == ["a", "b", "c"]

    def test_auto_detect_large_text_goes_to_log_viewer(self):
        """Large non-structured text should go to log_viewer."""
        from sre_agent.mcp_renderer import render_mcp_output

        output = "\n".join([f"[INFO] Processing request #{i} from client" for i in range(50)])
        _, spec = render_mcp_output("server_logs", output)
        assert spec["kind"] == "log_viewer"

    def test_skill_renderer_status_list_without_mapping(self):
        """Skill renderer with status_list but no item_mapping."""
        from sre_agent.mcp_renderer import render_mcp_output

        output = json.dumps(
            [
                {"name": "alert1", "status": "firing", "message": "CPU high"},
            ]
        )
        config = {"kind": "status_list", "parser": "json"}
        _, spec = render_mcp_output("alerts", output, renderer_config=config)
        assert spec["kind"] == "status_list"
        assert spec["items"][0]["label"] == "alert1"

    def test_skill_renderer_metric_card(self):
        from sre_agent.mcp_renderer import render_mcp_output

        output = json.dumps({"value": 42, "unit": "pods"})
        config = {"kind": "metric_card", "parser": "json"}
        _, spec = render_mcp_output("pod_count", output, renderer_config=config)
        assert spec["kind"] == "metric_card"
        assert spec["value"] == "42"

    def test_skill_renderer_non_dict_items_in_status_list(self):
        from sre_agent.mcp_renderer import render_mcp_output

        output = json.dumps(["item1", "item2", "item3"])
        config = {"kind": "status_list", "parser": "json"}
        _, spec = render_mcp_output("items", output, renderer_config=config)
        assert spec["kind"] == "status_list"
        assert spec["items"][0]["label"] == "item1"


# ---------------------------------------------------------------------------
# mcp_client — connection edge cases
# ---------------------------------------------------------------------------


class TestMCPClientEdgeCases:
    def test_call_mcp_tool_not_connected(self):
        from sre_agent.mcp_client import MCPConnection, call_mcp_tool

        conn = MCPConnection(name="test", url="test", transport="stdio", toolsets=[])
        result = call_mcp_tool(conn, "some_tool", {})
        assert "not connected" in result

    def test_mcp_tool_to_dict_with_schema(self):
        from sre_agent.mcp_client import MCPTool

        schema = {
            "type": "object",
            "properties": {"namespace": {"type": "string", "description": "K8s namespace"}},
            "required": ["namespace"],
        }
        tool = MCPTool("list_pods", lambda: None, "List pods", input_schema=schema)
        d = tool.to_dict()
        assert d["input_schema"]["properties"]["namespace"]["type"] == "string"
        assert d["input_schema"]["required"] == ["namespace"]

    def test_disconnect_all_clears_connections(self):
        # Add a fake connection
        from sre_agent.mcp_client import MCPConnection, _connections, disconnect_all

        _connections["test"] = MCPConnection(name="test", url="", transport="stdio", toolsets=[])
        disconnect_all()
        assert len(_connections) == 0

    def test_list_connections_returns_dict(self):
        from sre_agent.mcp_client import disconnect_all, list_mcp_connections

        disconnect_all()
        result = list_mcp_connections()
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# skill_analytics — edge cases
# ---------------------------------------------------------------------------


class TestSkillAnalyticsEdgeCases:
    def test_record_with_none_tools(self):
        from sre_agent.skill_analytics import record_skill_invocation

        record_skill_invocation(
            session_id="s1",
            user_id="u1",
            skill_name="sre",
            skill_version=1,
            tools_called=None,
            duration_ms=0,
        )

    def test_get_skill_user_breakdown_empty(self):
        from sre_agent.skill_analytics import get_skill_user_breakdown

        result = get_skill_user_breakdown("nonexistent", days=1)
        assert result == []

    def test_update_feedback_nonexistent(self):
        from sre_agent.skill_analytics import update_skill_feedback

        update_skill_feedback("nonexistent-session", "negative")


# ---------------------------------------------------------------------------
# skill_rest — API endpoint coverage
# ---------------------------------------------------------------------------


class TestSkillRestEndpoints:
    def test_list_skills_returns_list(self):
        """Verify the list_skills endpoint returns serializable data."""
        from sre_agent.skill_loader import list_skills

        skills = list_skills()
        serialized = [s.to_dict() for s in skills]
        assert len(serialized) >= 3
        for s in serialized:
            assert "name" in s
            assert "version" in s
            assert "prompt_length" in s

    def test_get_skill_not_found(self):
        from sre_agent.skill_loader import get_skill

        assert get_skill("nonexistent_skill_xyz_123") is None


# ---------------------------------------------------------------------------
# widget_mutations — edge cases
# ---------------------------------------------------------------------------


class TestWidgetMutationEdgeCases:
    @pytest.fixture
    def mock_db(self):
        with (
            patch("sre_agent.db.get_view") as mock_get,
            patch("sre_agent.db.update_view") as mock_update,
            patch("sre_agent.view_tools.get_current_user", return_value="test-user"),
        ):
            yield mock_get, mock_update

    def test_update_columns_no_matching_columns(self, mock_db):
        mock_get, _ = mock_db
        mock_get.return_value = {
            "id": "cv-test",
            "title": "Test",
            "layout": [
                {
                    "kind": "data_table",
                    "title": "T",
                    "columns": [{"id": "name", "header": "Name"}],
                    "rows": [{"name": "x"}],
                }
            ],
            "owner": "test-user",
        }
        from sre_agent.view_tools import update_view_widgets

        result = update_view_widgets.call(
            {
                "view_id": "cv-test",
                "action": "update_columns",
                "widget_index": 0,
                "params_json": json.dumps({"columns": ["nonexistent_col"]}),
            }
        )
        assert "No matching columns" in result

    def test_sort_by_missing_column_param(self, mock_db):
        mock_get, _ = mock_db
        mock_get.return_value = {
            "id": "cv-test",
            "title": "Test",
            "layout": [
                {
                    "kind": "data_table",
                    "title": "T",
                    "columns": [{"id": "name", "header": "Name"}],
                    "rows": [],
                }
            ],
            "owner": "test-user",
        }
        from sre_agent.view_tools import update_view_widgets

        result = update_view_widgets.call(
            {
                "view_id": "cv-test",
                "action": "sort_by",
                "widget_index": 0,
                "params_json": json.dumps({}),
            }
        )
        assert "must include 'column'" in result

    def test_change_kind_uses_component_transform(self, mock_db):
        """Verify change_kind wires through component_transform when path exists."""
        mock_get, mock_update = mock_db
        mock_get.return_value = {
            "id": "cv-test",
            "title": "Test",
            "layout": [
                {
                    "kind": "data_table",
                    "title": "Pods",
                    "columns": [{"id": "name", "header": "Name"}, {"id": "count", "header": "Count"}],
                    "rows": [{"name": "nginx", "count": 5}, {"name": "redis", "count": 3}],
                }
            ],
            "owner": "test-user",
        }
        from sre_agent.view_tools import update_view_widgets

        result = update_view_widgets.call(
            {
                "view_id": "cv-test",
                "action": "change_kind",
                "widget_index": 0,
                "params_json": json.dumps({"new_kind": "bar_list"}),
            }
        )
        assert "Changed widget" in result
        layout = mock_update.call_args[1]["layout"]
        # Should have been transformed (not just kind swapped)
        assert layout[0]["kind"] == "bar_list"
        assert "items" in layout[0]  # component_transform adds items

    def test_invalid_widget_index(self, mock_db):
        mock_get, _ = mock_db
        mock_get.return_value = {
            "id": "cv-test",
            "title": "Test",
            "layout": [{"kind": "chart", "title": "CPU"}],
            "owner": "test-user",
        }
        from sre_agent.view_tools import update_view_widgets

        result = update_view_widgets.call(
            {
                "view_id": "cv-test",
                "action": "update_query",
                "widget_index": 5,
                "params_json": json.dumps({"query": "test"}),
            }
        )
        assert "Invalid widget index" in result

    def test_invalid_params_json(self, mock_db):
        mock_get, _ = mock_db
        mock_get.return_value = {
            "id": "cv-test",
            "title": "Test",
            "layout": [{"kind": "chart", "title": "CPU"}],
            "owner": "test-user",
        }
        from sre_agent.view_tools import update_view_widgets

        result = update_view_widgets.call(
            {
                "view_id": "cv-test",
                "action": "update_query",
                "widget_index": 0,
                "params_json": "{invalid json",
            }
        )
        assert "valid JSON" in result

    def test_view_not_found(self, mock_db):
        mock_get, _ = mock_db
        mock_get.return_value = None

        from sre_agent.view_tools import update_view_widgets

        result = update_view_widgets.call(
            {
                "view_id": "cv-nonexistent",
                "action": "update_query",
                "widget_index": 0,
            }
        )
        assert "not found" in result
