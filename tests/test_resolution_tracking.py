"""Tests for resolution tracking API."""

from __future__ import annotations

import time
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


class TestFixHistorySummaryVerification:
    def test_summary_includes_verification_counts(self):
        from sre_agent import db
        from sre_agent.api.monitor_rest import get_fix_history_summary

        database = db.get_database()

        # Clean up any stale data from prior aborted runs, then insert
        ts = int(time.time() * 1000)
        database.execute("DELETE FROM actions WHERE id IN (?, ?, ?, ?)", ("a1", "a2", "a3", "a4"))
        database.commit()
        database.execute(
            "INSERT INTO actions (id, finding_id, status, category, duration_ms, verification_status, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("a1", "f1", "completed", "crashloop", 100, "verified", ts),
        )
        database.execute(
            "INSERT INTO actions (id, finding_id, status, category, duration_ms, verification_status, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("a2", "f2", "completed", "workloads", 200, "still_failing", ts),
        )
        database.execute(
            "INSERT INTO actions (id, finding_id, status, category, duration_ms, verification_status, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("a3", "f3", "completed", "crashloop", 150, "verified", ts),
        )
        database.execute(
            "INSERT INTO actions (id, finding_id, status, category, duration_ms, verification_status, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("a4", "f4", "failed", "image_pull", None, None, ts),
        )
        database.commit()

        try:
            result = get_fix_history_summary(days=7)

            assert "verification" in result
            assert result["verification"]["resolved"] == 2
            assert result["verification"]["still_failing"] == 1
            assert result["verification"]["pending"] == 1
            assert result["verification"]["resolution_rate"] == 0.5
        finally:
            # Cleanup
            database.execute("DELETE FROM actions WHERE id IN (?, ?, ?, ?)", ("a1", "a2", "a3", "a4"))

    def test_summary_verification_empty(self):
        from sre_agent.api.monitor_rest import get_fix_history_summary

        result = get_fix_history_summary(days=7)

        assert "verification" in result
        assert result["verification"]["resolved"] >= 0
        assert result["verification"]["resolution_rate"] >= 0.0


class TestResolutionsEndpoint:
    def test_endpoint_exists(self, api_client, api_headers):
        resp = api_client.get(
            "/fix-history/resolutions?days=7",
            headers=api_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "resolutions" in data
        assert "total" in data
        assert isinstance(data["resolutions"], list)

    def test_returns_verified_actions(self, api_client, api_headers):
        from sre_agent import db

        database = db.get_database()

        # Insert test action with verification
        ts = int(time.time() * 1000)
        database.execute(
            "INSERT INTO actions (id, finding_id, status, category, tool, reasoning, "
            "verification_status, verification_evidence, verification_timestamp, timestamp, duration_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "r1",
                "f1",
                "completed",
                "crashloop",
                "delete_pod",
                "Restarting pod",
                "verified",
                "Pod restarted successfully",
                ts + 5000,
                ts,
                100,
            ),
        )
        database.commit()

        try:
            resp = api_client.get(
                "/fix-history/resolutions?days=7&limit=100",
                headers=api_headers,
            )
            assert resp.status_code == 200
            data = resp.json()

            # Should find our test action
            resolutions = [r for r in data["resolutions"] if r["id"] == "r1"]
            assert len(resolutions) == 1

            r = resolutions[0]
            assert r["outcome"] == "verified"
            assert r["evidence"] == "Pod restarted successfully"
            assert r["timeToVerifyMs"] == 5000
        finally:
            # Cleanup
            database.execute("DELETE FROM actions WHERE id = ?", ("r1",))


class TestFixOutcomes:
    @patch("sre_agent.db.get_database")
    def test_returns_strategy_success_rates(self, mock_get_db):
        from sre_agent.intelligence import _compute_fix_outcomes

        db = MagicMock()
        mock_get_db.return_value = db
        db.fetchall.return_value = [
            {"tool": "rollback_deployment", "category": "image_pull", "total": 10, "resolved": 8},
            {"tool": "restart_deployment", "category": "workloads", "total": 5, "resolved": 1},
            {"tool": "patch_resources", "category": "crashloop", "total": 3, "resolved": 3},
            {"tool": "delete_pod", "category": "crashloop", "total": 8, "resolved": 2},
        ]
        result = _compute_fix_outcomes(7)
        assert "rollback_deployment" in result
        assert "80%" in result
        assert "effective" in result
        assert "patch_resources" in result
        assert "ineffective" in result  # delete_pod at 25%

    @patch("sre_agent.db.get_database")
    def test_returns_empty_when_no_data(self, mock_get_db):
        from sre_agent.intelligence import _compute_fix_outcomes

        db = MagicMock()
        mock_get_db.return_value = db
        db.fetchall.return_value = []
        result = _compute_fix_outcomes(7)
        assert result == ""

    @patch("sre_agent.db.get_database")
    def test_no_crash_on_db_error(self, mock_get_db):
        from sre_agent.intelligence import _compute_fix_outcomes

        mock_get_db.side_effect = Exception("DB down")
        result = _compute_fix_outcomes(7)
        assert result == ""
