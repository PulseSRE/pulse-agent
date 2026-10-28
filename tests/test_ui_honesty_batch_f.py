"""Batch F (agent side): changes the UI shows reach it over /ws/monitor."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import sre_agent.monitor.cluster_monitor as cm


def _capture(run):
    """Run ``run`` (sync, in a worker thread) with a fake monitor; return what it broadcast."""
    sent: list[dict] = []

    async def scenario():
        async def broadcast(message):
            sent.append(message)

        monitor = SimpleNamespace(_loop=asyncio.get_running_loop(), _broadcast_raw=broadcast)
        with patch.object(cm, "_cluster_monitor", monitor):
            await asyncio.to_thread(run)
            for _ in range(50):
                if sent:
                    break
                await asyncio.sleep(0.01)

    asyncio.run(scenario())
    return sent


def test_inbox_events_are_relayed_to_monitor_clients():
    from sre_agent import inbox

    sent = _capture(lambda: inbox._publish_event("inbox_item_claimed", "inb-1", {"claimed_by": "alice"}))
    assert sent and sent[0]["type"] == "inbox_item_claimed"
    assert sent[0]["id"] == "inb-1" and sent[0]["claimed_by"] == "alice"


def test_rest_approval_is_announced_so_pending_badges_clear():
    from sre_agent.monitor import approvals

    sent = _capture(lambda: approvals._announce({"id": "act-1", "status": "completed"}))
    assert sent == [{"type": "action_report", "id": "act-1", "status": "completed"}]


def test_no_running_monitor_is_not_an_error():
    from sre_agent import inbox

    with patch.object(cm, "_cluster_monitor", None):
        inbox._publish_event("inbox_item_updated", "inb-1", {"status": "triaged"})
        assert cm.broadcast_threadsafe({"type": "x"}) is False


def test_slos_without_data_are_listed_as_no_data():
    from fastapi.testclient import TestClient

    from sre_agent.slo_registry import SLODefinition, SLORegistry

    reg = SLORegistry()
    reg.register(SLODefinition("api", "availability", 0.999), persist=False)
    reg.register(SLODefinition("web", "latency", 0.99), persist=False)

    with (
        patch("sre_agent.slo_registry.get_slo_registry", return_value=reg),
        patch.object(reg, "query_prometheus_values", return_value={"api:availability": 0.9995}),
        patch("sre_agent.api.monitor_rest.verify_token", lambda: None),
    ):
        from fastapi import FastAPI

        from sre_agent.api import monitor_rest
        from sre_agent.api.auth import verify_token

        app = FastAPI()
        app.include_router(monitor_rest.router)
        app.dependency_overrides[verify_token] = lambda: None
        body = TestClient(app).get("/slo").json()

    rows = {r["service"]: r for r in body["slos"]}
    assert body["total"] == 2
    assert rows["web"]["alert_level"] == "no_data" and rows["web"]["current_value"] is None
    assert rows["api"]["alert_level"] == "ok" and rows["api"]["error_budget_remaining"] == 0.5
