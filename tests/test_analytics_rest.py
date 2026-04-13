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

        with patch("sre_agent.api.monitor_rest.get_fix_history_summary", return_value=mock_summary):
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

        with patch("sre_agent.api.monitor_rest.get_fix_history_summary", return_value=mock_empty):
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

        with patch("sre_agent.api.monitor_rest.get_fix_history_summary") as mock_fn:
            mock_fn.return_value = mock_summary
            r = api_client.get("/fix-history/summary?days=30", headers=api_headers)

        assert r.status_code == 200
        # Verify that the function was called with days=30
        mock_fn.assert_called_once_with(30)


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

        with patch("sre_agent.api.monitor_rest.get_scanner_coverage", return_value=mock_coverage):
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

        with patch("sre_agent.api.monitor_rest.get_scanner_coverage") as mock_fn:
            mock_fn.return_value = mock_coverage
            r = api_client.get("/monitor/coverage?days=30", headers=api_headers)

        assert r.status_code == 200
        # Verify that the function was called with days=30
        mock_fn.assert_called_once_with(30)
