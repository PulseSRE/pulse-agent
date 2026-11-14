"""Tests for GET /agent/activity endpoint."""

from __future__ import annotations

from unittest.mock import MagicMock


class TestBuildActivityEvents:
    def test_returns_events_list(self):
        from sre_agent.api.monitor_rest import _build_activity_events

        mock_repo = MagicMock()
        mock_repo.fetch_activity_actions.return_value = [
            {"category": "crashloop", "namespace": "production", "cnt": 2, "status": "completed"},
            {"category": "workloads", "namespace": "default", "cnt": 1, "status": "completed"},
        ]
        mock_repo.fetch_self_healed_count.return_value = [{"cnt": 3}]
        mock_repo.fetch_postmortem_activity.return_value = [{"cnt": 1, "latest_summary": "OOM incident in production"}]
        mock_repo.fetch_investigated_findings.return_value = [
            {"finding_type": "node_pressure", "target": "worker-3", "cnt": 1}
        ]

        events = _build_activity_events(mock_repo, days=7)
        assert len(events) >= 2
        assert events[0]["type"] == "auto_fix"
        assert events[0]["count"] == 2
        assert events[0]["namespace"] == "production"
        assert events[0]["link"] == "/inbox?tab=activity"

    def test_empty_when_no_data(self):
        from sre_agent.api.monitor_rest import _build_activity_events

        mock_repo = MagicMock()
        mock_repo.fetch_activity_actions.return_value = []
        mock_repo.fetch_self_healed_count.return_value = [{"cnt": 0}]
        mock_repo.fetch_postmortem_activity.return_value = []
        mock_repo.fetch_investigated_findings.return_value = []

        events = _build_activity_events(mock_repo, days=7)
        assert len(events) == 0

    def test_self_healed_included(self):
        from sre_agent.api.monitor_rest import _build_activity_events

        mock_repo = MagicMock()
        mock_repo.fetch_activity_actions.return_value = []
        mock_repo.fetch_self_healed_count.return_value = [{"cnt": 5}]
        mock_repo.fetch_postmortem_activity.return_value = []
        mock_repo.fetch_investigated_findings.return_value = []

        events = _build_activity_events(mock_repo, days=7)
        assert len(events) == 1
        assert events[0]["type"] == "self_healed"
        assert events[0]["count"] == 5

    def test_postmortem_with_summary(self):
        from sre_agent.api.monitor_rest import _build_activity_events

        mock_repo = MagicMock()
        mock_repo.fetch_activity_actions.return_value = []
        mock_repo.fetch_self_healed_count.return_value = [{"cnt": 0}]
        mock_repo.fetch_postmortem_activity.return_value = [
            {"cnt": 2, "latest_summary": "Memory leak in payment service"}
        ]
        mock_repo.fetch_investigated_findings.return_value = []

        events = _build_activity_events(mock_repo, days=7)
        assert len(events) == 1
        assert events[0]["type"] == "postmortem"
        assert "payment service" in events[0]["description"]

    def test_investigation_events(self):
        from sre_agent.api.monitor_rest import _build_activity_events

        mock_repo = MagicMock()
        mock_repo.fetch_activity_actions.return_value = []
        mock_repo.fetch_self_healed_count.return_value = [{"cnt": 0}]
        mock_repo.fetch_postmortem_activity.return_value = []
        mock_repo.fetch_investigated_findings.return_value = [
            {"finding_type": "node_pressure", "target": "worker-3", "cnt": 1}
        ]

        events = _build_activity_events(mock_repo, days=7)
        assert len(events) == 1
        assert events[0]["type"] == "investigation"


# ── Against the real schema ───────────────────────────────────────────────
#
# Every test above mocks the repository, so the SQL was never run: four of
# its queries named columns that do not exist (actions.namespace;
# findings.investigated/finding_type/target; postmortems.summary/created_at),
# and on a live cluster with 1,315 fix actions the endpoint returned
# {"events": []} while the same page said "Pulse took 439 fix actions".

import json
import time

import pytest

from tests.conftest import truncate_tables


@pytest.fixture
def db():
    from sre_agent.db import get_database

    database = get_database()
    truncate_tables(database, "actions", "investigations", "postmortems", "inbox_items")
    yield database
    truncate_tables(database, "actions", "investigations", "postmortems", "inbox_items")


def _action(db, i, status, category, key, verification=None, tool="delete_pod", resources=None, age_s=60):
    db.execute(
        "INSERT INTO actions (id, finding_id, timestamp, category, tool, status, verification_status, "
        "correlation_key, resources) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            f"a-{i}",
            f"f-{i}",
            int((time.time() - age_s) * 1000),
            category,
            tool,
            status,
            verification,
            key,
            json.dumps(resources or []),
        ),
    )
    db.commit()


