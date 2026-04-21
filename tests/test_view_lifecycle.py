"""Tests for view lifecycle — status transitions and claim mechanism."""

from __future__ import annotations

import pytest

from sre_agent import db as db_module
from sre_agent.db import Database, reset_database, set_database
from sre_agent.db_schema import ALL_SCHEMAS


@pytest.fixture(autouse=True)
def _view_db():
    from tests.conftest import _TEST_DB_URL

    test_db = Database(_TEST_DB_URL)
    test_db.execute("DROP TABLE IF EXISTS view_versions CASCADE")
    test_db.execute("DROP TABLE IF EXISTS views CASCADE")
    test_db.commit()
    test_db.executescript(ALL_SCHEMAS)
    set_database(test_db)
    yield test_db
    reset_database()


def _layout():
    return [{"kind": "data_table", "title": "Pods", "columns": [], "rows": []}]


def _create_incident():
    db_module.save_view(
        "alice",
        "cv-inc-1",
        "CrashLoop",
        "desc",
        _layout(),
        view_type="incident",
        status="investigating",
        visibility="team",
        trigger_source="monitor",
        finding_id="f-crash-1",
    )
    return "cv-inc-1"


class TestStatusTransition:
    def test_valid_incident_transition(self):
        view_id = _create_incident()
        ok = db_module.transition_view_status(view_id, "alice", "action_taken")
        assert ok is True
        view = db_module.get_view(view_id)
        assert view["status"] == "action_taken"

    def test_invalid_transition_rejected(self):
        view_id = _create_incident()
        ok = db_module.transition_view_status(view_id, "alice", "completed")
        assert ok is False
        view = db_module.get_view(view_id)
        assert view["status"] == "investigating"

    def test_custom_views_cannot_transition(self):
        db_module.save_view("alice", "cv-custom", "Custom", "", _layout())
        ok = db_module.transition_view_status("cv-custom", "alice", "resolved")
        assert ok is False

    def test_transition_creates_version(self):
        view_id = _create_incident()
        db_module.transition_view_status(view_id, "alice", "action_taken")
        versions = db_module.list_view_versions(view_id)
        actions = [v["action"] for v in versions]
        assert any("action_taken" in a for a in actions)

    def test_full_incident_lifecycle(self):
        view_id = _create_incident()
        assert db_module.transition_view_status(view_id, "alice", "action_taken")
        assert db_module.transition_view_status(view_id, "alice", "verifying")
        assert db_module.transition_view_status(view_id, "alice", "resolved")
        assert db_module.transition_view_status(view_id, "alice", "archived")
        view = db_module.get_view(view_id)
        assert view["status"] == "archived"

    def test_plan_lifecycle(self):
        db_module.save_view(
            "alice",
            "cv-plan",
            "Plan",
            "",
            _layout(),
            view_type="plan",
            status="analyzing",
            visibility="team",
        )
        assert db_module.transition_view_status("cv-plan", "alice", "ready")
        assert db_module.transition_view_status("cv-plan", "alice", "executing")
        assert db_module.transition_view_status("cv-plan", "alice", "completed")
        view = db_module.get_view("cv-plan")
        assert view["status"] == "completed"

    def test_assessment_lifecycle(self):
        db_module.save_view(
            "alice",
            "cv-assess",
            "Assessment",
            "",
            _layout(),
            view_type="assessment",
            status="analyzing",
            visibility="team",
        )
        assert db_module.transition_view_status("cv-assess", "alice", "ready")
        assert db_module.transition_view_status("cv-assess", "alice", "acknowledged")
        view = db_module.get_view("cv-assess")
        assert view["status"] == "acknowledged"

    def test_nonexistent_view(self):
        ok = db_module.transition_view_status("nonexistent", "alice", "action_taken")
        assert ok is False


class TestClaimView:
    def test_claim_view(self):
        view_id = _create_incident()
        ok = db_module.claim_view(view_id, "bob")
        assert ok is True
        view = db_module.get_view(view_id)
        assert view["claimed_by"] == "bob"
        assert view["claimed_at"] is not None

    def test_unclaim_view(self):
        view_id = _create_incident()
        db_module.claim_view(view_id, "bob")
        ok = db_module.unclaim_view(view_id, "bob")
        assert ok is True
        view = db_module.get_view(view_id)
        assert view["claimed_by"] is None

    def test_claim_private_view_denied(self):
        db_module.save_view("alice", "cv-priv", "Private", "", _layout())
        ok = db_module.claim_view("cv-priv", "bob")
        assert ok is False

    def test_unclaim_wrong_user_denied(self):
        view_id = _create_incident()
        db_module.claim_view(view_id, "bob")
        ok = db_module.unclaim_view(view_id, "charlie")
        assert ok is False
        view = db_module.get_view(view_id)
        assert view["claimed_by"] == "bob"


class TestFindByFinding:
    def test_find_view_by_finding_id(self):
        _create_incident()
        view = db_module.get_view_by_finding("f-crash-1")
        assert view is not None
        assert view["id"] == "cv-inc-1"

    def test_find_returns_none_for_unknown(self):
        view = db_module.get_view_by_finding("nonexistent")
        assert view is None


