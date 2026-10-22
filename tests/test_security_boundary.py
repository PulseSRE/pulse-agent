"""Security boundary regressions found in the 2026-10-21 review.

Each test reproduces a request that used to succeed:

- a non-admin rewrote the system prompt through a view's action endpoint,
  which ran whatever tool name the request body named;
- /log-counts and /query ran as the agent's service account, so a user with
  no RBAC could read any namespace's logs (by counting matches) and every
  metric;
- multi-skill chat turns dropped the user's token;
- describe_resource returned Secret values to the model;
- monitor fix approvals and plan-template edits were open to non-admins;
- the shared WebSocket token was written to the access log.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

TOKEN = "test-shared-token"


@pytest.fixture
def settings_env(monkeypatch):
    """Shared token set, admin allowlist = alice, caller identified as mallory."""
    from sre_agent.config import _reset_settings

    monkeypatch.setenv("PULSE_AGENT_WS_TOKEN", TOKEN)
    monkeypatch.setenv("PULSE_AGENT_MEMORY", "0")
    monkeypatch.setenv("PULSE_AGENT_ADMIN_USERS", "alice")
    monkeypatch.setenv("PULSE_AGENT_DEV_USER", "mallory")
    monkeypatch.setenv("PULSE_AGENT_TOKEN_FORWARDING", "true")
    _reset_settings()
    yield monkeypatch
    _reset_settings()


@pytest.fixture
def client(settings_env):
    from sre_agent.api.app import app

    return TestClient(app)


HEADERS = {"Authorization": f"Bearer {TOKEN}"}


# --- Admin-only tools -----------------------------------------------------


class TestAdminOnlyTools:
    def test_is_admin_follows_the_allowlist(self, settings_env):
        from sre_agent.admin_tools import is_admin

        assert is_admin("alice") is True
        assert is_admin("mallory") is False
        assert is_admin("anonymous") is False
        assert is_admin(None) is False

    def test_without_an_allowlist_any_identified_user_but_not_anonymous(self, settings_env):
        from sre_agent.admin_tools import is_admin
        from sre_agent.config import _reset_settings

        settings_env.delenv("PULSE_AGENT_ADMIN_USERS")
        _reset_settings()
        assert is_admin("mallory") is True
        assert is_admin("anonymous") is False

    def test_non_admin_chat_is_never_offered_skill_editing(self, settings_env):
        from sre_agent.admin_tools import ADMIN_ONLY_TOOLS, without_admin_tools

        config = {
            "tool_map": {"edit_skill": object(), "list_pods": object()},
            "tool_defs": [{"name": "edit_skill"}, {"name": "list_pods"}],
            "system_prompt": "x",
        }
        stripped = without_admin_tools(config, "mallory")
        assert set(stripped["tool_map"]) == {"list_pods"}
        assert [d["name"] for d in stripped["tool_defs"]] == ["list_pods"]
        assert without_admin_tools(config, "alice") is config
        assert "edit_skill" in ADMIN_ONLY_TOOLS

    def test_skill_executor_strips_admin_tools_before_the_model_sees_them(self, settings_env):
        from sre_agent.api.agent_ws import SkillExecutor

        seen: dict = {}

        async def fake_loop(client, messages, system_prompt, tool_defs, tool_map, *a, **kw):
            seen["tools"] = set(tool_map)
            seen["defs"] = [d["name"] for d in tool_defs]
            return "done"

        config = {
            "system_prompt": "x",
            "tool_defs": [{"name": "edit_skill"}, {"name": "list_pods"}],
            "tool_map": {"edit_skill": MagicMock(), "list_pods": MagicMock()},
        }
        ws = MagicMock(send_json=AsyncMock())
        with patch("sre_agent.api.agent_ws.run_agent_streaming", side_effect=fake_loop):
            asyncio.run(SkillExecutor(ws, "s1").run(config, [], MagicMock(), set(), "sre", current_user="mallory"))
        assert "edit_skill" not in seen["tools"] and "edit_skill" not in seen["defs"]
        assert "list_pods" in seen["tools"]


# --- View actions ---------------------------------------------------------


def _view(*buttons):
    return {"id": "cv-1", "owner": "mallory", "layout": list(buttons)}


class TestViewActions:
    def test_skill_editing_is_refused_even_when_the_view_carries_it(self, client):
        """The reproduced attack: a user's own view with an edit_skill button."""
        button = {"kind": "action_button", "label": "x", "action": "edit_skill", "action_input": {"name": "sre"}}
        with (
            patch("sre_agent.db.get_view", return_value=_view(button)),
            patch("sre_agent.agent._execute_tool") as execute,
        ):
            resp = client.post(
                "/views/cv-1/actions",
                headers=HEADERS,
                json={"action": "edit_skill", "action_input": {"name": "sre"}},
            )
        assert resp.status_code == 403
        execute.assert_not_called()

    def test_a_tool_the_view_does_not_carry_is_refused(self, client):
        with (
            patch("sre_agent.db.get_view", return_value=_view()),
            patch("sre_agent.agent._execute_tool") as execute,
        ):
            resp = client.post(
                "/views/cv-1/actions",
                headers=HEADERS,
                json={"action": "list_pods", "action_input": {"namespace": "default"}},
            )
        assert resp.status_code == 403
        assert "no such action" in resp.json()["error"]
        execute.assert_not_called()

    def test_input_must_match_the_stored_button(self, client):
        button = {"kind": "action_button", "label": "x", "action": "list_pods", "action_input": {"namespace": "a"}}
        with (
            patch("sre_agent.db.get_view", return_value=_view(button)),
            patch("sre_agent.agent._execute_tool") as execute,
        ):
            resp = client.post(
                "/views/cv-1/actions",
                headers=HEADERS,
                json={"action": "list_pods", "action_input": {"namespace": "kube-system"}},
            )
        assert resp.status_code == 403
        execute.assert_not_called()

    def test_a_stored_button_runs_and_trust_comes_from_the_button(self, client):
        button = {
            "kind": "action_button",
            "label": "x",
            "action": "list_pods",
            "action_input": {"namespace": "a"},
            "min_trust_level": 4,
        }
        with (
            patch("sre_agent.db.get_view", return_value=_view(button)),
            patch("sre_agent.agent._execute_tool", return_value=("ok", None, {"status": "success"})),
        ):
            # A body claiming min_trust_level 0 no longer lowers the stored requirement.
            resp = client.post(
                "/views/cv-1/actions",
                headers=HEADERS,
                json={"action": "list_pods", "action_input": {"namespace": "a"}, "min_trust_level": 0},
            )
        assert resp.status_code == 403
        assert "trust level 4" in resp.json()["error"]

        button.pop("min_trust_level")
        with (
            patch("sre_agent.db.get_view", return_value=_view(button)),
            patch("sre_agent.agent._execute_tool", return_value=("ok", None, {"status": "success"})),
        ):
            resp = client.post(
                "/views/cv-1/actions", headers=HEADERS, json={"action": "list_pods", "action_input": {"namespace": "a"}}
            )
        assert resp.status_code == 200


