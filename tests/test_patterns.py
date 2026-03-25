"""Tests for pattern recognition."""

from datetime import datetime, timezone, timedelta

import pytest
from sre_agent.memory.store import IncidentStore
from sre_agent.memory.patterns import detect_patterns


@pytest.fixture
def store():
    s = IncidentStore(":memory:")
    yield s
    s.close()


class TestDetectPatterns:
    def test_needs_minimum_incidents(self, store):
        store.record_incident(query="a", tool_sequence=[], resolution="r")
        store.record_incident(query="b", tool_sequence=[], resolution="r")
        assert detect_patterns(store) == []

    def test_detects_recurring_keywords(self, store):
        for i in range(5):
            store.record_incident(
                query=f"pods crashing in monitoring namespace attempt {i}",
                tool_sequence=[{"name": "list_pods"}],
                resolution="fixed",
            )
        patterns = detect_patterns(store)
        assert len(patterns) > 0
        assert any(p["type"] == "recurring" for p in patterns)

    def test_no_false_patterns(self, store):
        store.record_incident(query="node disk full", tool_sequence=[], resolution="r")
        store.record_incident(query="pod crash loop", tool_sequence=[], resolution="r")
        store.record_incident(query="service timeout", tool_sequence=[], resolution="r")
        patterns = detect_patterns(store)
        assert len(patterns) == 0

    def test_does_not_duplicate_patterns(self, store):
        for i in range(5):
            store.record_incident(
                query=f"pods crashing attempt {i}",
                tool_sequence=[], resolution="r",
            )
        p1 = detect_patterns(store)
        p2 = detect_patterns(store)
        # Second run should not create duplicates
        assert len(p2) == 0
