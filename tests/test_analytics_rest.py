"""Tests for analytics REST endpoints."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def pulse_token():
    return "test-token-secure-xyz"


@pytest.fixture
def api_headers(pulse_token):
    return {"Authorization": f"Bearer {pulse_token}"}


@pytest.fixture
def api_client(pulse_token, monkeypatch):
    monkeypatch.setenv("PULSE_AGENT_WS_TOKEN", pulse_token)
    monkeypatch.setenv("PULSE_AGENT_MEMORY", "0")
    # PULSE_AGENT_DATABASE_URL set by conftest autouse fixture

    with (
        patch("sre_agent.k8s_client._initialized", True),
        patch("sre_agent.k8s_client._load_k8s"),
        patch("sre_agent.k8s_client.get_core_client") as core,
        patch("sre_agent.k8s_client.get_apps_client"),
        patch("sre_agent.k8s_client.get_custom_client"),
        patch("sre_agent.k8s_client.get_version_client"),
    ):
        core.return_value = MagicMock()
        from sre_agent.api import app

        client = TestClient(app, raise_server_exceptions=False)
        yield client


# ── Fix History Summary ────────────────────────────────────────────────────


class TestFixHistorySummary:
    def test_returns_aggregated_stats(self, api_client, api_headers):
        """Test that fix-history/summary returns aggregated statistics."""
        mock_summary = {
            "total_actions": 42,
            "completed": 35,
            "failed": 5,
            "rolled_back": 2,
            "success_rate": 0.83,
            "rollback_rate": 0.05,
            "avg_resolution_ms": 1250,
            "by_category": [
                {
                    "category": "crashlooping-pods",
                    "count": 20,
                    "success_count": 18,
                    "auto_fixed": 18,
                    "confirmation_required": 0,
                },
                {
                    "category": "failed-deployments",
                    "count": 15,
                    "success_count": 12,
                    "auto_fixed": 10,
                    "confirmation_required": 5,
                },
            ],
            "trend": {"current_week": 25, "previous_week": 17, "delta": 8},
        }

        with patch("sre_agent.api.fix_rest.get_fix_history_summary", return_value=mock_summary):
            r = api_client.get("/fix-history/summary", headers=api_headers)

        assert r.status_code == 200
        data = r.json()
        assert data["total_actions"] == 42
        assert data["completed"] == 35
        assert data["failed"] == 5
        assert data["success_rate"] == 0.83
        assert len(data["by_category"]) == 2
        assert data["trend"]["delta"] == 8

    def test_empty_history(self, api_client, api_headers):
        """Test that empty history returns zero stats."""
        mock_empty = {
            "total_actions": 0,
            "completed": 0,
            "failed": 0,
            "rolled_back": 0,
            "success_rate": 0.0,
            "rollback_rate": 0.0,
            "avg_resolution_ms": 0,
            "by_category": [],
            "trend": {"current_week": 0, "previous_week": 0, "delta": 0},
        }

        with patch("sre_agent.api.fix_rest.get_fix_history_summary", return_value=mock_empty):
            r = api_client.get("/fix-history/summary", headers=api_headers)

        assert r.status_code == 200
        data = r.json()
        assert data["total_actions"] == 0
        assert data["success_rate"] == 0.0
        assert data["by_category"] == []

    def test_requires_auth(self, api_client):
        """Test that the endpoint requires authentication."""
        r = api_client.get("/fix-history/summary")
        assert r.status_code == 401

    def test_custom_days_parameter(self, api_client, api_headers):
        """Test that days parameter is passed through correctly."""
        mock_summary = {
            "total_actions": 10,
            "completed": 8,
            "failed": 2,
            "rolled_back": 0,
            "success_rate": 0.8,
            "rollback_rate": 0.0,
            "avg_resolution_ms": 1000,
            "by_category": [],
            "trend": {"current_week": 5, "previous_week": 5, "delta": 0},
        }

        with patch("sre_agent.api.fix_rest.get_fix_history_summary") as mock_fn:
            mock_fn.return_value = mock_summary
            r = api_client.get("/fix-history/summary?days=30", headers=api_headers)

        assert r.status_code == 200
        # Verify that the function was called with days=30
        mock_fn.assert_called_once_with(30)


# ── Confidence Calibration ─────────────────────────────────────────────────


class TestConfidenceCalibration:
    def test_returns_calibration_stats(self, api_client, api_headers):
        """Test that confidence endpoint returns calibration statistics."""
        mock_calibration = {
            "brier_score": 0.12,
            "accuracy_pct": 88.0,
            "rating": "good",
            "total_predictions": 150,
            "buckets": [
                {"range": "0.0-0.2", "predicted": 0.15, "actual": 0.10, "count": 5},
                {"range": "0.2-0.4", "predicted": 0.32, "actual": 0.30, "count": 10},
                {"range": "0.4-0.6", "predicted": 0.55, "actual": 0.50, "count": 20},
                {"range": "0.6-0.8", "predicted": 0.72, "actual": 0.75, "count": 50},
                {"range": "0.8-1.0", "predicted": 0.91, "actual": 0.95, "count": 65},
            ],
        }

        with patch("sre_agent.api.analytics_rest._compute_confidence_calibration", return_value=mock_calibration):
            r = api_client.get("/analytics/confidence", headers=api_headers)

        assert r.status_code == 200
        data = r.json()
        assert data["brier_score"] == 0.12
        assert data["accuracy_pct"] == 88.0
        assert data["rating"] == "good"
        assert data["total_predictions"] == 150
        assert len(data["buckets"]) == 5
        assert data["buckets"][0]["range"] == "0.0-0.2"

    def test_no_data(self, api_client, api_headers):
        """Test that insufficient data returns appropriate rating."""
        mock_no_data = {
            "brier_score": 0.0,
            "accuracy_pct": 0.0,
            "rating": "insufficient_data",
            "total_predictions": 0,
            "buckets": [],
        }

        with patch("sre_agent.api.analytics_rest._compute_confidence_calibration", return_value=mock_no_data):
            r = api_client.get("/analytics/confidence", headers=api_headers)

        assert r.status_code == 200
        data = r.json()
        assert data["rating"] == "insufficient_data"
        assert data["total_predictions"] == 0
        assert data["buckets"] == []

    def test_requires_auth(self, api_client):
        """Test that the endpoint requires authentication."""
        r = api_client.get("/analytics/confidence")
        assert r.status_code == 401

    def test_custom_days_parameter(self, api_client, api_headers):
        """Test that days parameter is passed through correctly."""
        mock_calibration = {
            "brier_score": 0.15,
            "accuracy_pct": 85.0,
            "rating": "good",
            "total_predictions": 50,
            "buckets": [],
        }

        with patch("sre_agent.api.analytics_rest._compute_confidence_calibration") as mock_fn:
            mock_fn.return_value = mock_calibration
            r = api_client.get("/analytics/confidence?days=7", headers=api_headers)

        assert r.status_code == 200
        mock_fn.assert_called_once_with(7)

    def test_validates_days_range(self, api_client, api_headers):
        """Test that days parameter validates range (1-365)."""
        # Test too low
        r = api_client.get("/analytics/confidence?days=0", headers=api_headers)
        assert r.status_code == 422  # Pydantic validation error

        # Test too high
        r = api_client.get("/analytics/confidence?days=366", headers=api_headers)
        assert r.status_code == 422


# ── Scanner Coverage ───────────────────────────────────────────────────────


class TestScannerCoverage:
    def test_returns_coverage_stats(self, api_client, api_headers):
        """Test that monitor/coverage returns scanner coverage statistics."""
        mock_coverage = {
            "active_scanners": 14,
            "total_scanners": 16,
            "coverage_pct": 0.85,
            "categories": [
                {"name": "pod_health", "covered": True, "scanners": ["crashloop", "pending", "oom", "image_pull"]},
                {"name": "node_pressure", "covered": True, "scanners": ["nodes"]},
                {"name": "certificate_expiry", "covered": False, "scanners": []},
            ],
            "per_scanner": [
                {"name": "crashloop", "enabled": True, "finding_count": 42, "actionable_count": 38, "noise_pct": 0.09},
                {"name": "pending", "enabled": True, "finding_count": 15, "actionable_count": 12, "noise_pct": 0.2},
                {"name": "cert_expiry", "enabled": False, "finding_count": 0, "actionable_count": 0, "noise_pct": 0.0},
            ],
        }

        with patch("sre_agent.api.scanner_rest.get_scanner_coverage", return_value=mock_coverage):
            r = api_client.get("/monitor/coverage", headers=api_headers)

        assert r.status_code == 200
        data = r.json()
        assert data["active_scanners"] == 14
        assert data["total_scanners"] == 16
        assert data["coverage_pct"] == 0.85
        assert len(data["categories"]) == 3
        assert data["categories"][0]["name"] == "pod_health"
        assert data["categories"][0]["covered"] is True
        assert len(data["per_scanner"]) == 3

    def test_requires_auth(self, api_client):
        """Test that the endpoint requires authentication."""
        r = api_client.get("/monitor/coverage")
        assert r.status_code == 401

    def test_custom_days_parameter(self, api_client, api_headers):
        """Test that days parameter is passed through correctly."""
        mock_coverage = {
            "active_scanners": 16,
            "total_scanners": 16,
            "coverage_pct": 1.0,
            "categories": [],
            "per_scanner": [],
        }

        with patch("sre_agent.api.scanner_rest.get_scanner_coverage") as mock_fn:
            mock_fn.return_value = mock_coverage
            r = api_client.get("/monitor/coverage?days=30", headers=api_headers)

        assert r.status_code == 200
        # Verify that the function was called with days=30
        mock_fn.assert_called_once_with(30)


# ── Accuracy Analytics ─────────────────────────────────────────────────────


class TestAccuracyAnalytics:
    def test_returns_accuracy_stats(self, api_client, api_headers):
        """Test that accuracy endpoint returns complete statistics."""
        mock_accuracy = {
            "avg_quality_score": 0.78,
            "quality_trend": {"current": 0.85, "previous": 0.80, "delta": 0.05},
            "dimensions": {
                "quality": 0.78,
                "override_rate": 0.12,
            },
            "anti_patterns": [
                {
                    "error_type": "ImagePullBackOff",
                    "namespace": "production",
                    "count": 5,
                },
                {
                    "error_type": "CrashLoopBackOff",
                    "namespace": "staging",
                    "count": 3,
                },
            ],
            "learning": {
                "runbook_count": 42,
                "success_rate": 0.91,
                "pattern_count": 15,
                "pattern_types": [
                    {"type": "crashloop", "count": 8},
                    {"type": "pending_pod", "count": 4},
                ],
                "new_runbooks_this_month": 3,
            },
            "override_rate": {
                "rate": 0.12,
                "total_proposed": 50,
                "rejected_actions": 6,
            },
        }

        with patch("sre_agent.api.analytics_rest._compute_accuracy_stats", return_value=mock_accuracy):
            r = api_client.get("/analytics/accuracy", headers=api_headers)

        assert r.status_code == 200
        data = r.json()
        assert data["avg_quality_score"] == 0.78
        assert data["quality_trend"]["delta"] == 0.05
        assert data["dimensions"]["quality"] == 0.78
        assert data["dimensions"]["override_rate"] == 0.12
        assert len(data["anti_patterns"]) == 2
        assert data["anti_patterns"][0]["error_type"] == "ImagePullBackOff"
        assert data["learning"]["runbook_count"] == 42
        assert data["learning"]["success_rate"] == 0.91
        assert data["override_rate"]["rate"] == 0.12
        assert data["override_rate"]["total_proposed"] == 50

    def test_empty_data(self, api_client, api_headers):
        """Test that empty data returns zero stats."""
        mock_empty = {
            "avg_quality_score": 0.0,
            "quality_trend": {"current": 0.0, "previous": 0.0, "delta": 0.0},
            "dimensions": {"quality": 0.0, "override_rate": 0.0},
            "anti_patterns": [],
            "learning": {
                "runbook_count": 0,
                "success_rate": 0.0,
                "pattern_count": 0,
                "pattern_types": [],
                "new_runbooks_this_month": 0,
            },
            "override_rate": {
                "rate": 0.0,
                "total_proposed": 0,
                "rejected_actions": 0,
            },
        }

        with patch("sre_agent.api.analytics_rest._compute_accuracy_stats", return_value=mock_empty):
            r = api_client.get("/analytics/accuracy", headers=api_headers)

        assert r.status_code == 200
        data = r.json()
        assert data["avg_quality_score"] == 0.0
        assert data["anti_patterns"] == []
        assert data["learning"]["runbook_count"] == 0

    def test_requires_auth(self, api_client):
        """Test that the endpoint requires authentication."""
        r = api_client.get("/analytics/accuracy")
        assert r.status_code == 401

    def test_custom_days_parameter(self, api_client, api_headers):
        """Test that days parameter is passed through correctly."""
        mock_accuracy = {
            "avg_quality_score": 0.85,
            "quality_trend": {"current": 0.83, "previous": 0.80, "delta": 0.03},
            "dimensions": {"quality": 0.85, "override_rate": 0.08},
            "anti_patterns": [],
            "learning": {
                "runbook_count": 20,
                "success_rate": 0.95,
                "pattern_count": 5,
                "pattern_types": [],
                "new_runbooks_this_month": 1,
            },
            "override_rate": {
                "rate": 0.08,
                "total_proposed": 25,
                "rejected_actions": 2,
            },
        }

        with patch("sre_agent.api.analytics_rest._compute_accuracy_stats") as mock_fn:
            mock_fn.return_value = mock_accuracy
            r = api_client.get("/analytics/accuracy?days=7", headers=api_headers)

        assert r.status_code == 200
        mock_fn.assert_called_once_with(7)

    def test_validates_days_range(self, api_client, api_headers):
        """Test that days parameter validates range (1-365)."""
        # Test too low
        r = api_client.get("/analytics/accuracy?days=0", headers=api_headers)
        assert r.status_code == 422  # Pydantic validation error

        # Test too high
        r = api_client.get("/analytics/accuracy?days=366", headers=api_headers)
        assert r.status_code == 422


# ── Cost Analytics ─────────────────────────────────────────────────────────


class TestCostAnalytics:
    def test_returns_cost_stats(self, api_client, api_headers):
        """Test that cost endpoint returns token usage statistics."""
        mock_cost = {
            "avg_tokens_per_incident": 12500.5,
            "trend": {
                "current": 12500.5,
                "previous": 11000.0,
                "delta_pct": 13.6,
            },
            "by_mode": [
                {
                    "mode": "sre",
                    "incident_count": 50,
                    "total_tokens": 500000,
                    "avg_tokens": 10000.0,
                },
                {
                    "mode": "security",
                    "incident_count": 25,
                    "total_tokens": 300000,
                    "avg_tokens": 12000.0,
                },
            ],
            "total_tokens": 800000,
            "total_incidents": 75,
        }

        with patch("sre_agent.api.analytics_rest._compute_cost_stats", return_value=mock_cost):
            r = api_client.get("/analytics/cost", headers=api_headers)

        assert r.status_code == 200
        data = r.json()
        assert data["avg_tokens_per_incident"] == 12500.5
        assert data["trend"]["current"] == 12500.5
        assert data["trend"]["previous"] == 11000.0
        assert data["trend"]["delta_pct"] == 13.6
        assert len(data["by_mode"]) == 2
        assert data["by_mode"][0]["mode"] == "sre"
        assert data["by_mode"][0]["incident_count"] == 50
        assert data["total_tokens"] == 800000
        assert data["total_incidents"] == 75

    def test_no_data(self, api_client, api_headers):
        """Test that no data returns zero stats."""
        mock_empty = {
            "avg_tokens_per_incident": 0,
            "trend": {"current": 0, "previous": 0, "delta_pct": 0.0},
            "by_mode": [],
            "total_tokens": 0,
            "total_incidents": 0,
        }

        with patch("sre_agent.api.analytics_rest._compute_cost_stats", return_value=mock_empty):
            r = api_client.get("/analytics/cost", headers=api_headers)

        assert r.status_code == 200
        data = r.json()
        assert data["avg_tokens_per_incident"] == 0
        assert data["total_tokens"] == 0
        assert data["total_incidents"] == 0
        assert data["by_mode"] == []

    def test_requires_auth(self, api_client):
        """Test that the endpoint requires authentication."""
        r = api_client.get("/analytics/cost")
        assert r.status_code == 401

    def test_custom_days_parameter(self, api_client, api_headers):
        """Test that days parameter is passed through correctly."""
        mock_cost = {
            "avg_tokens_per_incident": 10000.0,
            "trend": {"current": 10000.0, "previous": 9500.0, "delta_pct": 5.3},
            "by_mode": [],
            "total_tokens": 500000,
            "total_incidents": 50,
        }

        with patch("sre_agent.api.analytics_rest._compute_cost_stats") as mock_fn:
            mock_fn.return_value = mock_cost
            r = api_client.get("/analytics/cost?days=7", headers=api_headers)

        assert r.status_code == 200
        mock_fn.assert_called_once_with(7)

    def test_validates_days_range(self, api_client, api_headers):
        """Test that days parameter validates range (1-365)."""
        # Test too low
        r = api_client.get("/analytics/cost?days=0", headers=api_headers)
        assert r.status_code == 422  # Pydantic validation error

        # Test too high
        r = api_client.get("/analytics/cost?days=366", headers=api_headers)
        assert r.status_code == 422


# ── Intelligence Analytics ────────────────────────────────────────────────────


class TestIntelligenceAnalytics:
    def test_returns_all_sections(self, api_client, api_headers):
        """Test that intelligence endpoint returns all 8 sections."""
        mock_intelligence = {
            "query_reliability": {
                "preferred": [
                    {"query": "up{job='kube-state-metrics'}", "success_rate": 0.95, "total": 100},
                    {"query": "node_cpu_seconds_total", "success_rate": 0.92, "total": 85},
                ],
                "unreliable": [
                    {"query": "bad_query{}", "success_rate": 0.15, "total": 20},
                ],
            },
            "error_hotspots": [
                {"tool": "get_pod_logs", "error_rate": 0.15, "total": 50, "common_error": "Pod not found"},
                {"tool": "scale_deployment", "error_rate": 0.08, "total": 25, "common_error": "Permission denied"},
            ],
            "token_efficiency": {
                "avg_input": 12500,
                "avg_output": 3200,
                "cache_hit_rate": 45.5,
            },
            "harness_effectiveness": {
                "accuracy": 82.5,
                "wasted": [
                    {"tool": "unused_tool", "offered": 50, "used": 2},
                ],
            },
            "routing_accuracy": {
                "mode_switch_rate": 5.2,
                "total_sessions": 150,
            },
            "feedback_analysis": {
                "negative": [
                    {"tool": "some_tool", "count": 8},
                ],
            },
            "token_trending": {
                "input_delta_pct": 12.5,
                "output_delta_pct": -3.2,
                "cache_delta_pct": 8.1,
            },
            "dashboard_patterns": {
                "top_components": [
                    {"kind": "namespace_summary", "count": 42},
                    {"kind": "create_dashboard", "count": 25},
                ],
                "avg_widgets": 6,
            },
        }

        with patch("sre_agent.intelligence.get_intelligence_sections", return_value=mock_intelligence):
            r = api_client.get("/analytics/intelligence", headers=api_headers)

        assert r.status_code == 200
        data = r.json()

        # Verify all 8 sections are present
        assert "query_reliability" in data
        assert "error_hotspots" in data
        assert "token_efficiency" in data
        assert "harness_effectiveness" in data
        assert "routing_accuracy" in data
        assert "feedback_analysis" in data
        assert "token_trending" in data
        assert "dashboard_patterns" in data

        # Verify structure of a few sections
        assert len(data["query_reliability"]["preferred"]) == 2
        assert len(data["query_reliability"]["unreliable"]) == 1
        assert data["query_reliability"]["preferred"][0]["query"] == "up{job='kube-state-metrics'}"

        assert len(data["error_hotspots"]) == 2
        assert data["error_hotspots"][0]["tool"] == "get_pod_logs"
        assert data["error_hotspots"][0]["error_rate"] == 0.15

        assert data["token_efficiency"]["avg_input"] == 12500
        assert data["token_efficiency"]["cache_hit_rate"] == 45.5

        assert data["harness_effectiveness"]["accuracy"] == 82.5
        assert len(data["harness_effectiveness"]["wasted"]) == 1

        assert data["routing_accuracy"]["mode_switch_rate"] == 5.2

        assert len(data["feedback_analysis"]["negative"]) == 1

        assert data["token_trending"]["input_delta_pct"] == 12.5

        assert len(data["dashboard_patterns"]["top_components"]) == 2
        assert data["dashboard_patterns"]["avg_widgets"] == 6

    def test_requires_auth(self, api_client):
        """Test that the endpoint requires authentication."""
        r = api_client.get("/analytics/intelligence")
        assert r.status_code == 401

    def test_custom_days_parameter(self, api_client, api_headers):
        """Test that days parameter is passed through correctly."""
        mock_intelligence = {
            "query_reliability": {"preferred": [], "unreliable": []},
            "error_hotspots": [],
            "token_efficiency": {"avg_input": 0, "avg_output": 0, "cache_hit_rate": 0.0},
            "harness_effectiveness": {"accuracy": 0.0, "wasted": []},
            "routing_accuracy": {"mode_switch_rate": 0.0, "total_sessions": 0},
            "feedback_analysis": {"negative": []},
            "token_trending": {"input_delta_pct": 0.0, "output_delta_pct": 0.0, "cache_delta_pct": 0.0},
            "dashboard_patterns": {"top_components": [], "avg_widgets": 0},
        }

        with patch("sre_agent.intelligence.get_intelligence_sections") as mock_fn:
            mock_fn.return_value = mock_intelligence
            r = api_client.get("/analytics/intelligence?days=30", headers=api_headers)

        assert r.status_code == 200
        # Verify that the function was called with correct params
        mock_fn.assert_called_once_with("sre", 30)

    def test_validates_days_range(self, api_client, api_headers):
        """Test that days parameter validates range (1-90)."""
        # Test too low
        r = api_client.get("/analytics/intelligence?days=0", headers=api_headers)
        assert r.status_code == 422  # Pydantic validation error

        # Test too high
        r = api_client.get("/analytics/intelligence?days=91", headers=api_headers)
        assert r.status_code == 422

    def test_custom_mode_parameter(self, api_client, api_headers):
        """Test that mode parameter is passed through correctly."""
        mock_intelligence = {
            "query_reliability": {"preferred": [], "unreliable": []},
            "error_hotspots": [],
            "token_efficiency": {"avg_input": 0, "avg_output": 0, "cache_hit_rate": 0.0},
            "harness_effectiveness": {"accuracy": 0.0, "wasted": []},
            "routing_accuracy": {"mode_switch_rate": 0.0, "total_sessions": 0},
            "feedback_analysis": {"negative": []},
            "token_trending": {"input_delta_pct": 0.0, "output_delta_pct": 0.0, "cache_delta_pct": 0.0},
            "dashboard_patterns": {"top_components": [], "avg_widgets": 0},
        }

        with patch("sre_agent.intelligence.get_intelligence_sections") as mock_fn:
            mock_fn.return_value = mock_intelligence
            r = api_client.get("/analytics/intelligence?mode=security&days=14", headers=api_headers)

        assert r.status_code == 200
        # Verify that the function was called with correct params
        mock_fn.assert_called_once_with("security", 14)


# ── Prompt Analytics ──────────────────────────────────────────────────────


class TestPromptAnalytics:
    def test_returns_prompt_stats(self, api_client, api_headers):
        with patch("sre_agent.api.analytics_rest._get_prompt_analytics") as mock:
            mock.return_value = {
                "stats": {
                    "total_prompts": 200,
                    "avg_tokens": 14200,
                    "cache_hit_rate": 0.89,
                    "section_avg": {},
                    "by_skill": [],
                },
                "versions": [],
            }
            r = api_client.get("/analytics/prompt", headers=api_headers)
        assert r.status_code == 200
        assert r.json()["stats"]["total_prompts"] == 200

    def test_requires_auth(self, api_client):
        r = api_client.get("/analytics/prompt")
        assert r.status_code == 401


# ── Recommendations ───────────────────────────────────────────────────────


class TestRecommendations:
    def test_returns_recommendations(self, api_client, api_headers):
        with patch("sre_agent.api.analytics_rest._compute_recommendations") as mock:
            mock.return_value = {
                "recommendations": [
                    {
                        "type": "scanner",
                        "title": "Enable storage scanner",
                        "description": "...",
                        "action": {"kind": "enable_scanner", "scanner": "scan_storage"},
                    }
                ]
            }
            r = api_client.get("/recommendations", headers=api_headers)
        assert r.status_code == 200
        assert len(r.json()["recommendations"]) == 1

    def test_empty(self, api_client, api_headers):
        with patch("sre_agent.api.analytics_rest._compute_recommendations") as mock:
            mock.return_value = {"recommendations": []}
            r = api_client.get("/recommendations", headers=api_headers)
        assert r.status_code == 200
        assert r.json()["recommendations"] == []

    def test_requires_auth(self, api_client):
        r = api_client.get("/recommendations")
        assert r.status_code == 401


# ── Readiness Summary ─────────────────────────────────────────────────────


class TestReadinessSummary:
    def test_returns_summary(self, api_client, api_headers):
        with patch("sre_agent.api.analytics_rest._get_readiness_summary") as mock:
            mock.return_value = {
                "total_gates": 30,
                "passed": 28,
                "failed": 1,
                "attention": 1,
                "pass_rate": 0.933,
                "attention_items": [{"gate": "cert_expiry", "message": "Certificate expiring"}],
            }
            r = api_client.get("/analytics/readiness", headers=api_headers)
        assert r.status_code == 200
        assert r.json()["passed"] == 28

    def test_requires_auth(self, api_client):
        r = api_client.get("/analytics/readiness")
        assert r.status_code == 401


# ── Session Analytics ─────────────────────────────────────────────────────


class TestSessionAnalytics:
    def test_returns_session_data_with_summary(self, api_client, api_headers):
        """Test that sessions endpoint returns summary totals and breakdowns."""
        mock_db = MagicMock()
        mock_db.fetchone.side_effect = [
            {"total_views": 180, "total_sessions": 25, "unique_pages": 8},
            {"total_queries": 42},
            {"avg_ms": 35200},
        ]
        mock_db.fetchall.side_effect = [
            [{"page": "/pulse", "views": 50, "sessions": 20}],
            [{"page": "/pulse", "avg_ms": 45300, "sample_count": 18}],
            [{"page": "/incidents", "queries": 15}],
            [{"suggestion": "Show crashlooping pods", "clicks": 8}],
            [{"feature": "chart_edit", "uses": 12}],
        ]

        with patch("sre_agent.db.get_database", return_value=mock_db):
            r = api_client.get("/analytics/sessions?days=7", headers=api_headers)

        assert r.status_code == 200
        data = r.json()

        assert "summary" in data
        assert data["summary"]["total_sessions"] == 25
        assert data["summary"]["total_page_views"] == 180
        assert data["summary"]["unique_pages"] == 8
        assert data["summary"]["total_queries"] == 42
        assert data["summary"]["avg_duration_seconds"] == 35.2

        assert len(data["pages"]) == 1
        assert data["pages"][0]["page"] == "/pulse"
        assert data["pages"][0]["views"] == 50

        assert len(data["time_on_page"]) == 1
        assert data["time_on_page"][0]["avg_seconds"] == 45.3

        assert len(data["agent_queries_by_page"]) == 1
        assert data["agent_queries_by_page"][0]["queries"] == 15

        assert len(data["top_suggestions"]) == 1
        assert data["top_suggestions"][0]["clicks"] == 8

        assert len(data["feature_usage"]) == 1
        assert data["feature_usage"][0]["uses"] == 12

        assert data["days"] == 7

    def test_empty_session_data(self, api_client, api_headers):
        """Test that empty data returns zero summary and empty arrays."""
        mock_db = MagicMock()
        mock_db.fetchone.side_effect = [
            {"total_views": 0, "total_sessions": 0, "unique_pages": 0},
            {"total_queries": 0},
            {"avg_ms": None},
        ]
        mock_db.fetchall.side_effect = [[], [], [], [], []]

        with patch("sre_agent.db.get_database", return_value=mock_db):
            r = api_client.get("/analytics/sessions", headers=api_headers)

        assert r.status_code == 200
        data = r.json()
        assert data["summary"]["total_sessions"] == 0
        assert data["summary"]["total_page_views"] == 0
        assert data["summary"]["avg_duration_seconds"] == 0
        assert data["pages"] == []
        assert data["time_on_page"] == []

    def test_requires_auth(self, api_client):
        """Test that the endpoint requires authentication."""
        r = api_client.get("/analytics/sessions")
        assert r.status_code == 401

    def test_custom_days_parameter(self, api_client, api_headers):
        """Test that days parameter is passed through to queries."""
        mock_db = MagicMock()
        mock_db.fetchone.side_effect = [
            {"total_views": 0, "total_sessions": 0, "unique_pages": 0},
            {"total_queries": 0},
            {"avg_ms": None},
        ]
        mock_db.fetchall.side_effect = [[], [], [], [], []]

        with patch("sre_agent.db.get_database", return_value=mock_db):
            r = api_client.get("/analytics/sessions?days=30", headers=api_headers)

        assert r.status_code == 200
        assert r.json()["days"] == 30

    def test_validates_days_range(self, api_client, api_headers):
        """Test that days parameter validates range (1-90)."""
        r = api_client.get("/analytics/sessions?days=0", headers=api_headers)
        assert r.status_code == 422

        r = api_client.get("/analytics/sessions?days=91", headers=api_headers)
        assert r.status_code == 422

    def test_db_exception_returns_fallback(self, api_client, api_headers):
        """Test that database errors return graceful fallback with summary."""
        with patch("sre_agent.db.get_database", side_effect=Exception("DB down")):
            r = api_client.get("/analytics/sessions", headers=api_headers)

        assert r.status_code == 200
        data = r.json()
        assert data["summary"]["total_sessions"] == 0
        assert data["summary"]["total_page_views"] == 0
        assert data["pages"] == []
