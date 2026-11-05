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
    assert body["changes"] == [] and body["hidden"] == 2 and body["system_hidden"] == 0


def test_a_failed_access_check_hides_rather_than_shows(client):
    _history(_now_ms())
    with patch.object(rbac_history_rest, "_can_get", side_effect=RuntimeError("apiserver down")):
        body = client.get("/rbac/changes", headers=AS_USER).json()
    assert body["changes"] == [] and body["hidden"] == 2 and body["system_hidden"] == 0


def test_invalid_input_is_a_400_and_since_is_clamped_to_retention(client):
    assert client.get("/rbac/changes", headers=AS_USER, params={"kind": "Secret"}).status_code == 400
    assert client.get("/rbac/changes", headers=AS_USER, params={"namespace": "Bad_NS"}).status_code == 400
    with patch.object(rbac_history_rest, "_can_get", return_value=True):
        r = client.get("/rbac/changes", headers=AS_USER, params={"since": "2020-01-01T00:00:00Z"})
    assert r.status_code == 200


def test_system_hidden_counts_only_what_the_caller_could_read(client):
    _history(_now_ms())

    def can_get(group, plural, ns):
        return plural != "clusterroles"

    with patch.object(rbac_history_rest, "_can_get", side_effect=can_get):
        body = client.get("/rbac/changes", headers=AS_USER).json()
    # The system ClusterRole change is hidden by permission, so it is not also counted as "system".
    assert body["hidden"] == 1 and body["system_hidden"] == 0 and body["truncated"] is False


LEDGER = {
    "verb": "update",
    "kind": "Role",
    "namespace": "payments",
    "name": "secret-manager",
    "resource_version": "3",
    "reason": "restore",
    "actor": "mallory",
    "before": {"a": 1},
    "after": {"a": 2},
}


@pytest.fixture
def ledger_ok():
    """The caller's token says erin, and erin may make the write."""
    rbac_history_rest.reset_rate_limit_for_tests()
    calls = []

    def can(verb, group, plural, ns, name=""):
        from sre_agent import k8s_client

        calls.append((verb, group, plural, ns, name, k8s_client.get_current_user_token()))
        return True

    with (
        patch.object(rbac_history_rest, "_whoami", return_value="erin"),
        patch.object(rbac_history_rest, "_can", side_effect=can),
        patch.object(rbac_history_rest, "_can_get", return_value=True),
    ):
        yield calls
    rbac_history_rest.reset_rate_limit_for_tests()


def test_ledger_actor_comes_from_the_token_and_the_write_is_checked_on_the_exact_object(client, ledger_ok):
    # X-Forwarded-User says "erin" too here, but the actor is what the token review returns.
    r = client.post("/rbac/ledger", headers={**AS_USER, "X-Forwarded-User": "mallory"}, json=LEDGER)
    assert r.status_code == 200, r.text
    row = r.json()
    assert row["actor"] == "erin" and row["actor_verified"] is True and row["resource_version"] == "3"
    assert ledger_ok == [
        ("update", "rbac.authorization.k8s.io", "roles", "payments", "secret-manager", "user-token-erin")
    ]


def test_a_change_is_credited_only_to_the_entry_with_its_resource_version(client, ledger_ok):
    assert client.post("/rbac/ledger", headers=AS_USER, json={**LEDGER, "resource_version": "2"}).status_code == 200
    _history(_now_ms() + 1000)  # observes resourceVersion 3
    body = client.get("/rbac/changes", headers=AS_USER).json()
    # A write that produced version 2 did not produce the observed version 3: the client is shown, not erin.
    assert body["changes"][0]["actor"] is None and body["changes"][0]["manager"] == "oc"
    assert client.post("/rbac/ledger", headers=AS_USER, json=LEDGER).status_code == 200
    body = client.get("/rbac/changes", headers=AS_USER).json()
    actor = body["changes"][0]["actor"]
    assert actor["name"] == "erin" and actor["verified"] is True and actor["reason"] == "restore"


