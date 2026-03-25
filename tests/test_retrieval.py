"""Tests for memory retrieval and context assembly."""

import json
import pytest

from sre_agent.memory.store import IncidentStore
from sre_agent.memory.retrieval import build_memory_context, MAX_MEMORY_CHARS


@pytest.fixture
def store():
    s = IncidentStore(":memory:")
    yield s
    s.close()


class TestBuildMemoryContext:
    def test_empty_store_returns_empty(self, store):
        result = build_memory_context(store, "pods crashing")
        assert result == ""

    def test_includes_similar_incidents(self, store):
        store.record_incident(
            query="pods crashing in default",
            tool_sequence=[{"name": "list_pods"}, {"name": "get_pod_logs"}],
            resolution="OOMKilled — increased memory",
            outcome="resolved", score=0.9,
        )
        result = build_memory_context(store, "pods crashing in production")
        assert "Past Similar Incidents" in result
        assert "list_pods" in result
        assert "resolved" in result

    def test_includes_runbooks(self, store):
        store.save_runbook(
            name="crashloop-fix",
            description="Fix CrashLoopBackOff",
            trigger_keywords="crash loop pod",
            tool_sequence=[{"name": "list_pods"}, {"name": "get_pod_logs"}, {"name": "describe_pod"}],
        )
        result = build_memory_context(store, "pod crash loop")
        assert "Learned Runbooks" in result
        assert "crashloop-fix" in result
        assert "list_pods -> get_pod_logs -> describe_pod" in result

    def test_includes_patterns(self, store):
        store.record_pattern(
            pattern_type="recurring",
            description="OOMKilled recurs weekly",
            keywords="oomkilled pods memory",
            incident_ids=[1, 2, 3],
        )
        result = build_memory_context(store, "pods oomkilled")
        assert "Detected Patterns" in result
        assert "OOMKilled recurs weekly" in result

    def test_truncates_to_max_chars(self, store):
        # Insert many incidents to exceed MAX_MEMORY_CHARS
        for i in range(20):
            store.record_incident(
                query=f"pods crashing incident number {i} with lots of detail",
                tool_sequence=[{"name": f"tool_{j}"} for j in range(10)],
                resolution="x" * 200,
                score=0.5,
            )
        result = build_memory_context(store, "pods crashing")
        assert len(result) <= MAX_MEMORY_CHARS + 200  # some overhead for headers

    def test_no_match_returns_empty(self, store):
        store.record_incident(query="node disk full", tool_sequence=[], resolution="cleared")
        result = build_memory_context(store, "RBAC permissions")
        assert result == ""
