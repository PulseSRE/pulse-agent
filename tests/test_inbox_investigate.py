"""POST /inbox/{id}/investigate starts an investigation; GET /inbox/{id}/investigation reports it.

The endpoint used to claim the item and nothing else, so a task created for a
resource and then "investigated" (the UI's Diagnose button) never got an
investigation. These tests drive the HTTP API on one event loop (httpx +
ASGITransport) so the background run actually finishes, through the real
investigation pipeline with only the model run scripted.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

try:  # what starlette's TestClient uses, under either name
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx

TOKEN = "test-token-inbox-investigate"
USER_TOKEN = "sha256~forwarded-user-token-for-test"


@pytest.fixture
def headers():
    return {
        "Authorization": f"Bearer {TOKEN}",
        "X-Forwarded-User": "alice",
        "X-Forwarded-Access-Token": USER_TOKEN,
    }


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("PULSE_AGENT_WS_TOKEN", TOKEN)
    monkeypatch.setenv("PULSE_AGENT_MEMORY", "0")
    from sre_agent import inbox_investigation
    from sre_agent.config import _reset_settings
    from sre_agent.monitor import reset_cluster_monitor

    _reset_settings()
    reset_cluster_monitor()
    inbox_investigation._reset_for_tests()
    with (
        patch("sre_agent.k8s_client._initialized", True),
        patch("sre_agent.k8s_client._load_k8s"),
        patch("sre_agent.k8s_client.get_core_client", return_value=MagicMock()),
        patch("sre_agent.k8s_client.get_apps_client"),
        patch("sre_agent.k8s_client.get_custom_client"),
        patch("sre_agent.k8s_client.get_version_client"),
    ):
        yield
    reset_cluster_monitor()
    inbox_investigation._reset_for_tests()


def _client():
    from sre_agent.api import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _scripted(result=None, *, gate: asyncio.Event | None = None, raises: Exception | None = None, seen=None):
    """A stand-in for the model run: records calls (and the token it read with)."""
    calls = []

    async def run(finding, *, client=None):
        from sre_agent.k8s_client import get_current_user_token

        calls.append(finding)
        if seen is not None:
            seen.append(get_current_user_token())
        if gate is not None:
            await gate.wait()
        if raises is not None:
            raise raises
        return result or {
            "summary": "web-1 is OOMKilled: its 128Mi limit is below its working set.",
            "suspected_cause": "memory limit too low",
            "recommended_fix": "raise the memory limit to 256Mi",
            "confidence": 0.8,
            "evidence_detail": [{"observation": "lastState OOMKilled", "stance": "supports", "source": "describe_pod"}],
            "alternatives_considered": ["node memory pressure"],
            "tools_called": ["describe_pod"],
        }

    return run, calls


async def _create(c, headers, title="Pod web-1 crash-looping") -> str:
    resp = await c.post(
        "/inbox",
        json={
            "title": f"{title} {time.time_ns()}",
            "item_type": "task",
            "namespace": "shop",
            "resources": [{"kind": "Pod", "name": f"web-{time.time_ns()}", "namespace": "shop"}],
        },
        headers=headers,
    )
    assert resp.status_code == 201
    return resp.json()["id"]


async def _finish(item_id: str) -> None:
    from sre_agent import inbox_investigation

    run = inbox_investigation.active_run(item_id)
    if run is not None and run.task is not None:
        await asyncio.wait_for(run.task, timeout=10)


@pytest.mark.asyncio
async def test_create_investigate_then_report_is_done(env, headers):
    seen: list = []
    run, calls = _scripted(seen=seen)
    with patch("sre_agent.monitor.investigation_pipeline._run_proactive_investigation", run):
        async with _client() as c:
            item_id = await _create(c, headers)

            resp = await c.post(f"/inbox/{item_id}/investigate", headers=headers)
            assert resp.status_code == 202
            body = resp.json()
            assert body["status"] in ("queued", "running")
            assert body["state"] == body["status"]
            assert body["item_id"] == item_id
            assert body["requested_by"] == "alice"
            assert body["ok"] is True

            await _finish(item_id)

            resp = await c.get(f"/inbox/{item_id}/investigation", headers=headers)
            assert resp.status_code == 200
            report = resp.json()
            # New: the run's state. Unchanged: the report fields the drawer reads.
            assert report["state"] == "done"
            assert report["run"]["state"] == "done"
            assert report["run"]["report_id"] == report["id"]
            assert report["run"]["error"] is None
            assert report["status"] == "completed"
            assert report["summary"].startswith("web-1 is OOMKilled")
            assert report["suspected_cause"] == "memory limit too low"
            assert report["alternatives_considered"] == ["node memory pressure"]
            assert report["security_notes"] == []

            item = (await c.get(f"/inbox/{item_id}", headers=headers)).json()
    assert len(calls) == 1
    # The requester's forwarded token is what the investigation read with.
    assert seen == [USER_TOKEN]
    assert item["metadata"]["investigation_id"] == report["id"]
    assert item["metadata"]["investigation_run"]["state"] == "done"
    assert item["claimed_by"] == "alice"


@pytest.mark.asyncio
async def test_second_call_while_running_returns_the_same_run(env, headers):
    gate = asyncio.Event()
    run, calls = _scripted(gate=gate)
    with patch("sre_agent.monitor.investigation_pipeline._run_proactive_investigation", run):
        async with _client() as c:
            item_id = await _create(c, headers)
            first = await c.post(f"/inbox/{item_id}/investigate", headers=headers)
            await asyncio.sleep(0.05)  # let it start
            second = await c.post(f"/inbox/{item_id}/investigate", headers=headers)
            assert first.status_code == second.status_code == 202
            assert second.json()["status"] == "running"
            assert second.json()["queued_at"] == first.json()["queued_at"]

            polled = await c.get(f"/inbox/{item_id}/investigation", headers=headers)
            assert polled.status_code == 200
            assert polled.json()["state"] == "running"
            assert polled.json()["run"]["started_at"] is not None

            gate.set()
            await _finish(item_id)
            done = (await c.get(f"/inbox/{item_id}/investigation", headers=headers)).json()
    assert len(calls) == 1
    assert done["state"] == "done"


@pytest.mark.asyncio
async def test_refused_by_daily_budget_says_why(env, headers):
    from sre_agent.monitor import get_cluster_monitor

    monitor = await get_cluster_monitor()
    monitor._daily_investigation_count = 10_000
    run, calls = _scripted()
    with patch("sre_agent.monitor.investigation_pipeline._run_proactive_investigation", run):
        async with _client() as c:
            item_id = await _create(c, headers)
            resp = await c.post(f"/inbox/{item_id}/investigate", headers=headers)
            assert resp.status_code == 429
            detail = resp.json()["detail"]
            assert detail["reason"] == "daily_budget"
            assert "budget" in detail["message"]
            assert int(resp.headers["Retry-After"]) > 0

            # Refused means nothing happened: no run, no claim.
            assert (await c.get(f"/inbox/{item_id}/investigation", headers=headers)).status_code == 404
            item = (await c.get(f"/inbox/{item_id}", headers=headers)).json()
    assert calls == []
    assert item["claimed_by"] is None
    assert item["status"] == "new"


@pytest.mark.asyncio
async def test_refused_by_cooldown_after_a_run(env, headers):
    run, calls = _scripted()
    with patch("sre_agent.monitor.investigation_pipeline._run_proactive_investigation", run):
        async with _client() as c:
            item_id = await _create(c, headers)
            assert (await c.post(f"/inbox/{item_id}/investigate", headers=headers)).status_code == 202
            await _finish(item_id)
            again = await c.post(f"/inbox/{item_id}/investigate", headers=headers)
    assert again.status_code == 429
    assert again.json()["detail"]["reason"] == "cooldown"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_refused_by_concurrency_limit(env, headers):
    from sre_agent import inbox_investigation
    from sre_agent.config import get_settings

    limit = get_settings().monitor.max_concurrent_investigations
    for i in range(limit):
        inbox_investigation.reserve(f"other-{i}", "bob", "request")
    run, calls = _scripted()
    with patch("sre_agent.monitor.investigation_pipeline._run_proactive_investigation", run):
        async with _client() as c:
            item_id = await _create(c, headers)
            resp = await c.post(f"/inbox/{item_id}/investigate", headers=headers)
    assert resp.status_code == 429
    assert resp.json()["detail"]["reason"] == "concurrency"
    assert calls == []


@pytest.mark.asyncio
async def test_refused_when_circuit_breaker_open(env, headers):
    run, calls = _scripted()
    with (
        patch("sre_agent.monitor.investigation_pipeline._run_proactive_investigation", run),
        patch("sre_agent.agent._circuit_breaker") as cb,
    ):
        cb.is_open = True
        async with _client() as c:
            item_id = await _create(c, headers)
            resp = await c.post(f"/inbox/{item_id}/investigate", headers=headers)
    assert resp.status_code == 429
    assert resp.json()["detail"]["reason"] == "circuit_open"
    assert calls == []


@pytest.mark.asyncio
async def test_failure_surfaces_the_error(env, headers):
    run, _ = _scripted(raises=RuntimeError("model endpoint unavailable"))
    with patch("sre_agent.monitor.investigation_pipeline._run_proactive_investigation", run):
        async with _client() as c:
            item_id = await _create(c, headers)
            assert (await c.post(f"/inbox/{item_id}/investigate", headers=headers)).status_code == 202
            await _finish(item_id)
            resp = await c.get(f"/inbox/{item_id}/investigation", headers=headers)
            item = (await c.get(f"/inbox/{item_id}", headers=headers)).json()
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "failed"
    assert "model endpoint unavailable" in body["run"]["error"]
    # The pipeline saved its failed report; it is returned, but not linked as
    # the item's investigation (the drawer shows linked reports).
    assert body["status"] == "failed"
    assert body["id"] == body["run"]["report_id"]
    assert "investigation_id" not in item["metadata"]


@pytest.mark.asyncio
async def test_closed_item_is_409(env, headers):
    from sre_agent.inbox import update_item_status

    async with _client() as c:
        item_id = await _create(c, headers)
        assert update_item_status(item_id, "claimed")
        assert update_item_status(item_id, "resolved")
        resp = await c.post(f"/inbox/{item_id}/investigate", headers=headers)
        missing = await c.post("/inbox/inb-doesnotexist/investigate", headers=headers)
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "item_closed"
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_does_not_take_another_users_claim(env, headers):
    from sre_agent.inbox import claim_and_investigate

    run, calls = _scripted()
    with patch("sre_agent.monitor.investigation_pipeline._run_proactive_investigation", run):
        async with _client() as c:
            item_id = await _create(c, headers)
            assert claim_and_investigate(item_id, "bob")
            resp = await c.post(f"/inbox/{item_id}/investigate", headers=headers)
            await _finish(item_id)
            item = (await c.get(f"/inbox/{item_id}", headers=headers)).json()
    assert resp.status_code == 202
    assert len(calls) == 1
    assert item["claimed_by"] == "bob"


@pytest.mark.asyncio
async def test_run_cut_off_by_restart_reads_as_failed(env, headers):
    from sre_agent.inbox import get_inbox_item
    from sre_agent.repositories.inbox_repo import get_inbox_repo

    async with _client() as c:
        item_id = await _create(c, headers)
        item = get_inbox_item(item_id)
        md = item["metadata"]
        md["investigation_run"] = {"item_id": item_id, "state": "running", "queued_at": 1, "started_at": 1}
        get_inbox_repo().update_metadata(item_id, md, int(time.time()))
        resp = await c.get(f"/inbox/{item_id}/investigation", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["state"] == "failed"
    assert "restarted" in resp.json()["error"]


@pytest.mark.asyncio
async def test_report_without_run_record_reads_as_before(env, headers):
    """An item investigated before this change: same report, plus state, run=null."""
    from sre_agent.inbox import get_inbox_item
    from sre_agent.monitor.investigation_pipeline import _save, outcome_from_freeform
    from sre_agent.repositories.inbox_repo import get_inbox_repo

    async with _client() as c:
        item_id = await _create(c, headers)
        report = outcome_from_freeform({"summary": "old report", "confidence": 0.6}).to_report({"id": item_id})
        await _save(report, {"id": item_id})
        item = get_inbox_item(item_id)
        md = item["metadata"]
        md["investigation_id"] = report["id"]
        get_inbox_repo().update_metadata(item_id, md, int(time.time()))
        resp = await c.get(f"/inbox/{item_id}/investigation", headers=headers)
        none = await c.get(f"/inbox/{await _create(c, headers)}/investigation", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] == "old report"
    assert body["state"] == "done"
    assert body["run"] is None
    assert none.status_code == 404


def test_triage_skips_an_item_a_request_is_investigating(env):
    from sre_agent import inbox, inbox_investigation
    from sre_agent.db import get_database

    item_id = inbox.create_inbox_item(
        {"item_type": "task", "title": f"triage race {time.time_ns()}", "created_by": "alice"}
    )
    row = get_database().fetchone("SELECT * FROM inbox_items WHERE id = ?", (item_id,))
    inbox_investigation.reserve(item_id, "alice", "request")
    called = []

    async def fake(*a, **k):
        called.append(a)

    repo = MagicMock()
    repo.fetch_agent_reviewing.return_value = [row]
    with (
        patch("sre_agent.inbox.get_inbox_repo", return_value=repo),
        patch("sre_agent.monitor.investigation_pipeline.investigate", fake),
    ):
        inbox._last_investigate_time.pop(item_id, None)
        assert inbox._phase_b_investigate() == 0
    assert called == []
    # The request's run is untouched by triage.
    assert inbox_investigation.active_run(item_id).source == "request"
