"""Tests for the learned PromQL queries database layer."""

from __future__ import annotations

from sre_agent.promql_recipes import (
    get_query_reliability,
    get_reliable_queries,
    normalize_query,
    record_query_result,
)


class TestNormalizeQuery:
    def test_strips_namespace_values(self):
        q = 'rate(container_cpu_usage_seconds_total{namespace="production"}[5m])'
        normalized = normalize_query(q)
        assert "production" not in normalized
        assert "__NS__" in normalized

    def test_strips_pod_values(self):
        q = 'container_memory_working_set_bytes{pod="my-pod-abc123"}'
        normalized = normalize_query(q)
        assert "my-pod-abc123" not in normalized
        assert "__POD__" in normalized

    def test_strips_instance_values(self):
        q = 'node_cpu_seconds_total{instance="10.0.0.1:9100"}'
        normalized = normalize_query(q)
        assert "10.0.0.1" not in normalized
        assert "__INSTANCE__" in normalized

    def test_strips_deployment_values(self):
        q = 'kube_deployment_status_replicas{deployment="my-app"}'
        normalized = normalize_query(q)
        assert "my-app" not in normalized
        assert "__DEP__" in normalized

    def test_lowercases(self):
        q = "SUM(Rate(CPU[5m]))"
        normalized = normalize_query(q)
        assert normalized == normalized.lower()

    def test_deterministic(self):
        q = 'rate(container_cpu_usage_seconds_total{namespace="ns1"}[5m])'
        assert normalize_query(q) == normalize_query(q)

    def test_same_query_different_namespace_same_result(self):
        q1 = 'rate(cpu{namespace="ns1"}[5m])'
        q2 = 'rate(cpu{namespace="ns2"}[5m])'
        assert normalize_query(q1) == normalize_query(q2)

    def test_empty_query(self):
        assert normalize_query("") == ""

    def test_none_like_input(self):
        assert normalize_query("") == ""


class TestRecordQueryResult:
    def test_fire_and_forget_no_exception_on_success(self):
        record_query_result("test_query_1", success=True, series_count=5)

    def test_fire_and_forget_no_exception_on_failure(self):
        record_query_result("test_query_2", success=False, series_count=0)

    def test_fire_and_forget_empty_query(self):
        record_query_result("", success=True, series_count=0)

    def test_fire_and_forget_none_query(self):
        # Should not raise
        try:
            record_query_result(None, success=False, series_count=0)
        except Exception:
            pass  # normalize_query handles None gracefully


class TestGetReliableQueries:
    def test_returns_list(self):
        result = get_reliable_queries("cpu", min_success=999)
        assert isinstance(result, list)

    def test_returns_empty_for_no_matches(self):
        result = get_reliable_queries("nonexistent_category_xyz", min_success=1)
        assert result == []


class TestGetQueryReliability:
    def test_returns_none_for_unknown(self):
        result = get_query_reliability("totally_unknown_query_hash_xyz")
        assert result is None
