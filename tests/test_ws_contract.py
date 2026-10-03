"""WebSocket contract tests — validates message schemas against API_CONTRACT.md.

Ensures all documented message types are handled and follow the documented format.
Tests run against the FastAPI test client (no live server needed).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests.conftest import _StubAsyncClient


@pytest.fixture
def pulse_token():
    return "contract-test-token"


@pytest.fixture
def ws_client(pulse_token, monkeypatch):
    monkeypatch.setenv("PULSE_AGENT_WS_TOKEN", pulse_token)
    monkeypatch.setenv("PULSE_AGENT_MEMORY", "0")
    # Contract tests must remain deterministic even when a developer has live
    # provider credentials. Exercise the real websocket/agent path, replacing
    # only the external model transport.
    import anthropic

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _StubAsyncClient)
    monkeypatch.setattr(anthropic, "AsyncAnthropicVertex", _StubAsyncClient, raising=False)

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


# ---------------------------------------------------------------------------
# Chat Protocol (/ws/agent) — Client-to-Server Messages
# ---------------------------------------------------------------------------


class TestChatClientMessages:
    """Validate all client-to-server message types from API_CONTRACT.md."""

    def test_clear_message(self, ws_client, pulse_token):
        """clear → cleared response."""
        with ws_client.websocket_connect(f"/ws/agent?token={pulse_token}") as ws:
            ws.send_json({"type": "clear"})
            data = ws.receive_json()
            assert data["type"] == "cleared"

    def test_message_with_context(self, ws_client, pulse_token):
        """message with ResourceContext fields is accepted."""
        with ws_client.websocket_connect(f"/ws/agent?token={pulse_token}") as ws:
            ws.send_json(
                {
                    "type": "message",
                    "content": "test",
                    "context": {
                        "kind": "Deployment",
                        "name": "api-server",
                        "namespace": "production",
                        "gvr": "apps~v1~deployments",
                    },
                    "fleet": False,
                }
            )
            events = _receive_completed_turn(ws)
            assert events[-1]["full_response"] == _StubAsyncClient.STUB_TEXT

    def test_confirm_response_without_pending_nonce(self, ws_client, pulse_token):
        """confirm_response with no pending request should be ignored or error."""
        with ws_client.websocket_connect(f"/ws/agent?token={pulse_token}") as ws:
            ws.send_json(
                {
                    "type": "confirm_response",
                    "approved": True,
                    "nonce": "nonexistent-nonce",
                }
            )
            ws.send_json({"type": "clear"})
            data = ws.receive_json()
            assert data["type"] in ("cleared", "error")

    def test_unknown_message_type(self, ws_client, pulse_token):
        """Unknown message types should not crash the connection."""
        with ws_client.websocket_connect(f"/ws/agent?token={pulse_token}") as ws:
            ws.send_json({"type": "nonexistent_type"})
            ws.send_json({"type": "clear"})
            data = ws.receive_json()
            assert data["type"] in ("cleared", "error")


# ---------------------------------------------------------------------------
# Chat Protocol (/ws/agent) — Server-to-Client Events
# ---------------------------------------------------------------------------


class TestChatServerEvents:
    """Validate server-to-client event schemas."""

    def test_cleared_event_schema(self, ws_client, pulse_token):
        with ws_client.websocket_connect(f"/ws/agent?token={pulse_token}") as ws:
            ws.send_json({"type": "clear"})
            data = ws.receive_json()
            assert data == {"type": "cleared"}

    def test_done_event_has_full_response(self, ws_client, pulse_token):
        """After a message, the done event should include full_response."""
        with ws_client.websocket_connect(f"/ws/agent?token={pulse_token}") as ws:
            ws.send_json({"type": "message", "content": "hello"})
            events = _receive_completed_turn(ws)
            assert events[-1]["full_response"] == _StubAsyncClient.STUB_TEXT
            assert sum(event["type"] == "done" for event in events) == 1

    def test_text_delta_schema(self, ws_client, pulse_token):
        """A successful streamed turn must emit text, then a matching done."""
        with ws_client.websocket_connect(f"/ws/agent?token={pulse_token}") as ws:
            ws.send_json({"type": "message", "content": "hi"})
            events = _receive_completed_turn(ws)
            text_deltas = [event for event in events if event["type"] == "text_delta"]
            assert text_deltas, "Turn completed without streaming any text"
            assert all(isinstance(event["text"], str) for event in text_deltas)
            assert "".join(event["text"] for event in text_deltas) == events[-1]["full_response"]


def _receive_completed_turn(ws):
    """Errors, disconnects and missing terminal events must fail the contract."""
    events = []
    for _ in range(50):
        event = ws.receive_json()
        assert event.get("type") != "error", event
        events.append(event)
        if event.get("type") == "done":
            return events
    pytest.fail("No done event within 50 protocol messages")


# ---------------------------------------------------------------------------
# Monitor Protocol (/ws/monitor) — Client-to-Server Messages
# ---------------------------------------------------------------------------


class TestMonitorClientMessages:
    """Validate monitor client-to-server message types."""

    def test_subscribe_monitor(self, ws_client, pulse_token):
        """subscribe_monitor is accepted without error."""
        with ws_client.websocket_connect(f"/ws/monitor?token={pulse_token}") as ws:
            ws.send_json(
                {
                    "type": "subscribe_monitor",
                    "trustLevel": 1,
                    "autoFixCategories": ["crash_loop"],
                }
            )
            ws.close()

    def test_subscribe_monitor_defaults(self, ws_client, pulse_token):
        """subscribe_monitor works with minimal fields."""
        with ws_client.websocket_connect(f"/ws/monitor?token={pulse_token}") as ws:
            ws.send_json({"type": "subscribe_monitor"})
            ws.close()

    def test_monitor_rejects_no_token(self, ws_client):
        with pytest.raises(WebSocketDisconnect) as denied:
            with ws_client.websocket_connect("/ws/monitor"):
                pass
        assert denied.value.code == 4001


# ---------------------------------------------------------------------------
# Auth Contract
# ---------------------------------------------------------------------------


class TestAuthContract:
    """Verify auth behavior matches API_CONTRACT.md."""

    def test_no_token_disconnects_with_4001(self, ws_client):
        with pytest.raises(WebSocketDisconnect) as denied:
            with ws_client.websocket_connect("/ws/agent"):
                pass
        assert denied.value.code == 4001

    def test_wrong_token_disconnects(self, ws_client):
        with pytest.raises(WebSocketDisconnect) as denied:
            with ws_client.websocket_connect("/ws/agent?token=wrong"):
                pass
        assert denied.value.code == 4001

    def test_valid_token_connects(self, ws_client, pulse_token):
        with ws_client.websocket_connect(f"/ws/agent?token={pulse_token}") as ws:
            ws.send_json({"type": "clear"})
            assert ws.receive_json()["type"] == "cleared"


# ---------------------------------------------------------------------------
# REST Contract — Spot checks for key endpoints
# ---------------------------------------------------------------------------


class TestRESTContract:
    """Verify key REST endpoints match API_CONTRACT.md schemas."""

    def test_healthz(self, ws_client, pulse_token):
        resp = ws_client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_version(self, ws_client, pulse_token):
        resp = ws_client.get(f"/version?token={pulse_token}")
        data = resp.json()
        assert "protocol" in data
        assert "agent" in data
        assert "tools" in data
        assert isinstance(data["tools"], int)

    def test_health(self, ws_client, pulse_token):
        resp = ws_client.get(f"/health?token={pulse_token}")
        assert resp.status_code == 200
        data = resp.json()
        assert "circuit_breaker" in data
        assert data["circuit_breaker"]["state"] in ("closed", "open", "half_open")

    def test_tools(self, ws_client, pulse_token):
        resp = ws_client.get(f"/tools?token={pulse_token}")
        assert resp.status_code == 200
        data = resp.json()
        assert "sre" in data
        assert "security" in data
        assert isinstance(data["sre"], list)
        assert data["sre"], "SRE tool discovery unexpectedly returned no tools"
        for tool in data["sre"]:
            assert isinstance(tool["name"], str)
            assert isinstance(tool["description"], str)
            assert isinstance(tool["requires_confirmation"], bool)

    def test_agents(self, ws_client, pulse_token):
        resp = ws_client.get(f"/agents?token={pulse_token}")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert data, "Agent discovery unexpectedly returned no skills"
        for agent in data:
            assert isinstance(agent["name"], str)
            assert isinstance(agent["description"], str)
            assert isinstance(agent["tools_count"], int)

    def test_views_list(self, ws_client, pulse_token):
        resp = ws_client.get(
            f"/views?token={pulse_token}",
            headers={"X-Forwarded-User": "test-user"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "views" in data
        assert isinstance(data["views"], list)

    def test_unauthenticated_returns_401(self, ws_client):
        resp = ws_client.get("/tools")
        assert resp.status_code in (401, 403)
