"""Tests for memory/memory_tools.py — agent-callable memory tools."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from sre_agent.memory.memory_tools import (
    MEMORY_TOOLS,
    get_cluster_patterns,
    get_learned_runbooks,
    search_past_incidents,
    set_store,
)


@pytest.fixture(autouse=True)
def _reset_store():
    """Reset the module-level store before and after each test."""
    import sre_agent.memory.memory_tools as mt

    original = mt._store
    mt._store = None
    yield
    mt._store = original


class TestSearchPastIncidents:
    def test_no_store_returns_message(self):
        result = search_past_incidents.call({"query": "crashloop"})
        assert "not initialized" in result

    def test_no_results(self):
        store = MagicMock()
        store.search_incidents.return_value = []
        set_store(store)
        result = search_past_incidents.call({"query": "nothing"})
        assert "No similar past incidents" in result

    def test_returns_formatted_results(self):
        store = MagicMock()
        store.search_incidents.return_value = [
            {
                "id": 1,
                "timestamp": "2026-03-30T12:00:00",
                "query": "pod crashloop in monitoring",
                "tool_sequence": json.dumps([{"name": "list_pods"}, {"name": "get_pod_logs"}]),
                "outcome": "resolved",
                "score": 0.9,
                "resolution": "Restarted the pod and it recovered.",
            }
        ]
        set_store(store)
        result = search_past_incidents.call({"query": "crashloop", "limit": 5})
        assert "Incident #1" in result
        assert "list_pods" in result
        assert "resolved" in result
        store.search_incidents.assert_called_once_with("crashloop", limit=5)

    def test_limit_clamped(self):
        store = MagicMock()
        store.search_incidents.return_value = []
        set_store(store)
        search_past_incidents.call({"query": "test", "limit": 100})
        store.search_incidents.assert_called_once_with("test", limit=10)

    def test_limit_min_clamped(self):
        store = MagicMock()
        store.search_incidents.return_value = []
        set_store(store)
        search_past_incidents.call({"query": "test", "limit": -5})
        store.search_incidents.assert_called_once_with("test", limit=1)


class TestGetLearnedRunbooks:
    def test_no_store_returns_message(self):
        result = get_learned_runbooks.call({"query": ""})
        assert "not initialized" in result

    def test_no_runbooks(self):
        store = MagicMock()
        store.list_runbooks.return_value = []
        set_store(store)
        result = get_learned_runbooks.call({"query": ""})
        assert "No runbooks found" in result

    def test_list_runbooks(self):
        store = MagicMock()
        store.list_runbooks.return_value = [
            {
                "name": "Fix crashloop",
                "description": "Steps to fix crashlooping pods",
                "success_count": 5,
                "failure_count": 0,
                "tool_sequence": json.dumps(
                    [
                        {"name": "list_pods", "input_summary": {"namespace": "default"}},
                        {"name": "delete_pod", "input_summary": {"pod_name": "test"}},
                    ]
                ),
            }
        ]
        set_store(store)
        result = get_learned_runbooks.call({})
        assert "Fix crashloop" in result
        assert "list_pods" in result
        assert "success: 5" in result
        store.list_runbooks.assert_called_once_with(limit=10)

    def test_search_runbooks_with_query(self):
        store = MagicMock()
        store.find_runbooks.return_value = []
        set_store(store)
        get_learned_runbooks.call({"query": "crashloop"})
        store.find_runbooks.assert_called_once_with("crashloop", limit=5)


class TestGetClusterPatterns:
    def test_no_store_returns_message(self):
        result = get_cluster_patterns.call({})
        assert "not initialized" in result

    def test_no_patterns(self):
        store = MagicMock()
        store.list_patterns.return_value = []
        set_store(store)
        result = get_cluster_patterns.call({})
        assert "No patterns detected" in result

    def test_returns_formatted_patterns(self):
        store = MagicMock()
        store.list_patterns.return_value = [
            {
                "pattern_type": "recurring",
                "description": "Pods crash every Monday morning",
                "frequency": 4,
                "last_seen": "2026-03-30T10:00:00",
                "metadata": json.dumps({"day": "Monday"}),
            }
        ]
        set_store(store)
        result = get_cluster_patterns.call({})
        assert "RECURRING" in result
        assert "Monday" in result
        assert "Frequency: 4" in result

    def test_pattern_with_null_metadata(self):
        store = MagicMock()
        store.list_patterns.return_value = [
            {
                "pattern_type": "time_based",
                "description": "High CPU during business hours",
                "frequency": 10,
                "last_seen": "2026-03-30T15:00:00",
                "metadata": None,
            }
        ]
        set_store(store)
        result = get_cluster_patterns.call({})
        assert "TIME_BASED" in result
        assert "High CPU" in result


class TestMemoryToolsList:
    def test_all_tools_present(self):
        # 7, not 3: the four environment tools (migration 032) were defined but
        # never added to this list — and this list is the agent's only route to
        # them, so "remember that payments is owned by checkout" was silently
        # impossible. This count is the guard against that recurring.
        assert len(MEMORY_TOOLS) == 7
        names = {t.name for t in MEMORY_TOOLS}
        assert "search_past_incidents" in names
        assert "get_learned_runbooks" in names
        assert "get_cluster_patterns" in names
        assert "remember_environment_fact" in names
        assert "get_environment_facts" in names
        assert "compare_to_baseline" in names
        assert "search_conversations" in names
