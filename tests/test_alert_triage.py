"""``/inbox/by-alert``: from a firing alert to the Inbox item the monitor made for it.

Items are created the way the monitor creates them -- the firing-alerts
scanner's own resource mapping, ``_make_finding``, ``bridge_finding_to_inbox``
-- against the disposable test database, so the lookup is checked against the
real correlation key rather than a copy of its format.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from tests.conftest import truncate_tables

TOKEN = "test-token-by-alert"
AUTH = {"Authorization": f"Bearer {TOKEN}", "X-Forwarded-User": "alice"}
USER = {**AUTH, "X-Forwarded-Access-Token": "alice-token"}


@pytest.fixture
def db():
    from sre_agent.db import get_database

    database = get_database()
    truncate_tables(database, "inbox_items", "episode_symptoms", "episodes")
    yield database
    truncate_tables(database, "inbox_items", "episode_symptoms", "episodes")


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("PULSE_AGENT_WS_TOKEN", TOKEN)
    monkeypatch.setenv("PULSE_AGENT_MEMORY", "0")
    monkeypatch.setenv("PULSE_AGENT_TOKEN_FORWARDING", "true")
    from sre_agent.config import _reset_settings

    _reset_settings()
    from fastapi import FastAPI

    from sre_agent.api import inbox_rest

    app = FastAPI()
    app.include_router(inbox_rest.router)
    yield TestClient(app, raise_server_exceptions=False)
    _reset_settings()


def _alert_finding(labels: dict, severity: str = "critical") -> dict:
    """The finding scan_firing_alerts makes for an alert with these labels."""
    from sre_agent.monitor.findings import _make_finding
    from sre_agent.monitor.scanners import _alert_resources

    name = labels["alertname"]
    return _make_finding(
        severity=severity,
        category="alerts",
        title=name,
        summary=f"{name} firing",
        resources=_alert_resources(name, labels, labels.get("namespace", "")),
    )


def _bridge(finding: dict, **metadata) -> str:
    from sre_agent.inbox import bridge_finding_to_inbox, get_inbox_item
    from sre_agent.repositories.inbox_repo import get_inbox_repo

    item_id = bridge_finding_to_inbox(finding)
    if metadata:
        meta = get_inbox_item(item_id)["metadata"] or {}
        meta.update(metadata)
        get_inbox_repo().update_metadata(item_id, meta, 1)
    return item_id


CRASH = {"alertname": "KubePodCrashLooping", "namespace": "payments", "pod": "api-7f9c4d-x2k8q", "severity": "critical"}


def test_an_alert_finds_the_item_the_monitor_made_for_it(client):
    item_id = _bridge(_alert_finding(CRASH))
    resp = client.get("/inbox/by-alert", params=CRASH, headers=USER)
    assert resp.status_code == 200
    body = resp.json()
    assert body["item"]["id"] == item_id
    assert body["correlationKey"] == "alerts:payments:Pod/api"
    assert body["resource"] == {"kind": "Pod", "name": "api-7f9c4d-x2k8q", "namespace": "payments"}


def test_the_investigated_item_for_the_same_pod_comes_back_as_related(client):
    from sre_agent.monitor.findings import _make_finding

    _bridge(_alert_finding(CRASH))
    oom = _make_finding(
        severity="critical",
        category="oom",
        title="OOMKilled",
        summary="api OOMKilled 6x",
        resources=[{"kind": "Pod", "name": "api-7f9c4d-x2k8q", "namespace": "payments"}],
    )
    oom_id = _bridge(
        oom,
        investigation_id="inv-1",
        suspected_cause="Memory p95 410Mi against a 256Mi limit",
        investigation_confidence=0.85,
        action_plan=[{"title": "raise limit"}],
    )
    # Another pod's item in another namespace must not match.
    _bridge(
        _make_finding(
            severity="warning",
            category="oom",
            title="OOMKilled",
            summary="",
            resources=[{"kind": "Pod", "name": "api-1-2", "namespace": "shop"}],
        )
    )
    body = client.get("/inbox/by-alert", params=CRASH, headers=USER).json()
    assert [r["id"] for r in body["related"]] == [oom_id]
    inv = body["related"][0]["investigation"]
    assert inv["suspectedCause"].startswith("Memory p95") and inv["confidence"] == 0.85
    assert body["related"][0]["fixReady"] is True


def test_an_alert_with_no_item_says_so(client):
    body = client.get("/inbox/by-alert", params={"alertname": "KubeletDown", "node": "worker-5"}, headers=USER).json()
    assert body["item"] is None and body["related"] == [] and body["episode"] is None
    assert body["correlationKey"] == "alerts::Node/worker-5"


def test_an_episode_symptom_names_its_cause_and_the_causes_item(client):
    from sre_agent.monitor.findings import _make_finding

    labels = {"alertname": "KubeDeploymentReplicasMismatch", "namespace": "shop", "deployment": "storefront"}
    _bridge(_alert_finding(labels, "warning"))
    cause = _make_finding(
        severity="critical",
        category="alerts",
        title="HighErrorRate",
        summary="5xx",
        resources=[{"kind": "Deployment", "name": "storefront-route", "namespace": "shop"}],
    )
    cause_id = _bridge(cause)
    episode = {
        "id": "ep-1",
        "cause_title": "HighErrorRate",
        "correlation_key": "alerts:shop:Deployment/storefront-route",
    }
    with (
        patch(
            "sre_agent.monitor.episodes.symptom_keys_by_episode",
            return_value={"alerts:shop:Deployment/storefront": "ep-1"},
        ),
        patch("sre_agent.monitor.episodes.list_open", return_value=[episode]),
    ):
        body = client.get("/inbox/by-alert", params=labels, headers=USER).json()
    assert body["episode"]["causeTitle"] == "HighErrorRate"
    assert body["episode"]["causeItem"]["id"] == cause_id


def test_batch_answers_in_order(client):
    item_id = _bridge(_alert_finding(CRASH))
    resp = client.post(
        "/inbox/by-alert",
        json={"alerts": [{"labels": {"alertname": "KubeletDown", "node": "worker-5"}}, {"labels": CRASH}]},
        headers=USER,
    )
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert results[0]["item"] is None and results[1]["item"]["id"] == item_id


def test_a_caller_without_a_token_is_refused(client):
    assert client.get("/inbox/by-alert", params=CRASH, headers=AUTH).status_code == 401
    assert client.post("/inbox/by-alert", json={"alerts": []}, headers=AUTH).status_code == 401


@pytest.mark.parametrize(
    "params",
    [{"namespace": "x"}, {"alertname": "A", "bad-label": "x"}],
)
def test_bad_labels_are_a_400(client, params):
    assert client.get("/inbox/by-alert", params=params, headers=USER).status_code == 400


def test_bad_batch_bodies_are_a_400(client):
    assert client.post("/inbox/by-alert", json={"alerts": "x"}, headers=USER).status_code == 400
    assert client.post("/inbox/by-alert", json={"alerts": [{"labels": {}}]}, headers=USER).status_code == 400
    too_many = {"alerts": [{"labels": {"alertname": "A"}}] * 201}
    assert client.post("/inbox/by-alert", json=too_many, headers=USER).status_code == 400


def test_an_unreadable_inbox_is_a_503_not_an_empty_answer(client):
    with patch(
        "sre_agent.repositories.inbox_repo.InboxRepository.find_active_by_correlation_task",
        side_effect=RuntimeError("db down"),
    ):
        resp = client.get("/inbox/by-alert", params=CRASH, headers=USER)
    assert resp.status_code == 503 and "could not be read" in resp.json()["detail"]


def test_like_wildcards_in_a_resource_name_are_literal(db):
    from sre_agent.monitor.findings import _make_finding
    from sre_agent.repositories.inbox_repo import get_inbox_repo

    _bridge(
        _make_finding(
            severity="warning",
            category="alerts",
            title="TargetDown",
            summary="",
            resources=[{"kind": "Alert", "name": "TargetDown{job=a_b}", "namespace": "ns"}],
        )
    )
    _bridge(
        _make_finding(
            severity="warning",
            category="other",
            title="T",
            summary="",
            resources=[{"kind": "Alert", "name": "TargetDown{job=aXb}", "namespace": "ns"}],
        )
    )
    rows = get_inbox_repo().find_active_tasks_for_resource("ns:Alert/TargetDown{job=a_b}", "none")
    assert [r["correlation_key"] for r in rows] == ["alerts:ns:Alert/TargetDown{job=a_b}"]
