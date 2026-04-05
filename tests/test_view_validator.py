"""Tests for view_validator — dashboard component validation before save."""

from __future__ import annotations

import copy

import pytest

from sre_agent.view_validator import validate_components

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

GOLDEN_DASHBOARD = [
    {
        "kind": "grid",
        "title": "Cluster KPIs",
        "columns": 4,
        "items": [
            {"kind": "metric_card", "title": "Nodes Ready", "value": "3/3", "query": "count(kube_node_info)"},
            {
                "kind": "metric_card",
                "title": "Pods Running",
                "value": "45",
                "query": "count(kube_pod_status_phase{phase='Running'})",
            },
            {
                "kind": "metric_card",
                "title": "CPU Usage",
                "value": "23%",
                "query": "100 - avg(rate(node_cpu_seconds_total{mode='idle'}[5m])) * 100",
            },
            {
                "kind": "metric_card",
                "title": "Memory Usage",
                "value": "61%",
                "query": "100 - (sum(node_memory_MemAvailable_bytes) / sum(node_memory_MemTotal_bytes)) * 100",
            },
        ],
    },
    {
        "kind": "chart",
        "title": "CPU by Namespace",
        "query": "sum by (namespace) (rate(container_cpu_usage_seconds_total[5m]))",
        "series": [{"name": "ns1", "data": [1, 2, 3]}],
    },
    {
        "kind": "chart",
        "title": "Memory by Namespace",
        "query": "sum by (namespace) (container_memory_working_set_bytes)",
        "series": [{"name": "ns1", "data": [100, 200, 300]}],
    },
    {
        "kind": "data_table",
        "title": "Pod Status",
        "columns": [{"id": "name", "header": "Name"}],
        "rows": [{"name": "pod-1"}],
    },
]


