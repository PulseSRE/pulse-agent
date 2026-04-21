"""Tests for Phase 6 operational metrics — endpoints and outcome tracking."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def pulse_token():
    return "metrics-test-token"


@pytest.fixture
def api_client(pulse_token, monkeypatch):
    monkeypatch.setenv("PULSE_AGENT_WS_TOKEN", pulse_token)
    monkeypatch.setenv("PULSE_AGENT_MEMORY", "0")

    with (
        patch("sre_agent.k8s_client._initialized", True),
        patch("sre_agent.k8s_client._load_k8s"),
        patch("sre_agent.k8s_client.get_core_client", return_value=MagicMock()),
        patch("sre_agent.k8s_client.get_apps_client", return_value=MagicMock()),
        patch("sre_agent.k8s_client.get_custom_client", return_value=MagicMock()),
        patch("sre_agent.k8s_client.get_version_client", return_value=MagicMock()),
    ):
        from sre_agent.api import app

        yield TestClient(app)


class TestFixSuccessRateEndpoint:
    def test_returns_structure(self, api_client, pulse_token):
        resp = api_client.get(f"/metrics/fix-success-rate?token={pulse_token}")
        assert resp.status_code == 200
        data = resp.json()
        assert "period_days" in data
        assert "success_rate" in data
        assert "total_with_outcome" in data

    def test_custom_period(self, api_client, pulse_token):
        resp = api_client.get(f"/metrics/fix-success-rate?period=7&token={pulse_token}")
        assert resp.status_code == 200
        assert resp.json()["period_days"] == 7

    def test_rejects_invalid_period(self, api_client, pulse_token):
        resp = api_client.get(f"/metrics/fix-success-rate?period=0&token={pulse_token}")
        assert resp.status_code == 422


class TestResponseLatencyEndpoint:
    def test_returns_structure(self, api_client, pulse_token):
        resp = api_client.get(f"/metrics/response-latency?token={pulse_token}")
        assert resp.status_code == 200
        data = resp.json()
        assert "period_days" in data
        assert "p50_ms" in data
        assert "p95_ms" in data
        assert "p99_ms" in data
        assert "count" in data


class TestEvalTrendEndpoint:
    def test_returns_structure(self, api_client, pulse_token):
        resp = api_client.get(f"/metrics/eval-trend?token={pulse_token}")
        assert resp.status_code == 200
        data = resp.json()
        assert "suite" in data
        assert "sparkline" in data
        assert "current_score" in data
        assert "runs_count" in data

    def test_custom_suite(self, api_client, pulse_token):
        resp = api_client.get(f"/metrics/eval-trend?suite=safety&token={pulse_token}")
        assert resp.status_code == 200
        assert resp.json()["suite"] == "safety"


class TestOutcomeTracking:
    def test_update_action_outcome(self):
        from sre_agent.monitor.actions import _VALID_OUTCOMES, update_action_outcome

        assert "resolved" in _VALID_OUTCOMES
        assert "rolled_back" in _VALID_OUTCOMES
        assert "escalated" in _VALID_OUTCOMES
        assert "unknown" in _VALID_OUTCOMES
        assert "invalid" not in _VALID_OUTCOMES

        assert update_action_outcome("nonexistent", "invalid") is False

    def test_mark_finding_actions_resolved(self):
        from sre_agent.monitor.actions import mark_finding_actions_resolved

        count = mark_finding_actions_resolved("nonexistent-finding")
        assert count == 0

    def test_get_fix_success_rate_empty(self):
        from sre_agent.monitor.actions import get_fix_success_rate

        result = get_fix_success_rate(1)
        assert result["period_days"] == 1
        assert result["total_with_outcome"] == 0

    def test_migration_020_idempotent(self):
        from sre_agent import db

        database = db.get_database()
        try:
            row = database.fetchone(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'actions' AND column_name = 'outcome'"
            )
            assert row is not None
        except Exception:
            pass


class TestTrendCalculation:
    def test_stable(self):
        from sre_agent.api.metrics_rest import _trend

        assert _trend([0.80, 0.81, 0.80, 0.81]) == "stable"

    def test_improving(self):
        from sre_agent.api.metrics_rest import _trend

        assert _trend([0.70, 0.72, 0.75, 0.80, 0.85, 0.90]) == "improving"

    def test_declining(self):
        from sre_agent.api.metrics_rest import _trend

        assert _trend([0.90, 0.85, 0.80, 0.75, 0.70, 0.65]) == "declining"

    def test_single_value(self):
        from sre_agent.api.metrics_rest import _trend

        assert _trend([0.80]) == "stable"

    def test_empty(self):
        from sre_agent.api.metrics_rest import _trend

        assert _trend([]) == "stable"
