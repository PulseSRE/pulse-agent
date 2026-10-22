"""The Temporal worker's connection, and what callers see when it is not polling.

Found by running the agent against a real Temporal server: the worker tried to
connect once at startup, so an agent that booted before Temporal (a fresh
install) accepted durable runs that then sat in RUNNING forever. And activities
ran blocking Kubernetes calls on the event loop the API shares, so one stalled
API server call froze the whole agent.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sre_agent.temporal import worker


@pytest.fixture(autouse=True)
def _reset_state():
    saved = dict(worker._STATE)
    yield
    worker._STATE.clear()
    worker._STATE.update(saved)


class TestConnectWithRetry:
    def test_retries_until_the_server_answers(self):
        attempts = {"n": 0}
        client = object()

        async def connect(*_a, **_k):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("connection refused")
            return client

        async def go():
            with (
                patch("temporalio.client.Client.connect", side_effect=connect),
                patch.object(worker, "_RETRY_FIRST", 0.01),
            ):
                return await worker._connect_with_retry(asyncio.Event())

        assert asyncio.run(go()) is client
        assert attempts["n"] == 3

    def test_reports_connecting_with_the_error_while_it_waits(self):
        async def go():
            shutdown = asyncio.Event()
            with (
                patch("temporalio.client.Client.connect", side_effect=RuntimeError("connection refused")),
                patch.object(worker, "_RETRY_FIRST", 0.01),
            ):
                task = asyncio.create_task(worker._connect_with_retry(shutdown))
                await asyncio.sleep(0.05)
                state = worker.worker_state()
                shutdown.set()
                return state, await task

        state, client = asyncio.run(go())
        assert state["state"] == "connecting"
        assert "connection refused" in state["detail"]
        assert client is None

    def test_shutdown_before_connecting_stops_cleanly(self):
        async def go():
            shutdown = asyncio.Event()
            shutdown.set()
            with patch("temporalio.client.Client.connect", side_effect=RuntimeError("down")):
                await worker.run_worker(shutdown)

        asyncio.run(go())
        assert worker.worker_state()["state"] == "stopped"


class TestRunsRefusedWithoutAWorker:
    def test_start_is_refused_while_connecting(self):
        from sre_agent.temporal import client as tclient

        worker._set_state("connecting", "ConnectionRefused")
        fake_client = MagicMock()
        fake_client.start_workflow = AsyncMock()
        with patch.object(tclient, "_connect", AsyncMock(return_value=fake_client)):
            with pytest.raises(tclient.TemporalWorkerUnavailableError, match="has not connected yet"):
                asyncio.run(tclient.start_plan_run("crashloop", {}))
            with pytest.raises(tclient.TemporalWorkerUnavailableError):
                asyncio.run(tclient.start_incident_run("f1", {}, {"strategy": "restart_controller"}))
        fake_client.start_workflow.assert_not_awaited()

    def test_start_proceeds_when_polling(self):
        from sre_agent.temporal import client as tclient

        worker._set_state("polling", "host:7233")
        handle = MagicMock(result_run_id="r1")
        fake_client = MagicMock()
        fake_client.start_workflow = AsyncMock(return_value=handle)
        with patch.object(tclient, "_connect", AsyncMock(return_value=fake_client)):
            out = asyncio.run(tclient.start_plan_run("crashloop", {}))
        assert out["run_id"] == "r1"

    def test_no_local_worker_means_no_local_check(self):
        """A worker split into its own Deployment leaves this process 'disabled'."""
        from sre_agent.temporal import client as tclient

        worker._set_state("disabled")
        tclient._require_worker()

    def test_rest_answers_503_with_the_reason(self):
        from fastapi.testclient import TestClient

        from sre_agent.api.app import app
        from sre_agent.api.auth import require_admin

        worker._set_state("connecting", "ConnectionRefused")
        app.dependency_overrides[require_admin] = lambda: "admin"
        try:
            fake_client = MagicMock()
            with patch("sre_agent.temporal.client._connect", AsyncMock(return_value=fake_client)):
                resp = TestClient(app).post("/plan-templates/crashloop/run", json={"incident": {}})
        finally:
            app.dependency_overrides.pop(require_admin, None)
        assert resp.status_code == 503
        assert "has not connected yet" in resp.json()["detail"]


class TestServerUnreachable:
    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("post", "/plan-templates/crashloop/run", {"incident": {}}),
            ("get", "/workflow-runs/plan-x", None),
            ("get", "/workflow-runs", None),
            ("post", "/workflow-runs/plan-x/cancel", {}),
            ("post", "/workflow-runs/plan-x/approve", {"phase_id": "p", "approved": True}),
        ],
    )
    def test_every_durable_endpoint_answers_503_not_500(self, method, path, body):
        """With the server down these were a bare 500, or a misleading 404."""
        from fastapi.testclient import TestClient

        from sre_agent.api.app import app
        from sre_agent.api.auth import require_admin, verify_token

        worker._set_state("disabled")
        app.dependency_overrides[require_admin] = lambda: "admin"
        app.dependency_overrides[verify_token] = lambda: None
        try:
            with (
                patch("sre_agent.temporal.client._settings", return_value=MagicMock(host="t:7233", namespace="default")),
                patch("temporalio.client.Client.connect", side_effect=RuntimeError("connection refused")),
            ):
                client = TestClient(app)
                resp = client.post(path, json=body) if method == "post" else client.get(path)
        finally:
            app.dependency_overrides.pop(require_admin, None)
            app.dependency_overrides.pop(verify_token, None)
        assert resp.status_code == 503
        assert "Cannot reach the Temporal server" in resp.json()["detail"]


class TestReadiness:
    def test_absent_when_temporal_is_not_configured(self, monkeypatch):
        from sre_agent import readiness

        monkeypatch.setattr("sre_agent.config.get_settings", lambda: MagicMock(temporal=MagicMock(host="")))
        assert readiness.temporal_check() is None

    @pytest.mark.parametrize(("state", "status"), [("polling", "healthy"), ("connecting", "unhealthy")])
    def test_reports_the_worker_state(self, monkeypatch, state, status):
        from sre_agent import readiness

        monkeypatch.setattr("sre_agent.config.get_settings", lambda: MagicMock(temporal=MagicMock(host="t:7233")))
        worker._set_state(state, "ConnectionRefused" if state == "connecting" else "")
        result = readiness.temporal_check()
        assert result["id"] == "temporal_worker"
        assert result["status"] == status


class TestActivitiesStayOffTheEventLoop:
    def test_a_slow_kubernetes_call_does_not_stall_the_loop(self):
        """The worker shares its loop with the API server and WebSockets."""
        from sre_agent.temporal import incident_activities

        def slow_check(*_a):
            time.sleep(0.6)
            return MagicMock(status="pass", detail="ok", observations={})

        async def go():
            lags: list[float] = []

            async def ticker():
                last = time.monotonic()
                while True:
                    await asyncio.sleep(0.05)
                    now = time.monotonic()
                    lags.append(now - last - 0.05)
                    last = now

            t = asyncio.create_task(ticker())
            with (
                patch("sre_agent.monitor.health_gate.check_resource", side_effect=slow_check),
                patch("sre_agent.monitor.health_gate.PASS", "pass"),
            ):
                await incident_activities.verify_fix({"kind": "Deployment", "name": "web", "namespace": "a"})
            t.cancel()
            return max(lags)

        assert asyncio.run(go()) < 0.3
