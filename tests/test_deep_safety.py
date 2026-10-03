"""Cross-boundary regressions for dynamic tools, plans and owner migration."""

import hashlib
import sqlite3
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from sre_agent.mcp_client import MCPConnection, register_mcp_tools
from sre_agent.plan_runtime import PlanRuntime
from sre_agent.repositories.view_repo import ViewRepository
from sre_agent.skill_plan import SkillPhase
from sre_agent.tool_registry import TOOL_REGISTRY, WRITE_TOOL_NAMES, unregister_tool


class _MemoryDB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row

    def execute(self, sql, params=()):
        return self.conn.execute(sql, params)

    def commit(self):
        self.conn.commit()


def test_legacy_migration_cannot_claim_other_users_or_require_empty_new_owner():
    db = _MemoryDB()
    db.execute("CREATE TABLE views (id text, owner text)")
    alice = "user-" + hashlib.sha256(b"alice-token").hexdigest()[:16]
    bob = "user-" + hashlib.sha256(b"bob-token").hexdigest()[:16]
    for row in [("a", alice), ("b", bob), ("existing", "alice")]:
        db.execute("INSERT INTO views VALUES (?,?)", row)
    repo = ViewRepository(db=db)
    assert repo.migrate_view_ownership("charlie") == 0
    assert repo.migrate_view_ownership("alice", alice) == 1
    assert dict(db.execute("SELECT id,owner FROM views").fetchall()) == {"a": "alice", "b": bob, "existing": "alice"}


def test_forwarded_username_without_token_does_not_trigger_migration():
    from sre_agent.api.auth import _get_current_user

    with patch("sre_agent.db.migrate_view_ownership") as migrate:
        assert _get_current_user(x_forwarded_user="alice") == "alice"
        migrate.assert_not_called()


def test_forwarded_username_migrates_exact_token_hash():
    from sre_agent.api.auth import _get_current_user

    with patch("sre_agent.db.migrate_view_ownership", return_value=0) as migrate:
        assert _get_current_user(x_forwarded_user="alice", x_forwarded_access_token="alice-token") == "alice"
        migrate.assert_called_once_with("alice", "user-" + hashlib.sha256(b"alice-token").hexdigest()[:16])


@pytest.mark.parametrize(
    "annotations,write",
    [
        ({}, True),
        ({"readOnlyHint": False}, True),
        ({"readOnlyHint": True}, False),
        ({"readOnlyHint": True, "destructiveHint": True}, True),
    ],
)
def test_mcp_unknown_and_mutating_tools_require_gate(annotations, write):
    name = "deep_review_tool"
    conn = MCPConnection(
        name="test",
        url="none",
        transport="sse",
        toolsets=[],
        tools=[name],
        tool_schemas={name: {"annotations": annotations}},
    )
    try:
        assert register_mcp_tools(conn) == 1
        assert (name in WRITE_TOOL_NAMES) is write
    finally:
        unregister_tool(name)


def test_mcp_cannot_replace_native_write_or_unregister_it():
    from sre_agent.tool_registry import register_tool

    native = type("Native", (), {"name": "deep_native_write"})()
    register_tool(native, is_write=True)
    conn = MCPConnection(name="test", url="none", transport="sse", toolsets=[], tools=[native.name])
    try:
        assert register_mcp_tools(conn) == 0
        assert TOOL_REGISTRY[native.name] is native
        assert native.name in WRITE_TOOL_NAMES
        assert conn.tools == []
    finally:
        unregister_tool(native.name)


@pytest.mark.asyncio
async def test_plan_preserves_actual_write_gate_and_caller_credentials():
    from sre_agent.skill_loader import build_config_from_skill, get_skill

    cfg = build_config_from_skill(get_skill("sre"))
    assert "restart_deployment" in cfg["tool_map"]
    assert "restart_deployment" in cfg["write_tools"]

    @asynccontextmanager
    async def borrow(_):
        yield object()

    confirm = AsyncMock(return_value=False)
    run = AsyncMock(return_value="Investigation complete")
    with patch("sre_agent.agent.borrow_async_client", borrow), patch("sre_agent.agent.run_agent_streaming", run):
        await PlanRuntime(on_confirm=confirm, user_token="user-credential")._run_phase_once(
            SkillPhase(id="triage", skill_name="sre"), {}, {}
        )
    assert "restart_deployment" in run.call_args.args[5]
    assert run.call_args.kwargs["on_confirm"] is confirm
    assert run.call_args.kwargs["user_token"] == "user-credential"


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("POST", "/admin/mcp/toolsets", {"toolsets": ["helm"]}),
        ("POST", "/admin/mcp", {"name": "untrusted", "url": "http://example.org"}),
        ("DELETE", "/admin/mcp/untrusted", None),
        ("POST", "/admin/mcp/test", {"url": "http://example.org"}),
    ],
)
def test_non_admin_cannot_mutate_mcp_capabilities(method, path, payload):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from sre_agent.api.skill_rest import router

    app = FastAPI()
    app.include_router(router)
    settings = SimpleNamespace(server=SimpleNamespace(ws_token="test-secret", admin_users="alice"))
    with (
        patch("sre_agent.api.auth.get_settings", return_value=settings),
        patch("sre_agent.k8s_client.get_apps_client") as apps,
    ):
        response = TestClient(app).request(
            method, path, json=payload, headers={"Authorization": "Bearer test-secret", "X-Forwarded-User": "bob"}
        )
    assert response.status_code == 403
    apps.assert_not_called()


