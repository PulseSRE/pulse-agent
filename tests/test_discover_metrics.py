"""Tests for the discover_metrics tool."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from sre_agent.k8s_tools import discover_metrics


def _mock_prom_labels(metric_names: list[str]):
    """Mock Prometheus label values API response."""
    response_data = json.dumps({"status": "success", "data": metric_names}).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = response_data
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return patch("sre_agent.bearer_http.open_with_token", return_value=mock_resp)


class TestDiscoverMetrics:
    def setup_method(self):
        import sre_agent.k8s_tools as kt

        kt._metric_names_cache["data"] = None
        kt._metric_names_cache["ts"] = 0

    def test_cpu_category_filters(self):
        metrics = [
            "container_cpu_usage_seconds_total",
            "node_cpu_seconds_total",
            "container_memory_working_set_bytes",
            "kube_pod_info",
        ]
        with _mock_prom_labels(metrics):
            result = discover_metrics.call({"category": "cpu"})
        assert "container_cpu_usage_seconds_total" in result
        assert "node_cpu_seconds_total" in result
        assert "container_memory" not in result

    def test_memory_category_filters(self):
        metrics = [
            "container_cpu_usage_seconds_total",
            "container_memory_working_set_bytes",
            "node_memory_MemAvailable_bytes",
        ]
        with _mock_prom_labels(metrics):
            result = discover_metrics.call({"category": "memory"})
        assert "container_memory_working_set_bytes" in result
        assert "node_memory_MemAvailable_bytes" in result
        assert "cpu" not in result

    def test_all_category_returns_all(self):
        metrics = [
            "container_cpu_usage_seconds_total",
            "container_memory_working_set_bytes",
        ]
        with _mock_prom_labels(metrics):
            result = discover_metrics.call({"category": "all"})
        assert "container_cpu" in result
        assert "container_memory" in result

    def test_includes_recipe_when_available(self):
        metrics = ["container_cpu_usage_seconds_total"]
        with _mock_prom_labels(metrics):
            result = discover_metrics.call({"category": "cpu"})
        assert "Recipe:" in result

    def test_empty_prometheus_response(self):
        with _mock_prom_labels([]):
            result = discover_metrics.call({"category": "cpu"})
        assert "No metrics found" in result or "0" in result

    def test_invalid_category(self):
        metrics = ["container_cpu_usage_seconds_total"]
        with _mock_prom_labels(metrics):
            result = discover_metrics.call({"category": "nonexistent"})
        assert "Invalid category" in result

    def test_caching_second_call_uses_cache(self):
        metrics = ["container_cpu_usage_seconds_total"]
        with _mock_prom_labels(metrics) as mock_urlopen:
            discover_metrics.call({"category": "cpu"})
            discover_metrics.call({"category": "memory"})
        assert mock_urlopen.call_count == 1

    def test_prometheus_unreachable_falls_back_to_recipes(self):
        with patch("sre_agent.bearer_http.open_with_token", side_effect=Exception("connection refused")):
            result = discover_metrics.call({"category": "cpu"})
        assert "Cannot reach" in result
        assert "Recipe:" in result

    def test_default_category_is_all(self):
        metrics = ["container_cpu_usage_seconds_total", "etcd_server_has_leader"]
        with _mock_prom_labels(metrics):
            result = discover_metrics.call({})
        assert "container_cpu" in result
        assert "etcd_server" in result

    def test_caps_output_at_30_metrics(self):
        metrics = [f"node_metric_{i}" for i in range(50)]
        with _mock_prom_labels(metrics):
            result = discover_metrics.call({"category": "nodes"})
        assert "... and" in result or "more" in result