class TestClaimExpiry:
    def test_expire_stale_claims(self):
        view_id = _create_incident()
        db_module.claim_view(view_id, "bob")
        # Manually backdate the claim
        from sre_agent.db import get_database

        db = get_database()
        db.execute("UPDATE views SET claimed_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (view_id,))
        db.commit()
        count = db_module.expire_stale_claims()
        assert count == 1
        view = db_module.get_view(view_id)
        assert view["claimed_by"] is None

    def test_fresh_claims_not_expired(self):
        view_id = _create_incident()
        db_module.claim_view(view_id, "bob")
        count = db_module.expire_stale_claims()
        assert count == 0
        view = db_module.get_view(view_id)
        assert view["claimed_by"] == "bob"


class TestFindSimilarViews:
    def test_finds_similar_by_title(self):
        db_module.save_view(
            "alice",
            "cv-old",
            "CrashLoop in payment-api",
            "",
            _layout(),
            view_type="incident",
            status="resolved",
            visibility="team",
        )
        results = db_module.find_similar_views("CrashLoop in payment-api pod")
        assert len(results) >= 1
        assert results[0]["id"] == "cv-old"

    def test_no_match_for_unrelated(self):
        db_module.save_view(
            "alice",
            "cv-old",
            "CrashLoop in payment-api",
            "",
            _layout(),
            view_type="incident",
            status="resolved",
            visibility="team",
        )
        results = db_module.find_similar_views("TLS certificate expiry")
        assert len(results) == 0

    def test_only_returns_terminal_status(self):
        db_module.save_view(
            "alice",
            "cv-active",
            "CrashLoop in payment-api",
            "",
            _layout(),
            view_type="incident",
            status="investigating",
            visibility="team",
        )
        results = db_module.find_similar_views("CrashLoop in payment-api")
        assert len(results) == 0


class TestRecurrenceHandling:
    def test_reopen_resolved_view(self):
        view_id = _create_incident()
        db_module.transition_view_status(view_id, "alice", "action_taken")
        db_module.transition_view_status(view_id, "alice", "verifying")
        db_module.transition_view_status(view_id, "alice", "resolved")

        reopened_id = db_module.reopen_view_for_finding("f-crash-1")
        assert reopened_id == view_id
        view = db_module.get_view(view_id)
        assert view["status"] == "investigating"

    def test_reopen_archived_view(self):
        view_id = _create_incident()
        db_module.transition_view_status(view_id, "alice", "action_taken")
        db_module.transition_view_status(view_id, "alice", "verifying")
        db_module.transition_view_status(view_id, "alice", "resolved")
        db_module.transition_view_status(view_id, "alice", "archived")

        reopened_id = db_module.reopen_view_for_finding("f-crash-1")
        assert reopened_id == view_id
        view = db_module.get_view(view_id)
        assert view["status"] == "investigating"

    def test_no_reopen_if_still_active(self):
        _create_incident()
        reopened_id = db_module.reopen_view_for_finding("f-crash-1")
        assert reopened_id is None

    def test_no_reopen_for_unknown_finding(self):
        reopened_id = db_module.reopen_view_for_finding("nonexistent")
        assert reopened_id is None


class TestAssessmentEscalation:
    def test_escalate_assessment(self):
        db_module.save_view(
            "alice",
            "cv-assess",
            "Memory Forecast",
            "",
            _layout(),
            view_type="assessment",
            status="ready",
            visibility="team",
        )
        ok = db_module.escalate_assessment_to_incident("cv-assess")
        assert ok is True
        view = db_module.get_view("cv-assess")
        assert view["view_type"] == "incident"
        assert view["status"] == "investigating"

    def test_cannot_escalate_non_assessment(self):
        _create_incident()
        ok = db_module.escalate_assessment_to_incident("cv-inc-1")
        assert ok is False

    def test_cannot_escalate_nonexistent(self):
        ok = db_module.escalate_assessment_to_incident("nonexistent")
        assert ok is False


class TestExtractResolutionTools:
    def test_extracts_action_buttons(self):
        layout = [
            {"kind": "metric_card", "title": "CPU", "value": "72%"},
            {
                "kind": "action_button",
                "label": "Restart",
                "action": "restart_deployment",
                "action_input": {"name": "nginx", "namespace": "default"},
            },
            {
                "kind": "action_button",
                "label": "Scale Up",
                "action": "scale_deployment",
                "action_input": {"name": "nginx", "namespace": "default", "replicas": 4},
            },
        ]
        db_module.save_view("alice", "cv-tools", "Test", "", layout, view_type="incident", status="resolved")
        tools = db_module.extract_resolution_tools("cv-tools")
        assert len(tools) == 2
        assert tools[0]["action"] == "restart_deployment"
        assert tools[1]["action"] == "scale_deployment"
        assert tools[1]["action_input"]["replicas"] == 4

    def test_extracts_from_nested_grid(self):
        layout = [
            {
                "kind": "grid",
                "items": [
                    {"kind": "action_button", "label": "Fix", "action": "apply_yaml", "action_input": {}},
                ],
            },
        ]
        db_module.save_view("alice", "cv-nested", "Test", "", layout)
        tools = db_module.extract_resolution_tools("cv-nested")
        assert len(tools) == 1
        assert tools[0]["action"] == "apply_yaml"

    def test_empty_for_no_action_buttons(self):
        db_module.save_view("alice", "cv-empty", "Test", "", _layout())
        tools = db_module.extract_resolution_tools("cv-empty")
        assert tools == []

    def test_empty_for_nonexistent_view(self):
        tools = db_module.extract_resolution_tools("nonexistent")
        assert tools == []