@pytest.mark.asyncio
async def test_caller_empty_write_set_cannot_bypass_dynamic_registration(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from sre_agent.agent import run_agent_streaming
    from sre_agent.tool_registry import register_tool

    monkeypatch.setenv("PULSE_AGENT_HARNESS", "0")
    tool = MagicMock(name="tool")
    tool.name = "deep_dynamic_write"
    tool.call.return_value = "mutated"
    register_tool(tool, is_write=True)

    class Stream:
        def __init__(self, response):
            self.response = response

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def get_final_message(self):
            return self.response

    client = MagicMock()
    client.messages.stream.side_effect = [
        Stream(
            SimpleNamespace(
                stop_reason="tool_use", content=[SimpleNamespace(type="tool_use", id="t1", name=tool.name, input={})]
            )
        ),
        Stream(SimpleNamespace(stop_reason="end_turn", content=[])),
    ]
    try:
        await run_agent_streaming(
            client, [{"role": "user", "content": "write"}], "test", [], {tool.name: tool}, write_tools=set()
        )
        tool.call.assert_not_called()
    finally:
        unregister_tool(tool.name)


def test_reregister_same_connection_preserves_tools_and_refreshes_write_classification():
    name = "deep_reregister"
    conn = MCPConnection(name="test", url="none", transport="sse", toolsets=[], tools=[name])
    try:
        assert register_mcp_tools(conn) == 1
        assert name in WRITE_TOOL_NAMES
        conn.tool_schemas[name] = {"annotations": {"readOnlyHint": True}}
        assert register_mcp_tools(conn) == 1
        assert conn.tools == [name]
        assert name in TOOL_REGISTRY
        assert name not in WRITE_TOOL_NAMES
    finally:
        unregister_tool(name)


def test_read_only_skill_filters_mutating_mcp_tools():
    from unittest.mock import patch

    from sre_agent.skill_loader import build_config_from_skill, get_skill

    name = "deep_mcp_uninstall"
    conn = MCPConnection(name="test", url="none", transport="sse", toolsets=[], tools=[name])
    try:
        register_mcp_tools(conn)
        with patch("sre_agent.mcp_client.list_mcp_tools", return_value=[{"name": name}]):
            cfg = build_config_from_skill(get_skill("security"))
        assert name not in cfg["tool_map"]
        assert not cfg["write_tools"]
    finally:
        unregister_tool(name)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "approved,trust,allowed", [(False, 4, False), (True, 0, False), (True, 1, False), (True, 2, True)]
)
async def test_durable_worker_requires_approval_and_server_trust(approved, trust, allowed):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from sre_agent.skill_plan import SkillOutput
    from sre_agent.temporal.activities import run_plan_phase

    client = MagicMock()
    client.close = AsyncMock()
    observed = []

    async def execute(runtime, phase, incident, priors):
        observed.append(await runtime._on_confirm("restart_deployment", {"namespace": "demo"}))
        return SkillOutput(skill_id="sre", phase_id=phase.id, status="complete")

    with (
        patch("sre_agent.agent.create_async_client", return_value=client),
        patch.object(PlanRuntime, "_execute_phase", execute),
        patch(
            "sre_agent.config.get_settings",
            return_value=SimpleNamespace(monitor=SimpleNamespace(max_trust_level=trust)),
        ),
    ):
        await run_plan_phase({"phases": [{"id": "fix", "skill_name": "sre"}]}, "fix", {}, {}, writes_approved=approved)
    assert observed == [allowed]
    client.close.assert_awaited_once()


def test_zero_match_owner_migration_releases_table_lock():
    import psycopg2

    from sre_agent.db import get_database
    from tests.conftest import _TEST_DB_URL

    db = get_database()
    other = psycopg2.connect(_TEST_DB_URL, options="-c lock_timeout=1000")
    try:
        assert ViewRepository(db=db).migrate_view_ownership("review-no-match", "user-0000000000000000") == 0
        # Migration19 takes an exclusive relation lock. A login that found no
        # legacy views must not leave its zero-row UPDATE transaction open.
        with other.cursor() as cursor:
            cursor.execute("LOCK TABLE views IN ACCESS EXCLUSIVE MODE")
    finally:
        other.rollback()
        other.close()
        db.commit()


