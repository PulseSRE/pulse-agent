"""RBAC/SCC/webhook change history and the Pulse write ledger.

Observation runs against a scripted lister and the disposable test database:
the first observation of a kind is a baseline (not a change); an edit is an
``updated`` row whose ``before`` is the previous content; a resourceVersion
bump with no content change records nothing; a disappearance is ``deleted``;
a kind the service account cannot list is reported unwatched and its objects
are never marked deleted. The REST routes need the caller's token, hide what
the caller may not read (a failed check hides too), attribute a change to a
Pulse write when the ledger has one, and take the ledger's actor from the
login proxy, never from the body.
"""

from __future__ import annotations

import copy
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kubernetes.client.rest import ApiException

from sre_agent import rbac_history
from sre_agent.api import rbac_history_rest
from sre_agent.config import _reset_settings
from sre_agent.db import get_database

pytestmark = pytest.mark.real_rbac_history

TOKEN = "rbac-history-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
AS_USER = {**AUTH, "X-Forwarded-User": "erin", "X-Forwarded-Access-Token": "user-token-erin"}

T0 = 1_762_300_000_000  # epoch ms


def _clean(db):
    db.execute("TRUNCATE rbac_object_versions, pulse_write_ledger RESTART IDENTITY")
    db.commit()


@pytest.fixture(autouse=True)
def _db():
    db = get_database()
    _clean(db)
    rbac_history.reset_for_tests()
    yield db
    _clean(db)
    rbac_history.reset_for_tests()


def role(name="secret-manager", ns="payments", verbs=("get", "list"), rv="1", manager="kubectl-edit"):
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {
            "name": name,
            "namespace": ns,
            "uid": f"uid-{name}",
            "resourceVersion": rv,
            "creationTimestamp": "2026-01-01T00:00:00Z",
            "managedFields": [
                {"manager": "kube-controller", "operation": "Update", "time": "2026-01-01T00:00:00Z"},
                {"manager": manager, "operation": "Update", "time": "2026-11-05T02:14:00Z"},
            ],
        },
        "rules": [{"apiGroups": [""], "resources": ["secrets"], "verbs": list(verbs)}],
    }


class Cluster:
    def __init__(self):
        self.items: dict[str, list[dict]] = {k: [] for k in rbac_history.WATCHED_KINDS}
        self.forbidden: set[str] = set()

    def __call__(self, kind):
        if kind in self.forbidden:
            raise ApiException(status=403, reason="Forbidden")
        return copy.deepcopy(self.items[kind])


def _rows(db):
    return db.fetchall("SELECT kind, namespace, name, change, manager, content FROM rbac_object_versions ORDER BY id")


# -- Observation ---------------------------------------------------------------


def test_first_observation_is_a_baseline_then_edits_creates_and_deletes_are_changes(_db):
    c = Cluster()
    c.items["Role"] = [role()]
    out = rbac_history.observe(T0, lister=c)
    assert out == {"recorded": 1, "unwatched": {}}
    assert [r["change"] for r in _rows(_db)] == ["baseline"]

    # Same version again: nothing.
    assert rbac_history.observe(T0 + 60_000, lister=c)["recorded"] == 0
    # resourceVersion moved but content did not (e.g. a no-op apply): nothing.
    c.items["Role"] = [role(rv="2")]
    assert rbac_history.observe(T0 + 120_000, lister=c)["recorded"] == 0

    c.items["Role"] = [role(rv="3", verbs=("get", "list", "delete", "patch"), manager="oc")]
    c.items["ClusterRoleBinding"] = []
    assert rbac_history.observe(T0 + 180_000, lister=c)["recorded"] == 1
    c.items["Role"] = []
    c.items["RoleBinding"] = [
        {
            "metadata": {"name": "contractor-view", "namespace": "shop", "resourceVersion": "9"},
            "roleRef": {"kind": "ClusterRole", "name": "view"},
            "subjects": [],
        }
    ]
    rbac_history.observe(T0 + 240_000, lister=c)

    rows = _rows(_db)
    assert [(r["kind"], r["name"], r["change"]) for r in rows] == [
        ("Role", "secret-manager", "baseline"),
        ("Role", "secret-manager", "updated"),
        ("Role", "secret-manager", "deleted"),
        # RoleBinding was observed (empty) before, so a new one is "created", not a baseline.
        ("RoleBinding", "contractor-view", "created"),
    ]
    updated = rows[1]
    assert updated["manager"] == "oc"
    assert updated["content"]["rules"][0]["verbs"] == ["get", "list", "delete", "patch"]
    # Stored without server-owned fields, so it can be replayed.
    assert set(updated["content"]["metadata"]) == {"name", "namespace"}
    assert rows[2]["content"] is None


