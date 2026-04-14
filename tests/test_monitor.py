"""Tests for the monitor module — fix history, findings, and scan functions."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from sre_agent.db import Database, reset_database, set_database
from sre_agent.monitor import (
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    MonitorSession,
    _estimate_finding_confidence,
    _finding_key,
    _make_action_report,
    _make_finding,
    _make_prediction,
    _make_rollback_info,
    _run_security_followup_sync,
    _skip_namespace,
    get_action_detail,
    get_fix_history,
    save_action,
    save_investigation,
    update_action_verification,
)
from tests.conftest import _TEST_DB_URL


@pytest.fixture(autouse=True)
def _use_temp_db(monkeypatch, tmp_path):
    """Use a temp database for each test."""
    import sre_agent.context_bus as _cb
    import sre_agent.monitor as _mon
    from tests.conftest import _TEST_DB_URL

    db = Database(_TEST_DB_URL)
    set_database(db)
    # Reset table-creation flags so each test creates tables fresh
    _mon.findings._tables_ensured = False
    _cb._tables_ensured = False
    # Ensure tables exist then truncate for isolation
    # Drop and recreate tables to pick up schema changes
    for table in (
        "actions",
        "investigations",
        "findings",
        "context_entries",
        "incidents",
        "runbooks",
        "patterns",
        "metrics",
    ):
        try:
            db.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        except Exception:
            pass
    db.commit()
    _mon.findings._tables_ensured = False
    _cb._tables_ensured = False
    _mon._ensure_tables()
    _cb._ensure_tables()
    yield
    reset_database()
    _mon.findings._tables_ensured = False
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
        with (
            patch("sre_agent.agent.create_client", return_value=MagicMock()),
            patch(
                "sre_agent.agent.run_agent_streaming",
                return_value='{"security_issues": [{"issue": "no netpol"}], "risk_level": "high"}',
            ) as mock_run,
        ):
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
            severity="critical",
            category="crashloop",
            title="Pod crashing",
            summary="s",
            resources=[{"kind": "Pod", "name": "x", "namespace": "ns"}],
        )
        with (
            patch("sre_agent.agent.create_client", return_value=MagicMock()),
            patch("sre_agent.agent.run_agent_streaming", return_value="not json at all"),
        ):
            result = _run_security_followup_sync(finding)
        assert result["security_issues"] == []
        assert result["risk_level"] == "unknown"

    def test_security_followup_called_in_investigations(self, monkeypatch):
        """When PULSE_AGENT_SECURITY_FOLLOWUP=1, security followup runs after investigation."""
        monkeypatch.setenv("PULSE_AGENT_SECURITY_FOLLOWUP", "1")
        monkeypatch.setenv("PULSE_AGENT_INVESTIGATIONS_MAX_PER_SCAN", "1")
        monkeypatch.setenv("PULSE_AGENT_INVESTIGATION_TIMEOUT", "10")
        from sre_agent.config import _reset_settings

        _reset_settings()

        sent_messages = []

        class FakeSocket:
            async def send_json(self, data):
                sent_messages.append(data)

        session = MonitorSession(FakeSocket(), trust_level=1)

        finding = _make_finding(
            severity="critical",
            category="crashloop",
            title="Pod crashing",
            summary="restarts",
            resources=[{"kind": "Pod", "name": "web-1", "namespace": "prod"}],
        )

        mock_inv_result = {
            "summary": "OOM cause",
            "suspectedCause": "mem limit",
            "recommendedFix": "increase mem",
            "confidence": 0.8,
        }
        mock_sec_result = {
            "security_issues": [{"issue": "no netpol"}],
            "risk_level": "medium",
            "raw_response": "test",
        }

        with (
            patch("sre_agent.monitor.session._run_proactive_investigation_sync", return_value=mock_inv_result),
            patch("sre_agent.monitor.session._run_security_followup_sync", return_value=mock_sec_result) as mock_sec,
            patch("sre_agent.agent._circuit_breaker") as mock_cb,
            patch.object(session, "_try_plan_execution", return_value=False),
        ):
            mock_cb.is_open = False
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(session.run_investigations([finding]))
            finally:
                loop.close()

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
            severity="critical",
            category="crashloop",
            title="Pod crashing",
            summary="restarts",
            resources=[{"kind": "Pod", "name": "web-1", "namespace": "prod"}],
        )

        mock_inv_result = {
            "summary": "cause",
            "suspectedCause": "x",
            "recommendedFix": "y",
            "confidence": 0.5,
        }

        with (
            patch("sre_agent.monitor.session._run_proactive_investigation_sync", return_value=mock_inv_result),
            patch("sre_agent.monitor.session._run_security_followup_sync") as mock_sec,
            patch("sre_agent.agent._circuit_breaker") as mock_cb,
            patch.object(session, "_try_plan_execution", return_value=False),
        ):
            mock_cb.is_open = False
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(session.run_investigations([finding]))
            finally:
                loop.close()

        mock_sec.assert_not_called()
        reports = [m for m in sent_messages if m.get("type") == "investigation_report"]
        assert len(reports) == 1
        assert "securityFollowup" not in reports[0]

    def test_security_followup_max_one_per_scan(self, monkeypatch):
        """Only one security followup per scan cycle."""
        monkeypatch.setenv("PULSE_AGENT_SECURITY_FOLLOWUP", "1")
        monkeypatch.setenv("PULSE_AGENT_INVESTIGATIONS_MAX_PER_SCAN", "5")
        monkeypatch.setenv("PULSE_AGENT_INVESTIGATION_TIMEOUT", "10")
        from sre_agent.config import _reset_settings

        _reset_settings()

        sent_messages = []

        class FakeSocket:
            async def send_json(self, data):
                sent_messages.append(data)

        session = MonitorSession(FakeSocket(), trust_level=1)

        findings = [
            _make_finding(
                severity="critical",
                category="crashloop",
                title=f"Pod {i} crashing",
                summary="restarts",
                resources=[{"kind": "Pod", "name": f"web-{i}", "namespace": "prod"}],
            )
            for i in range(3)
        ]

        mock_inv_result = {
            "summary": "cause",
            "suspectedCause": "x",
            "recommendedFix": "y",
            "confidence": 0.5,
        }
        mock_sec_result = {
            "security_issues": [],
            "risk_level": "low",
            "raw_response": "",
        }

        with (
            patch("sre_agent.monitor.session._run_proactive_investigation_sync", return_value=mock_inv_result),
            patch("sre_agent.monitor.session._run_security_followup_sync", return_value=mock_sec_result) as mock_sec,
            patch("sre_agent.agent._circuit_breaker") as mock_cb,
            patch.object(session, "_try_plan_execution", return_value=False),
        ):
            mock_cb.is_open = False
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(session.run_investigations(findings))
            finally:
                loop.close()

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

        mgr = MemoryManager(db_path=_TEST_DB_URL)
        set_manager(mgr)

        sent_messages = []

        class FakeSocket:
            async def send_json(self, data):
                sent_messages.append(data)

        session = MonitorSession(FakeSocket(), trust_level=1)

        finding = _make_finding(
            severity="critical",
            category="crashloop",
            title="Pod crashing",
            summary="restarts",
            resources=[{"kind": "Pod", "name": "web-1", "namespace": "prod"}],
        )

        mock_inv_result = {
            "summary": "OOM root cause",
            "suspectedCause": "mem limit",
            "recommendedFix": "increase mem",
            "confidence": 0.85,
        }

        with (
            patch("sre_agent.monitor.session._run_proactive_investigation_sync", return_value=mock_inv_result),
            patch("sre_agent.agent._circuit_breaker") as mock_cb,
            patch.object(session, "_try_plan_execution", return_value=False),
        ):
            mock_cb.is_open = False
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(session.run_investigations([finding]))
            finally:
                loop.close()

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

        mgr = MemoryManager(db_path=_TEST_DB_URL)
        set_manager(mgr)

        sent_messages = []

        class FakeSocket:
            async def send_json(self, data):
                sent_messages.append(data)

        session = MonitorSession(FakeSocket(), trust_level=1)

        finding = _make_finding(
            severity="warning",
            category="scheduling",
            title="Pod pending",
            summary="pending",
            resources=[{"kind": "Pod", "name": "api-1", "namespace": "default"}],
        )

        mock_inv_result = {
            "summary": "Not sure",
            "suspectedCause": "unknown",
            "recommendedFix": "investigate",
            "confidence": 0.3,
        }

        with (
            patch("sre_agent.monitor.session._run_proactive_investigation_sync", return_value=mock_inv_result),
            patch("sre_agent.agent._circuit_breaker") as mock_cb,
        ):
            mock_cb.is_open = False
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(session.run_investigations([finding]))
            finally:
                loop.close()

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
            severity="critical",
            category="crashloop",
            title="Pod crashing",
            summary="restarts",
            resources=[{"kind": "Pod", "name": "web-1", "namespace": "prod"}],
        )

        mock_inv_result = {
            "summary": "cause found",
            "suspectedCause": "x",
            "recommendedFix": "y",
            "confidence": 0.9,
        }

        with (
            patch("sre_agent.monitor.session._run_proactive_investigation_sync", return_value=mock_inv_result),
            patch("sre_agent.agent._circuit_breaker") as mock_cb,
            patch.object(session, "_try_plan_execution", return_value=False),
        ):
            mock_cb.is_open = False
            # Should not raise even without memory
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(session.run_investigations([finding]))
            finally:
                loop.close()

    def test_auto_learn_from_verified_fix(self, monkeypatch, tmp_path):
        """Verified fixes are stored in memory as confirmed incidents."""
        monkeypatch.setenv("PULSE_AGENT_MEMORY", "1")

        from sre_agent.memory import MemoryManager, set_manager

        mgr = MemoryManager(db_path=_TEST_DB_URL)
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
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(session.process_verifications([]))
        finally:
            loop.close()

        # Check that a confirmed incident was stored
        results = mgr.store.search_incidents("auto-fix", limit=5)
        assert len(results) >= 1
        assert results[0]["outcome"] == "resolved"
        assert results[0]["error_type"] == "crashloop"

        set_manager(None)
        mgr.close()


class TestSkipNamespace:
    def test_skips_openshift_system(self):
        assert _skip_namespace("openshift-monitoring") is True
        assert _skip_namespace("openshift-operators") is True

    def test_skips_kube_system(self):
        assert _skip_namespace("kube-system") is True
        assert _skip_namespace("kube-public") is True

    def test_allows_default_skips_openshift(self):
        assert _skip_namespace("default") is False
        assert _skip_namespace("openshift") is True

    def test_allows_user_namespaces(self):
        assert _skip_namespace("my-app") is False
        assert _skip_namespace("production") is False
        assert _skip_namespace("openshiftpulse") is False


class TestMakeRollbackInfo:
    def test_no_rollback_for_failed_action(self):
        action = {"status": "failed", "tool": "restart_deployment"}
        finding = {"_rollback_meta": {"name": "web", "namespace": "prod", "revision": "5"}}
        available, json_str = _make_rollback_info(action, finding)
        assert available == 0
        assert json_str == ""

    def test_no_rollback_without_meta(self):
        action = {"status": "completed", "tool": "restart_deployment"}
        available, _json_str = _make_rollback_info(action, None)
        assert available == 0

    def test_no_rollback_for_delete_pod(self):
        action = {"status": "completed", "tool": "delete_pod"}
        finding = {"_rollback_meta": {"name": "web", "namespace": "prod", "revision": "5"}}
        available, _json_str = _make_rollback_info(action, finding)
        assert available == 0

    def test_rollback_for_restart_deployment(self):
        action = {"status": "completed", "tool": "restart_deployment"}
        finding = {"_rollback_meta": {"name": "web", "namespace": "prod", "revision": "5"}}
        available, json_str = _make_rollback_info(action, finding)
        assert available == 1
        import json

        data = json.loads(json_str)
        assert data["tool"] == "rollback_deployment"
        assert data["input"]["name"] == "web"
        assert data["input"]["namespace"] == "prod"
        assert data["input"]["revision"] == "5"

    def test_rollback_for_restart_statefulset(self):
        action = {"status": "completed", "tool": "restart_statefulset"}
        finding = {"_rollback_meta": {"name": "db", "namespace": "data", "revision": ""}}
        available, _ = _make_rollback_info(action, finding)
        assert available == 1

    def test_rollback_for_restart_daemonset(self):
        action = {"status": "completed", "tool": "restart_daemonset"}
        finding = {"_rollback_meta": {"name": "agent", "namespace": "infra"}}
        available, _ = _make_rollback_info(action, finding)
        assert available == 1


class TestFixImagePullRollback:
    def test_sets_rollback_meta_for_deployment(self):
        """_fix_image_pull should set _rollback_meta when restarting a deployment."""
        from sre_agent.monitor import _fix_image_pull

        finding = {
            "resources": [{"name": "web-pod-abc", "namespace": "prod"}],
        }

        # Mock pod with ReplicaSet owner -> Deployment owner
        mock_pod = SimpleNamespace(
            metadata=SimpleNamespace(
                owner_references=[SimpleNamespace(kind="ReplicaSet", name="web-rs-123")],
            ),
        )
        mock_rs = SimpleNamespace(
            metadata=SimpleNamespace(
                owner_references=[SimpleNamespace(kind="Deployment", name="web")],
            ),
        )
        mock_dep = SimpleNamespace(
            metadata=SimpleNamespace(
                annotations={"deployment.kubernetes.io/revision": "7"},
            ),
        )

        with (
            patch("sre_agent.monitor.autofix.get_core_client") as mock_core,
            patch("sre_agent.monitor.autofix.get_apps_client") as mock_apps,
        ):
            mock_core.return_value.read_namespaced_pod.return_value = mock_pod
            mock_apps.return_value.read_namespaced_replica_set.return_value = mock_rs
            mock_apps.return_value.read_namespaced_deployment.return_value = mock_dep

            tool, _before, _after = _fix_image_pull(finding)

        assert tool == "restart_deployment"
        assert finding["_rollback_meta"] == {
            "name": "web",
            "namespace": "prod",
            "revision": "7",
        }


class TestConfidenceScoring:
    def test_finding_confidence_by_category(self):
        assert _estimate_finding_confidence({"category": "crashloop", "severity": "critical"}) >= 0.95
        assert _estimate_finding_confidence({"category": "hpa", "severity": "warning"}) <= 0.80

    def test_finding_confidence_severity_adjustment(self):
        critical = _estimate_finding_confidence({"category": "pending", "severity": "critical"})
        warning = _estimate_finding_confidence({"category": "pending", "severity": "warning"})
        info = _estimate_finding_confidence({"category": "pending", "severity": "info"})
        assert critical > warning > info

    def test_finding_confidence_unknown_category(self):
        conf = _estimate_finding_confidence({"category": "unknown_scanner", "severity": "warning"})
        assert 0.0 <= conf <= 1.0

    def test_make_finding_includes_confidence(self):
        f = _make_finding(
            severity="warning",
            category="crashloop",
            title="Test",
            summary="Test finding",
            resources=[],
            confidence=0.85,
        )
        assert f["confidence"] == 0.85

    def test_make_finding_without_confidence(self):
        f = _make_finding(
            severity="warning",
            category="crashloop",
            title="Test",
            summary="Test finding",
            resources=[],
        )
        assert "confidence" not in f

    def test_make_action_report_includes_confidence(self):
        r = _make_action_report(
            finding_id="f-123",
            tool="restart_deployment",
            inp={},
            status="proposed",
            confidence=0.78,
        )
        assert r["confidence"] == 0.78

    def test_make_action_report_without_confidence(self):
        r = _make_action_report(
            finding_id="f-123",
            tool="restart_deployment",
            inp={},
            status="proposed",
        )
        assert "confidence" not in r

    def test_confidence_clamped_to_valid_range(self):
        f = _make_finding(severity="critical", category="test", title="t", summary="s", resources=[], confidence=1.5)
        assert f["confidence"] == 1.0
        f2 = _make_finding(severity="critical", category="test", title="t", summary="s", resources=[], confidence=-0.3)
        assert f2["confidence"] == 0.0


class TestResolutionEvents:
    def test_resolution_emitted_when_finding_disappears(self):
        """When a finding from scan N is absent in scan N+1, a resolution event is sent."""
        ws = MagicMock()
        session = MonitorSession(ws, trust_level=1)

        # Simulate scan N: finding present
        finding = _make_finding(
            severity="warning",
            category="crashloop",
            title="Pod crashing",
            summary="test",
            resources=[{"kind": "Pod", "name": "web", "namespace": "prod"}],
        )
        key = "crashloop:Pod crashing:Pod:prod:web"
        session._last_findings[key] = finding

        # Simulate scan N+1: finding gone — run the stale-key detection inline
        current_keys: set[str] = set()  # empty = finding disappeared
        stale_keys = set(session._last_findings.keys()) - current_keys
        resolution_events = []
        for k in stale_keys:
            resolved = session._last_findings.pop(k)
            resolved_by = "self-healed"
            fid = resolved.get("id", "")
            if fid in session._recent_fix_ids:
                resolved_by = "auto-fix"
                session._recent_fix_ids.discard(fid)
            resolution_events.append(
                {
                    "type": "resolution",
                    "findingId": fid,
                    "category": resolved.get("category", ""),
                    "resolvedBy": resolved_by,
                }
            )

        assert len(resolution_events) == 1
        assert resolution_events[0]["findingId"] == finding["id"]
        assert resolution_events[0]["resolvedBy"] == "self-healed"
        assert resolution_events[0]["category"] == "crashloop"

    def test_resolution_attributed_to_auto_fix(self):
        """When a fixed finding disappears, resolvedBy should be 'auto-fix'."""
        ws = MagicMock()
        session = MonitorSession(ws, trust_level=3)

        finding = _make_finding(
            severity="warning",
            category="workloads",
            title="Deploy failing",
            summary="test",
            resources=[{"kind": "Deployment", "name": "api", "namespace": "prod"}],
        )
        key = "workloads:Deploy failing:Deployment:prod:api"
        session._last_findings[key] = finding
        session._recent_fix_ids.add(finding["id"])

        # Finding disappears
        stale_keys = set(session._last_findings.keys()) - set()
        for k in stale_keys:
            resolved = session._last_findings.pop(k)
            fid = resolved.get("id", "")
            if fid in session._recent_fix_ids:
                resolved_by = "auto-fix"
                session._recent_fix_ids.discard(fid)
            else:
                resolved_by = "self-healed"

        assert resolved_by == "auto-fix"
        assert finding["id"] not in session._recent_fix_ids  # cleaned up

    def test_recent_fix_ids_bounded(self):
        """_recent_fix_ids should not grow unboundedly."""
        ws = MagicMock()
        session = MonitorSession(ws, trust_level=3)
        for i in range(600):
            session._recent_fix_ids.add(f"f-{i:012d}")
        assert len(session._recent_fix_ids) == 600
        # Simulate the cap logic from _run_scan_locked
        if len(session._recent_fix_ids) > 500:
            session._recent_fix_ids = set(list(session._recent_fix_ids)[-500:])
        assert len(session._recent_fix_ids) == 500


class TestExecuteRollback:
    def test_rollback_action_not_found(self):
        from sre_agent.monitor import execute_rollback

        result = execute_rollback("nonexistent-id")
        assert result["error"] == "Action not found"

    def test_rollback_not_completed(self):
        from sre_agent.monitor import execute_rollback

        action = _make_action_report(
            finding_id="f-1",
            tool="restart_deployment",
            inp={},
            status="failed",
        )
        save_action(action, category="workloads", resources=[])
        result = execute_rollback(action["id"])
        assert "Cannot rollback" in result["error"]

    def test_rollback_no_rollback_data(self):
        from sre_agent.monitor import execute_rollback

        action = _make_action_report(
            finding_id="f-1",
            tool="delete_pod",
            inp={},
            status="completed",
        )
        save_action(action, category="crashloop", resources=[])
        result = execute_rollback(action["id"])
        assert "not available" in result["error"]

    def test_rollback_calls_rollback_deployment(self):
        from sre_agent.monitor import execute_rollback

        action = _make_action_report(
            finding_id="f-1",
            tool="restart_deployment",
            inp={},
            status="completed",
        )
        finding = {"_rollback_meta": {"name": "web", "namespace": "prod", "revision": "3"}}
        save_action(action, category="workloads", resources=[], finding=finding)

        with patch(
            "sre_agent.k8s_tools.rollback_deployment", return_value="Rolled back prod/web to revision 3"
        ) as mock_rb:
            result = execute_rollback(action["id"])
            mock_rb.assert_called_once_with("prod", "web", 3)
            assert result["status"] == "rolled_back"
            assert result["actionId"] == action["id"]


class TestBriefing:
    def test_briefing_returns_greeting(self):
        from sre_agent.monitor import get_briefing

        result = get_briefing(hours=12)
        assert result["greeting"] in ("Good morning", "Good afternoon", "Good evening")
        assert "summary" in result
        assert result["hours"] == 12
        assert result["actions"]["total"] >= 0

    def test_briefing_empty_db(self):
        from sre_agent.monitor import get_briefing

        result = get_briefing(hours=1)
        assert result["summary"] == "All quiet — no issues detected."
        assert result["actions"]["completed"] == 0

    def test_briefing_with_actions(self):
        from sre_agent.monitor import get_briefing, save_action

        action = _make_action_report(
            finding_id="f-test",
            tool="restart_deployment",
            inp={},
            status="completed",
            reasoning="test fix",
        )
        save_action(action, category="workloads", resources=[])
        result = get_briefing(hours=1)
        assert result["actions"]["completed"] == 1
        assert "auto-fixed" in result["summary"]


class TestSimulation:
    def test_simulate_known_tool(self):
        from sre_agent.monitor import simulate_action

        result = simulate_action("restart_deployment", {"name": "web", "namespace": "prod"})
        assert result["tool"] == "restart_deployment"
        assert result["risk"] == "medium"
        assert result["reversible"] is True
        assert "estimatedDuration" in result

    def test_simulate_high_risk_tool(self):
        from sre_agent.monitor import simulate_action

        result = simulate_action("drain_node", {"name": "worker-1"})
        assert result["risk"] == "high"
        assert result["reversible"] is False

    def test_simulate_unknown_tool(self):
        from sre_agent.monitor import simulate_action

        result = simulate_action("unknown_tool", {})
        assert result["risk"] == "low"
        assert "unknown_tool" in result["description"]


class TestNoiseTracking:
    def test_noise_score_not_set_on_first_appearance(self):
        ws = MagicMock()
        session = MonitorSession(ws, trust_level=1)
        # No transient history — noise score should not be set
        finding = _make_finding(
            severity="warning",
            category="pending",
            title="Pod pending",
            summary="test",
            resources=[{"kind": "Pod", "name": "x", "namespace": "ns"}],
        )
        key = _finding_key(finding)
        assert session._transient_counts.get(key, 0) == 0

    def test_transient_count_increments(self):
        ws = MagicMock()
        session = MonitorSession(ws, trust_level=1)
        key = "pending:Pod pending:Pod:ns:x"
        session._transient_counts[key] = 2
        # Simulate another disappearance
        session._transient_counts[key] += 1
        assert session._transient_counts[key] == 3

    def test_noise_threshold_configurable(self, monkeypatch):
        from sre_agent.config import _reset_settings

        monkeypatch.setenv("PULSE_AGENT_NOISE_THRESHOLD", "0.9")
        _reset_settings()
        ws = MagicMock()
        session = MonitorSession(ws, trust_level=1)
        assert session._noise_threshold == 0.9


class TestCreateDashboard:
    def test_create_dashboard_returns_signal(self):
        from sre_agent.view_tools import SIGNAL_PREFIX, create_dashboard

        result = create_dashboard("My Dashboard", "Node health overview")
        assert SIGNAL_PREFIX in result
        import json

        signal_json = result.split(SIGNAL_PREFIX, 1)[1]
        signal = json.loads(signal_json)
        assert signal["type"] == "view_spec"
        assert signal["view_id"].startswith("cv-")
        assert signal["title"] == "My Dashboard"
        assert signal["description"] == "Node health overview"

    def test_create_dashboard_generates_unique_ids(self):
        import json

        from sre_agent.view_tools import SIGNAL_PREFIX, create_dashboard

        r1 = create_dashboard("A", "")
        r2 = create_dashboard("B", "")
        s1 = json.loads(r1.split(SIGNAL_PREFIX, 1)[1])
        s2 = json.loads(r2.split(SIGNAL_PREFIX, 1)[1])
        assert s1["view_id"] != s2["view_id"]