def _events():
    from sre_agent.api.monitor_rest import _get_agent_activity_sync

    return _get_agent_activity_sync(7)["events"]


def test_fix_actions_appear_in_the_feed(db):
    key = "crashloop:finance:Pod/billing-service-helm-guestbook"
    _action(db, 1, "completed", "crashloop", key, verification="still_failing")
    _action(db, 2, "completed", "crashloop", key, verification="verified")
    _action(db, 3, "failed", "workloads", "workloads:eval-staging:Deployment/ui", verification="unverifiable")
    # An older row with no correlation key: the namespace comes from resources.
    _action(db, 4, "completed", "image_pull", None, resources=[{"kind": "Pod", "name": "x", "namespace": "shop"}])

    events = _events()
    applied = {(e["category"], e["namespace"]): e for e in events if e["type"] == "auto_fix"}
    assert applied[("crashloop", "finance")]["count"] == 2
    assert applied[("crashloop", "finance")]["description"] == "Applied 2 crashloop fixes in finance"
    assert ("image_pull", "shop") in applied
    failed = [e for e in events if e["type"] == "fix_failed"]
    assert failed and failed[0]["namespace"] == "eval-staging"
    assert not any("Auto-fixed" in e["description"] for e in events), "applied is not fixed"


def test_verification_outcomes_appear_in_the_feed(db):
    key = "crashloop:finance:Pod/api"
    _action(db, 1, "completed", "crashloop", key, verification="verified")
    _action(db, 2, "completed", "crashloop", key, verification="still_failing")
    _action(db, 3, "completed", "crashloop", key, verification="verified_then_recurred")
    _action(db, 4, "failed", "crashloop", key, verification="unverifiable")

    by_type: dict[str, int] = {}
    for e in _events():
        by_type[e["type"]] = by_type.get(e["type"], 0) + e["count"]
    assert by_type["fix_verified"] == 1
    assert by_type["fix_not_held"] == 2
    assert by_type["fix_unverifiable"] == 1


def test_proposals_human_review_and_old_actions_are_not_fix_activity(db):
    _action(db, 1, "proposed", "crashloop", "crashloop:a:Pod/x")
    _action(db, 2, "proposed", "crashloop", "crashloop:a:Pod/y", tool="require_human_review")
    _action(db, 3, "completed", "crashloop", "crashloop:a:Pod/z", age_s=30 * 86400)
    assert [e for e in _events() if e["type"] in ("auto_fix", "fix_failed", "rollback")] == []


def test_investigations_postmortems_and_self_healed_are_read_from_their_tables(db):
    now_ms = int(time.time() * 1000)
    db.execute(
        "INSERT INTO investigations (id, finding_id, timestamp, category, status) VALUES (?, ?, ?, ?, ?)",
        ("i-1", "f-1", now_ms, "crashloop", "completed"),
    )
    db.execute(
        "INSERT INTO postmortems (id, incident_type, plan_id, root_cause, generated_at) VALUES (?, ?, ?, ?, ?)",
        ("pm-1", "crashloop", "plan-1", "Bad config in billing", now_ms),
    )
    now_s = int(time.time())
    db.execute(
        "INSERT INTO inbox_items (id, item_type, status, title, summary, severity, priority_score, confidence, "
        "noise_score, correlation_key, created_by, created_at, updated_at, resolved_at, resources, metadata) "
        "VALUES (?, 'task', 'resolved', 't', '', 'warning', 1, 0.9, 0, ?, 'system:monitor', ?, ?, ?, '[]', '{}')",
        ("inb-1", "crashloop:ns:Pod/healed", now_s, now_s, now_s),
    )
    db.commit()

    types = {e["type"]: e for e in _events()}
    assert types["investigation"]["description"] == "Investigated 1 crashloop finding"
    assert "Bad config in billing" in types["postmortem"]["description"]
    assert types["self_healed"]["count"] == 1


def test_one_broken_source_does_not_empty_the_feed():
    from sre_agent.api.monitor_rest import _build_activity_events

    repo = MagicMock()
    repo.fetch_activity_actions.return_value = [
        {"category": "crashloop", "namespace": "prod", "cnt": 3, "status": "completed"}
    ]
    repo.fetch_activity_verifications.side_effect = RuntimeError("column does not exist")
    repo.fetch_self_healed_count.side_effect = RuntimeError("relation does not exist")
    repo.fetch_postmortem_activity.return_value = []
    repo.fetch_investigated_findings.return_value = []
    events = _build_activity_events(repo, days=7)
    assert [e["type"] for e in events] == ["auto_fix"]
