"""Tests for MCP tool output renderer."""

from __future__ import annotations

import json

from sre_agent.mcp_renderer import render_mcp_output


class TestAutoDetectJSON:
    def test_json_array_to_table(self):
        output = json.dumps(
            [
                {"name": "nginx", "namespace": "production", "status": "deployed"},
                {"name": "redis", "namespace": "staging", "status": "deployed"},
            ]
        )
        _text, spec = render_mcp_output("helm_list", output)
        assert spec["kind"] == "data_table"
        assert len(spec["rows"]) == 2
        assert len(spec["columns"]) == 3

    def test_json_object_to_key_value(self):
        output = json.dumps({"name": "nginx", "revision": 3, "status": "deployed"})
        _text, spec = render_mcp_output("helm_status", output)
        assert spec["kind"] == "key_value"
        assert len(spec["pairs"]) == 3

    def test_empty_json_array(self):
        _text, spec = render_mcp_output("helm_list", "[]")
        # Empty array has no objects to infer columns from → metric_card fallback
        assert "kind" in spec


class TestAutoDetectKeyValue:
    def test_colon_separated(self):
        output = "Name: nginx\nNamespace: production\nStatus: deployed\nRevision: 3"
        _text, spec = render_mcp_output("helm_status", output)
        assert spec["kind"] == "key_value"
        assert len(spec["pairs"]) == 4

    def test_equals_separated(self):
        output = "cpu_cores=4\nmemory_gb=16\ndisk_gb=100"
        _text, spec = render_mcp_output("node_stats", output)
        assert spec["kind"] == "key_value"
        assert len(spec["pairs"]) == 3


class TestAutoDetectCSV:
    def test_comma_separated(self):
        output = "name,namespace,revision,status\nnginx,production,3,deployed\nredis,staging,1,deployed"
        _text, spec = render_mcp_output("helm_list", output)
        assert spec["kind"] == "data_table"
        assert len(spec["rows"]) == 2

    def test_tab_separated(self):
        output = "name\tnamespace\tstatus\nnginx\tproduction\tdeployed\nredis\tstaging\tdeployed"
        _text, spec = render_mcp_output("helm_list", output)
        assert spec["kind"] == "data_table"
        assert len(spec["rows"]) == 2


class TestAutoDetectList:
    def test_numbered_list(self):
        output = "1. Check pod status\n2. Review logs\n3. Restart deployment\n4. Verify health"
        _text, spec = render_mcp_output("runbook", output)
        assert spec["kind"] == "status_list"
        assert len(spec["items"]) == 4

    def test_bulleted_list(self):
        output = "- item one\n- item two\n- item three"
        _text, spec = render_mcp_output("status_check", output)
        assert spec["kind"] == "status_list"
        assert len(spec["items"]) == 3


class TestAutoDetectSingleValue:
    def test_short_value(self):
        output = "42"
        _text, spec = render_mcp_output("pod_count", output)
        assert spec["kind"] == "metric_card"
        assert spec["value"] == "42"

    def test_short_string(self):
        output = "deployed"
        _text, spec = render_mcp_output("release_status", output)
        assert spec["kind"] == "metric_card"
        assert spec["value"] == "deployed"


class TestAutoDetectFallback:
    def test_multiline_text_to_log_viewer(self):
        output = (
            "Starting deployment...\nPulling image nginx:latest\nCreating container\nContainer started successfully"
        )
        _text, spec = render_mcp_output("deploy_log", output)
        assert spec["kind"] == "log_viewer"
        assert len(spec["lines"]) == 4

    def test_error_lines_detected(self):
        output = "Starting deployment process\nPulling image from registry\nERROR: failed to connect to database\nRetrying connection\nConnection established"
        _text, spec = render_mcp_output("pipeline", output)
        assert spec["kind"] == "log_viewer"
        error_lines = [entry for entry in spec["lines"] if entry["level"] == "error"]
        assert len(error_lines) >= 1

    def test_empty_output(self):
        _text, spec = render_mcp_output("empty_tool", "")
        assert spec["kind"] == "metric_card"
        assert spec["value"] == "empty"


class TestSkillDefinedRenderer:
    def test_json_to_table_with_columns(self):
        output = json.dumps(
            [
                {"name": "nginx", "revision": 3, "status": "deployed", "extra": "ignored"},
            ]
        )
        config = {"kind": "data_table", "parser": "json", "columns": ["name", "revision", "status"]}
        _text, spec = render_mcp_output("helm_list", output, renderer_config=config)
        assert spec["kind"] == "data_table"
        col_ids = [c["id"] for c in spec["columns"]]
        assert col_ids == ["name", "revision", "status"]

    def test_json_to_status_list_with_mapping(self):
        output = json.dumps(
            [
                {"alertname": "CPUThrottling", "state": "firing", "description": "Pod CPU throttled"},
            ]
        )
        config = {
            "kind": "status_list",
            "parser": "json",
            "item_mapping": {"label": "{{alertname}}", "status": "{{state}}", "detail": "{{description}}"},
        }
        _text, spec = render_mcp_output("alertmanager_alerts", output, renderer_config=config)
        assert spec["kind"] == "status_list"
        assert spec["items"][0]["label"] == "CPUThrottling"
        assert spec["items"][0]["status"] == "firing"

    def test_key_value_to_key_value(self):
        output = "Name: nginx\nStatus: deployed\nRevision: 3"
        config = {"kind": "key_value", "parser": "key_value"}
        _text, spec = render_mcp_output("helm_status", output, renderer_config=config)
        assert spec["kind"] == "key_value"
        assert len(spec["pairs"]) == 3

    def test_fallback_on_bad_config(self):
        """If skill renderer fails, fall back to auto-detect."""
        output = json.dumps([{"name": "nginx"}])
        config = {"kind": "data_table", "parser": "csv"}  # Wrong parser for JSON
        _text, spec = render_mcp_output("helm_list", output, renderer_config=config)
        # Should fall back to auto-detect (JSON → data_table)
        assert spec["kind"] == "data_table"


class TestNeverPlainText:
    """Every output must produce a component — never plain text."""

    def test_json(self):
        _, spec = render_mcp_output("t", '[{"a": 1}]')
        assert "kind" in spec

    def test_key_value(self):
        _, spec = render_mcp_output("t", "a: 1\nb: 2\nc: 3")
        assert "kind" in spec

    def test_csv(self):
        _, spec = render_mcp_output("t", "a,b\n1,2\n3,4")
        assert "kind" in spec

    def test_list(self):
        _, spec = render_mcp_output("t", "1. first\n2. second\n3. third")
        assert "kind" in spec

    def test_single_value(self):
        _, spec = render_mcp_output("t", "42")
        assert "kind" in spec

    def test_multiline(self):
        _, spec = render_mcp_output("t", "line 1\nline 2\nline 3\nline 4")
        assert "kind" in spec

    def test_empty(self):
        _, spec = render_mcp_output("t", "")
        assert "kind" in spec
