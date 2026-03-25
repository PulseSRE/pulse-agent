"""Tests for runbook extraction."""

import pytest
from sre_agent.memory.store import IncidentStore
from sre_agent.memory.runbooks import extract_runbook, is_duplicate_runbook


@pytest.fixture
def store():
    s = IncidentStore(":memory:")
    yield s
    s.close()


class TestExtractRunbook:
    def test_extracts_from_resolved(self, store):
        iid = store.record_incident(
            query="pods crashing in monitoring",
            tool_sequence=[{"name": "list_pods"}, {"name": "get_pod_logs"}, {"name": "describe_pod"}],
            resolution="Found OOMKilled",
            outcome="resolved",
            error_type="OOMKilled",
            resource_type="pod",
            score=0.9,
        )
        rid = extract_runbook(store, iid)
        assert rid is not None
        runbooks = store.list_runbooks()
        assert len(runbooks) == 1
        assert "OOMKilled" in runbooks[0]["name"]

    def test_skips_unresolved(self, store):
        iid = store.record_incident(
            query="test", tool_sequence=[{"name": "a"}, {"name": "b"}],
            resolution="r", outcome="unknown",
        )
        assert extract_runbook(store, iid) is None

    def test_skips_single_tool(self, store):
        iid = store.record_incident(
            query="test", tool_sequence=[{"name": "a"}],
            resolution="r", outcome="resolved",
        )
        assert extract_runbook(store, iid) is None

    def test_custom_name(self, store):
        iid = store.record_incident(
            query="test", tool_sequence=[{"name": "a"}, {"name": "b"}],
            resolution="r", outcome="resolved",
        )
        rid = extract_runbook(store, iid, name="my-custom-runbook")
        runbooks = store.list_runbooks()
        assert runbooks[0]["name"] == "my-custom-runbook"

    def test_nonexistent_incident(self, store):
        assert extract_runbook(store, 999) is None


class TestIsDuplicateRunbook:
    def test_detects_duplicate(self, store):
        store.save_runbook(
            name="rb", description="d", trigger_keywords="kw",
            tool_sequence=[{"name": "list_pods"}, {"name": "get_pod_logs"}],
        )
        assert is_duplicate_runbook(store, [{"name": "list_pods"}, {"name": "get_pod_logs"}]) is True

    def test_different_sequence(self, store):
        store.save_runbook(
            name="rb", description="d", trigger_keywords="kw",
            tool_sequence=[{"name": "list_pods"}, {"name": "get_pod_logs"}],
        )
        assert is_duplicate_runbook(store, [{"name": "list_pods"}, {"name": "describe_pod"}]) is False

    def test_empty_store(self, store):
        assert is_duplicate_runbook(store, [{"name": "a"}]) is False
