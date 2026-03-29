"""Tests for the monitor module — fix history, findings, and scan functions."""

import asyncio
import json
from unittest.mock import patch, MagicMock
import pytest

from sre_agent.db import Database, set_database, reset_database
from sre_agent.monitor import (
    _make_finding,
    _make_prediction,
    _make_action_report,
    save_action,
    get_fix_history,
    get_action_detail,
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
    SEVERITY_INFO,
    save_investigation,
    update_action_verification,
    MonitorSession,
    _run_security_followup_sync,
)


@pytest.fixture(autouse=True)
def _use_temp_db(monkeypatch, tmp_path):
    """Use a temp database for each test."""
    import sre_agent.monitor as _mon
    import sre_agent.context_bus as _cb
    db_path = str(tmp_path / "test_fix_history.db")
    db = Database(f"sqlite:///{db_path}")
    set_database(db)
    # Reset table-creation flags so each test creates tables fresh
    _mon._tables_ensured = False
    _cb._tables_ensured = False
    yield
    reset_database()
    _mon._tables_ensured = False
    _cb._tables_ensured = False


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

    def test_action_verification_fields_persist(self):
        action = _make_action_report("f-1", "restart_deployment", {}, "completed")
        save_action(action, category="workloads")
        update_action_verification(action["id"], "verified", "No active workload finding on next scan")
        detail = get_action_detail(action["id"])
        assert detail is not None
        assert detail["verificationStatus"] == "verified"
        assert "next scan" in (detail["verificationEvidence"] or "")
        assert isinstance(detail["verificationTimestamp"], int)

    def test_save_investigation_persists_row(self, tmp_path):
        finding = _make_finding(
            severity="critical",
            category="crashloop",
            title="pod crashlooping",
            summary="restarts detected",
            resources=[{"kind": "Pod", "name": "api-1", "namespace": "prod"}],
        )
        report = {
            "id": "i-test-1",
            "findingId": finding["id"],
            "timestamp": 1234567890,
            "status": "completed",
            "summary": "Root cause identified",
            "suspectedCause": "ConfigMap removed",
            "recommendedFix": "Restore ConfigMap",
            "confidence": 0.9,
        }
        save_investigation(report, finding)
        from sre_agent.db import get_database
        db = get_database()
        row = db.fetchone("SELECT finding_id, status, summary FROM investigations WHERE id = ?", ("i-test-1",))
        assert row is not None
        assert row["finding_id"] == finding["id"]
        assert row["status"] == "completed"
        assert row["summary"] == "Root cause identified"


class TestMonitorSessionApprovals:
    def test_resolve_action_response_sets_future(self):
        class DummySocket:
            async def send_json(self, _data):
                return None

        session = MonitorSession(DummySocket(), trust_level=2, auto_fix_categories=[])
        loop = asyncio.new_event_loop()
        fut = loop.create_future()
        session._pending_action_approvals["a-1"] = fut
        try:
            assert session.resolve_action_response("a-1", True) is True
            assert fut.result() is True
        finally:
            loop.close()


class TestFindingSeverity:
    def test_severity_constants(self):
        assert SEVERITY_CRITICAL == "critical"
        assert SEVERITY_WARNING == "warning"
        assert SEVERITY_INFO == "info"

    def test_finding_ids_unique(self):
        f1 = _make_finding("info", "test", "t1", "s1", [])
        f2 = _make_finding("info", "test", "t2", "s2", [])
        assert f1["id"] != f2["id"]