def _dash(*overrides: dict) -> list[dict]:
    """Return a deep copy of GOLDEN_DASHBOARD with top-level component overrides."""
    d = copy.deepcopy(GOLDEN_DASHBOARD)
    for i, ov in enumerate(overrides):
        if i < len(d):
            d[i].update(ov)
    return d


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestGoldenDashboard:
    def test_golden_dashboard_passes(self):
        result = validate_components(GOLDEN_DASHBOARD)
        assert result.valid is True
        assert result.errors == []
        assert len(result.components) == 4

    def test_unique_titles_pass(self):
        result = validate_components(GOLDEN_DASHBOARD)
        assert result.valid is True
        assert not any("uplicate" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


class TestDeduplication:
    def test_dedup_by_query(self):
        d = copy.deepcopy(GOLDEN_DASHBOARD)
        dup = copy.deepcopy(d[1])
        dup["title"] = "CPU by Namespace (copy)"
        d.append(dup)
        result = validate_components(d)
        assert result.deduped_count == 1

    def test_dedup_by_title_and_kind(self):
        d = copy.deepcopy(GOLDEN_DASHBOARD)
        dup = copy.deepcopy(d[1])
        dup["query"] = "different_query"
        d.append(dup)
        result = validate_components(d)
        assert result.deduped_count == 1

    def test_dedup_preserves_order(self):
        d = copy.deepcopy(GOLDEN_DASHBOARD)
        dup = copy.deepcopy(d[1])
        dup["title"] = "CPU by Namespace (copy)"
        d.append(dup)
        result = validate_components(d)
        # First occurrence (index 1) kept — its title is "CPU by Namespace"
        charts = [c for c in result.components if c["kind"] == "chart"]
        assert charts[0]["title"] == "CPU by Namespace"


# ---------------------------------------------------------------------------
# Generic / bad titles
# ---------------------------------------------------------------------------


class TestGenericTitles:
    @pytest.mark.parametrize(
        "title",
        [
            "Chart",
            "chart",
            "Table",
            "Metric Card",
            "metric",
            "Card",
            "Widget",
            "Component",
            "Data Table",
            "Status List",
            "Info Card",
        ],
    )
    def test_generic_titles_rejected(self, title: str):
        d = _dash({}, {"title": title})  # Set generic title on chart (index 1), not grid
        result = validate_components(d)
        assert result.valid is False
        assert any("generic" in e.lower() or "title" in e.lower() for e in result.errors)

    @pytest.mark.parametrize("title", ["Chart 1", "Table 2", "metric card 3", "Widget 99", "Component 0"])
    def test_numbered_generic_rejected(self, title: str):
        d = _dash({}, {"title": title})  # Set on chart (index 1), not grid
        result = validate_components(d)
        assert result.valid is False

    def test_kind_as_title_rejected(self):
        d = copy.deepcopy(GOLDEN_DASHBOARD)
        d[3]["title"] = "data table"
        result = validate_components(d)
        assert result.valid is False
        assert any("generic" in e.lower() or "title" in e.lower() for e in result.errors)


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


class TestSchemaValidation:
    def test_missing_kind_rejected(self):
        d = copy.deepcopy(GOLDEN_DASHBOARD)
        del d[1]["kind"]
        result = validate_components(d)
        assert result.valid is False
        assert any("kind" in e.lower() for e in result.errors)

    def test_missing_title_rejected(self):
        d = copy.deepcopy(GOLDEN_DASHBOARD)
        del d[1]["title"]
        result = validate_components(d)
        assert result.valid is False
        assert any("title" in e.lower() for e in result.errors)

    def test_invalid_kind_rejected(self):
        d = copy.deepcopy(GOLDEN_DASHBOARD)
        d[1]["kind"] = "foobar"
        result = validate_components(d)
        assert result.valid is False
        assert any("kind" in e.lower() for e in result.errors)

    def test_chart_needs_series_or_query(self):
        d = copy.deepcopy(GOLDEN_DASHBOARD)
        del d[1]["series"]
        del d[1]["query"]
        result = validate_components(d)
        assert result.valid is False
        assert any("series" in e.lower() or "query" in e.lower() for e in result.errors)

    def test_chart_with_query_passes(self):
        d = copy.deepcopy(GOLDEN_DASHBOARD)
        del d[1]["series"]
        # query is still present
        result = validate_components(d)
        assert result.valid is True

    def test_metric_card_needs_value_or_query(self):
        comp = [
            {
                "kind": "metric_card",
                "title": "Bad Metric",
            },
            copy.deepcopy(GOLDEN_DASHBOARD[1]),
            copy.deepcopy(GOLDEN_DASHBOARD[2]),
            copy.deepcopy(GOLDEN_DASHBOARD[3]),
        ]
        result = validate_components(comp)
        assert result.valid is False
        assert any("value" in e.lower() or "query" in e.lower() for e in result.errors)

    def test_table_needs_columns_and_rows(self):
        d = copy.deepcopy(GOLDEN_DASHBOARD)
        del d[3]["columns"]
        result = validate_components(d)
        assert result.valid is False
        assert any("columns" in e.lower() for e in result.errors)

    def test_table_empty_rows_ok(self):
        d = copy.deepcopy(GOLDEN_DASHBOARD)
        d[3]["rows"] = []
        result = validate_components(d)
        assert result.valid is True


# ---------------------------------------------------------------------------
# Widget count
# ---------------------------------------------------------------------------


class TestWidgetCount:
    def test_min_widget_count(self):
        components = [
            {"kind": "metric_card", "title": "A Metric", "value": "1"},
            {"kind": "chart", "title": "A Chart", "series": [{"name": "x", "data": [1]}]},
        ]
        result = validate_components(components)
        assert result.valid is False
        assert any("min" in e.lower() or "at least" in e.lower() for e in result.errors)

    def test_max_widget_count(self):
        base = copy.deepcopy(GOLDEN_DASHBOARD)
        for i in range(5):
            base.append({"kind": "chart", "title": f"Extra Chart {chr(65 + i)}", "query": f"query_{i}"})
        result = validate_components(base)
        assert result.valid is False
        assert any("max" in e.lower() or "exceed" in e.lower() or "at most" in e.lower() for e in result.errors)

    def test_max_widget_configurable(self):
        base = copy.deepcopy(GOLDEN_DASHBOARD)
        for i in range(5):
            base.append({"kind": "chart", "title": f"Extra Chart {chr(65 + i)}", "query": f"query_{i}"})
        result = validate_components(base, max_widgets=10)
        assert not any("max" in e.lower() or "exceed" in e.lower() or "at most" in e.lower() for e in result.errors)


# ---------------------------------------------------------------------------
# Required structure
# ---------------------------------------------------------------------------


class TestRequiredStructure:
    def test_requires_metric_source(self):
        components = [
            {"kind": "chart", "title": "A Chart", "query": "up"},
            {"kind": "chart", "title": "B Chart", "query": "up2"},
            {"kind": "data_table", "title": "A Table", "columns": [{"id": "x"}], "rows": []},
        ]
        result = validate_components(components)
        assert result.valid is False
        assert any("metric" in e.lower() for e in result.errors)

    def test_requires_chart(self):
        components = [
            {
                "kind": "grid",
                "title": "KPIs",
                "items": [
                    {"kind": "metric_card", "title": "M1", "value": "1"},
                ],
            },
            {"kind": "metric_card", "title": "M2", "value": "2"},
            {"kind": "data_table", "title": "A Table", "columns": [{"id": "x"}], "rows": []},
        ]
        result = validate_components(components)
        assert result.valid is False
        assert any("chart" in e.lower() for e in result.errors)

    def test_requires_table(self):
        components = [
            {
                "kind": "grid",
                "title": "KPIs",
                "items": [
                    {"kind": "metric_card", "title": "M1", "value": "1"},
                ],
            },
            {"kind": "chart", "title": "A Chart", "query": "up"},
            {"kind": "chart", "title": "B Chart", "query": "up2"},
        ]
        result = validate_components(components)
        assert result.valid is False
        assert any("table" in e.lower() or "data_table" in e.lower() for e in result.errors)


# ---------------------------------------------------------------------------
# PromQL validation (warnings only)
# ---------------------------------------------------------------------------


class TestPromQL:
    def test_promql_valid_passes(self):
        result = validate_components(GOLDEN_DASHBOARD)
        assert result.warnings == []

    def test_promql_unbalanced_braces(self):
        d = copy.deepcopy(GOLDEN_DASHBOARD)
        d[1]["query"] = "sum by (namespace) (rate(container_cpu{bad[5m])"
        result = validate_components(d)
        assert any("brace" in w.lower() or "{" in w for w in result.warnings)

    def test_promql_double_braces(self):
        d = copy.deepcopy(GOLDEN_DASHBOARD)
        d[1]["query"] = "metric{a='1'}{b='2'}"
        result = validate_components(d)
        assert any("double" in w.lower() or "}{" in w for w in result.warnings)

    def test_promql_unbalanced_parens(self):
        d = copy.deepcopy(GOLDEN_DASHBOARD)
        d[1]["query"] = "sum(rate(metric[5m])"
        result = validate_components(d)
        assert any("paren" in w.lower() or "(" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_list_invalid(self):
        result = validate_components([])
        assert result.valid is False

    def test_nested_grid_items_validated(self):
        d = copy.deepcopy(GOLDEN_DASHBOARD)
        d[0]["items"][0]["kind"] = "foobar"
        result = validate_components(d)
        assert result.valid is False
        assert any("kind" in e.lower() for e in result.errors)

    def test_duplicate_titles_error(self):
        d = copy.deepcopy(GOLDEN_DASHBOARD)
        d[1]["title"] = "Pod Status"  # same as d[3]
        # Also change query so dedup doesn't remove it
        d[1]["query"] = "different_query_here"
        result = validate_components(d)
        assert result.valid is False
        assert any("uplicate" in e.lower() or "unique" in e.lower() for e in result.errors)