def test_a_kind_that_cannot_be_listed_is_unwatched_and_never_marked_deleted(_db):
    c = Cluster()
    c.items["SecurityContextConstraints"] = [{"metadata": {"name": "anyuid", "resourceVersion": "1"}, "users": []}]
    rbac_history.observe(T0, lister=c)
    c.forbidden.add("SecurityContextConstraints")
    out = rbac_history.observe(T0 + 60_000, lister=c)
    assert out["unwatched"] == {"SecurityContextConstraints": "403 Forbidden"}
    assert [r["change"] for r in _rows(_db)] == ["baseline"]
    assert rbac_history.status()["unwatched"] == {"SecurityContextConstraints": "403 Forbidden"}


def test_history_survives_a_restart(_db):
    c = Cluster()
    c.items["Role"] = [role()]
    rbac_history.observe(T0, lister=c)
    rbac_history.reset_for_tests()  # a new process: the in-memory view is rebuilt from the database
    c.items["Role"] = [role(rv="5", verbs=("*",))]
    rbac_history.observe(T0 + 60_000, lister=c)
    assert [r["change"] for r in _rows(_db)] == ["baseline", "updated"]


def test_prune_keeps_each_objects_newest_row(_db):
    from sre_agent.repositories import get_rbac_history_repo

    c = Cluster()
    c.items["Role"] = [role()]
    rbac_history.observe(T0, lister=c)
    c.items["Role"] = [role(rv="2", verbs=("get",))]
    rbac_history.observe(T0 + 1000, lister=c)
    get_rbac_history_repo().prune(T0 + 10_000)
    assert [r["change"] for r in _rows(_db)] == ["updated"]


def test_observe_safely_never_raises(_db):
    with patch.object(rbac_history, "observe", side_effect=RuntimeError("db down")):
        rbac_history.observe_safely()
    assert rbac_history.status()["last_error"] == "db down"


# -- REST ------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PULSE_AGENT_WS_TOKEN", TOKEN)
    monkeypatch.setenv("PULSE_AGENT_TOKEN_FORWARDING", "true")
    monkeypatch.setenv("PULSE_AGENT_DEV_USER", "")
    _reset_settings()
    app = FastAPI()
    app.include_router(rbac_history_rest.router)
    yield TestClient(app)
    _reset_settings()


def _history(now_ms):
    c = Cluster()
    c.items["Role"] = [role()]
    c.items["ClusterRole"] = [{"metadata": {"name": "system:aggregate-to-view", "resourceVersion": "1"}, "rules": []}]
    rbac_history.observe(now_ms - 600_000, lister=c)
    c.items["Role"] = [role(rv="3", verbs=("get", "list", "delete", "patch"), manager="oc")]
    c.items["ClusterRole"] = [
        {"metadata": {"name": "system:aggregate-to-view", "resourceVersion": "2"}, "rules": [{"verbs": ["get"]}]}
    ]
    rbac_history.observe(now_ms, lister=c)


def _now_ms():
    import time

    return int(time.time() * 1000)


def test_changes_need_the_callers_token(client):
    r = client.get("/rbac/changes", headers=AUTH)
    assert r.status_code == 401
    assert client.get("/rbac/changes").status_code == 401


def test_changes_serve_before_and_after_and_hide_system_objects(client):
    _history(_now_ms())
    seen_tokens = []

    def can_get(group, plural, ns):
        from sre_agent import k8s_client

        seen_tokens.append(k8s_client.get_current_user_token())
        return True

    with patch.object(rbac_history_rest, "_can_get", side_effect=can_get):
        r = client.get("/rbac/changes", headers=AS_USER)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [(c["kind"], c["name"], c["change"]) for c in body["changes"]] == [("Role", "secret-manager", "updated")]
    ch = body["changes"][0]
    assert ch["before"]["rules"][0]["verbs"] == ["get", "list"]
    assert ch["after"]["rules"][0]["verbs"] == ["get", "list", "delete", "patch"]
    assert ch["manager"] == "oc" and ch["actor"] is None
    assert body["system_hidden"] == 1 and body["hidden"] == 0
    assert body["history"]["source"] == "observed" and body["history"]["since"]
    # The access checks ran as the caller.
    assert seen_tokens and set(seen_tokens) == {"user-token-erin"}

    with patch.object(rbac_history_rest, "_can_get", return_value=True):
        r = client.get("/rbac/changes", headers=AS_USER, params={"include_system": "true"})
    assert {c["name"] for c in r.json()["changes"]} == {"secret-manager", "system:aggregate-to-view"}


