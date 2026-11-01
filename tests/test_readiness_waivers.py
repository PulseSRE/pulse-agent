"""Readiness gate waivers kept by the agent.

Waivers lived in one browser's localStorage, so a colleague saw the gate
failing and a cleared browser lost who accepted which risk and why. These
tests drive the real routes and the real ``require_admin`` (shared token +
forwarded identity + allowlist) against the disposable test database: writes
need an admin, the author is the signed-in user and never the client's claim,
an empty reason is refused, expired waivers stop being active, and lifting a
waiver is idempotent and audited.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sre_agent.api import readiness_rest
from sre_agent.config import _reset_settings
from sre_agent.db import get_database
from sre_agent.repositories import ReadinessWaiverRepository, get_readiness_waiver_repo

TOKEN = "waiver-test-token"
ADMIN = {"Authorization": f"Bearer {TOKEN}", "X-Forwarded-User": "alice"}
OTHER_ADMIN = {"Authorization": f"Bearer {TOKEN}", "X-Forwarded-User": "bob"}
NON_ADMIN = {"Authorization": f"Bearer {TOKEN}", "X-Forwarded-User": "mallory"}
TOKEN_ONLY = {"Authorization": f"Bearer {TOKEN}"}


def _clean(db):
    db.execute("DELETE FROM readiness_waivers")
    db.execute("DELETE FROM user_interactions WHERE interaction_type LIKE ?", ("%readiness_gate",))
    db.commit()


@pytest.fixture(autouse=True)
def _db():
    db = get_database()
    _clean(db)
    yield db
    _clean(db)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PULSE_AGENT_WS_TOKEN", TOKEN)
    monkeypatch.setenv("PULSE_AGENT_ADMIN_USERS", "alice,bob")
    monkeypatch.setenv("PULSE_AGENT_DEV_USER", "")
    _reset_settings()
    app = FastAPI()
    app.include_router(readiness_rest.router)
    yield TestClient(app)
    _reset_settings()


def _future(days: int = 30) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def _interactions(kind: str) -> list[dict]:
    return get_database().fetchall(
        "SELECT actor, interaction_type, item_id, decision, metadata FROM user_interactions "
        "WHERE interaction_type = ? ORDER BY id",
        (kind,),
    )


# -- Endpoint behaviour --------------------------------------------------------


def test_waive_records_reason_author_and_time_and_lists_it(client):
    before = datetime.now(UTC) - timedelta(seconds=1)
    r = client.put("/readiness/waivers/etcd-backup", headers=ADMIN, json={"reason": "  backups run off-cluster  "})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["gate_id"] == "etcd-backup"
    assert body["reason"] == "backups run off-cluster"
    assert body["waived_by"] == "alice"
    assert body["expires_at"] is None
    assert datetime.fromisoformat(body["created_at"]) >= before

    listed = client.get("/readiness/waivers", headers=TOKEN_ONLY)
    assert listed.status_code == 200
    assert listed.json() == {"waivers": [body]}


def test_the_author_is_the_signed_in_admin_not_what_the_client_says(client):
    r = client.put(
        "/readiness/waivers/etcd-backup",
        headers=ADMIN,
        json={"reason": "accepted", "waived_by": "ceo", "approvedBy": "ceo", "author": "ceo"},
    )
    assert r.status_code == 200
    assert r.json()["waived_by"] == "alice"
    assert client.get("/readiness/waivers", headers=TOKEN_ONLY).json()["waivers"][0]["waived_by"] == "alice"


@pytest.mark.parametrize("body", [{}, {"reason": ""}, {"reason": "   \n "}, {"reason": 42}, {"reason": None}, []])
def test_an_empty_or_missing_reason_is_refused(client, body):
    r = client.put("/readiness/waivers/etcd-backup", headers=ADMIN, json=body)
    assert r.status_code == 400
    assert "reason" in r.json()["error"]
    assert get_readiness_waiver_repo().fetch_active(int(time.time() * 1000)) == []


def test_a_body_that_is_not_json_is_refused(client):
    r = client.put(
        "/readiness/waivers/etcd-backup",
        headers={**ADMIN, "Content-Type": "application/json"},
        content=b"not json",
    )
    assert r.status_code == 400


def test_an_overlong_reason_is_refused(client):
    r = client.put(
        "/readiness/waivers/etcd-backup",
        headers=ADMIN,
        json={"reason": "x" * (readiness_rest.MAX_REASON_LENGTH + 1)},
    )
    assert r.status_code == 400


@pytest.mark.parametrize("gate_id", ["Etcd-Backup", "-leading-dash", "a" * 101, "has space", "semi;colon"])
def test_an_invalid_gate_id_is_refused(client, gate_id):
    assert client.put(f"/readiness/waivers/{gate_id}", headers=ADMIN, json={"reason": "r"}).status_code == 400
    assert client.delete(f"/readiness/waivers/{gate_id}", headers=ADMIN).status_code == 400


def test_expiry_is_stored_and_returned(client):
    expires = datetime.now(UTC) + timedelta(days=30)
    r = client.put(
        "/readiness/waivers/network-policies",
        headers=ADMIN,
        json={"reason": "until Q1", "expires_at": expires.isoformat().replace("+00:00", "Z")},
    )
    assert r.status_code == 200, r.text
    got = datetime.fromisoformat(r.json()["expires_at"])
    assert abs((got - expires).total_seconds()) < 1


@pytest.mark.parametrize(
    "expires_at",
    [
        "2020-01-01T00:00:00Z",  # past
        "2099-01-01T00:00:00",  # no time zone
        "next tuesday",
        "",
        12345,
    ],
)
def test_a_bad_or_past_expiry_is_refused(client, expires_at):
    r = client.put("/readiness/waivers/network-policies", headers=ADMIN, json={"reason": "r", "expires_at": expires_at})
    assert r.status_code == 400
    assert "expires_at" in r.json()["error"]


def test_an_expired_waiver_is_not_returned_as_active(client):
    now = int(time.time() * 1000)
    repo = get_readiness_waiver_repo()
    repo.upsert("etcd-backup", "lapsed", "alice", now - 10_000, now - 1)
    repo.upsert("network-policies", "standing", "alice", now - 10_000, None)
    repo.upsert("tls-profile", "still valid", "alice", now - 10_000, now + 3_600_000)

    ids = [w["gate_id"] for w in client.get("/readiness/waivers", headers=TOKEN_ONLY).json()["waivers"]]
    assert ids == ["network-policies", "tls-profile"]


def test_rewaiving_replaces_the_earlier_waiver(client):
    client.put("/readiness/waivers/etcd-backup", headers=ADMIN, json={"reason": "first", "expires_at": _future()})
    r = client.put("/readiness/waivers/etcd-backup", headers=OTHER_ADMIN, json={"reason": "second"})
    assert r.status_code == 200
    (only,) = client.get("/readiness/waivers", headers=TOKEN_ONLY).json()["waivers"]
    assert (only["reason"], only["waived_by"], only["expires_at"]) == ("second", "bob", None)


def test_delete_lifts_the_waiver_and_is_idempotent(client):
    client.put("/readiness/waivers/etcd-backup", headers=ADMIN, json={"reason": "accepted"})
    r = client.delete("/readiness/waivers/etcd-backup", headers=OTHER_ADMIN)
    assert r.status_code == 200
    assert r.json() == {"gate_id": "etcd-backup", "deleted": True}
    assert client.get("/readiness/waivers", headers=TOKEN_ONLY).json() == {"waivers": []}

    # Not 404: a 404 is how a client recognises an agent without this endpoint.
    again = client.delete("/readiness/waivers/etcd-backup", headers=ADMIN)
    assert again.status_code == 200
    assert again.json() == {"gate_id": "etcd-backup", "deleted": False}


def test_waive_and_unwaive_are_in_the_audit_trail(client):
    client.put("/readiness/waivers/etcd-backup", headers=ADMIN, json={"reason": "accepted"})
    client.delete("/readiness/waivers/etcd-backup", headers=OTHER_ADMIN)
    client.delete("/readiness/waivers/etcd-backup", headers=OTHER_ADMIN)  # nothing to lift: not audited

    (waived,) = _interactions("waive_readiness_gate")
    assert (waived["actor"], waived["item_id"], waived["decision"]) == ("alice", "etcd-backup", "waived")
    assert waived["metadata"]["reason"] == "accepted"
    (lifted,) = _interactions("unwaive_readiness_gate")
    assert (lifted["actor"], lifted["item_id"], lifted["decision"]) == ("bob", "etcd-backup", "unwaived")


# -- Authorization -------------------------------------------------------------


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({}, 401),  # no shared token
        ({"Authorization": "Bearer wrong", "X-Forwarded-User": "alice"}, 401),  # wrong shared token
        (TOKEN_ONLY, 401),  # shared token but nobody signed in
        (NON_ADMIN, 403),  # signed in, not on the admin allowlist
    ],
)
def test_writes_need_a_signed_in_admin(client, headers, status):
    get_readiness_waiver_repo().upsert("tls-profile", "keep", "alice", int(time.time() * 1000), None)

    put = client.put("/readiness/waivers/etcd-backup", headers=headers, json={"reason": "sneaky"})
    assert put.status_code == status
    delete = client.delete("/readiness/waivers/tls-profile", headers=headers)
    assert delete.status_code == status

    ids = [w["gate_id"] for w in get_readiness_waiver_repo().fetch_active(int(time.time() * 1000))]
    assert ids == ["tls-profile"]
    assert _interactions("waive_readiness_gate") == []
    assert _interactions("unwaive_readiness_gate") == []


def test_reads_use_the_shared_token_like_readiness(client):
    assert client.get("/readiness/waivers").status_code == 401
    assert client.get("/readiness/waivers", headers={"Authorization": "Bearer wrong"}).status_code == 401
    # No signed-in user or admin needed to read, same as GET /readiness.
    assert client.get("/readiness/waivers", headers=TOKEN_ONLY).status_code == 200
    assert client.get("/readiness/waivers", headers=NON_ADMIN).status_code == 200


def test_storage_failure_is_reported_not_swallowed(client):
    class Broken(ReadinessWaiverRepository):
        def fetch_active(self, now_ms):
            raise RuntimeError("connection to postgresql://secret@db failed")

        def upsert(self, *a, **k):
            raise RuntimeError("connection to postgresql://secret@db failed")

        def delete(self, gate_id):
            raise RuntimeError("connection to postgresql://secret@db failed")

    with patch("sre_agent.repositories.get_readiness_waiver_repo", return_value=Broken()):
        responses = [
            client.get("/readiness/waivers", headers=TOKEN_ONLY),
            client.put("/readiness/waivers/etcd-backup", headers=ADMIN, json={"reason": "r"}),
            client.delete("/readiness/waivers/etcd-backup", headers=ADMIN),
        ]
    for r in responses:
        assert r.status_code == 503
        assert "secret" not in r.text
    assert _interactions("waive_readiness_gate") == []


# -- Repository ----------------------------------------------------------------


def test_repository_round_trip_against_postgres():
    repo = ReadinessWaiverRepository(get_database())
    now = int(time.time() * 1000)
    saved = repo.upsert("etcd-backup", "accepted", "alice", now, now + 60_000)
    assert saved == {
        "gate_id": "etcd-backup",
        "reason": "accepted",
        "waived_by": "alice",
        "created_at": now,
        "expires_at": now + 60_000,
    }
    assert repo.fetch_active(now) == [saved]
    # Active strictly before expiry, not at or after it.
    assert repo.fetch_active(now + 59_999) == [saved]
    assert repo.fetch_active(now + 60_000) == []

    assert repo.delete("etcd-backup") is True
    assert repo.fetch_active(now) == []
    assert repo.delete("etcd-backup") is False


def test_repository_delete_lifts_an_expired_waiver_too():
    repo = ReadinessWaiverRepository(get_database())
    now = int(time.time() * 1000)
    repo.upsert("etcd-backup", "lapsed", "alice", now - 2, now - 1)
    assert repo.delete("etcd-backup") is True
    assert get_database().fetchall("SELECT gate_id FROM readiness_waivers") == []


def test_repository_orders_oldest_first():
    repo = ReadinessWaiverRepository(get_database())
    repo.upsert("b-gate", "r", "alice", 2_000, None)
    repo.upsert("a-gate", "r", "alice", 3_000, None)
    repo.upsert("c-gate", "r", "alice", 1_000, None)
    assert [w["gate_id"] for w in repo.fetch_active(0)] == ["c-gate", "b-gate", "a-gate"]


def test_migration_042_creates_the_table_idempotently():
    from sre_agent.db_migrations import MIGRATIONS, _migrate_042_readiness_waivers

    assert MIGRATIONS[-1] == (42, "readiness_waivers", _migrate_042_readiness_waivers)
    db = get_database()
    _migrate_042_readiness_waivers(db)
    _migrate_042_readiness_waivers(db)  # idempotent
    cols = {
        r["column_name"]: r["is_nullable"]
        for r in db.fetchall(
            "SELECT column_name, is_nullable FROM information_schema.columns WHERE table_name = 'readiness_waivers'"
        )
    }
    assert cols == {
        "gate_id": "NO",
        "reason": "NO",
        "waived_by": "NO",
        "created_at": "NO",
        "expires_at": "YES",
    }
