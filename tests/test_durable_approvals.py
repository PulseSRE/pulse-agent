"""Durable runs waiting on a person reach that person.

The only place to answer a durable approval was the plan panel that started
the run, while it stayed open; navigate away and the run quietly timed out.
And "Run durably" started plans with an empty incident.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sre_agent.inbox_generators as gens


def _settings(host="t:7233"):
    return MagicMock(temporal=MagicMock(host=host))


@pytest.fixture(autouse=True)
def _reset_cache():
    gens._last_durable_approvals = []
    yield
    gens._last_durable_approvals = []


class TestApprovalGenerator:
    def test_nothing_without_temporal(self):
        with patch("sre_agent.config.get_settings", return_value=_settings("")):
            assert gens.gen_durable_approvals() == []

    def test_a_waiting_run_becomes_an_item_that_names_the_phase_and_target(self):
        waiting = [
            {
                "workflow_id": "plan-crashloop-abc",
                "phase_id": "remediate",
                "memo": {"incident_type": "crashloop", "resource_namespace": "shop", "resource_name": "web"},
            }
        ]
        with (
            patch("sre_agent.config.get_settings", return_value=_settings()),
            patch("sre_agent.temporal.client.list_awaiting_approvals", AsyncMock(return_value=waiting)),
        ):
            items = gens.gen_durable_approvals()
        assert len(items) == 1
        item = items[0]
        assert "approval of 'remediate'" in item["title"] and "shop/web" in item["title"]
        assert item["correlation_key"] == "durable-approval:plan-crashloop-abc:remediate"
        assert item["metadata"]["workflow_id"] == "plan-crashloop-abc"
        assert item["metadata"]["phase_id"] == "remediate"
        assert item["metadata"]["generator"] == "durable_approvals"
        assert item["namespace"] == "shop"

    def test_an_outage_keeps_the_last_answer_instead_of_resolving_items(self):
        """Returning [] would auto-resolve every waiting item while Temporal is down."""
        waiting = [{"workflow_id": "w", "phase_id": "p", "memo": {}}]
        with patch("sre_agent.config.get_settings", return_value=_settings()):
            with patch("sre_agent.temporal.client.list_awaiting_approvals", AsyncMock(return_value=waiting)):
                first = gens.gen_durable_approvals()
            with patch(
                "sre_agent.temporal.client.list_awaiting_approvals", AsyncMock(side_effect=RuntimeError("down"))
            ):
                second = gens.gen_durable_approvals()
        assert second == first and len(second) == 1


@pytest.fixture
def api():
    from fastapi.testclient import TestClient

    from sre_agent.api.app import app
    from sre_agent.api.auth import require_admin, verify_token

    app.dependency_overrides[require_admin] = lambda: "admin"
    app.dependency_overrides[verify_token] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.pop(require_admin, None)
    app.dependency_overrides.pop(verify_token, None)


ITEM = {
    "id": "inbox-1",
    "finding_id": "f-1",
    "title": "Pod web-0 crash-looping",
    "summary": "exit 1",
    "severity": "critical",
    "resources": [{"kind": "Pod", "name": "web-0", "namespace": "shop"}],
    "metadata": {"category": "crashloop"},
}


class TestStartFromInbox:
    def test_the_run_investigates_the_standing_finding(self, api):
        from sre_agent.monitor import cluster_monitor

        finding = {**ITEM, "id": "f-1", "category": "crashloop", "summary": "full scanner detail"}
        monitor = MagicMock(_last_findings={"k": finding})
        start = AsyncMock(return_value={"workflow_id": "plan-crashloop-1", "run_id": "r"})
        with (
            patch("sre_agent.inbox.get_inbox_item", return_value=ITEM),
            patch.object(cluster_monitor, "_cluster_monitor", monitor),
            patch("sre_agent.temporal.client.start_plan_run", start),
        ):
            resp = api.post("/inbox/inbox-1/durable-run")
        assert resp.status_code == 200, resp.text
        assert resp.json()["workflow_id"] == "plan-crashloop-1"
        incident_type, incident = start.await_args.args
        assert incident_type == "crashloop"
        assert incident["summary"] == "full scanner detail"

    def test_falls_back_to_the_inbox_item_when_the_finding_is_gone(self, api):
        from sre_agent.monitor import cluster_monitor

        start = AsyncMock(return_value={"workflow_id": "w", "run_id": "r"})
        with (
            patch("sre_agent.inbox.get_inbox_item", return_value=ITEM),
            patch.object(cluster_monitor, "_cluster_monitor", None),
            patch("sre_agent.temporal.client.start_plan_run", start),
        ):
            assert api.post("/inbox/inbox-1/durable-run").status_code == 200
        incident = start.await_args.args[1]
        assert incident["category"] == "crashloop"
        assert incident["resources"][0]["name"] == "web-0"

    def test_plan_preview_and_no_match(self, api):
        from sre_agent.monitor import cluster_monitor

        with (
            patch("sre_agent.inbox.get_inbox_item", return_value=ITEM),
            patch.object(cluster_monitor, "_cluster_monitor", None),
        ):
            ok = api.get("/inbox/inbox-1/plan")
        assert ok.status_code == 200
        assert ok.json()["incident_type"] == "crashloop"
        assert any(p["approval_required"] for p in ok.json()["phases"])

        other = {**ITEM, "metadata": {"category": "no-such-thing"}}
        with (
            patch("sre_agent.inbox.get_inbox_item", return_value=other),
            patch.object(cluster_monitor, "_cluster_monitor", None),
        ):
            assert api.get("/inbox/inbox-1/plan").status_code == 404
            assert api.post("/inbox/inbox-1/durable-run").status_code == 404

    def test_unknown_item_is_404(self, api):
        with patch("sre_agent.inbox.get_inbox_item", return_value=None):
            assert api.get("/inbox/nope/plan").status_code == 404

    def test_awaiting_endpoint_lists_runs(self, api):
        runs = [{"workflow_id": "w", "phase_id": "p", "memo": {}, "phases": {}}]
        with patch("sre_agent.temporal.client.list_awaiting_approvals", AsyncMock(return_value=runs)):
            resp = api.get("/workflow-runs/awaiting-approval")
        assert resp.status_code == 200 and resp.json()["runs"] == runs
