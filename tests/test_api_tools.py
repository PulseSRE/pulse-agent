"""Tests for tool usage REST endpoints."""

from __future__ import annotations

import os
from unittest.mock import patch

from fastapi.testclient import TestClient

os.environ.setdefault("PULSE_AGENT_WS_TOKEN", "test-token-123")


class TestAgentsEndpoint:
    def test_returns_agents(self):
        from sre_agent.api import app

        client = TestClient(app)
        resp = client.get("/agents", headers={"Authorization": "Bearer test-token-123"})
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        names = {a["name"] for a in data}
        assert "sre" in names
        assert "security" in names

    def test_unauthorized(self):
        from sre_agent.api import app

        client = TestClient(app)
        resp = client.get("/agents")
        assert resp.status_code == 401


class TestToolsEndpointEnhanced:
    def test_includes_category(self):
        from sre_agent.api import app

        client = TestClient(app)
        resp = client.get("/tools", headers={"Authorization": "Bearer test-token-123"})
        assert resp.status_code == 200
        data = resp.json()
        sre_tools = data["sre"]
        assert len(sre_tools) > 0
        has_category = [t for t in sre_tools if t.get("category") is not None]
        assert len(has_category) > 0


class TestToolsUsageEndpoint:
    @patch("sre_agent.tool_usage.query_usage")
    def test_basic_query(self, mock_query):
        mock_query.return_value = {"entries": [], "total": 0, "page": 1, "per_page": 50}
        from sre_agent.api import app

        client = TestClient(app)
        resp = client.get("/tools/usage", headers={"Authorization": "Bearer test-token-123"})
        assert resp.status_code == 200
        assert resp.json()["total"] == 0
        mock_query.assert_called_once()

    @patch("sre_agent.tool_usage.query_usage")
    def test_passes_filters(self, mock_query):
        mock_query.return_value = {"entries": [], "total": 0, "page": 1, "per_page": 50}
        from sre_agent.api import app

        client = TestClient(app)
        resp = client.get(
            "/tools/usage?tool_name=list_pods&agent_mode=sre&status=success&page=2&per_page=10",
            headers={"Authorization": "Bearer test-token-123"},
        )
        assert resp.status_code == 200
        mock_query.assert_called_once_with(
            tool_name="list_pods",
            agent_mode="sre",
            status="success",
            session_id=None,
            tool_source=None,
            time_from=None,
            time_to=None,
            page=2,
            per_page=10,
        )


class TestToolResultRecording:
    @patch("sre_agent.tool_usage.record_tool_call")
    def test_on_tool_result_records(self, mock_call):
        from sre_agent.api import _build_tool_result_handler

        handler = _build_tool_result_handler(session_id="test-sess", agent_mode="sre", write_tools={"delete_pod"})
        handler(
            {
                "tool_name": "list_pods",
                "input": {"namespace": "default"},
                "status": "success",
                "error_message": None,
                "error_category": None,
                "duration_ms": 100,
                "result_bytes": 500,
                "was_confirmed": None,
                "turn_number": 1,
            }
        )
        mock_call.assert_called_once()
        call_kwargs = mock_call.call_args[1]
        assert call_kwargs["session_id"] == "test-sess"
        assert call_kwargs["tool_name"] == "list_pods"
        assert call_kwargs["requires_confirmation"] is False

    @patch("sre_agent.tool_usage.record_tool_call")
    def test_write_tool_flagged(self, mock_call):
        from sre_agent.api import _build_tool_result_handler

        handler = _build_tool_result_handler(session_id="test-sess", agent_mode="sre", write_tools={"delete_pod"})
        handler(
            {
                "tool_name": "delete_pod",
                "input": {"pod_name": "x"},
                "status": "success",
                "error_message": None,
                "error_category": None,
                "duration_ms": 50,
                "result_bytes": 10,
                "was_confirmed": True,
                "turn_number": 1,
            }
        )
        call_kwargs = mock_call.call_args[1]
        assert call_kwargs["requires_confirmation"] is True

    @patch("sre_agent.tool_usage.record_tool_call")
    def test_handler_swallows_errors(self, mock_call):
        mock_call.side_effect = RuntimeError("DB down")
        from sre_agent.api import _build_tool_result_handler

        handler = _build_tool_result_handler(session_id="test-sess", agent_mode="sre", write_tools=set())
        # Should not raise
        handler(
            {
                "tool_name": "list_pods",
                "input": {},
                "status": "success",
                "error_message": None,
                "error_category": None,
                "duration_ms": 0,
                "result_bytes": 0,
                "was_confirmed": None,
                "turn_number": 1,
            }
        )


