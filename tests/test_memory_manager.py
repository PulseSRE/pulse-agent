"""Tests for MemoryManager.store_incident and module-level singleton."""

from unittest.mock import patch, MagicMock

import pytest

from sre_agent.memory import get_manager, set_manager, MemoryManager


class TestSingleton:
    def teardown_method(self):
        set_manager(None)

    def test_get_manager_returns_none_by_default(self):
        set_manager(None)
        assert get_manager() is None

    def test_set_and_get_manager(self):
        mgr = MagicMock(spec=MemoryManager)
        set_manager(mgr)
        assert get_manager() is mgr

    def test_set_none_clears_manager(self):
        set_manager(MagicMock(spec=MemoryManager))
        set_manager(None)
        assert get_manager() is None


class TestStoreIncident:
    @pytest.fixture
    def manager(self, tmp_path):
        m = MemoryManager(db_path=str(tmp_path / "test.db"))
        yield m
        m.close()

    def test_store_unconfirmed_incident(self, manager):
        incident = {
            "query": "Pod crashing in monitoring",
            "tool_sequence": ["list_pods", "get_pod_logs"],
            "resolution": "Found OOM, increased limits",
            "namespace": "monitoring",
            "resource_type": "pod",
            "error_type": "OOMKilled",
        }
        iid = manager.store_incident(incident, confirmed=False)
        assert iid is not None and iid > 0
        # Unconfirmed → outcome=unknown, score=0.5
        results = manager.store.search_incidents("crashing", limit=5)
        assert len(results) == 1
        assert results[0]["outcome"] == "unknown"

    def test_store_confirmed_incident_extracts_runbook(self, manager):
        incident = {
            "query": "Deployment failing",
            "tool_sequence": ["list_deployments", "describe_deployment"],
            "resolution": "Restarted deployment",
            "namespace": "default",
            "resource_type": "deployment",
            "error_type": "CrashLoopBackOff",
        }
        iid = manager.store_incident(incident, confirmed=True)
        assert iid is not None and iid > 0
        # Confirmed → outcome=resolved, score=0.8
        results = manager.store.search_incidents("deployment", limit=5)
        assert results[0]["outcome"] == "resolved"

    def test_normalises_string_tool_sequence(self, manager):
        incident = {
            "query": "test",
            "tool_sequence": ["delete_pod"],
            "resolution": "deleted",
        }
        iid = manager.store_incident(incident, confirmed=False)
        assert iid is not None and iid > 0

    def test_handles_dict_tool_sequence(self, manager):
        incident = {
            "query": "test",
            "tool_sequence": [{"name": "list_pods"}],
            "resolution": "listed",
        }
        iid = manager.store_incident(incident, confirmed=False)
        assert iid is not None and iid > 0

    def test_empty_tool_sequence_no_runbook(self, manager):
        incident = {
            "query": "test",
            "tool_sequence": [],
            "resolution": "nothing",
        }
        iid = manager.store_incident(incident, confirmed=True)
        assert iid is not None
        # No runbook extracted for empty tool sequence
        runbooks = manager.store.list_runbooks(limit=10)
        assert len(runbooks) == 0

    def test_missing_keys_use_defaults(self, manager):
        iid = manager.store_incident({}, confirmed=False)
        assert iid is not None and iid > 0