class TestSecurityFollowup:
    def test_run_security_followup_sync_returns_parsed(self):
        """_run_security_followup_sync calls the security agent and parses JSON."""
        finding = _make_finding(
            severity="critical",
            category="crashloop",
            title="Pod crashing",
            summary="restarts",
            resources=[{"kind": "Pod", "name": "web-1", "namespace": "prod"}],
        )
        with patch("sre_agent.agent.create_client", return_value=MagicMock()), \
             patch("sre_agent.agent.run_agent_streaming",
                   return_value='{"security_issues": [{"issue": "no netpol"}], "risk_level": "high"}') as mock_run:
            result = _run_security_followup_sync(finding)

        assert result["risk_level"] == "high"
        assert len(result["security_issues"]) == 1
        assert result["security_issues"][0]["issue"] == "no netpol"
        assert "raw_response" in result
        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args
        # Verify read-only mode
        assert call_kwargs.kwargs.get("write_tools") == set() or call_kwargs[1].get("write_tools") == set()

    def test_run_security_followup_sync_handles_bad_json(self):
        """_run_security_followup_sync returns empty defaults on unparseable response."""
        finding = _make_finding(
            severity="critical", category="crashloop",
            title="Pod crashing", summary="s",
            resources=[{"kind": "Pod", "name": "x", "namespace": "ns"}],
        )
        with patch("sre_agent.agent.create_client", return_value=MagicMock()), \
             patch("sre_agent.agent.run_agent_streaming", return_value="not json at all"):
            result = _run_security_followup_sync(finding)
        assert result["security_issues"] == []
        assert result["risk_level"] == "unknown"

    def test_security_followup_called_in_investigations(self, monkeypatch):
        """When PULSE_AGENT_SECURITY_FOLLOWUP=1, security followup runs after investigation."""
        monkeypatch.setenv("PULSE_AGENT_SECURITY_FOLLOWUP", "1")
        monkeypatch.setenv("PULSE_AGENT_INVESTIGATIONS_MAX_PER_SCAN", "1")
        monkeypatch.setenv("PULSE_AGENT_INVESTIGATION_TIMEOUT", "10")

        sent_messages = []

        class FakeSocket:
            async def send_json(self, data):
                sent_messages.append(data)

        session = MonitorSession(FakeSocket(), trust_level=1)

        finding = _make_finding(
            severity="critical", category="crashloop",
            title="Pod crashing", summary="restarts",
            resources=[{"kind": "Pod", "name": "web-1", "namespace": "prod"}],
        )

        mock_inv_result = {
            "summary": "OOM cause", "suspectedCause": "mem limit",
            "recommendedFix": "increase mem", "confidence": 0.8,
        }
        mock_sec_result = {
            "security_issues": [{"issue": "no netpol"}],
            "risk_level": "medium",
            "raw_response": "test",
        }

        with patch("sre_agent.monitor._run_proactive_investigation_sync", return_value=mock_inv_result), \
             patch("sre_agent.monitor._run_security_followup_sync", return_value=mock_sec_result) as mock_sec, \
             patch("sre_agent.agent._circuit_breaker") as mock_cb:
            mock_cb.is_open = False
            asyncio.get_event_loop().run_until_complete(
                session.run_investigations([finding])
            )

        mock_sec.assert_called_once_with(finding)
        # The investigation_report should have securityFollowup field
        reports = [m for m in sent_messages if m.get("type") == "investigation_report"]
        assert len(reports) == 1
        assert "securityFollowup" in reports[0]
        assert reports[0]["securityFollowup"]["riskLevel"] == "medium"
        assert reports[0]["securityFollowup"]["issues"] == [{"issue": "no netpol"}]

    def test_security_followup_not_called_when_disabled(self, monkeypatch):
        """When PULSE_AGENT_SECURITY_FOLLOWUP is not set, no security followup runs."""
        monkeypatch.delenv("PULSE_AGENT_SECURITY_FOLLOWUP", raising=False)
        monkeypatch.setenv("PULSE_AGENT_INVESTIGATIONS_MAX_PER_SCAN", "1")
        monkeypatch.setenv("PULSE_AGENT_INVESTIGATION_TIMEOUT", "10")

        sent_messages = []

        class FakeSocket:
            async def send_json(self, data):
                sent_messages.append(data)

        session = MonitorSession(FakeSocket(), trust_level=1)

        finding = _make_finding(
            severity="critical", category="crashloop",
            title="Pod crashing", summary="restarts",
            resources=[{"kind": "Pod", "name": "web-1", "namespace": "prod"}],
        )

        mock_inv_result = {
            "summary": "cause", "suspectedCause": "x",
            "recommendedFix": "y", "confidence": 0.5,
        }

        with patch("sre_agent.monitor._run_proactive_investigation_sync", return_value=mock_inv_result), \
             patch("sre_agent.monitor._run_security_followup_sync") as mock_sec, \
             patch("sre_agent.agent._circuit_breaker") as mock_cb:
            mock_cb.is_open = False
            asyncio.get_event_loop().run_until_complete(
                session.run_investigations([finding])
            )

        mock_sec.assert_not_called()
        reports = [m for m in sent_messages if m.get("type") == "investigation_report"]
        assert len(reports) == 1
        assert "securityFollowup" not in reports[0]

    def test_security_followup_max_one_per_scan(self, monkeypatch):
        """Only one security followup per scan cycle."""
        monkeypatch.setenv("PULSE_AGENT_SECURITY_FOLLOWUP", "1")
        monkeypatch.setenv("PULSE_AGENT_INVESTIGATIONS_MAX_PER_SCAN", "5")
        monkeypatch.setenv("PULSE_AGENT_INVESTIGATION_TIMEOUT", "10")

        sent_messages = []

        class FakeSocket:
            async def send_json(self, data):
                sent_messages.append(data)

        session = MonitorSession(FakeSocket(), trust_level=1)

        findings = [
            _make_finding(
                severity="critical", category="crashloop",
                title=f"Pod {i} crashing", summary="restarts",
                resources=[{"kind": "Pod", "name": f"web-{i}", "namespace": "prod"}],
            )
            for i in range(3)
        ]

        mock_inv_result = {
            "summary": "cause", "suspectedCause": "x",
            "recommendedFix": "y", "confidence": 0.5,
        }
        mock_sec_result = {
            "security_issues": [], "risk_level": "low", "raw_response": "",
        }

        with patch("sre_agent.monitor._run_proactive_investigation_sync", return_value=mock_inv_result), \
             patch("sre_agent.monitor._run_security_followup_sync", return_value=mock_sec_result) as mock_sec, \
             patch("sre_agent.agent._circuit_breaker") as mock_cb:
            mock_cb.is_open = False
            asyncio.get_event_loop().run_until_complete(
                session.run_investigations(findings)
            )

        # Should only be called once despite multiple investigations
        assert mock_sec.call_count == 1