class TestToolsUsageStatsEndpoint:
    @patch("sre_agent.tool_usage.get_usage_stats")
    def test_basic_stats(self, mock_stats):
        mock_stats.return_value = {
            "total_calls": 100,
            "unique_tools_used": 10,
            "error_rate": 0.05,
            "avg_duration_ms": 200,
            "avg_result_bytes": 3000,
            "by_tool": [],
            "by_mode": [],
            "by_category": [],
            "by_status": {},
        }
        from sre_agent.api import app

        client = TestClient(app)
        resp = client.get("/tools/usage/stats", headers={"Authorization": "Bearer test-token-123"})
        assert resp.status_code == 200
        assert resp.json()["total_calls"] == 100


class TestToolsUsageChainsEndpoint:
    @patch("sre_agent.tool_chains.discover_chains")
    def test_returns_chains(self, mock_discover):
        mock_discover.return_value = {
            "bigrams": [
                {"from_tool": "list_resources", "to_tool": "get_pod_logs", "frequency": 42, "probability": 0.78},
            ],
            "total_sessions_analyzed": 120,
        }
        from sre_agent.api import app

        client = TestClient(app)
        resp = client.get("/tools/usage/chains", headers={"Authorization": "Bearer test-token-123"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["bigrams"]) == 1
        assert data["bigrams"][0]["from_tool"] == "list_resources"
        assert data["total_sessions_analyzed"] == 120

    def test_unauthorized(self):
        from sre_agent.api import app

        client = TestClient(app)
        resp = client.get("/tools/usage/chains")
        assert resp.status_code == 401


class TestPromQLSanitization:
    def test_fix_double_braces(self):
        from sre_agent.api import _fix_promql

        assert _fix_promql('metric{a="1"}{b="2"}') == 'metric{a="1",b="2"}'

    def test_fix_double_braces_with_space(self):
        from sre_agent.api import _fix_promql

        assert _fix_promql('metric{a="1"} {b="2"}') == 'metric{a="1",b="2"}'

    def test_valid_query_unchanged(self):
        from sre_agent.api import _fix_promql

        q = 'count(kube_pod_status_phase{namespace="prod",phase="Running"})'
        assert _fix_promql(q) == q

    def test_empty_query(self):
        from sre_agent.api import _fix_promql

        assert _fix_promql("") == ""

    def test_sanitize_components_fixes_metric_card(self):
        from sre_agent.api import _sanitize_components

        comps = [
            {"kind": "metric_card", "title": "Pods", "query": 'count(kube_pod{ns="x"}{phase="Running"})'},
            {"kind": "data_table", "title": "Table"},
        ]
        _sanitize_components(comps)
        assert comps[0]["query"] == 'count(kube_pod{ns="x",phase="Running"})'
        assert comps[1].get("query") is None  # untouched

    def test_sanitize_nested_in_grid(self):
        from sre_agent.api import _sanitize_components

        comps = [
            {
                "kind": "grid",
                "items": [
                    {"kind": "metric_card", "query": 'a{x="1"}{y="2"}'},
                ],
            }
        ]
        _sanitize_components(comps)
        assert comps[0]["items"][0]["query"] == 'a{x="1",y="2"}'
