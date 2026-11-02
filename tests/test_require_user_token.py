"""Routes that read the cluster as the caller refuse a caller without a token.

``user_token_context(None)`` does nothing, so with token forwarding on, a read
wrapped in it ran as the agent's service account whenever
X-Forwarded-Access-Token was missing: anyone holding only the shared agent
token saw whatever the service account can read. Each route here now answers
401 instead; with a token the read runs under that token; with forwarding off
the service account path is unchanged, by design.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

SHARED = "test-shared-token"
AUTH = {"Authorization": f"Bearer {SHARED}"}
# X-Forwarded-User pins the identity so view ownership does not depend on a
# TokenReview the test cannot answer; the token is what is under test.
AS_USER = {**AUTH, "X-Forwarded-User": "mallory", "X-Forwarded-Access-Token": "user-token-abc"}
NO_TOKEN = {**AUTH, "X-Forwarded-User": "mallory"}


def _settings(monkeypatch, forwarding: bool):
    from sre_agent.config import _reset_settings

    monkeypatch.setenv("PULSE_AGENT_WS_TOKEN", SHARED)
    monkeypatch.setenv("PULSE_AGENT_MEMORY", "0")
    monkeypatch.setenv("PULSE_AGENT_DEV_USER", "mallory")
    monkeypatch.setenv("PULSE_AGENT_ADMIN_USERS", "mallory")
    monkeypatch.setenv("PULSE_AGENT_TOKEN_FORWARDING", "true" if forwarding else "false")
    _reset_settings()


@pytest.fixture
def client(monkeypatch):
    from sre_agent.config import _reset_settings

    _settings(monkeypatch, forwarding=True)
    from sre_agent.api.app import app

    yield TestClient(app)
    _reset_settings()


@pytest.fixture
def sa_client(monkeypatch):
    """Token forwarding off: the service account is the intended reader."""
    from sre_agent.config import _reset_settings

    _settings(monkeypatch, forwarding=False)
    from sre_agent.api.app import app

    yield TestClient(app)
    _reset_settings()


# --- helpers -------------------------------------------------------------------


def _graph():
    node = SimpleNamespace(kind="Pod", namespace="a", name="p", labels={})
    graph = MagicMock()
    graph.get_nodes.return_value = {"Pod/a/p": node}
    graph.get_edges.return_value = []
    graph.get_node.return_value = node
    graph.downstream_blast_radius.return_value = []
    graph.upstream_dependencies.return_value = []
    graph._last_refresh = 0
    return graph


def _repo():
    repo = MagicMock()
    repo.async_fetch_active_findings = AsyncMock(return_value=[])
    repo.fetch_deployment_risk_findings.return_value = []
    repo.fetch_recent_deployments.return_value = []
    return repo


class _Recorder:
    """Records the user token current when the cluster is read."""

    def __init__(self):
        self.seen: list = []

    def metrics(self, namespace):
        from sre_agent import k8s_client

        self.seen.append(k8s_client.get_current_user_token())
        return {}, {}

    def promql(self, q, time_range):
        from sre_agent import k8s_client

        self.seen.append(k8s_client.get_current_user_token())
        return ("text", None)

    def core(self):
        from sre_agent import k8s_client

        self.seen.append(k8s_client.get_current_user_token())
        m = MagicMock()
        m.list_namespaced_pod.return_value = MagicMock(items=[])
        return m


def _patched_graph(rec: _Recorder):
    return (
        patch("sre_agent.dependency_graph.get_dependency_graph", return_value=_graph()),
        patch("sre_agent.dependency_graph._fetch_metrics", side_effect=rec.metrics),
        patch("sre_agent.repositories.get_monitor_repo", return_value=_repo()),
    )


GRAPH_ROUTES = [
    ("get", "/topology?include_metrics=true", None),
    ("get", "/topology/blast-radius?node_id=Pod/a/p", None),
    ("get", "/incidents/f-1/impact?kind=Pod&namespace=a&name=p", None),
    (
        "post",
        "/monitor/simulate",
        {"tool": "restart_deployment", "input": {}, "target_resource": {"kind": "Pod", "namespace": "a", "name": "p"}},
    ),
]


# --- topology / impact / simulate ----------------------------------------------


class TestGraphRoutes:
    @pytest.mark.parametrize(("method", "path", "body"), GRAPH_ROUTES)
    def test_no_user_token_is_401_and_nothing_is_read(self, client, method, path, body):
        rec = _Recorder()
        g, m, r = _patched_graph(rec)
        with g as graph, m, r:
            resp = client.request(method, path, headers=NO_TOKEN, json=body)
        assert resp.status_code == 401
        assert "User access token required" in resp.json()["detail"]
        graph.assert_not_called()
        assert rec.seen == []

    @pytest.mark.parametrize(("method", "path", "body"), GRAPH_ROUTES)
    def test_with_user_token_the_route_answers(self, client, method, path, body):
        rec = _Recorder()
        g, m, r = _patched_graph(rec)
        with g, m, r:
            resp = client.request(method, path, headers=AS_USER, json=body)
        assert resp.status_code == 200

    def test_topology_metrics_are_read_with_the_callers_token(self, client):
        rec = _Recorder()
        g, m, r = _patched_graph(rec)
        with g, m, r:
            resp = client.get("/topology?include_metrics=true", headers=AS_USER)
        assert resp.status_code == 200
        assert rec.seen == ["user-token-abc"]

    @pytest.mark.parametrize(("method", "path", "body"), GRAPH_ROUTES)
    def test_forwarding_off_keeps_the_service_account_path(self, sa_client, method, path, body):
        rec = _Recorder()
        g, m, r = _patched_graph(rec)
        with g, m, r:
            resp = sa_client.request(method, path, headers=NO_TOKEN, json=body)
        assert resp.status_code == 200
        if "include_metrics" in path:
            assert rec.seen == [None]


# --- /query and /log-counts -------------------------------------------------


class TestWidgetData:
    def test_query_without_token_is_401(self, client):
        rec = _Recorder()
        with patch("sre_agent.k8s_tools.get_prometheus_query.func", side_effect=rec.promql):
            resp = client.get("/query?q=up", headers=NO_TOKEN)
        assert resp.status_code == 401
        assert rec.seen == []

    def test_log_counts_without_token_is_401(self, client):
        rec = _Recorder()
        with patch("sre_agent.k8s_client.get_core_client", side_effect=rec.core):
            resp = client.get("/log-counts?namespace=openshift-authentication", headers=NO_TOKEN)
        assert resp.status_code == 401
        assert rec.seen == []

    def test_query_with_token_runs_as_the_caller(self, client):
        rec = _Recorder()
        with patch("sre_agent.k8s_tools.get_prometheus_query.func", side_effect=rec.promql):
            resp = client.get("/query?q=up", headers=AS_USER)
        assert resp.status_code == 200
        assert rec.seen == ["user-token-abc"]

    def test_forwarding_off_query_and_log_counts_use_the_service_account(self, sa_client):
        rec = _Recorder()
        with (
            patch("sre_agent.k8s_tools.get_prometheus_query.func", side_effect=rec.promql),
            patch("sre_agent.k8s_client.get_core_client", side_effect=rec.core),
        ):
            assert sa_client.get("/query?q=up", headers=NO_TOKEN).status_code == 200
            assert sa_client.get("/log-counts?namespace=a", headers=NO_TOKEN).status_code == 200
        assert rec.seen == [None, None]


# --- view actions ----------------------------------------------------------------


def _view_with_read_button():
    button = {"kind": "action_button", "label": "x", "action": "list_pods", "action_input": {"namespace": "a"}}
    return {"id": "cv-1", "owner": "mallory", "layout": [button]}


ACTION = {"action": "list_pods", "action_input": {"namespace": "a"}}


class TestViewActions:
    def test_a_read_action_without_token_is_401(self, client):
        with (
            patch("sre_agent.db.get_view", return_value=_view_with_read_button()),
            patch("sre_agent.agent._execute_tool") as execute,
        ):
            resp = client.post("/views/cv-1/actions", headers=NO_TOKEN, json=ACTION)
        assert resp.status_code == 401
        assert "sign in again" in resp.json()["error"]
        execute.assert_not_called()

    def test_a_read_action_runs_with_the_callers_token(self, client):
        with (
            patch("sre_agent.db.get_view", return_value=_view_with_read_button()),
            patch("sre_agent.agent._execute_tool", return_value=("ok", None, {"status": "success"})) as execute,
        ):
            resp = client.post("/views/cv-1/actions", headers=AS_USER, json=ACTION)
        assert resp.status_code == 200
        assert execute.call_args.args[3] == "user-token-abc"

    def test_forwarding_off_runs_as_the_service_account(self, sa_client):
        with (
            patch("sre_agent.db.get_view", return_value=_view_with_read_button()),
            patch("sre_agent.agent._execute_tool", return_value=("ok", None, {"status": "success"})) as execute,
        ):
            resp = sa_client.post("/views/cv-1/actions", headers=NO_TOKEN, json=ACTION)
        assert resp.status_code == 200
        assert execute.call_args.args[3] is None


# --- inbox claim -------------------------------------------------------------------


class TestInboxClaim:
    def test_claim_without_token_is_401_and_nothing_runs(self, client):
        with patch("sre_agent.api.inbox_rest.claim_item") as claim:
            resp = client.post("/inbox/i-1/claim", headers=NO_TOKEN)
        assert resp.status_code == 401
        claim.assert_not_called()

    def test_claim_builds_the_view_with_the_callers_token(self, client):
        with patch("sre_agent.api.inbox_rest.claim_item", return_value=True) as claim:
            resp = client.post("/inbox/i-1/claim", headers=AS_USER)
        assert resp.status_code == 200
        assert claim.call_args.args == ("i-1", "mallory", "user-token-abc")

    def test_forwarding_off_claims_as_the_service_account(self, sa_client):
        with patch("sre_agent.api.inbox_rest.claim_item", return_value=True) as claim:
            resp = sa_client.post("/inbox/i-1/claim", headers=NO_TOKEN)
        assert resp.status_code == 200
        assert claim.call_args.args == ("i-1", "mallory", None)


# --- rollback keeps its message ------------------------------------------------------


class TestRollback:
    def test_rollback_without_token_keeps_its_401_message(self, client):
        with patch("sre_agent.api.fix_rest.execute_rollback") as rollback:
            resp = client.post("/fix-history/a-1/rollback", headers=NO_TOKEN)
        assert resp.status_code == 401
        assert resp.json()["detail"] == "User access token required for rollback"
        rollback.assert_not_called()

    def test_rollback_runs_with_the_callers_token(self, client):
        from sre_agent import k8s_client

        seen: list = []

        def rollback(action_id):
            seen.append(k8s_client.get_current_user_token())
            return {"ok": True}

        with patch("sre_agent.api.fix_rest.execute_rollback", side_effect=rollback):
            resp = client.post("/fix-history/a-1/rollback", headers=AS_USER)
        assert resp.status_code == 200
        assert seen == ["user-token-abc"]


# --- agent chat sockets ----------------------------------------------------------------


@pytest.fixture
def k8s_stubbed():
    with (
        patch("sre_agent.k8s_client._initialized", True),
        patch("sre_agent.k8s_client._load_k8s"),
        patch("sre_agent.k8s_client.get_core_client", return_value=MagicMock()),
        patch("sre_agent.k8s_client.get_apps_client", return_value=MagicMock()),
        patch("sre_agent.k8s_client.get_custom_client", return_value=MagicMock()),
        patch("sre_agent.k8s_client.get_version_client", return_value=MagicMock()),
    ):
        yield


class TestAgentSockets:
    # Only /ws/agent and /ws/monitor are routed; the per-mode handler carries the
    # same gate but is not mounted.
    @pytest.mark.parametrize("path", ["/ws/agent"])
    def test_chat_without_user_token_is_closed(self, client, k8s_stubbed, path):
        with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect(f"{path}?token={SHARED}") as ws:
            ws.receive_json()
        assert exc.value.code == 4001
        assert "User access token required" in exc.value.reason

    @pytest.mark.parametrize("path", ["/ws/agent"])
    def test_chat_with_user_token_connects(self, client, k8s_stubbed, path):
        with client.websocket_connect(
            f"{path}?token={SHARED}", headers={"X-Forwarded-Access-Token": "user-token-abc"}
        ) as ws:
            ws.send_json({"type": "clear"})
            assert ws.receive_json()["type"] == "cleared"

    def test_forwarding_off_chat_connects_without_user_token(self, sa_client, k8s_stubbed):
        with sa_client.websocket_connect(f"/ws/agent?token={SHARED}") as ws:
            ws.send_json({"type": "clear"})
            assert ws.receive_json()["type"] == "cleared"

    def test_monitor_socket_is_not_gated(self, client, k8s_stubbed):
        """The monitor pushes the service account's findings by design."""
        with client.websocket_connect(f"/ws/monitor?token={SHARED}") as ws:
            ws.send_json({"type": "subscribe_monitor", "trustLevel": 0})
            assert ws.receive_json()["type"] in {"ready", "monitor_status", "finding", "findings_snapshot"}


def test_require_user_token_helper(monkeypatch):
    from fastapi import HTTPException

    from sre_agent.api.auth import require_user_token, user_token_missing
    from sre_agent.config import _reset_settings

    _settings(monkeypatch, forwarding=True)
    assert user_token_missing(None) and user_token_missing("")
    assert require_user_token("t") == "t"
    with pytest.raises(HTTPException) as exc:
        require_user_token(None, "custom")
    assert exc.value.status_code == 401 and exc.value.detail == "custom"

    _settings(monkeypatch, forwarding=False)
    assert not user_token_missing(None)
    assert require_user_token(None) is None
    _reset_settings()
