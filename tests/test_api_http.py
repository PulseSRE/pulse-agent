"""HTTP-level tests for the 21 REST endpoints in sre_agent/api.py.

Tests authentication, response shapes, error handling, and status codes.
All K8s clients and database operations are mocked.
"""

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
def api_client(pulse_token, monkeypatch, tmp_path):
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


# ── Unauthenticated Endpoints ──────────────────────────────────────────────


class TestPublicEndpoints:
    def test_healthz(self, api_client):
        r = api_client.get("/healthz")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_version(self, api_client):
        r = api_client.get("/version")
        assert r.status_code == 200
        data = r.json()
        assert "protocol" in data
        assert "agent" in data
        assert "tools" in data
        assert "features" in data


# ── Authentication ──────────────────────────────────────────────────────────


class TestAuthentication:
    @pytest.mark.parametrize(
        "endpoint",
        [
            "/health",
            "/tools",
            "/fix-history",
            "/briefing",
            "/memory/stats",
            "/memory/export",
            "/monitor/capabilities",
        ],
    )
    def test_missing_token_returns_401(self, api_client, endpoint):
        r = api_client.get(endpoint)
        assert r.status_code == 401

    @pytest.mark.parametrize("endpoint", ["/health", "/tools"])
    def test_invalid_token_returns_401(self, api_client, endpoint):
        r = api_client.get(endpoint, headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401

    @pytest.mark.parametrize("endpoint", ["/health", "/tools"])
    def test_bearer_header_auth(self, api_client, api_headers, endpoint):
        r = api_client.get(endpoint, headers=api_headers)
        assert r.status_code == 200

    @pytest.mark.parametrize("endpoint", ["/health", "/tools"])
    def test_query_param_auth(self, api_client, pulse_token, endpoint):
        r = api_client.get(endpoint, params={"token": pulse_token})
        assert r.status_code == 200

    def test_server_not_configured(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PULSE_AGENT_WS_TOKEN", "")
        monkeypatch.setenv("PULSE_AGENT_MEMORY", "0")
        # PULSE_AGENT_DATABASE_URL set by conftest autouse fixture
        with (
            patch("sre_agent.k8s_client._initialized", True),
            patch("sre_agent.k8s_client._load_k8s"),
            patch("sre_agent.k8s_client.get_core_client", return_value=MagicMock()),
            patch("sre_agent.k8s_client.get_apps_client"),
            patch("sre_agent.k8s_client.get_custom_client"),
            patch("sre_agent.k8s_client.get_version_client"),
        ):
            from sre_agent.api import app

            client = TestClient(app, raise_server_exceptions=False)
            r = client.get("/health", headers={"Authorization": "Bearer test"})
            assert r.status_code in (401, 503)


# ── Fix History ─────────────────────────────────────────────────────────────


class TestFixHistory:
    def test_fix_history_empty(self, api_client, api_headers):
        r = api_client.get("/fix-history", headers=api_headers)
        assert r.status_code == 200
        data = r.json()
        assert "actions" in data
        assert "total" in data
        assert "page" in data

    def test_fix_history_pagination(self, api_client, api_headers):
        r = api_client.get("/fix-history?page=2&page_size=5", headers=api_headers)
        assert r.status_code == 200

    def test_action_detail_not_found(self, api_client, api_headers):
        r = api_client.get("/fix-history/nonexistent", headers=api_headers)
        assert r.status_code in (200, 404)

    def test_rollback_not_found(self, api_client, api_headers):
        r = api_client.post("/fix-history/nonexistent/rollback", headers=api_headers)
        assert r.status_code == 400


# ── Health & Tools ──────────────────────────────────────────────────────────


class TestHealthAndTools:
    def test_health_returns_status(self, api_client, api_headers):
        r = api_client.get("/health", headers=api_headers)
        assert r.status_code == 200
        data = r.json()
        assert "status" in data

    def test_tools_lists_modes(self, api_client, api_headers):
        r = api_client.get("/tools", headers=api_headers)
        assert r.status_code == 200
        data = r.json()
        assert "sre" in data
        assert "security" in data


# ── Briefing & Simulate ────────────────────────────────────────────────────


class TestBriefingAndSimulate:
    def test_briefing_default(self, api_client, api_headers):
        r = api_client.get("/briefing", headers=api_headers)
        assert r.status_code == 200
        data = r.json()
        assert "greeting" in data
        assert "summary" in data

    def test_briefing_custom_hours(self, api_client, api_headers):
        r = api_client.get("/briefing?hours=1", headers=api_headers)
        assert r.status_code == 200


# ── Monitor Control ─────────────────────────────────────────────────────────


class TestMonitorControl:
    def test_capabilities(self, api_client, api_headers):
        r = api_client.get("/monitor/capabilities", headers=api_headers)
        assert r.status_code == 200
        data = r.json()
        assert "max_trust_level" in data

    def test_pause(self, api_client, api_headers):
        r = api_client.post("/monitor/pause", headers=api_headers)
        assert r.status_code == 200

    def test_resume(self, api_client, api_headers):
        r = api_client.post("/monitor/resume", headers=api_headers)
        assert r.status_code == 200


# ── Memory Endpoints ────────────────────────────────────────────────────────


class TestMemoryEndpoints:
    def test_memory_stats_disabled(self, api_client, api_headers):
        r = api_client.get("/memory/stats", headers=api_headers)
        assert r.status_code == 200
        data = r.json()
        assert data.get("enabled") is False or "incidents" in data

    def test_memory_export(self, api_client, api_headers):
        r = api_client.get("/memory/export", headers=api_headers)
        assert r.status_code == 200
        data = r.json()
        assert "runbooks" in data
        assert "patterns" in data

    def test_memory_import(self, api_client, api_headers):
        r = api_client.post("/memory/import", headers=api_headers, json={"runbooks": [], "patterns": []})
        assert r.status_code == 200

    def test_memory_runbooks(self, api_client, api_headers):
        r = api_client.get("/memory/runbooks", headers=api_headers)
        assert r.status_code == 200

    def test_memory_runbooks_trigger_keywords_normalized(self, api_client, api_headers, monkeypatch):
        """trigger_keywords must be an array, never a raw string (GH fix for .map crash)."""
        fake_runbooks = [
            {"name": "rb1", "trigger_keywords": "crashloop oom restart", "tool_sequence": []},
            {"name": "rb2", "trigger_keywords": "", "tool_sequence": []},
            {"name": "rb3", "trigger_keywords": ["already", "an", "array"], "tool_sequence": []},
        ]

        class FakeStore:
            def list_runbooks(self):
                return [dict(r) for r in fake_runbooks]

        class FakeManager:
            store = FakeStore()

        monkeypatch.setattr("sre_agent.memory.get_manager", lambda: FakeManager())
        r = api_client.get("/memory/runbooks", headers=api_headers)
        assert r.status_code == 200
        rbs = r.json()["runbooks"]
        assert rbs[0]["trigger_keywords"] == ["crashloop", "oom", "restart"]
        assert rbs[1]["trigger_keywords"] == []
        assert rbs[2]["trigger_keywords"] == ["already", "an", "array"]

    def test_memory_incidents(self, api_client, api_headers):
        r = api_client.get("/memory/incidents", headers=api_headers)
        assert r.status_code == 200

    def test_memory_patterns(self, api_client, api_headers):
        r = api_client.get("/memory/patterns", headers=api_headers)
        assert r.status_code == 200


# ── Context & Eval ──────────────────────────────────────────────────────────


class TestContextAndEval:
    def test_eval_status(self, api_client, api_headers):
        r = api_client.get("/eval/status", headers=api_headers)
        assert r.status_code == 200


class TestChatHistoryAPI:
    @pytest.fixture(autouse=True)
    def _set_dev_user(self, monkeypatch):
        monkeypatch.setenv("PULSE_AGENT_DEV_USER", "test-user")
        from sre_agent.config import _reset_settings

        _reset_settings()

    def test_list_sessions(self, api_client, api_headers):
        r = api_client.get("/chat/sessions", headers=api_headers)
        assert r.status_code == 200
        assert isinstance(r.json()["sessions"], list)

    def test_create_and_list_session(self, api_client, api_headers):
        r = api_client.post("/chat/sessions", headers=api_headers, json={"title": "Test Chat", "agent_mode": "sre"})
        assert r.status_code == 200
        session_id = r.json()["id"]
        assert session_id

        r = api_client.get("/chat/sessions", headers=api_headers)
        sessions = r.json()["sessions"]
        assert any(s["id"] == session_id for s in sessions)
        assert any(s["title"] == "Test Chat" for s in sessions)

    def test_get_messages_empty(self, api_client, api_headers):
        r = api_client.post("/chat/sessions", headers=api_headers, json={"title": "Empty", "agent_mode": "auto"})
        sid = r.json()["id"]
        r = api_client.get(f"/chat/sessions/{sid}/messages", headers=api_headers)
        assert r.status_code == 200
        assert r.json()["messages"] == []
        assert r.json()["total"] == 0

    def test_delete_session(self, api_client, api_headers):
        r = api_client.post("/chat/sessions", headers=api_headers, json={"title": "To Delete"})
        sid = r.json()["id"]
        r = api_client.delete(f"/chat/sessions/{sid}", headers=api_headers)
        assert r.status_code == 200
        r = api_client.get("/chat/sessions", headers=api_headers)
        assert not any(s["id"] == sid for s in r.json()["sessions"])

    def test_rename_session(self, api_client, api_headers):
        r = api_client.post("/chat/sessions", headers=api_headers, json={"title": "Old Name"})
        sid = r.json()["id"]
        r = api_client.put(f"/chat/sessions/{sid}", headers=api_headers, json={"title": "New Name"})
        assert r.status_code == 200
        r = api_client.get("/chat/sessions", headers=api_headers)
        assert any(s["title"] == "New Name" for s in r.json()["sessions"])

    def test_nonexistent_session_messages(self, api_client, api_headers):
        r = api_client.get("/chat/sessions/nonexistent/messages", headers=api_headers)
        assert r.status_code == 200
        assert r.json()["messages"] == []

    def test_unauthorized(self, api_client):
        r = api_client.get("/chat/sessions")
        assert r.status_code in (401, 403, 503)


# ── SLO Registration Validation ───────────────────────────────────────────


class TestSLORegistration:
    def test_valid_slo(self, api_client, api_headers):
        r = api_client.post(
            "/slo",
            headers=api_headers,
            json={
                "service": "checkout",
                "type": "availability",
                "target": 99.9,
                "window_days": 30,
                "description": "Checkout service availability",
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "registered"
        assert data["service"] == "checkout"
        assert data["type"] == "availability"

    def test_invalid_slo_type(self, api_client, api_headers):
        r = api_client.post(
            "/slo",
            headers=api_headers,
            json={"service": "api", "type": "throughput", "target": 99.9},
        )
        assert r.status_code == 422
        assert "slo_type must be one of" in r.json()["detail"]

    def test_target_out_of_range_high(self, api_client, api_headers):
        r = api_client.post(
            "/slo",
            headers=api_headers,
            json={"service": "api", "type": "availability", "target": 100.0},
        )
        assert r.status_code == 422
        assert "target" in r.json()["detail"]

    def test_target_out_of_range_low(self, api_client, api_headers):
        r = api_client.post(
            "/slo",
            headers=api_headers,
            json={"service": "api", "type": "latency", "target": 0.0},
        )
        assert r.status_code == 422
        assert "target" in r.json()["detail"]

    def test_target_not_a_number(self, api_client, api_headers):
        r = api_client.post(
            "/slo",
            headers=api_headers,
            json={"service": "api", "type": "availability", "target": "high"},
        )
        assert r.status_code == 422
        assert "target" in r.json()["detail"]

    def test_window_days_too_large(self, api_client, api_headers):
        r = api_client.post(
            "/slo",
            headers=api_headers,
            json={"service": "api", "type": "error_rate", "target": 1.0, "window_days": 365},
        )
        assert r.status_code == 422
        assert "window_days" in r.json()["detail"]

    def test_window_days_zero(self, api_client, api_headers):
        r = api_client.post(
            "/slo",
            headers=api_headers,
            json={"service": "api", "type": "availability", "target": 99.9, "window_days": 0},
        )
        assert r.status_code == 422
        assert "window_days" in r.json()["detail"]

    def test_window_days_not_a_number(self, api_client, api_headers):
        r = api_client.post(
            "/slo",
            headers=api_headers,
            json={"service": "api", "type": "availability", "target": 99.9, "window_days": "monthly"},
        )
        assert r.status_code == 422
        assert "window_days" in r.json()["detail"]

    def test_missing_service_name(self, api_client, api_headers):
        r = api_client.post(
            "/slo",
            headers=api_headers,
            json={"type": "availability", "target": 99.9},
        )
        assert r.status_code == 400
        assert "service name" in r.json()["detail"]

    def test_all_valid_slo_types(self, api_client, api_headers):
        for slo_type in ("availability", "latency", "error_rate"):
            r = api_client.post(
                "/slo",
                headers=api_headers,
                json={"service": f"svc-{slo_type}", "type": slo_type, "target": 99.5},
            )
            assert r.status_code == 200, f"Failed for slo_type={slo_type}"

    def test_target_negative(self, api_client, api_headers):
        r = api_client.post(
            "/slo",
            headers=api_headers,
            json={"service": "api", "type": "availability", "target": -5.0},
        )
        assert r.status_code == 422
        assert "target" in r.json()["detail"]