def test_a_write_the_caller_may_not_make_is_refused(client, ledger_ok):
    with patch.object(rbac_history_rest, "_can", return_value=False):
        r = client.post("/rbac/ledger", headers=AS_USER, json=LEDGER)
    assert r.status_code == 403
    assert get_database().fetchall("SELECT id FROM pulse_write_ledger") == []


def test_without_the_review_api_the_proxy_user_is_used_and_marked_unverified(client):
    rbac_history_rest.reset_rate_limit_for_tests()
    with (
        patch.object(rbac_history_rest, "_whoami", return_value=None),
        patch.object(rbac_history_rest, "_can", return_value=True),
    ):
        r = client.post("/rbac/ledger", headers=AS_USER, json=LEDGER)
    assert r.json()["actor"] == "erin" and r.json()["actor_verified"] is False


def test_a_refused_token_is_401(client):
    with patch.object(
        rbac_history_rest, "_whoami", side_effect=rbac_history_rest._ActorUnknownError("401 Unauthorized")
    ):
        assert client.post("/rbac/ledger", headers=AS_USER, json=LEDGER).status_code == 401


def test_ledger_needs_the_callers_token_and_refuses_bad_entries(client, ledger_ok):
    no_token = {**AUTH, "X-Forwarded-User": "erin"}
    assert client.post("/rbac/ledger", headers=no_token, json=LEDGER).status_code == 401
    bad = [
        {**LEDGER, "verb": "drop"},
        {**LEDGER, "kind": "ConfigMap"},
        {**LEDGER, "kind": "Secret", "after": {"data": "x"}},
    ]
    for b in bad:
        assert client.post("/rbac/ledger", headers=AS_USER, json=b).status_code == 400, b
    big = {**LEDGER, "after": {"x": "y" * (65 * 1024)}}
    assert client.post("/rbac/ledger", headers=AS_USER, json=big).status_code == 413
    huge = {**LEDGER, "after": {"x": "y" * (300 * 1024)}}
    assert client.post("/rbac/ledger", headers=AS_USER, json=huge).status_code == 413


def test_ledger_writes_are_rate_limited_per_user(client, ledger_ok):
    codes = [client.post("/rbac/ledger", headers=AS_USER, json=LEDGER).status_code for _ in range(61)]
    assert codes[:60] == [200] * 60 and codes[60] == 429


def test_prune_also_drops_old_ledger_entries(_db):
    from sre_agent.repositories import get_rbac_history_repo

    repo = get_rbac_history_repo()
    for at in (T0, T0 + 10_000):
        repo.record_write({"at": at, "actor": "erin", "source": "ui", "verb": "update", "kind": "Role", "name": "x"})
    repo.prune(T0 + 5000)
    assert [r["at"] for r in _db.fetchall("SELECT at FROM pulse_write_ledger")] == [T0 + 10_000]


def test_agent_write_tools_store_only_the_target_never_the_arguments(_db):
    from sre_agent.request_context import _current_user

    tok = _current_user.set("erin")
    try:
        rbac_history.record_agent_write(
            "exec_command", {"namespace": "shop", "pod_name": "web-1", "command": "cat /etc/secret"}, "success"
        )
        manifest = (
            "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: cfg, namespace: shop}\ndata: {password: hunter2}\n"
        )
        rbac_history.record_agent_write("apply_yaml", {"yaml_content": manifest}, "error", "Forbidden")
    finally:
        _current_user.reset(tok)
    rows = _db.fetchall(
        "SELECT actor, source, verb, kind, namespace, name, outcome, detail FROM pulse_write_ledger ORDER BY id"
    )
    assert [(r["verb"], r["kind"], r["namespace"], r["name"], r["outcome"]) for r in rows] == [
        ("exec_command", "Pod", "shop", "web-1", "ok"),
        ("apply_yaml", "ConfigMap", "shop", "cfg", "failed"),
    ]
    assert all(r["actor"] == "erin" and r["source"] == "agent" for r in rows)
    stored = str(rows)
    assert "cat /etc/secret" not in stored and "hunter2" not in stored


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
