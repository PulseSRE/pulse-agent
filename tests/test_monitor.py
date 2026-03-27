"""Tests for the monitor module — fix history, findings, and scan functions."""

import json
import os
import tempfile
import pytest

from sre_agent.monitor import (
    _make_finding,
    _make_prediction,
    _make_action_report,
    save_action,
    get_fix_history,
    get_action_detail,
    _FIX_SCHEMA,
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
    SEVERITY_INFO,
)


@pytest.fixture(autouse=True)
def _use_temp_db(monkeypatch, tmp_path):
    """Use a temp database for each test."""
    db_path = str(tmp_path / "test_fix_history.db")
    monkeypatch.setattr("sre_agent.monitor._FIX_DB_PATH", db_path)
    yield


class TestMakeHelpers:
    def test_make_finding(self):
        f = _make_finding(
            severity="critical",
            category="crashloop",
            title="Pod crashing",
            summary="It crashed",
            resources=[{"kind": "Pod", "name": "test", "namespace": "default"}],
            auto_fixable=True,
        )
        assert f["type"] == "finding"
        assert f["id"].startswith("f-")
        assert f["severity"] == "critical"
        assert f["category"] == "crashloop"
        assert f["autoFixable"] is True
        assert len(f["resources"]) == 1
        assert isinstance(f["timestamp"], int)

    def test_make_prediction(self):
        p = _make_prediction(
            category="disk",
            title="Disk filling",
            detail="Node will run out of disk",
            eta="2026-04-01T00:00:00Z",
            confidence=0.85,
            resources=[{"kind": "Node", "name": "node-1"}],
            recommended_action="Add storage",
        )
        assert p["type"] == "prediction"
        assert p["id"].startswith("p-")
        assert p["confidence"] == 0.85
        assert p["recommendedAction"] == "Add storage"

    def test_make_action_report(self):
        a = _make_action_report(
            finding_id="f-abc",
            tool="restart_deployment",
            inp={"name": "web", "namespace": "prod"},
            status="completed",
            reasoning="Pod was crashlooping",
            duration_ms=1500,
        )
        assert a["type"] == "action_report"
        assert a["id"].startswith("a-")
        assert a["findingId"] == "f-abc"
        assert a["status"] == "completed"
        assert a["durationMs"] == 1500


class TestFixHistory:
    def test_save_and_retrieve(self):
        action = _make_action_report(
            finding_id="f-123",
            tool="restart_deployment",
            inp={"name": "web"},
            status="completed",
            reasoning="Fixed crashloop",
        )
        save_action(action, category="crashloop", resources=[{"kind": "Deployment", "name": "web"}])

        result = get_fix_history()
        assert result["total"] == 1
        assert len(result["actions"]) == 1
        assert result["actions"][0]["tool"] == "restart_deployment"
        assert result["actions"][0]["category"] == "crashloop"

    def test_pagination(self):
        for i in range(25):
            action = _make_action_report(
                finding_id=f"f-{i}",
                tool="restart_deployment",
                inp={"i": i},
                status="completed",
            )
            save_action(action)

        page1 = get_fix_history(page=1, page_size=10)
        assert page1["total"] == 25
        assert len(page1["actions"]) == 10
        assert page1["page"] == 1

        page3 = get_fix_history(page=3, page_size=10)
        assert len(page3["actions"]) == 5

    def test_filter_by_status(self):
        for status in ["completed", "failed", "completed"]:
            action = _make_action_report("f-1", "tool", {}, status)
            save_action(action)

        result = get_fix_history(filters={"status": "failed"})
        assert result["total"] == 1
        assert result["actions"][0]["status"] == "failed"

    def test_filter_by_category(self):
        a1 = _make_action_report("f-1", "tool", {}, "completed")
        save_action(a1, category="crashloop")
        a2 = _make_action_report("f-2", "tool", {}, "completed")
        save_action(a2, category="scaling")

        result = get_fix_history(filters={"category": "crashloop"})
        assert result["total"] == 1

    def test_search_filter(self):
        a1 = _make_action_report("f-1", "restart_deployment", {}, "completed", reasoning="Fixed OOM")
        save_action(a1)
        a2 = _make_action_report("f-2", "scale_deployment", {}, "completed", reasoning="Scaled up")
        save_action(a2)

        result = get_fix_history(filters={"search": "OOM"})
        assert result["total"] == 1
        assert result["actions"][0]["reasoning"] == "Fixed OOM"

    def test_get_action_detail(self):
        action = _make_action_report(
            finding_id="f-1",
            tool="drain_node",
            inp={"node": "node-1"},
            status="completed",
            before_state="Ready",
            after_state="SchedulingDisabled",
            reasoning="Node had disk pressure",
            duration_ms=5000,
        )
        save_action(action, category="nodes", resources=[{"kind": "Node", "name": "node-1"}])

        detail = get_action_detail(action["id"])
        assert detail is not None
        assert detail["tool"] == "drain_node"
        assert detail["beforeState"] == "Ready"
        assert detail["afterState"] == "SchedulingDisabled"
        assert detail["resources"] == [{"kind": "Node", "name": "node-1"}]

    def test_get_action_detail_not_found(self):
        assert get_action_detail("nonexistent") is None

    def test_empty_history(self):
        result = get_fix_history()
        assert result["total"] == 0
        assert result["actions"] == []


class TestFindingSeverity:
    def test_severity_constants(self):
        assert SEVERITY_CRITICAL == "critical"
        assert SEVERITY_WARNING == "warning"
        assert SEVERITY_INFO == "info"

    def test_finding_ids_unique(self):
        f1 = _make_finding("info", "test", "t1", "s1", [])
        f2 = _make_finding("info", "test", "t2", "s2", [])
        assert f1["id"] != f2["id"]
