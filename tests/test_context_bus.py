"""Tests for the shared context bus."""

import threading
import time

import pytest

from sre_agent.context_bus import ContextBus, ContextEntry, get_context_bus


class TestContextBusPublishAndRetrieve:
    def test_publish_and_get(self):
        bus = ContextBus()
        entry = ContextEntry(
            source="monitor",
            category="finding",
            summary="Test finding",
            details={"severity": "critical"},
            namespace="default",
        )
        bus.publish(entry)
        results = bus.get_context_for()
        assert len(results) == 1
        assert results[0].source == "monitor"
        assert results[0].summary == "Test finding"

    def test_multiple_entries_returned_newest_first(self):
        bus = ContextBus()
        for i in range(3):
            bus.publish(ContextEntry(
                source="monitor",
                category="finding",
                summary=f"Finding {i}",
                details={},
                timestamp=time.time() + i,
            ))
        results = bus.get_context_for(limit=10)
        assert len(results) == 3
        # Newest first
        assert results[0].summary == "Finding 2"
        assert results[2].summary == "Finding 0"


class TestContextBusTTL:
    def test_expired_entries_excluded(self):
        bus = ContextBus(ttl_seconds=1)
        bus.publish(ContextEntry(
            source="monitor",
            category="finding",
            summary="Old finding",
            details={},
            timestamp=time.time() - 10,  # 10 seconds ago, TTL is 1s
        ))
        bus.publish(ContextEntry(
            source="sre_agent",
            category="diagnosis",
            summary="Fresh finding",
            details={},
        ))
        results = bus.get_context_for()
        assert len(results) == 1
        assert results[0].summary == "Fresh finding"

    def test_all_expired_returns_empty(self):
        bus = ContextBus(ttl_seconds=1)
        bus.publish(ContextEntry(
            source="monitor",
            category="finding",
            summary="Expired",
            details={},
            timestamp=time.time() - 5,
        ))
        results = bus.get_context_for()
        assert len(results) == 0


class TestContextBusNamespaceFiltering:
    def test_filter_by_namespace(self):
        bus = ContextBus()
        bus.publish(ContextEntry(
            source="monitor", category="finding", summary="In prod",
            details={}, namespace="production",
        ))
        bus.publish(ContextEntry(
            source="monitor", category="finding", summary="In staging",
            details={}, namespace="staging",
        ))
        bus.publish(ContextEntry(
            source="monitor", category="finding", summary="Cluster-wide",
            details={}, namespace="",
        ))
        results = bus.get_context_for(namespace="production")
        # Should include "production" entries and cluster-wide (empty namespace)
        summaries = {r.summary for r in results}
        assert "In prod" in summaries
        assert "Cluster-wide" in summaries
        assert "In staging" not in summaries

    def test_filter_by_category(self):
        bus = ContextBus()
        bus.publish(ContextEntry(
            source="monitor", category="finding", summary="A finding",
            details={},
        ))
        bus.publish(ContextEntry(
            source="monitor", category="fix", summary="A fix",
            details={},
        ))
        results = bus.get_context_for(category="fix")
        assert len(results) == 1
        assert results[0].summary == "A fix"


class TestContextBusMaxEntries:
    def test_max_entries_cap(self):
        bus = ContextBus(max_entries=5)
        for i in range(10):
            bus.publish(ContextEntry(
                source="monitor", category="finding", summary=f"Finding {i}",
                details={},
            ))
        results = bus.get_context_for(limit=100)
        assert len(results) == 5
        # Should have the last 5 entries (5-9)
        summaries = [r.summary for r in results]
        for i in range(5, 10):
            assert f"Finding {i}" in summaries

    def test_limit_parameter(self):
        bus = ContextBus()
        for i in range(10):
            bus.publish(ContextEntry(
                source="monitor", category="finding", summary=f"Finding {i}",
                details={},
            ))
        results = bus.get_context_for(limit=3)
        assert len(results) == 3


class TestContextBusThreadSafety:
    def test_concurrent_publish(self):
        bus = ContextBus(max_entries=500)
        errors = []

        def publish_batch(start: int):
            try:
                for i in range(50):
                    bus.publish(ContextEntry(
                        source="monitor",
                        category="finding",
                        summary=f"Thread {start} finding {i}",
                        details={},
                    ))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=publish_batch, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        results = bus.get_context_for(limit=500)
        assert len(results) == 250  # 5 threads * 50 entries each

    def test_concurrent_publish_and_read(self):
        bus = ContextBus(max_entries=500)
        errors = []
        read_results = []

        def publisher():
            try:
                for i in range(100):
                    bus.publish(ContextEntry(
                        source="monitor", category="finding",
                        summary=f"Finding {i}", details={},
                    ))
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for _ in range(50):
                    results = bus.get_context_for(limit=10)
                    read_results.append(len(results))
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=publisher)
        t2 = threading.Thread(target=reader)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert len(errors) == 0


class TestBuildContextPrompt:
    def test_empty_bus_returns_empty_string(self):
        bus = ContextBus()
        assert bus.build_context_prompt() == ""

    def test_prompt_contains_header(self):
        bus = ContextBus()
        bus.publish(ContextEntry(
            source="monitor", category="finding", summary="Test",
            details={},
        ))
        prompt = bus.build_context_prompt()
        assert "## Recent Agent Activity (shared context)" in prompt

    def test_prompt_contains_entry_details(self):
        bus = ContextBus()
        bus.publish(ContextEntry(
            source="sre_agent", category="investigation",
            summary="Investigated crashloop",
            details={
                "suspected_cause": "OOM in container",
                "fix_applied": "restart_deployment",
            },
        ))
        prompt = bus.build_context_prompt()
        assert "[sre_agent]" in prompt
        assert "Investigated crashloop" in prompt
        assert "Suspected cause: OOM in container" in prompt
        assert "Fix applied: restart_deployment" in prompt

    def test_prompt_age_seconds(self):
        bus = ContextBus()
        bus.publish(ContextEntry(
            source="monitor", category="finding", summary="Recent",
            details={}, timestamp=time.time() - 30,
        ))
        prompt = bus.build_context_prompt()
        assert "30s ago" in prompt

    def test_prompt_age_minutes(self):
        bus = ContextBus()
        bus.publish(ContextEntry(
            source="monitor", category="finding", summary="Older",
            details={}, timestamp=time.time() - 120,
        ))
        prompt = bus.build_context_prompt()
        assert "2m ago" in prompt

    def test_prompt_respects_namespace_filter(self):
        bus = ContextBus()
        bus.publish(ContextEntry(
            source="monitor", category="finding", summary="In prod",
            details={}, namespace="production",
        ))
        bus.publish(ContextEntry(
            source="monitor", category="finding", summary="In staging",
            details={}, namespace="staging",
        ))
        prompt = bus.build_context_prompt(namespace="production")
        assert "In prod" in prompt
        assert "In staging" not in prompt


class TestSingleton:
    def test_get_context_bus_returns_same_instance(self):
        bus1 = get_context_bus()
        bus2 = get_context_bus()
        assert bus1 is bus2
