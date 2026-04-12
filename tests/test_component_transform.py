"""Tests for component transformation engine."""

from __future__ import annotations

import pytest

from sre_agent.component_transform import can_transform, list_transformations, transform


class TestTransformMatrix:
    def test_table_to_chart(self):
        assert can_transform("data_table", "chart")

    def test_table_to_bar_list(self):
        assert can_transform("data_table", "bar_list")

    def test_table_to_metric_card(self):
        assert can_transform("data_table", "metric_card")

    def test_chart_to_table(self):
        assert can_transform("chart", "data_table")

    def test_chart_to_metric_card(self):
        assert can_transform("chart", "metric_card")

    def test_metric_card_to_chart(self):
        assert can_transform("metric_card", "chart")

    def test_status_list_to_table(self):
        assert can_transform("status_list", "data_table")

    def test_bar_list_to_table(self):
        assert can_transform("bar_list", "data_table")

    def test_resource_counts_to_table(self):
        assert can_transform("resource_counts", "data_table")

    def test_progress_list_to_table(self):
        assert can_transform("progress_list", "data_table")

    def test_invalid_transform_raises(self):
        with pytest.raises(ValueError, match="No transformation"):
            transform({"kind": "log_viewer"}, "chart")

    def test_same_kind_returns_copy(self):
        spec = {"kind": "chart", "title": "CPU", "query": "rate(...)"}
        result = transform(spec, "chart")
        assert result == spec
        assert result is not spec

    def test_list_transformations(self):
        targets = list_transformations("data_table")
        assert "chart" in targets
        assert "bar_list" in targets
        assert "metric_card" in targets


class TestTableToChart:
    def test_basic(self):
        spec = {
            "kind": "data_table",
            "title": "Pods",
            "columns": [{"id": "name", "header": "Name"}, {"id": "restarts", "header": "Restarts"}],
            "rows": [
                {"name": "nginx", "restarts": 0},
                {"name": "api", "restarts": 12},
                {"name": "redis", "restarts": 1},
            ],
        }
        result = transform(spec, "chart")
        assert result["kind"] == "chart"
        assert result["title"] == "Pods"
        assert result["chartType"] == "bar"
        assert len(result["series"]) == 1
        assert result["series"][0]["name"] == "Restarts"

    def test_preserves_title(self):
        spec = {
            "kind": "data_table",
            "title": "My Table",
            "columns": [{"id": "x", "header": "X"}],
            "rows": [{"x": "a"}],
        }
        result = transform(spec, "chart")
        assert result["title"] == "My Table"

    def test_empty_table(self):
        spec = {"kind": "data_table", "columns": [], "rows": []}
        result = transform(spec, "chart")
        assert result["series"] == []


class TestTableToBarList:
    def test_basic(self):
        spec = {
            "kind": "data_table",
            "title": "Top Pods",
            "columns": [{"id": "name", "header": "Name"}, {"id": "cpu", "header": "CPU"}],
            "rows": [
                {"name": "nginx", "cpu": 450},
                {"name": "api", "cpu": 800},
                {"name": "redis", "cpu": 200},
            ],
        }
        result = transform(spec, "bar_list")
        assert result["kind"] == "bar_list"
        assert len(result["items"]) == 3
        # Should be sorted descending
        assert result["items"][0]["label"] == "api"
        assert result["items"][0]["value"] == 800


class TestTableToMetricCard:
    def test_count(self):
        spec = {
            "kind": "data_table",
            "title": "Pods",
            "columns": [{"id": "name", "header": "Name"}],
            "rows": [{"name": "a"}, {"name": "b"}, {"name": "c"}],
        }
        result = transform(spec, "metric_card")
        assert result["kind"] == "metric_card"
        assert result["value"] == "3"

    def test_sum(self):
        spec = {
            "kind": "data_table",
            "columns": [{"id": "name", "header": "Name"}, {"id": "restarts", "header": "Restarts"}],
            "rows": [{"name": "a", "restarts": 5}, {"name": "b", "restarts": 3}],
        }
        result = transform(spec, "metric_card", options={"aggregation": "sum", "value_column": "restarts"})
        assert result["value"] == "8"


class TestChartToTable:
    def test_bar_chart(self):
        spec = {
            "kind": "chart",
            "title": "CPU by Pod",
            "series": [{"name": "CPU", "data": [{"label": "nginx", "value": 0.45}, {"label": "api", "value": 0.82}]}],
        }
        result = transform(spec, "data_table")
        assert result["kind"] == "data_table"
        assert len(result["columns"]) == 2
        assert len(result["rows"]) == 2

    def test_time_series(self):
        spec = {
            "kind": "chart",
            "series": [{"name": "CPU", "data": [{"timestamp": 1000, "value": 0.5}, {"timestamp": 2000, "value": 0.7}]}],
        }
        result = transform(spec, "data_table")
        assert result["columns"][0]["id"] == "timestamp"
        assert len(result["rows"]) == 2


class TestChartToMetricCard:
    def test_takes_latest(self):
        spec = {
            "kind": "chart",
            "title": "CPU",
            "series": [{"name": "CPU", "data": [{"value": 0.5}, {"value": 0.7}, {"value": 0.9}]}],
        }
        result = transform(spec, "metric_card")
        assert result["value"] == "0.90"

    def test_empty_series(self):
        spec = {"kind": "chart", "series": []}
        result = transform(spec, "metric_card")
        assert result["value"] == "n/a"


class TestMetricCardToChart:
    def test_uses_query(self):
        spec = {"kind": "metric_card", "title": "CPU", "value": "72%", "query": "rate(cpu[5m])"}
        result = transform(spec, "chart")
        assert result["kind"] == "chart"
        assert result["query"] == "rate(cpu[5m])"
        assert result["chartType"] == "line"


class TestStatusListToTable:
    def test_basic(self):
        spec = {
            "kind": "status_list",
            "title": "Alerts",
            "items": [
                {"label": "CPUThrottling", "status": "warning", "detail": "pod/api"},
                {"label": "MemoryPressure", "status": "error", "detail": "node/worker-2"},
            ],
        }
        result = transform(spec, "data_table")
        assert len(result["rows"]) == 2
        assert result["rows"][0]["label"] == "CPUThrottling"
        assert result["rows"][0]["status"] == "warning"


class TestResourceCountsToTable:
    def test_basic(self):
        spec = {
            "kind": "resource_counts",
            "items": [
                {"resource": "pods", "count": 42, "status": "healthy"},
                {"resource": "deployments", "count": 12, "status": "warning"},
            ],
        }
        result = transform(spec, "data_table")
        assert len(result["rows"]) == 2
        assert result["rows"][0]["resource"] == "pods"
        assert result["rows"][0]["count"] == 42


class TestProgressListToTable:
    def test_basic(self):
        spec = {
            "kind": "progress_list",
            "items": [
                {"label": "worker-1", "value": 70, "max": 100},
                {"label": "worker-2", "value": 90, "max": 100},
            ],
        }
        result = transform(spec, "data_table")
        assert len(result["rows"]) == 2
        assert result["rows"][0]["pct"] == "70%"
        assert result["rows"][1]["pct"] == "90%"