# --- Caller's token, not the service account --------------------------------


class TestCallerToken:
    def test_log_counts_reads_logs_with_the_callers_token(self, client):
        from sre_agent import k8s_client

        seen: list = []

        def core():
            seen.append(k8s_client.get_current_user_token())
            m = MagicMock()
            m.list_namespaced_pod.return_value = MagicMock(items=[])
            return m

        with patch("sre_agent.k8s_client.get_core_client", side_effect=core):
            resp = client.get(
                "/log-counts?namespace=openshift-authentication",
                headers={**HEADERS, "X-Forwarded-Access-Token": "user-token-abc"},
            )
        assert resp.status_code == 200
        assert seen == ["user-token-abc"]

    def test_query_runs_promql_with_the_callers_token(self, client):
        from sre_agent import k8s_client

        seen: list = []

        def fake_query(q, time_range):
            seen.append(k8s_client.get_current_user_token())
            return ("text", None)

        with patch("sre_agent.k8s_tools.get_prometheus_query.func", side_effect=fake_query):
            resp = client.get("/query?q=up", headers={**HEADERS, "X-Forwarded-Access-Token": "user-token-abc"})
        assert resp.status_code == 200
        assert seen == ["user-token-abc"]

    def test_prometheus_sends_the_callers_token_when_there_is_one(self, settings_env):
        from sre_agent import k8s_client
        from sre_agent.prometheus import PrometheusBackend, PrometheusClient

        sent: list = []

        def urlopen(req, context=None, timeout=None):
            sent.append(req.headers.get("Authorization"))
            return MagicMock(read=lambda: b'{"status": "success"}')

        client = PrometheusClient()
        with (
            patch.object(PrometheusClient, "_get_url", return_value="https://thanos:9091"),
            patch.object(PrometheusClient, "_build_ssl_context", return_value=None),
            patch.object(PrometheusClient, "_get_token", return_value="sa-token"),
            patch("urllib.request.urlopen", side_effect=urlopen),
        ):
            with k8s_client.user_token_context("user-token-abc"):
                client.request("api/v1/query", {"query": "up"}, 5, PrometheusBackend.LOCAL)
            client.request("api/v1/query", {"query": "up"}, 5, PrometheusBackend.LOCAL)
        assert sent == ["Bearer user-token-abc", "Bearer sa-token"]

    def test_parallel_skills_carry_the_users_token_and_identity(self, settings_env):
        from sre_agent import plan_runtime

        made: list = []

        class FakeExecutor:
            def __init__(self, websocket, session_id, user_token=None):
                made.append(user_token)

            async def run(self, config, messages, client, write_tools, mode, *, skill_tag="", current_user="anonymous"):
                made.append(current_user)
                return MagicMock(text="", components=[], token_usage={})

        skill = MagicMock(name="sre")
        with (
            patch("sre_agent.api.agent_ws.SkillExecutor", FakeExecutor),
            patch(
                "sre_agent.skill_loader.build_config_from_skill",
                return_value={"system_prompt": "", "tool_defs": [], "tool_map": {}, "write_tools": set()},
            ),
        ):
            asyncio.run(
                plan_runtime.run_parallel_skills(
                    skill,
                    skill,
                    "q",
                    [],
                    MagicMock(close=AsyncMock()),
                    websocket=MagicMock(send_json=AsyncMock()),
                    session_id="s",
                    user_token="user-token-abc",
                    current_user="mallory",
                )
            )
        assert made[0] == "user-token-abc"
        assert made[1:] == ["mallory", "mallory"]


