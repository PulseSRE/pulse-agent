"""WebSocket integration tests for /ws/agent, /ws/agent, /ws/monitor endpoints."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


@pytest.fixture
def pulse_token():
    return "test-ws-token-xyz"


@pytest.fixture
def ws_client(pulse_token, monkeypatch, tmp_path):
    monkeypatch.setenv("PULSE_AGENT_WS_TOKEN", pulse_token)
    monkeypatch.setenv("PULSE_AGENT_MEMORY", "0")
    # PULSE_AGENT_DATABASE_URL set by conftest autouse fixture

    with (
        patch("sre_agent.k8s_client._initialized", True),
        patch("sre_agent.k8s_client._load_k8s"),
        patch("sre_agent.k8s_client.get_core_client", return_value=MagicMock()),
        patch("sre_agent.k8s_client.get_apps_client", return_value=MagicMock()),
        patch("sre_agent.k8s_client.get_custom_client", return_value=MagicMock()),
        patch("sre_agent.k8s_client.get_version_client", return_value=MagicMock()),
    ):
        from sre_agent.api import app

        # The UI's proxy forwards the signed-in user's token on every agent
        # socket; chat without one is refused while token forwarding is on.
        yield TestClient(app, headers={"X-Forwarded-Access-Token": "test-user-token"})


# ── Auth ────────────────────────────────────────────────────────────────────


class TestWebSocketAuth:
    def test_sre_rejects_no_token(self, ws_client):
        with pytest.raises((WebSocketDisconnect, Exception)), ws_client.websocket_connect("/ws/agent"):
            pass

    def test_sre_rejects_wrong_token(self, ws_client):
        with pytest.raises((WebSocketDisconnect, Exception)), ws_client.websocket_connect("/ws/agent?token=wrong"):
            pass

    def test_sre_accepts_valid_token(self, ws_client, pulse_token):
        with ws_client.websocket_connect(f"/ws/agent?token={pulse_token}") as ws:
            ws.send_json({"type": "clear"})
            data = ws.receive_json()
            assert data["type"] == "cleared"

    def test_invalid_mode_rejected(self, ws_client, pulse_token):
        with (
            pytest.raises((WebSocketDisconnect, Exception)),
            ws_client.websocket_connect(f"/ws/invalid?token={pulse_token}"),
        ):
            pass

    def test_security_mode_connects(self, ws_client, pulse_token):
        with ws_client.websocket_connect(f"/ws/agent?token={pulse_token}") as ws:
            ws.send_json({"type": "clear"})
            data = ws.receive_json()
            assert data["type"] == "cleared"


# ── Protocol ────────────────────────────────────────────────────────────────


class TestWebSocketProtocol:
    def test_clear_resets_conversation(self, ws_client, pulse_token):
        with ws_client.websocket_connect(f"/ws/agent?token={pulse_token}") as ws:
            ws.send_json({"type": "clear"})
            data = ws.receive_json()
            assert data["type"] == "cleared"

    def test_invalid_json_returns_error(self, ws_client, pulse_token):
        with ws_client.websocket_connect(f"/ws/agent?token={pulse_token}") as ws:
            ws.send_text("not json at all")
            data = ws.receive_json()
            assert data["type"] == "error"
            assert "Invalid JSON" in data["message"]

    def test_feedback_returns_ack(self, ws_client, pulse_token):
        with ws_client.websocket_connect(f"/ws/agent?token={pulse_token}") as ws:
            ws.send_json({"type": "feedback", "resolved": True})
            data = ws.receive_json()
            assert data["type"] == "feedback_ack"
            assert data["resolved"] is True
            assert "score" in data

    def test_feedback_with_message_id(self, ws_client, pulse_token):
        with ws_client.websocket_connect(f"/ws/agent?token={pulse_token}") as ws:
            ws.send_json({"type": "feedback", "resolved": False, "messageId": "msg-123"})
            data = ws.receive_json()
            assert data["type"] == "feedback_ack"
            assert data["resolved"] is False

    def test_message_triggers_agent_response(self, ws_client, pulse_token):
        """Send a message and verify we get at least a done or error event back."""
        with (
            patch("sre_agent.api.agent_ws.run_agent_streaming", return_value="Test response"),
            patch("sre_agent.api.agent_ws.create_async_client", return_value=MagicMock()),
            ws_client.websocket_connect(f"/ws/agent?token={pulse_token}") as ws,
        ):
            ws.send_json({"type": "message", "content": "hello"})
            events = []
            for _ in range(20):
                try:
                    data = ws.receive_json(mode="text")
                    events.append(data)
                    if data["type"] in ("done", "error"):
                        break
                except Exception:
                    break
            assert len(events) > 0
            last = events[-1]
            assert last["type"] in ("done", "error")


# ── Auto-Agent ──────────────────────────────────────────────────────────────


class TestAutoAgent:
    def test_agent_mode_connects(self, ws_client, pulse_token):
        with ws_client.websocket_connect(f"/ws/agent?token={pulse_token}") as ws:
            ws.send_json({"type": "clear"})
            data = ws.receive_json()
            assert data["type"] == "cleared"

    def test_agent_mode_feedback(self, ws_client, pulse_token):
        with ws_client.websocket_connect(f"/ws/agent?token={pulse_token}") as ws:
            ws.send_json({"type": "feedback", "resolved": True})
            data = ws.receive_json()
            assert data["type"] == "feedback_ack"


# ── Monitor ─────────────────────────────────────────────────────────────────


class TestMonitorWebSocket:
    def test_monitor_connects(self, ws_client, pulse_token):
        with ws_client.websocket_connect(f"/ws/monitor?token={pulse_token}") as ws:
            ws.send_json(
                {
                    "type": "subscribe_monitor",
                    "trustLevel": 1,
                    "autoFixCategories": [],
                }
            )
            ws.close()

    def test_monitor_rejects_no_token(self, ws_client):
        with pytest.raises((WebSocketDisconnect, Exception)), ws_client.websocket_connect("/ws/monitor"):
            pass


# ── Multi-Skill ───────────────────────────────────────────────────────────


def _configure_multi_skill_settings(mock_settings, pulse_token):
    mock_settings.return_value.multi_skill = True
    mock_settings.return_value.max_trust_level = 3
    mock_settings.return_value.ws_token = pulse_token
    mock_settings.return_value.scan_interval = 60
    mock_settings.return_value.autofix_enabled = True
    mock_settings.return_value.memory_enabled = False


def _collect_events(ws, content="check pods and scan security"):
    ws.send_json({"type": "message", "content": content})
    events = []
    for _ in range(30):
        try:
            data = ws.receive_json(mode="text")
            events.append(data)
            if data["type"] in ("done", "error"):
                break
        except Exception:
            break
    return events


class TestMultiSkillFlow:
    """Integration tests for parallel multi-skill execution via /ws/agent."""

    def test_multi_skill_emits_correct_event_sequence(self, ws_client, pulse_token):
        """Multi-skill query should emit multi_skill_start → skill_progress → done with multi_skill metadata."""
        from unittest.mock import AsyncMock

        from sre_agent.synthesis import ParallelSkillResult, SynthesisResult

        from .conftest import _mock_skill

        mock_sre = _mock_skill("sre")
        mock_sec = _mock_skill("security")
        mock_parallel = ParallelSkillResult(
            primary_output="SRE: pods crashlooping due to OOM",
            secondary_output="Security: RBAC risks found",
            primary_skill="sre",
            secondary_skill="security",
            primary_confidence=0.8,
            secondary_confidence=0.7,
            duration_ms=3000,
        )
        mock_synthesis = SynthesisResult(
            unified_response="Combined: OOM crashes detected and RBAC risks found.",
            conflicts=[],
            sources={"sre": "pods crashlooping", "security": "RBAC risks"},
        )

        with (
            patch("sre_agent.skill_loader.classify_query_multi", return_value=(mock_sre, mock_sec)),
            patch("sre_agent.skill_loader.classify_query", return_value=mock_sre),
            patch("sre_agent.config.get_settings") as mock_settings,
            patch("sre_agent.plan_runtime.run_parallel_skills", new_callable=AsyncMock, return_value=mock_parallel),
            patch(
                "sre_agent.synthesis.synthesize_parallel_outputs", new_callable=AsyncMock, return_value=mock_synthesis
            ),
            patch("sre_agent.agent.create_async_client", return_value=MagicMock()),
            ws_client.websocket_connect(f"/ws/agent?token={pulse_token}") as ws,
        ):
            _configure_multi_skill_settings(mock_settings, pulse_token)
            events = _collect_events(ws)

            event_types = [e["type"] for e in events]
            assert "multi_skill_start" in event_types, f"Expected multi_skill_start, got: {event_types}"
            assert "done" in event_types

            done_event = next(e for e in events if e["type"] == "done")
            assert "multi_skill" in done_event
            assert done_event["multi_skill"]["skills"] == ["sre", "security"]

    def test_empty_output_skips_synthesis(self, ws_client, pulse_token):
        """When one skill returns empty, synthesis is skipped and a warning is shown."""
        from unittest.mock import AsyncMock

        from sre_agent.synthesis import ParallelSkillResult

        from .conftest import _mock_skill

        mock_sre = _mock_skill("sre")
        mock_sec = _mock_skill("security")
        mock_parallel = ParallelSkillResult(
            primary_output="SRE: pods crashlooping due to OOM",
            secondary_output="",
            primary_skill="sre",
            secondary_skill="security",
            primary_confidence=0.8,
            secondary_confidence=0.0,
            duration_ms=2000,
        )

        with (
            patch("sre_agent.skill_loader.classify_query_multi", return_value=(mock_sre, mock_sec)),
            patch("sre_agent.skill_loader.classify_query", return_value=mock_sre),
            patch("sre_agent.config.get_settings") as mock_settings,
            patch("sre_agent.plan_runtime.run_parallel_skills", new_callable=AsyncMock, return_value=mock_parallel),
            ws_client.websocket_connect(f"/ws/agent?token={pulse_token}") as ws,
        ):
            _configure_multi_skill_settings(mock_settings, pulse_token)
            events = _collect_events(ws)

            done_event = next((e for e in events if e["type"] == "done"), None)
            assert done_event is not None
            assert "security skill did not return results" in done_event["full_response"]
            assert done_event.get("multi_skill", {}).get("empty_skill") == "security"