def test_mcp_refresh_removes_withdrawn_tools_from_registry():
    name = "deep_withdrawn"
    conn = MCPConnection(name="test", url="none", transport="sse", toolsets=[], tools=[name])
    try:
        register_mcp_tools(conn)
        assert name in TOOL_REGISTRY
        conn.tools = []
        register_mcp_tools(conn)
        assert name not in TOOL_REGISTRY
        assert name not in WRITE_TOOL_NAMES
    finally:
        unregister_tool(name)


@pytest.mark.parametrize("approved", ["false", "0", None, 0, 1])
def test_durable_verdict_requires_explicit_boolean(approved):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from sre_agent.api.auth import require_admin
    from sre_agent.api.monitor_rest import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_admin] = lambda: "alice"
    with patch("sre_agent.temporal.client.approve_plan_phase", new_callable=AsyncMock) as signal:
        response = TestClient(app).post(
            "/workflow-runs/example/approve", json={"phase_id": "fix", "approved": approved}
        )
    assert response.status_code == 400
    signal.assert_not_awaited()


@pytest.mark.parametrize("approved", ["false", "0", 0, 1, None])
def test_chat_websocket_malformed_denial_never_approves(approved):
    import asyncio
    from contextlib import suppress

    from fastapi import FastAPI, WebSocket
    from fastapi.testclient import TestClient

    from sre_agent.api.agent_ws import (
        _create_and_register_future,
        _make_receive_loop,
        _pending_confirms,
        _pending_nonces,
    )

    app = FastAPI()

    @app.websocket("/confirm")
    async def endpoint(websocket: WebSocket):
        await websocket.accept()
        sid = "deep-confirm"
        future = await _create_and_register_future(sid, "restart_deployment", {}, websocket)
        task = asyncio.create_task(_make_receive_loop(websocket, sid, [], asyncio.Queue())())
        try:
            result = await future
            await websocket.send_json({"approved": result})
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            _pending_confirms.pop(sid, None)
            _pending_nonces.pop(sid, None)

    with TestClient(app).websocket_connect("/confirm") as websocket:
        request = websocket.receive_json()
        websocket.send_json({"type": "confirm_response", "approved": approved, "nonce": request["nonce"]})
        assert websocket.receive_json() == {"approved": False}


@pytest.mark.asyncio
async def test_monitor_session_rejects_string_false_without_resolving():
    import asyncio

    from sre_agent.monitor.session import MonitorClient

    client = MonitorClient(None)
    future = asyncio.get_running_loop().create_future()
    client._pending_action_approvals["a"] = future
    assert not client.resolve_action_response("a", "false")
    assert not future.done()
    assert client.resolve_action_response("a", False)
    assert await future is False


def test_monitor_websocket_string_false_cannot_execute_or_audit_approval():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from sre_agent.api.ws_endpoints import websocket_monitor

    app = FastAPI()
    app.websocket("/monitor")(websocket_monitor)
    monitor = MagicMock()
    monitor.running = True
    monitor.subscriber_count = 1
    monitor.unsubscribe = AsyncMock()
    pending = []

    async def subscribe(client):
        future = asyncio.get_running_loop().create_future()
        client._pending_action_approvals["a"] = future
        pending.append(future)
        await client.send({"type": "ready"})

    monitor.subscribe = subscribe
    settings = SimpleNamespace(
        server=SimpleNamespace(max_monitor_clients=100), monitor=SimpleNamespace(max_trust_level=2)
    )
    with (
        patch("sre_agent.api.ws_endpoints._verify_ws_token", return_value="test-token"),
        patch("sre_agent.api.ws_endpoints.get_settings", return_value=settings),
        patch("sre_agent.api.ws_endpoints.get_cluster_monitor", new_callable=AsyncMock, return_value=monitor),
        patch("sre_agent.inbox.record_interaction") as audit,
    ):
        with TestClient(app).websocket_connect("/monitor") as websocket:
            websocket.send_json({"type": "subscribe_monitor"})
            assert websocket.receive_json()["type"] == "ready"
            websocket.send_json({"type": "action_response", "actionId": "a", "approved": "false"})
            assert websocket.receive_json()["type"] == "error"
            assert not pending[0].done()
            audit.assert_not_called()
            websocket.send_json({"type": "action_response", "actionId": "a", "approved": False})
            websocket.send_json({"type": "set_disabled_scanners", "scannerIds": []})
            assert websocket.receive_json()["type"] == "ack"
            assert pending[0].result() is False
            assert audit.call_args.kwargs["decision"] == "rejected"
