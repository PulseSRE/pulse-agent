"""Tests for the SQLite incident store."""

import json
import pytest

from sre_agent.memory.store import IncidentStore, extract_keywords


@pytest.fixture
def store():
    s = IncidentStore(":memory:")
    yield s
    s.close()


class TestExtractKeywords:
    def test_removes_stop_words(self):
        assert "pods" in extract_keywords("why are the pods crashing")
        assert "the" not in extract_keywords("why are the pods crashing")
        assert "are" not in extract_keywords("why are the pods crashing")

    def test_lowercases(self):
        assert extract_keywords("CrashLoopBackOff") == "crashloopbackoff"

    def test_strips_punctuation(self):
        assert extract_keywords("pods?") == "pods"
        assert extract_keywords("(namespace)") == "namespace"

    def test_empty(self):
        assert extract_keywords("") == ""
        assert extract_keywords("the a an") == ""


class TestIncidentStore:
    def test_record_and_search(self, store):
        iid = store.record_incident(
            query="pods crashing in monitoring namespace",
            tool_sequence=[{"name": "list_pods"}, {"name": "get_pod_logs"}],
            resolution="Found OOMKilled, increased memory limits",
            outcome="resolved",
            namespace="monitoring",
            error_type="OOMKilled",
            score=0.85,
        )
        assert iid == 1
        results = store.search_incidents("pods crashing", limit=5)
        assert len(results) == 1
        assert results[0]["namespace"] == "monitoring"

    def test_search_no_match(self, store):
        store.record_incident(query="node disk full", tool_sequence=[], resolution="cleared logs")
        results = store.search_incidents("pods crashing")
        assert len(results) == 0

    def test_search_ranks_by_score(self, store):
        store.record_incident(query="pods crashing", tool_sequence=[], resolution="low score", score=0.3)
        store.record_incident(query="pods crashing", tool_sequence=[], resolution="high score", score=0.9)
        results = store.search_incidents("pods crashing")
        assert results[0]["score"] > results[1]["score"]

    def test_update_outcome(self, store):
        iid = store.record_incident(query="test", tool_sequence=[], resolution="r", outcome="unknown")
        store.update_incident_outcome(iid, "resolved", 0.95)
        results = store.search_incidents("test")
        assert results[0]["outcome"] == "resolved"
        assert results[0]["score"] == 0.95

    def test_incident_count(self, store):
        assert store.get_incident_count() == 0
        store.record_incident(query="a", tool_sequence=[], resolution="r")
        store.record_incident(query="b", tool_sequence=[], resolution="r")
        assert store.get_incident_count() == 2


class TestRunbookStore:
    def test_save_and_find(self, store):
        rid = store.save_runbook(
            name="crashloop-runbook",
            description="Debug CrashLoopBackOff",
            trigger_keywords="crash loop pod",
            tool_sequence=[{"name": "list_pods"}, {"name": "get_pod_logs"}],
        )
        assert rid == 1
        results = store.find_runbooks("pod crash loop")
        assert len(results) == 1
        assert results[0]["name"] == "crashloop-runbook"

    def test_list_runbooks(self, store):
        store.save_runbook(name="rb1", description="d", trigger_keywords="kw", tool_sequence=[])
        store.save_runbook(name="rb2", description="d", trigger_keywords="kw", tool_sequence=[])
        assert len(store.list_runbooks()) == 2

    def test_find_no_match(self, store):
        store.save_runbook(name="rb", description="d", trigger_keywords="node disk", tool_sequence=[])
        assert len(store.find_runbooks("pod crash")) == 0


class TestPatternStore:
    def test_record_and_list(self, store):
        pid = store.record_pattern(
            pattern_type="recurring",
            description="OOMKilled in monitoring",
            keywords="oomkilled monitoring",
            incident_ids=[1, 2, 3],
        )
        assert pid == 1
        patterns = store.list_patterns()
        assert len(patterns) == 1
        assert patterns[0]["pattern_type"] == "recurring"

    def test_search_patterns(self, store):
        store.record_pattern(
            pattern_type="time_based",
            description="CrashLoop on Tuesdays",
            keywords="crashloop tuesday",
            incident_ids=[1, 2],
            metadata={"day_of_week": "Tuesday", "hour": 3},
        )
        results = store.search_patterns("crashloop")
        assert len(results) == 1
        assert "Tuesday" in results[0]["description"]


class TestMetricsStore:
    def test_record_and_summary(self, store):
        store.record_metric("interaction_score", 0.8)
        store.record_metric("interaction_score", 0.9)
        store.record_metric("tool_count", 5.0)
        summary = store.get_metrics_summary()
        assert "interaction_score" in summary
        assert summary["interaction_score"]["count"] == 2
        assert abs(summary["interaction_score"]["avg"] - 0.85) < 0.01
