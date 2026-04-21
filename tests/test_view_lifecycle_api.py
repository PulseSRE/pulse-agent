"""Tests for view lifecycle REST endpoints."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sre_agent.api.auth import get_owner
from sre_agent.api.views import router


def _mock_owner():
    return "testuser"


@pytest.fixture
def app():
    a = FastAPI()
    a.include_router(router)
    a.dependency_overrides[get_owner] = _mock_owner
    yield a
    a.dependency_overrides.clear()


@pytest.fixture
def client(app):
    return TestClient(app)


class TestListViewsFilters:
    def test_filter_by_view_type(self, client):
        with patch("sre_agent.db.list_views", return_value=[]) as mock:
            client.get("/views?view_type=incident")
            mock.assert_called_once_with("testuser", view_type="incident", visibility=None, exclude_status=None)

    def test_filter_by_visibility(self, client):
        with patch("sre_agent.db.list_views", return_value=[]) as mock:
            client.get("/views?visibility=team")
            mock.assert_called_once_with("testuser", view_type=None, visibility="team", exclude_status=None)

    def test_filter_exclude_status(self, client):
        with patch("sre_agent.db.list_views", return_value=[]) as mock:
            client.get("/views?view_type=plan&exclude_status=completed")
            mock.assert_called_once_with("testuser", view_type="plan", visibility=None, exclude_status="completed")


class TestStatusTransitionEndpoint:
    def test_valid_transition(self, client):
        with patch("sre_agent.db.transition_view_status", return_value=True):
            resp = client.post("/views/cv-1/status", json={"status": "action_taken"})
        assert resp.status_code == 200
        assert resp.json()["transitioned"] is True

    def test_invalid_transition(self, client):
        with patch("sre_agent.db.transition_view_status", return_value=False):
            resp = client.post("/views/cv-1/status", json={"status": "completed"})
        assert resp.status_code == 409

    def test_missing_status_field(self, client):
        resp = client.post("/views/cv-1/status", json={})
        assert resp.status_code == 400


class TestClaimEndpoint:
    def test_claim(self, client):
        with patch("sre_agent.db.claim_view", return_value=True):
            resp = client.post("/views/cv-1/claim")
        assert resp.status_code == 200
        assert resp.json()["claimed_by"] == "testuser"

    def test_claim_denied(self, client):
        with patch("sre_agent.db.claim_view", return_value=False):
            resp = client.post("/views/cv-1/claim")
        assert resp.status_code == 409

    def test_unclaim(self, client):
        with patch("sre_agent.db.unclaim_view", return_value=True):
            resp = client.delete("/views/cv-1/claim")
        assert resp.status_code == 200

    def test_unclaim_denied(self, client):
        with patch("sre_agent.db.unclaim_view", return_value=False):
            resp = client.delete("/views/cv-1/claim")
        assert resp.status_code == 409