class TestMonitorAutoLearn:
    """Tests for auto-learning from investigations and verified fixes."""

    def test_auto_learn_from_high_confidence_investigation(self, monkeypatch, tmp_path):
        """High-confidence investigations are stored in memory when enabled."""
        monkeypatch.setenv("PULSE_AGENT_MEMORY", "1")
        monkeypatch.setenv("PULSE_AGENT_INVESTIGATIONS_MAX_PER_SCAN", "1")
        monkeypatch.setenv("PULSE_AGENT_INVESTIGATION_TIMEOUT", "10")

        from sre_agent.memory import MemoryManager, set_manager
        mgr = MemoryManager(db_path=str(tmp_path / "learn.db"))
        set_manager(mgr)

        sent_messages = []

        class FakeSocket:
            async def send_json(self, data):
                sent_messages.append(data)

        session = MonitorSession(FakeSocket(), trust_level=1)

        finding = _make_finding(
            severity="critical", category="crashloop",
            title="Pod crashing", summary="restarts",
            resources=[{"kind": "Pod", "name": "web-1", "namespace": "prod"}],
        )

        mock_inv_result = {
            "summary": "OOM root cause", "suspectedCause": "mem limit",
            "recommendedFix": "increase mem", "confidence": 0.85,
        }

        with patch("sre_agent.monitor._run_proactive_investigation_sync", return_value=mock_inv_result), \
             patch("sre_agent.agent._circuit_breaker") as mock_cb:
            mock_cb.is_open = False
            asyncio.get_event_loop().run_until_complete(
                session.run_investigations([finding])
            )

        # Verify incident was stored in memory
        results = mgr.store.search_incidents("investigation", limit=5)
        assert len(results) >= 1
        assert results[0]["namespace"] == "prod"
        assert results[0]["error_type"] == "crashloop"
        assert results[0]["outcome"] == "unknown"  # not confirmed

        set_manager(None)
        mgr.close()

    def test_no_auto_learn_below_confidence_threshold(self, monkeypatch, tmp_path):
        """Low-confidence investigations are NOT stored in memory."""
        monkeypatch.setenv("PULSE_AGENT_MEMORY", "1")
        monkeypatch.setenv("PULSE_AGENT_INVESTIGATIONS_MAX_PER_SCAN", "1")
        monkeypatch.setenv("PULSE_AGENT_INVESTIGATION_TIMEOUT", "10")

        from sre_agent.memory import MemoryManager, set_manager
        mgr = MemoryManager(db_path=str(tmp_path / "learn2.db"))
        set_manager(mgr)

        sent_messages = []

        class FakeSocket:
            async def send_json(self, data):
                sent_messages.append(data)

        session = MonitorSession(FakeSocket(), trust_level=1)

        finding = _make_finding(
            severity="warning", category="scheduling",
            title="Pod pending", summary="pending",
            resources=[{"kind": "Pod", "name": "api-1", "namespace": "default"}],
        )

        mock_inv_result = {
            "summary": "Not sure", "suspectedCause": "unknown",
            "recommendedFix": "investigate", "confidence": 0.3,
        }

        with patch("sre_agent.monitor._run_proactive_investigation_sync", return_value=mock_inv_result), \
             patch("sre_agent.agent._circuit_breaker") as mock_cb:
            mock_cb.is_open = False
            asyncio.get_event_loop().run_until_complete(
                session.run_investigations([finding])
            )

        # No incident stored (confidence too low)
        results = mgr.store.search_incidents("investigation", limit=5)
        assert len(results) == 0

        set_manager(None)
        mgr.close()

    def test_no_auto_learn_when_memory_disabled(self, monkeypatch, tmp_path):
        """When PULSE_AGENT_MEMORY is not '1', no auto-learn happens."""
        monkeypatch.delenv("PULSE_AGENT_MEMORY", raising=False)
        monkeypatch.setenv("PULSE_AGENT_INVESTIGATIONS_MAX_PER_SCAN", "1")
        monkeypatch.setenv("PULSE_AGENT_INVESTIGATION_TIMEOUT", "10")

        from sre_agent.memory import set_manager
        set_manager(None)

        sent_messages = []

        class FakeSocket:
            async def send_json(self, data):
                sent_messages.append(data)

        session = MonitorSession(FakeSocket(), trust_level=1)

        finding = _make_finding(
            severity="critical", category="crashloop",
            title="Pod crashing", summary="restarts",
            resources=[{"kind": "Pod", "name": "web-1", "namespace": "prod"}],
        )

        mock_inv_result = {
            "summary": "cause found", "suspectedCause": "x",
            "recommendedFix": "y", "confidence": 0.9,
        }

        with patch("sre_agent.monitor._run_proactive_investigation_sync", return_value=mock_inv_result), \
             patch("sre_agent.agent._circuit_breaker") as mock_cb:
            mock_cb.is_open = False
            # Should not raise even without memory
            asyncio.get_event_loop().run_until_complete(
                session.run_investigations([finding])
            )

    def test_auto_learn_from_verified_fix(self, monkeypatch, tmp_path):
        """Verified fixes are stored in memory as confirmed incidents."""
        monkeypatch.setenv("PULSE_AGENT_MEMORY", "1")

        from sre_agent.memory import MemoryManager, set_manager
        mgr = MemoryManager(db_path=str(tmp_path / "learn3.db"))
        set_manager(mgr)

        sent_messages = []

        class FakeSocket:
            async def send_json(self, data):
                sent_messages.append(data)

        session = MonitorSession(FakeSocket(), trust_level=3, auto_fix_categories=["crashloop"])

        # Create a pending action in session state
        action = _make_action_report(
            finding_id="f-test",
            tool="delete_pod",
            inp={"name": "web-1", "namespace": "prod"},
            status="completed",
            reasoning="Auto-fix crashloop",
        )
        save_action(action, category="crashloop", resources=[{"kind": "Pod", "name": "web-1", "namespace": "prod"}])
        session._pending_verifications[action["id"]] = {
            "category": "crashloop",
            "resources": [{"kind": "Pod", "name": "web-1", "namespace": "prod"}],
            "payload": {"tool": "delete_pod"},
        }

        # Simulate no active finding → verified
        asyncio.get_event_loop().run_until_complete(
            session.process_verifications([])
        )

        # Check that a confirmed incident was stored
        results = mgr.store.search_incidents("auto-fix", limit=5)
        assert len(results) >= 1
        assert results[0]["outcome"] == "resolved"
        assert results[0]["error_type"] == "crashloop"

        set_manager(None)
        mgr.close()
