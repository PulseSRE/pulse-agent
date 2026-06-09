"""Tests for inbox REST API endpoints."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def pulse_token():
    return "test-token-inbox-xyz"


@pytest.fixture
def auth_headers(pulse_token):
    return {
        "Authorization": f"Bearer {pulse_token}",
        "X-Forwarded-User": "test-admin",
    }


@pytest.fixture
def client(pulse_token, monkeypatch):
    monkeypatch.setenv("PULSE_AGENT_WS_TOKEN", pulse_token)
    monkeypatch.setenv("PULSE_AGENT_MEMORY", "0")

    with (
        patch("sre_agent.k8s_client._initialized", True),
        patch("sre_agent.k8s_client._load_k8s"),
        patch("sre_agent.k8s_client.get_core_client") as core,
        patch("sre_agent.k8s_client.get_apps_client"),
        patch("sre_agent.k8s_client.get_custom_client"),
        patch("sre_agent.k8s_client.get_version_client"),
    ):
        core.return_value = MagicMock()
        from sre_agent.api import app

        yield TestClient(app, raise_server_exceptions=False)


class TestInboxListEndpoint:
    def test_list_empty(self, client, auth_headers):
        resp = client.get("/inbox", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "stats" in data

    def test_list_with_type_filter(self, client, auth_headers):
        client.post(
            "/inbox",
            json={"title": "Test task", "item_type": "task"},
            headers=auth_headers,
        )
        resp = client.get("/inbox?type=task", headers=auth_headers)
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert all(i["item_type"] == "task" for i in items)

    def test_list_includes_current_user(self, client, auth_headers):
        resp = client.get("/inbox", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "current_user" in data
        assert data["current_user"] == "test-admin"

    def test_list_requires_auth(self, client):
        resp = client.get("/inbox")
        assert resp.status_code == 401


class TestInboxCreateEndpoint:
    def test_create_task(self, client, auth_headers):
        resp = client.post(
            "/inbox",
            json={
                "title": "Rotate TLS certs",
                "summary": "Certs expire in 5 days",
                "namespace": "production",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["id"].startswith("inb-")
        assert data["item_type"] == "task"

    def test_create_requires_title(self, client, auth_headers):
        resp = client.post("/inbox", json={}, headers=auth_headers)
        assert resp.status_code == 422


class TestInboxItemEndpoint:
    def test_get_item(self, client, auth_headers):
        create = client.post("/inbox", json={"title": "Get me"}, headers=auth_headers)
        item_id = create.json()["id"]

        resp = client.get(f"/inbox/{item_id}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["title"] == "Get me"

    def test_get_nonexistent(self, client, auth_headers):
        resp = client.get("/inbox/inb-nonexistent", headers=auth_headers)
        assert resp.status_code == 404


class TestInboxActions:
    def test_claim(self, client, auth_headers):
        create = client.post("/inbox", json={"title": "Claim me"}, headers=auth_headers)
        item_id = create.json()["id"]

        resp = client.post(f"/inbox/{item_id}/claim", headers=auth_headers)
        assert resp.status_code == 200

        item = client.get(f"/inbox/{item_id}", headers=auth_headers).json()
        assert item["claimed_by"] is not None

    def test_unclaim(self, client, auth_headers):
        create = client.post("/inbox", json={"title": "Unclaim me"}, headers=auth_headers)
        item_id = create.json()["id"]
        client.post(f"/inbox/{item_id}/claim", headers=auth_headers)

        resp = client.delete(f"/inbox/{item_id}/claim", headers=auth_headers)
        assert resp.status_code == 200

        item = client.get(f"/inbox/{item_id}", headers=auth_headers).json()
        assert item["claimed_by"] is None

    def test_acknowledge(self, client, auth_headers):
        create = client.post(
            "/inbox",
            json={"title": "Ack me"},
            headers=auth_headers,
        )
        item_id = create.json()["id"]

        resp = client.post(f"/inbox/{item_id}/acknowledge", headers=auth_headers)
        assert resp.status_code == 200

        item = client.get(f"/inbox/{item_id}", headers=auth_headers).json()
        assert item["status"] == "triaged"

    def test_snooze(self, client, auth_headers):
        create = client.post("/inbox", json={"title": "Snooze me"}, headers=auth_headers)
        item_id = create.json()["id"]

        resp = client.post(
            f"/inbox/{item_id}/snooze",
            json={"hours": 24},
            headers=auth_headers,
        )
        assert resp.status_code == 200

    def test_dismiss(self, client, auth_headers):
        create = client.post("/inbox", json={"title": "Dismiss me"}, headers=auth_headers)
        item_id = create.json()["id"]

        resp = client.post(f"/inbox/{item_id}/dismiss", headers=auth_headers)
        assert resp.status_code == 200

        item = client.get(f"/inbox/{item_id}", headers=auth_headers).json()
        assert item["status"] == "archived"

    def test_resolve(self, client, auth_headers):
        create = client.post(
            "/inbox",
            json={"title": "Resolve me"},
            headers=auth_headers,
        )
        item_id = create.json()["id"]
        # Follow the unified lifecycle: new → triaged → claimed → in_progress → resolved
        client.post(f"/inbox/{item_id}/acknowledge", headers=auth_headers)
        client.post(f"/inbox/{item_id}/claim", headers=auth_headers)
        client.patch(f"/inbox/{item_id}", json={"status": "in_progress"}, headers=auth_headers)

        resp = client.post(f"/inbox/{item_id}/resolve", headers=auth_headers)
        assert resp.status_code == 200

        item = client.get(f"/inbox/{item_id}", headers=auth_headers).json()
        assert item["status"] == "resolved"

    def test_pin(self, client, auth_headers):
        create = client.post("/inbox", json={"title": "Pin me"}, headers=auth_headers)
        item_id = create.json()["id"]

        resp = client.post(f"/inbox/{item_id}/pin", headers=auth_headers)
        assert resp.status_code == 200

    def test_escalate(self, client, auth_headers):
        create = client.post(
            "/inbox",
            json={"title": "Escalate me"},
            headers=auth_headers,
        )
        item_id = create.json()["id"]

        resp = client.post(f"/inbox/{item_id}/escalate", headers=auth_headers)
        assert resp.status_code == 200
        assert "finding_id" in resp.json()

    def test_stats(self, client, auth_headers):
        resp = client.get("/inbox/stats", headers=auth_headers)
        assert resp.status_code == 200
        assert "total" in resp.json()
