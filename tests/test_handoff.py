"""Tests for agent-to-agent handoff tools and monitor processing."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from sre_agent.context_bus import ContextEntry, get_context_bus
from sre_agent.db import Database, reset_database, set_database
from sre_agent.handoff_tools import request_security_scan, request_sre_investigation
from sre_agent.monitor.cluster_monitor import ClusterMonitor


@pytest.fixture(autouse=True)
def _use_temp_db(monkeypatch, tmp_path):
    """Use a temp database for each test."""
    import sre_agent.context_bus as _cb
    import sre_agent.monitor as _mon
    from tests.conftest import _TEST_DB_URL

    db = Database(_TEST_DB_URL)
    set_database(db)
    _mon.findings._tables_ensured = False
    _cb._tables_ensured = False
    _mon._ensure_tables()
    _cb._ensure_tables()
    for table in ("actions", "investigations", "findings", "context_entries"):
        try:
            db.execute(f"TRUNCATE {table} RESTART IDENTITY CASCADE")
        except Exception:
            pass
    db.commit()
    yield
    reset_database()
    _mon.findings._tables_ensured = False
    _cb._tables_ensured = False


class TestRequestSecurityScan:
    def test_publishes_to_context_bus(self):
        result = request_security_scan("my-namespace", context="suspicious RBAC config")
        assert "my-namespace" in result

        bus = get_context_bus()
        entries = bus.get_context_for(namespace="my-namespace", category="handoff_request", limit=5)
        assert len(entries) >= 1
        entry = entries[0]
        assert entry.category == "handoff_request"
        assert entry.source == "sre_agent"
        assert entry.namespace == "my-namespace"
        details = entry.details
        assert details["target"] == "security_agent"
        assert details["namespace"] == "my-namespace"
        assert details["context"] == "suspicious RBAC config"

    def test_publishes_without_context(self):
        result = request_security_scan("default")
        assert "default" in result

        bus = get_context_bus()
        entries = bus.get_context_for(namespace="default", category="handoff_request", limit=5)
        assert len(entries) >= 1
        assert entries[0].details["target"] == "security_agent"


class TestRequestSreInvestigation:
    def test_publishes_to_context_bus(self):
        result = request_sre_investigation(
            namespace="prod",
            resource_kind="Pod",
            resource_name="web-app-123",
            context="running as root",
        )
        assert "prod" in result
        assert "Pod" in result

        bus = get_context_bus()
        entries = bus.get_context_for(namespace="prod", category="handoff_request", limit=5)
        assert len(entries) >= 1
        entry = entries[0]
        assert entry.category == "handoff_request"
        assert entry.source == "security_agent"
        assert entry.namespace == "prod"
        details = entry.details
        assert details["target"] == "sre_agent"
        assert details["namespace"] == "prod"
        assert details["kind"] == "Pod"
        assert details["name"] == "web-app-123"
        assert details["context"] == "running as root"

    def test_publishes_without_optional_fields(self):
        result = request_sre_investigation(namespace="staging")
        assert "staging" in result

        bus = get_context_bus()
        entries = bus.get_context_for(namespace="staging", category="handoff_request", limit=5)
        assert len(entries) >= 1
        assert entries[0].details["target"] == "sre_agent"


class TestProcessHandoffs:
    @pytest.fixture
    def monitor(self):
        return ClusterMonitor()

    def test_processes_security_handoff(self, monitor):
        # Publish a security handoff request
        bus = get_context_bus()
        bus.publish(
            ContextEntry(
                source="sre_agent",
                category="handoff_request",
                summary="SRE agent requests security scan",
                details={"target": "security_agent", "namespace": "prod", "context": "suspicious config"},
                namespace="prod",
            )
        )

        with patch(
            "sre_agent.monitor.cluster_monitor._run_security_followup_sync",
            return_value={"security_issues": [], "risk_level": "low"},
        ) as mock_sec:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(monitor.process_handoffs())
            finally:
                loop.close()
            mock_sec.assert_called_once()
            finding_arg = mock_sec.call_args[0][0]
            assert finding_arg["category"] == "handoff"
            assert "prod" in finding_arg["title"]

    def test_processes_sre_handoff(self, monitor):
        bus = get_context_bus()
        bus.publish(
            ContextEntry(
                source="security_agent",
                category="handoff_request",
                summary="Security agent requests SRE investigation",
                details={
                    "target": "sre_agent",
                    "namespace": "staging",
                    "kind": "Pod",
                    "name": "web-1",
                    "context": "running as root",
                },
                namespace="staging",
            )
        )

        with patch(
            "sre_agent.monitor.cluster_monitor._run_proactive_investigation_sync",
            return_value={"summary": "ok", "confidence": 0.5},
        ) as mock_sre:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(monitor.process_handoffs())
            finally:
                loop.close()
            mock_sre.assert_called_once()
            finding_arg = mock_sre.call_args[0][0]
            assert finding_arg["category"] == "handoff"
            assert "staging" in finding_arg["resources"][0]["namespace"]

    def test_cleans_up_processed_requests(self, monitor):
        from sre_agent.db import get_database

        bus = get_context_bus()
        bus.publish(
            ContextEntry(
                source="sre_agent",
                category="handoff_request",
                summary="test",
                details={"target": "security_agent", "namespace": "ns1", "context": ""},
                namespace="ns1",
            )
        )

        with patch("sre_agent.monitor.cluster_monitor._run_security_followup_sync", return_value={}):
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(monitor.process_handoffs())
            finally:
                loop.close()

        # Verify cleanup
        db = get_database()
        import time

        cutoff = int(time.time() * 1000) - 300_000
        rows = db.fetchall(
            "SELECT * FROM context_entries WHERE category = ? AND timestamp > ?",
            ("handoff_request", cutoff),
        )
        assert len(rows) == 0

    def test_no_handoffs_is_noop(self, monitor):
        # Should not raise
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(monitor.process_handoffs())
        finally:
            loop.close()

    def test_ignores_unknown_target(self, monitor):
        bus = get_context_bus()
        bus.publish(
            ContextEntry(
                source="unknown",
                category="handoff_request",
                summary="test",
                details={"target": "unknown_agent", "namespace": "ns1", "context": ""},
                namespace="ns1",
            )
        )

        # Should not call either investigation function
        with (
            patch("sre_agent.monitor.cluster_monitor._run_security_followup_sync") as mock_sec,
            patch("sre_agent.monitor.cluster_monitor._run_proactive_investigation_sync") as mock_sre,
        ):
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(monitor.process_handoffs())
            finally:
                loop.close()
            mock_sec.assert_not_called()
            mock_sre.assert_not_called()
