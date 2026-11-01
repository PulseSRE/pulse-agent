"""Saying no to a proposed fix.

A proposal could be approved or left to expire, so "no" looked exactly like
"nobody looked" and fix learning never heard it. These tests drive the real
REST route against the test database: the proposal is answered, the
rejection is filed against its (cause, strategy), the finding's inbox item
records it (and stays open), and every UI is told.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sre_agent.api.auth import require_admin
from sre_agent.api.fix_rest import router
from sre_agent.db import get_database
from sre_agent.fix_learning import HUMAN_REVIEW, rejections, steer, track_record
from sre_agent.inbox import create_inbox_item, get_inbox_item
from sre_agent.monitor.actions import get_action_detail, save_action
from sre_agent.monitor.fix_planner import FixPlan

FINDING_ID = "f-reject-1"
RESOURCES = [{"kind": "Pod", "name": "api-7f9c8d6b5-abcde", "namespace": "shop"}]


def _clean(db):
    db.execute("DELETE FROM fix_outcomes")
    db.execute("DELETE FROM actions WHERE id LIKE ?", ("rj-%",))
    db.execute("DELETE FROM inbox_items WHERE finding_id = ?", (FINDING_ID,))
    db.commit()


@pytest.fixture(autouse=True)
def _db():
    db = get_database()
    from sre_agent.repositories import get_monitor_repo

    get_monitor_repo().ensure_tables()
    _clean(db)
    yield db
    _clean(db)


@pytest.fixture
def broadcasts():
    sent: list[dict] = []
    with patch("sre_agent.monitor.cluster_monitor.broadcast_threadsafe", side_effect=lambda m: sent.append(m) or True):
        yield sent


def _client(user: str | None = "alice") -> TestClient:
    app = FastAPI()
    app.include_router(router)
    if user is not None:
        app.dependency_overrides[require_admin] = lambda: user
    return TestClient(app)


def _propose(
    action_id: str = "rj-1",
    *,
    cause: str | None = "crash_exit",
    status: str = "proposed",
    corr: str = "crashloop:shop:api",
    finding_id: str = FINDING_ID,
) -> None:
    inp: dict = {"category": "crashloop", "resources": RESOURCES, "fixStrategy": "restart_controller"}
    if cause:
        inp["causeCategory"] = cause
    save_action(
        {
            "id": action_id,
            "findingId": finding_id,
            "tool": "",
            "input": inp,
            "status": status,
            "reasoning": "Auto-fix for crashloop",
            "correlationKey": corr,
        },
        category="crashloop",
        resources=RESOURCES,
    )


def _inbox_item() -> str:
    return create_inbox_item(
        {
            "item_type": "finding",
            "title": "Pod api crashlooping",
            "severity": "warning",
            "finding_id": FINDING_ID,
            "created_by": "system:monitor",
            "namespace": "shop",
            "resources": RESOURCES,
        }
    )


def test_reject_answers_the_proposal_and_says_who_and_why(broadcasts):
    _propose()
    resp = _client().post("/fix-history/rj-1/reject", json={"reason": "  restarting  hides the leak "})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert body["rejectedBy"] == "alice"
    assert body["rejectionReason"] == "restarting hides the leak"
    assert body["learned"] is True

    stored = get_action_detail("rj-1")
    assert stored["status"] == "failed"
    assert stored["error"] == "Rejected by alice: restarting hides the leak"
    row = get_database().fetchone("SELECT approved_by FROM actions WHERE id = ?", ("rj-1",))
    assert row["approved_by"] == "alice"

    # Every open UI drops it from its pending list.
    reports = [m for m in broadcasts if m.get("type") == "action_report"]
    assert reports and reports[-1]["id"] == "rj-1" and reports[-1]["status"] == "failed"


def test_reject_is_counted_by_fix_learning(broadcasts):
    _propose()
    _client().post("/fix-history/rj-1/reject", json={"reason": "not this fix"})

    assert track_record("crash_exit") == {"restart_controller": (0, 1)}
    assert rejections("crash_exit") == {"restart_controller": 1}


def test_reject_notes_the_inbox_item_and_leaves_it_open(broadcasts):
    item_id = _inbox_item()
    _propose()
    resp = _client().post("/fix-history/rj-1/reject", json={"reason": "owner is rolling back instead"})

    assert resp.json()["inboxItemId"] == item_id
    item = get_inbox_item(item_id)
    assert item["status"] not in ("resolved", "archived"), "declining a fix does not fix the problem"
    md = item["metadata"] if isinstance(item["metadata"], dict) else json.loads(item["metadata"])
    assert md["fix_rejected"]["action_id"] == "rj-1"
    assert md["fix_rejected"]["by"] == "alice"
    assert md["fix_rejected"]["reason"] == "owner is rolling back instead"

    updates = [m for m in broadcasts if m.get("type") == "inbox_item_updated" and m.get("id") == item_id]
    assert updates and updates[-1]["fix_rejected"]["action_id"] == "rj-1"

    interaction = get_database().fetchone(
        "SELECT * FROM user_interactions WHERE action_id = ? ORDER BY id DESC LIMIT 1", ("rj-1",)
    )
    assert interaction["interaction_type"] == "reject_fix"
    assert interaction["decision"] == "rejected"
    assert interaction["actor"] == "alice"
    assert interaction["item_id"] == item_id


def test_a_reason_is_required(broadcasts):
    _propose()
    for body in ({}, {"reason": ""}, {"reason": "   "}, {"reason": 3}):
        resp = _client().post("/fix-history/rj-1/reject", json=body)
        assert resp.status_code == 400, body
    assert get_action_detail("rj-1")["status"] == "proposed"
    assert track_record("crash_exit") == {}


def test_unknown_action_is_404(broadcasts):
    resp = _client().post("/fix-history/rj-missing/reject", json={"reason": "x"})
    assert resp.status_code == 404


@pytest.mark.parametrize("status", ["completed", "failed", "approved", "expired"])
def test_only_a_pending_proposal_can_be_rejected(broadcasts, status):
    _propose(status=status)
    resp = _client().post("/fix-history/rj-1/reject", json={"reason": "no"})
    assert resp.status_code == 409
    assert track_record("crash_exit") == {}


def test_rejecting_twice_is_a_conflict_not_two_verdicts(broadcasts):
    _propose()
    assert _client().post("/fix-history/rj-1/reject", json={"reason": "no"}).status_code == 200
    second = _client("bob").post("/fix-history/rj-1/reject", json={"reason": "also no"})
    assert second.status_code == 409
    assert rejections("crash_exit") == {"restart_controller": 1}
    assert get_action_detail("rj-1")["error"] == "Rejected by alice: no"


def test_approve_after_reject_is_refused(broadcasts):
    _propose()
    _client().post("/fix-history/rj-1/reject", json={"reason": "no"})
    resp = _client().post("/fix-history/rj-1/approve")
    assert resp.status_code == 409


def test_an_older_proposal_without_a_cause_is_answered_but_not_learned(broadcasts):
    _propose(cause=None)
    resp = _client().post("/fix-history/rj-1/reject", json={"reason": "no"})
    assert resp.status_code == 200
    assert resp.json()["learned"] is False
    assert get_database().fetchall("SELECT * FROM fix_outcomes") == []


def test_reject_needs_an_authenticated_admin():
    _propose()
    # No override: require_admin runs for real and there is no user.
    resp = _client(user=None).post("/fix-history/rj-1/reject", json={"reason": "no"})
    # 401/403 for a missing or non-admin user; 503 when no admin is configured.
    assert resp.status_code in (401, 403, 503)
    assert get_action_detail("rj-1")["status"] == "proposed"


def test_repeated_rejections_escalate_the_strategy_to_a_person(broadcasts):
    for i in range(3):
        _propose(f"rj-{i}", corr=f"crashloop:shop:Deployment/api-{i}")
        assert _client().post(f"/fix-history/rj-{i}/reject", json={"reason": "no"}).status_code == 200

    plan = steer(
        FixPlan(
            strategy="restart_controller",
            cause_category="crash_exit",
            confidence=0.6,
            description="d",
            params={"resources": RESOURCES},
        )
    )
    assert plan.strategy == HUMAN_REVIEW
    assert "3 declined by a person" in plan.params["track_record"]


def test_fix_history_links_each_pending_proposal_to_its_inbox_item(broadcasts):
    """The UI shows the Approve button on the item and opens the item from the proposal."""
    item_id = _inbox_item()
    _propose()
    _propose("rj-done", status="completed")
    from sre_agent.api.auth import verify_token

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[verify_token] = lambda: None
    resp = TestClient(app).get("/fix-history", params={"status": "proposed"})
    assert resp.status_code == 200
    actions = {a["id"]: a for a in resp.json()["actions"]}
    assert actions["rj-1"]["inboxItemId"] == item_id
    assert "rj-done" not in actions


def test_episode_symptoms_carry_their_inbox_item_id():
    """Symptoms are folded out of the inbox list; the card needs the id to open one."""
    import asyncio
    from unittest.mock import MagicMock

    from sre_agent.api import episode_rest

    episodes = MagicMock()
    episodes.get.return_value = {"id": "ep-1"}
    episodes.symptoms.return_value = [
        {"correlation_key": "crashloop:shop:Pod/api", "title": "api crashlooping"},
        {"correlation_key": "oom:shop:Pod/gone", "title": "gone"},
    ]
    inbox = MagicMock()
    inbox.find_active_by_correlation_task.side_effect = lambda key: {"id": "inb-api"} if "api" in key else None
    with (
        patch.object(episode_rest, "get_episode_repo", return_value=episodes),
        patch("sre_agent.repositories.inbox_repo.get_inbox_repo", return_value=inbox),
        patch.object(episode_rest, "changes_around", return_value=[]),
        patch.object(episode_rest, "recurrence_summary", return_value=None),
        patch.object(episode_rest, "investigation_for", return_value=None),
    ):
        result = asyncio.run(episode_rest.rest_get_episode("ep-1"))
    assert [s["item_id"] for s in result["symptoms"]] == ["inb-api", None]


def test_rejecting_one_workload_again_and_again_counts_once(broadcasts):
    """Re-rejecting one noisy workload is one opinion, not cluster-wide evidence."""
    for i in range(4):
        _propose(f"rj-{i}")
        resp = _client().post(f"/fix-history/rj-{i}/reject", json={"reason": "no"})
        assert resp.status_code == 200
        assert resp.json()["learned"] is (i == 0)
    assert rejections("crash_exit") == {"restart_controller": 1}
    plan = steer(
        FixPlan(strategy="restart_controller", cause_category="crash_exit", confidence=0.6, description="d", params={})
    )
    assert plan.strategy == "restart_controller", "one workload's rejections must not escalate the cause"


def test_a_rejection_is_found_for_its_condition_and_strategy_only(broadcasts):
    import time

    from sre_agent.repositories import get_monitor_repo

    _propose()
    _client().post("/fix-history/rj-1/reject", json={"reason": "no"})
    repo = get_monitor_repo()
    since = int(time.time() * 1000) - 3600_000
    assert repo.find_recent_rejection("crashloop:shop:api", "restart_controller", since) == "rj-1"
    assert repo.find_recent_rejection("crashloop:shop:api", "patch_resources", since) is None
    assert repo.find_recent_rejection("crashloop:shop:other", "restart_controller", since) is None
    assert repo.find_recent_rejection("crashloop:shop:api", "restart_controller", since + 2 * 3600_000) is None


def test_rejected_proposals_are_not_in_the_fix_rate(broadcasts):
    """Nothing ran, so a rejection is not a fix that failed."""
    from sre_agent.repositories import get_monitor_repo

    repo = get_monitor_repo()
    before = repo.fetch_fix_rate(1)
    _propose()
    _client().post("/fix-history/rj-1/reject", json={"reason": "no"})
    _propose("rj-ran", status="failed")
    after = repo.fetch_fix_rate(1)
    assert after["total"] - before["total"] == 1, "only the fix that ran and failed counts"


def test_the_inbox_item_is_found_by_condition_when_the_finding_id_changed(broadcasts):
    """Finding ids are minted per sighting; after a restart the proposal's id names nothing."""
    item_id = create_inbox_item(
        {
            "item_type": "task",
            "title": "Pod api crashlooping",
            "severity": "warning",
            "finding_id": "f-new-sighting",
            "created_by": "system:monitor",
            "correlation_key": "crashloop:shop:api",
            "namespace": "shop",
            "resources": RESOURCES,
        }
    )
    try:
        _propose(finding_id="f-old-sighting")
        resp = _client().post("/fix-history/rj-1/reject", json={"reason": "no"})
        assert resp.json()["inboxItemId"] == item_id
        md = get_inbox_item(item_id)["metadata"]
        md = md if isinstance(md, dict) else json.loads(md)
        assert md["fix_rejected"]["action_id"] == "rj-1"
    finally:
        get_database().execute("DELETE FROM inbox_items WHERE id = ?", (item_id,))
        get_database().commit()


def test_a_superseded_proposal_is_withdrawn_not_left_to_be_rejected(broadcasts):
    """Approve refuses when the plan changed; rejecting it would judge a fix nobody judged."""
    from unittest.mock import MagicMock

    from sre_agent.monitor.approvals import ApprovalError, approve_fix

    _propose()  # proposed with restart_controller
    new_plan = MagicMock(strategy="patch_resources", cause_category="crash_exit", description="d", params={})
    with (
        patch("sre_agent.monitor.approvals._current_finding", return_value={"id": FINDING_ID, "category": "crashloop"}),
        patch("sre_agent.monitor.autofix.remediation_blocked_reason", return_value=""),
        patch("sre_agent.monitor.fix_planner.get_investigation_for_finding", return_value=None),
        patch("sre_agent.monitor.fix_planner.default_fix_plan", return_value=new_plan),
    ):
        with pytest.raises(ApprovalError) as e:
            approve_fix("rj-1", "alice")
    assert e.value.status_code == 409
    assert "withdrawn" in e.value.reason
    assert get_action_detail("rj-1")["status"] == "expired"
    assert track_record("crash_exit") == {}
    assert _client().post("/fix-history/rj-1/reject", json={"reason": "no"}).status_code == 409
