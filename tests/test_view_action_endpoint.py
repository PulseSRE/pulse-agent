"""Tests for POST /views/{view_id}/actions endpoint."""

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


class TestActionEndpoint:
    def test_missing_action_field(self, client):
        resp = client.post("/views/cv-test/actions", json={"action_input": {}})
        assert resp.status_code == 400
        assert "action" in resp.json()["error"].lower()

    def test_blocked_tool(self, client):
        resp = client.post(
            "/views/cv-test/actions",
            json={"action": "drain_node", "action_input": {"node_name": "worker-1"}},
        )
        assert resp.status_code == 403
        assert "not allowed" in resp.json()["error"].lower()

    def test_unknown_tool(self, client):
        resp = client.post(
            "/views/cv-test/actions",
            json={"action": "nonexistent_tool_xyz", "action_input": {}},
        )
        assert resp.status_code == 400
        assert "not found" in resp.json()["error"].lower()

    def test_view_not_found(self, client):
        from sre_agent.tool_registry import TOOL_REGISTRY

        tool_name = next(
            (n for n in TOOL_REGISTRY if n not in {"drain_node", "exec_command"}),
            None,
        )
        # Empty registry is a genuine failure — every tool module registers at
        # import. Skipping here would report success while testing nothing.
        assert tool_name is not None, "TOOL_REGISTRY is empty — tool registration is broken"

        with patch("sre_agent.db.get_view", return_value=None):
            resp = client.post(
                "/views/cv-test/actions",
                json={"action": tool_name, "action_input": {}},
            )
        assert resp.status_code == 404

    def test_trust_level_zero_blocks_write(self, client):
        with patch("sre_agent.api.views.get_settings") as mock_settings:
            mock_settings.return_value.max_trust_level = 0
            resp = client.post(
                "/views/cv-test/actions",
                json={
                    "action": "restart_deployment",
                    "action_input": {"name": "nginx", "namespace": "default"},
                },
            )
        assert resp.status_code == 403

    def test_invalid_namespace_rejected(self, client):
        from sre_agent.tool_registry import TOOL_REGISTRY

        tool_name = next(
            (n for n in TOOL_REGISTRY if n not in {"drain_node", "exec_command"}),
            None,
        )
        # Empty registry is a genuine failure — every tool module registers at
        # import. Skipping here would report success while testing nothing.
        assert tool_name is not None, "TOOL_REGISTRY is empty — tool registration is broken"

        with patch("sre_agent.db.get_view", return_value={"id": "cv-test", "owner": "testuser", "layout": []}):
            resp = client.post(
                "/views/cv-test/actions",
                json={"action": tool_name, "action_input": {"namespace": "INVALID!!"}},
            )
        assert resp.status_code == 400

    def test_replicas_out_of_range(self, client):
        from sre_agent.tool_registry import TOOL_REGISTRY

        tool_name = next(
            (n for n in TOOL_REGISTRY if n not in {"drain_node", "exec_command"}),
            None,
        )
        # Empty registry is a genuine failure — every tool module registers at
        # import. Skipping here would report success while testing nothing.
        assert tool_name is not None, "TOOL_REGISTRY is empty — tool registration is broken"

        with patch("sre_agent.db.get_view", return_value={"id": "cv-test", "owner": "testuser", "layout": []}):
            resp = client.post(
                "/views/cv-test/actions",
                json={"action": tool_name, "action_input": {"replicas": 999}},
            )
        assert resp.status_code == 400
        assert "0-100" in resp.json()["error"]

    def test_action_input_must_be_dict(self, client):
        from sre_agent.tool_registry import TOOL_REGISTRY

        tool_name = next(
            (n for n in TOOL_REGISTRY if n not in {"drain_node", "exec_command"}),
            None,
        )
        # Empty registry is a genuine failure — every tool module registers at
        # import. Skipping here would report success while testing nothing.
        assert tool_name is not None, "TOOL_REGISTRY is empty — tool registration is broken"

        resp = client.post(
            "/views/cv-test/actions",
            json={"action": tool_name, "action_input": "not a dict"},
        )
        assert resp.status_code == 400
        assert "dict" in resp.json()["error"].lower()