def test_objects_the_caller_cannot_read_are_hidden_and_counted(client):
    _history(_now_ms())
    with patch.object(rbac_history_rest, "_can_get", return_value=False):
        body = client.get("/rbac/changes", headers=AS_USER).json()
    assert body["changes"] == [] and body["hidden"] == 1


def test_a_failed_access_check_hides_rather_than_shows(client):
    _history(_now_ms())
    with patch.object(rbac_history_rest, "_can_get", side_effect=RuntimeError("apiserver down")):
        body = client.get("/rbac/changes", headers=AS_USER).json()
    assert body["changes"] == [] and body["hidden"] == 1


def test_invalid_input_is_a_400(client):
    assert client.get("/rbac/changes", headers=AS_USER, params={"kind": "Secret"}).status_code == 400
    assert client.get("/rbac/changes", headers=AS_USER, params={"since": "2020-01-01T00:00:00Z"}).status_code == 400
    assert client.get("/rbac/changes", headers=AS_USER, params={"namespace": "Bad_NS"}).status_code == 400


def test_ledger_records_the_signed_in_actor_and_attributes_the_change(client):
    r = client.post(
        "/rbac/ledger",
        headers=AS_USER,
        json={
            "verb": "update",
            "kind": "Role",
            "namespace": "payments",
            "name": "secret-manager",
            "reason": "restore",
            "actor": "mallory",
            "before": {"a": 1},
            "after": {"a": 2},
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["actor"] == "erin" and r.json()["source"] == "ui" and r.json()["has_before"] is True
    # The observation that sees the change comes after the write.
    _history(_now_ms() + 1000)
    with patch.object(rbac_history_rest, "_can_get", return_value=True):
        body = client.get("/rbac/changes", headers=AS_USER).json()
    assert body["changes"][0]["actor"]["name"] == "erin"
    assert body["changes"][0]["actor"]["reason"] == "restore"
    assert [e["actor"] for e in body["ledger"]] == ["erin"]


def test_ledger_needs_an_identity_and_refuses_bad_entries(client):
    assert (
        client.post("/rbac/ledger", headers=AUTH, json={"verb": "update", "kind": "Role", "name": "x"}).status_code
        == 401
    )
    assert (
        client.post("/rbac/ledger", headers=AS_USER, json={"verb": "drop", "kind": "Role", "name": "x"}).status_code
        == 400
    )
    assert (
        client.post(
            "/rbac/ledger", headers=AS_USER, json={"verb": "update", "kind": "ConfigMap", "name": "x"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/rbac/ledger",
            headers=AS_USER,
            json={"verb": "update", "kind": "Secret", "name": "x", "after": {"data": "x"}},
        ).status_code
        == 400
    )


def test_agent_write_tools_are_recorded_with_the_signed_in_user(_db):
    from sre_agent.request_context import _current_user

    tok = _current_user.set("erin")
    try:
        rbac_history.record_agent_write(
            "delete_resource", {"kind": "RoleBinding", "namespace": "shop", "name": "x"}, "success"
        )
    finally:
        _current_user.reset(tok)
    rows = _db.fetchall("SELECT actor, source, verb, kind, namespace, name, outcome FROM pulse_write_ledger")
    assert rows == [
        {
            "actor": "erin",
            "source": "agent",
            "verb": "delete_resource",
            "kind": "RoleBinding",
            "namespace": "shop",
            "name": "x",
            "outcome": "ok",
        }
    ]


def test_migration_043_is_last_and_idempotent(_db):
    from sre_agent.db_migrations import MIGRATIONS, _migrate_043_rbac_history

    assert MIGRATIONS[-1] == (43, "rbac_history", _migrate_043_rbac_history)
    _migrate_043_rbac_history(_db)
    _migrate_043_rbac_history(_db)
    tables = {
        r["table_name"]
        for r in _db.fetchall(
            "SELECT table_name FROM information_schema.tables WHERE table_name IN ('rbac_object_versions', 'pulse_write_ledger')"
        )
    }
    assert tables == {"rbac_object_versions", "pulse_write_ledger"}
