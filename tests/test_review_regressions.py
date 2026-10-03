"""Rollback authorization, exact restoration, and topology ID regressions."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sre_agent import k8s_client, snapshot
from sre_agent.api import fix_rest, topology_rest
from sre_agent.config import _reset_settings
from sre_agent.dependency_graph import DependencyGraph


@pytest.fixture
def rollback_client(monkeypatch):
    monkeypatch.setenv("PULSE_AGENT_WS_TOKEN", "service")
    monkeypatch.setenv("PULSE_AGENT_ADMIN_USERS", "alice")
    monkeypatch.setenv("PULSE_AGENT_TOKEN_FORWARDING", "true")
    monkeypatch.setenv("PULSE_AGENT_DEV_USER", "")
    _reset_settings()
    app = FastAPI()
    app.include_router(fix_rest.router)
    return TestClient(app)


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({"Authorization": "Bearer service"}, 401),
        ({"Authorization": "Bearer service", "X-Forwarded-User": "bob"}, 403),
        ({"Authorization": "Bearer service", "X-Forwarded-User": "alice"}, 401),
    ],
)
def test_rollback_rejects_unauthorized_or_missing_user_token(rollback_client, monkeypatch, headers, status):
    execute = MagicMock()
    monkeypatch.setattr(fix_rest, "execute_rollback", execute)
    assert rollback_client.post("/fix-history/action/rollback", headers=headers).status_code == status
    execute.assert_not_called()


def test_rollback_forwards_caller_credentials_in_worker(rollback_client, monkeypatch):
    def execute(action_id):
        assert action_id == "action"
        assert k8s_client._user_token_var.get() == "alice-token"
        assert k8s_client._require_user_token_var.get()
        # A synchronous Kubernetes call must not block the request event loop.
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        return {"status": "rolled_back"}

    monkeypatch.setattr(fix_rest, "execute_rollback", execute)
    response = rollback_client.post(
        "/fix-history/action/rollback",
        headers={
            "Authorization": "Bearer service",
            "X-Forwarded-User": "alice",
            "X-Forwarded-Access-Token": "alice-token",
        },
    )
    assert response.status_code == 200
    assert k8s_client._user_token_var.get() is None


class ResourceAPI:
    def __init__(self, obj):
        self.obj = deepcopy(obj)
        self.patches = []
        self.ignore_patch = False
        self.concurrent_change = False

    def read(self, name, namespace):
        return deepcopy(self.obj)

    def patch(self, name, namespace, body, *, _content_type):
        assert _content_type == "application/json-patch+json"
        self.patches.append(body)
        if self.concurrent_change:
            self.obj["metadata"]["resourceVersion"] = "concurrent"
        candidate = deepcopy(self.obj)
        for op in body:
            parts = op["path"].strip("/").split("/")
            parent = candidate
            for part in parts[:-1]:
                parent = parent[part]
            if op["op"] == "test":
                if parent[parts[-1]] != op["value"]:
                    raise ValueError("JSON patch conflict")
            else:
                assert op["op"] == "add"
                parent[parts[-1]] = deepcopy(op["value"])
        if not self.ignore_patch:
            self.obj = candidate
        return deepcopy(self.obj)


def resource_api(monkeypatch, kind="ConfigMap"):
    obj = {"metadata": {"name": "api", "namespace": "prod", "uid": "original", "resourceVersion": "2"}}
    if kind == "ConfigMap":
        obj.update(data={"keep": "old"}, binaryData={"blob": "YWJj"})
        suffix = "config_map"
    else:
        obj["spec"] = {"template": {"spec": {"containers": [{"name": "api", "env": [{"name": "KEEP", "value": "1"}]}]}}}
        suffix = "deployment"
    api = ResourceAPI(obj)
    for reader_suffix in ("config_map", "deployment", "stateful_set", "daemon_set"):
        setattr(api, f"read_namespaced_{reader_suffix}", api.read)
    setattr(api, f"patch_namespaced_{suffix}", api.patch)
    monkeypatch.setattr(k8s_client, "get_core_client" if kind == "ConfigMap" else "get_apps_client", lambda: api)
    return api


@pytest.mark.parametrize("kind", ["ConfigMap", "Deployment"])
def test_snapshot_removes_added_map_keys_and_list_entries(monkeypatch, kind):
    api = resource_api(monkeypatch, kind)
    before = snapshot.capture(kind, "api", "prod")
    assert before["uid"] == "original"
    if kind == "ConfigMap":
        api.obj["data"]["introduced"] = "bad"
        api.obj["binaryData"]["introduced"] = "YmFk"
    else:
        api.obj["spec"]["template"]["spec"]["containers"][0]["env"].append({"name": "BAD", "value": "1"})
    api.obj["metadata"]["labels"] = {"introduced": "bad"}
    snapshot.restore(before)
    assert api.obj.get("data") == before.get("data")
    assert api.obj.get("binaryData") == before.get("binaryData")
    assert api.obj.get("spec") == before.get("spec")
    assert api.obj["metadata"]["labels"] == {}


@pytest.mark.parametrize("failure", ["recreated", "concurrent", "ignored", "legacy"])
def test_snapshot_refuses_unsafe_or_unverified_restore(monkeypatch, failure):
    api = resource_api(monkeypatch)
    before = snapshot.capture("ConfigMap", "api", "prod")
    api.obj["data"]["introduced"] = "bad"
    if failure == "recreated":
        api.obj["metadata"]["uid"] = "replacement"
    elif failure == "concurrent":
        api.concurrent_change = True
    elif failure == "ignored":
        api.ignore_patch = True
    else:
        before.pop("uid")
    with pytest.raises(ValueError):
        snapshot.restore(before)
    assert api.obj["data"]["introduced"] == "bad"


@pytest.mark.parametrize(
    ("kind", "namespace", "relationship"), [("Deployment", "prod", "owns"), ("Node", "", "schedules")]
)
def test_topology_id_round_trips_to_blast_radius(monkeypatch, kind, namespace, relationship):
    graph = DependencyGraph()
    source = graph.add_node(kind, namespace, "source")
    child = graph.add_node("Pod", "prod", "pod")
    graph.add_edge(source, child, relationship)
    monkeypatch.setattr("sre_agent.dependency_graph.get_dependency_graph", lambda: graph)
    result = asyncio.run(topology_rest.get_blast_radius(source, "", None, None))
    assert result["affected"] == 1
    assert result["source"] == source
    assert result["resources"][0]["id"] == child
    assert result["resources"][0]["relationship"] == relationship