# --- Secrets ----------------------------------------------------------------


class TestSecretRedaction:
    def test_describe_resource_never_returns_secret_values(self, settings_env):
        from sre_agent.k8s_tools import generic

        secret = {
            "kind": "Secret",
            "metadata": {
                "name": "db",
                "namespace": "shop",
                "annotations": {
                    "kubectl.kubernetes.io/last-applied-configuration": '{"data":{"password":"aHVudGVyMg=="}}'
                },
            },
            "data": {"password": "aHVudGVyMg=="},
            "stringData": {"token": "plaintext-token"},
            "type": "Opaque",
        }
        events = MagicMock()
        events.list_namespaced_event.return_value = MagicMock(items=[])
        with (
            patch.object(generic._kc, "get_raw_json", return_value=secret),
            patch("sre_agent.k8s_client._load_k8s"),
            patch("sre_agent.k8s_client.get_core_client", return_value=events),
        ):
            out = generic.describe_resource.func("shop", "db", "Secret")
        text = out[0] if isinstance(out, tuple) else out
        assert "aHVudGVyMg==" not in text
        assert "plaintext-token" not in text
        assert "hunter2" not in text
        assert "<redacted: 7 bytes>" in text
        assert '"password"' in text, "key names stay visible for diagnosis"


# --- Admin acts -------------------------------------------------------------


class TestAdminActs:
    def test_non_admin_cannot_answer_monitor_fix_proposals(self, settings_env):
        from sre_agent.monitor.session import MonitorClient

        ws = MagicMock()
        assert MonitorClient(ws, 2, [], user="mallory").can_approve is False
        assert MonitorClient(ws, 2, [], user="alice").can_approve is True

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("post", "/plan-templates"),
            ("put", "/plan-templates/crashloop"),
            ("delete", "/plan-templates/crashloop"),
        ],
    )
    def test_plan_template_changes_need_an_admin(self, client, method, path):
        body = {"incident_type": "x", "phases": [{"id": "a", "skill_name": "sre"}]}
        resp = getattr(client, method)(path, headers=HEADERS, **({"json": body} if method != "delete" else {}))
        assert resp.status_code == 403


# --- Logs -------------------------------------------------------------------


class TestTokenNotLogged:
    def test_websocket_token_is_masked_in_uvicorn_lines(self, caplog):
        from sre_agent.logging_config import RedactQueryTokens

        logger = logging.getLogger("uvicorn.error.test-redaction")
        logger.addFilter(RedactQueryTokens())
        with caplog.at_level(logging.INFO, logger="uvicorn.error.test-redaction"):
            logger.info('%s - "WebSocket %s" [accepted]', "10.0.0.1:5555", "/ws/agent?token=s3cr3t&x=1")
        assert "s3cr3t" not in caplog.text
        assert "token=<redacted>&x=1" in caplog.text
