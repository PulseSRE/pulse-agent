"""Tests for quality_engine — merged validation + scoring."""

from __future__ import annotations

import copy

import pytest

from sre_agent.quality_engine import QualityResult, evaluate_components, is_generic_title

# ---------------------------------------------------------------------------
# Golden dashboard fixture
# ---------------------------------------------------------------------------

GOLDEN = [
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
            {"kind": "metric_card", "title": "CPU Usage", "value": "23%", "query": "avg(rate(node_cpu[5m]))"},
            {"kind": "metric_card", "title": "Memory", "value": "61%", "query": "avg(node_memory_usage)"},
        ],
    },
    {
        "kind": "chart",
        "title": "CPU by Namespace",
        "description": "Watch for spikes",
        "query": "sum by (ns) (rate(cpu[5m]))",
        "series": [{"name": "ns1", "data": [1, 2, 3]}],
    },
    {
        "kind": "chart",
        "title": "Memory by Namespace",
        "description": "Watch for growth",
        "query": "sum by (ns) (memory_bytes)",
        "series": [{"name": "ns1", "data": [100, 200]}],
    },
    {
        "kind": "data_table",
        "title": "Pod Status",
        "columns": [{"id": "name", "header": "Name"}],
        "rows": [{"name": "pod-1"}],
    },
]


# ---------------------------------------------------------------------------
# Basic validation (from view_validator)
# ---------------------------------------------------------------------------


class TestValidation:
    def test_golden_is_valid(self):
        r = evaluate_components(GOLDEN)
        assert r.valid is True
        assert r.errors == []

    def test_empty_list_invalid(self):
        r = evaluate_components([])
        assert r.valid is False

    def test_missing_kind_error(self):
        d = copy.deepcopy(GOLDEN)
        del d[1]["kind"]
        r = evaluate_components(d)
        assert r.valid is False
        assert any("kind" in e.lower() for e in r.errors)

    def test_generic_title_error(self):
        d = copy.deepcopy(GOLDEN)
        d[1]["title"] = "Chart"
        r = evaluate_components(d)
        assert r.valid is False
        assert any("generic" in e.lower() for e in r.errors)

    def test_duplicate_title_error(self):
        d = copy.deepcopy(GOLDEN)
        d[1]["title"] = "Pod Status"
        d[1]["query"] = "different_query"
        r = evaluate_components(d)
        assert r.valid is False
        assert any("uplicate" in e.lower() for e in r.errors)

    def test_deduplication(self):
        d = copy.deepcopy(GOLDEN)
        dup = copy.deepcopy(d[1])
        dup["title"] = "CPU by Namespace (copy)"
        d.append(dup)
        r = evaluate_components(d)
        assert r.deduped_count == 1


# ---------------------------------------------------------------------------
# New component type validation
# ---------------------------------------------------------------------------


class TestNewComponentValidation:
    """Test per-component validation for new types using _validate_component."""

    def _errors(self, comp: dict) -> list[str]:
        from sre_agent.quality_engine import QualityResult, _validate_component

        r = QualityResult()
        _validate_component(comp, r)
        return r.errors

    def test_bar_list_valid(self):
        assert self._errors({"kind": "bar_list", "items": [{"label": "foo", "value": 42}]}) == []

    def test_bar_list_empty_items(self):
        errs = self._errors({"kind": "bar_list", "items": []})
        assert any("at least 1 item" in e for e in errs)

    def test_bar_list_missing_label(self):
        errs = self._errors({"kind": "bar_list", "items": [{"value": 42}]})
        assert any("label" in e.lower() for e in errs)

    def test_bar_list_missing_value(self):
        errs = self._errors({"kind": "bar_list", "items": [{"label": "x"}]})
        assert any("value" in e.lower() for e in errs)

    def test_progress_list_valid(self):
        assert self._errors({"kind": "progress_list", "items": [{"label": "n", "value": 70, "max": 100}]}) == []

    def test_progress_list_max_zero(self):
        errs = self._errors({"kind": "progress_list", "items": [{"label": "x", "value": 50, "max": 0}]})
        assert any("max" in e.lower() and "> 0" in e for e in errs)

    def test_progress_list_max_negative(self):
        errs = self._errors({"kind": "progress_list", "items": [{"label": "x", "value": 50, "max": -5}]})
        assert len(errs) > 0

    def test_stat_card_valid(self):
        assert self._errors({"kind": "stat_card", "title": "CPU", "value": "42%"}) == []

    def test_stat_card_missing_value(self):
        errs = self._errors({"kind": "stat_card", "title": "CPU"})
        assert any("value" in e.lower() for e in errs)

    def test_stat_card_missing_title(self):
        errs = self._errors({"kind": "stat_card", "value": "42%"})
        assert any("title" in e.lower() for e in errs)

    def test_timeline_valid(self):
        assert (
            self._errors(
                {
                    "kind": "timeline",
                    "lanes": [
                        {
                            "label": "Alerts",
                            "category": "alert",
                            "events": [{"timestamp": 1000, "label": "test", "severity": "info"}],
                        }
                    ],
                }
            )
            == []
        )

    def test_timeline_empty_lanes(self):
        errs = self._errors({"kind": "timeline", "lanes": []})
        assert any("at least 1 lane" in e for e in errs)


# ---------------------------------------------------------------------------
# Scoring (from view_critic)
# ---------------------------------------------------------------------------


class TestScoring:
    def test_golden_scores_high_with_positions(self):
        r = evaluate_components(GOLDEN, positions={0: {"x": 0, "y": 0, "w": 12, "h": 2}})
        assert r.score >= 8

    def test_golden_without_positions_loses_2_points(self):
        with_pos = evaluate_components(GOLDEN, positions={0: {"x": 0}})
        without_pos = evaluate_components(GOLDEN, positions={})
        assert with_pos.score - without_pos.score == 2

    def test_score_clamped_0_to_10(self):
        bad = [
            {"kind": "chart", "title": "Chart", "series": []},
            {"kind": "chart", "title": "Chart 2", "series": []},
            {"kind": "chart", "title": "Chart 3", "series": []},
        ]
        r = evaluate_components(bad)
        assert 0 <= r.score <= 10

    def test_empty_chart_penalizes_score(self):
        d = copy.deepcopy(GOLDEN)
        d[1]["series"] = []
        del d[1]["query"]
        r1 = evaluate_components(d)
        r2 = evaluate_components(GOLDEN)
        assert r2.score > r1.score

    def test_generic_title_penalizes_score(self):
        d = copy.deepcopy(GOLDEN)
        d[1]["title"] = "Chart"
        r_bad = evaluate_components(d)
        r_good = evaluate_components(GOLDEN)
        assert r_good.score > r_bad.score


# ---------------------------------------------------------------------------
# is_generic_title
# ---------------------------------------------------------------------------


class TestIsGenericTitle:
    @pytest.mark.parametrize("title", ["Chart", "chart", "Table", "Widget", "Card", "Metric Card"])
    def test_generic_detected(self, title):
        assert is_generic_title(title, "chart") is True

    @pytest.mark.parametrize("title", ["Chart 1", "Table 2", "Widget 99"])
    def test_numbered_generic_detected(self, title):
        assert is_generic_title(title, "chart") is True

    def test_kind_as_title_detected(self):
        assert is_generic_title("data table", "data_table") is True

    def test_descriptive_title_passes(self):
        assert is_generic_title("CPU by Namespace", "chart") is False


# ---------------------------------------------------------------------------
# Result shape compatibility
# ---------------------------------------------------------------------------


class TestResultShape:
    def test_result_has_all_fields(self):
        r = evaluate_components(GOLDEN)
        assert isinstance(r, QualityResult)
        assert hasattr(r, "valid")
        assert hasattr(r, "score")
        assert hasattr(r, "max_score")
        assert hasattr(r, "errors")
        assert hasattr(r, "warnings")
        assert hasattr(r, "suggestions")
        assert hasattr(r, "deduped_count")
        assert hasattr(r, "components")


# ---------------------------------------------------------------------------
# normalize_layout — empty tabs stripping
# ---------------------------------------------------------------------------


class TestNormalizeLayout:
    def test_empty_tabs_widget_dropped(self):
        """A tabs widget where all tabs have 0 components is removed entirely."""
        from sre_agent.quality_engine import normalize_layout

        layout = [
            {"kind": "metric_card", "title": "Nodes Ready", "value": "6"},
            {
                "kind": "tabs",
                "tabs": [
                    {"label": "Recommendations", "components": []},
                    {"label": "Impact", "components": []},
                ],
            },
        ]
        result = normalize_layout(layout)
        kinds = [w["kind"] for w in result]
        assert "tabs" not in kinds, f"Empty tabs widget should be dropped, got: {kinds}"
        assert "metric_card" in kinds

    def test_mixed_tabs_strips_empty_keeps_populated(self):
        """A tabs widget with one populated and one empty tab keeps only the populated tab."""
        from sre_agent.quality_engine import normalize_layout

        layout = [
            {
                "kind": "tabs",
                "tabs": [
                    {"label": "Trend", "components": [{"kind": "chart", "title": "CPU"}]},
                    {"label": "Recommendations", "components": []},
                ],
            },
        ]
        result = normalize_layout(layout)
        assert len(result) == 1
        remaining_tabs = result[0]["tabs"]
        assert len(remaining_tabs) == 1
        assert remaining_tabs[0]["label"] == "Trend"

    def test_status_list_text_to_name(self):
        """status_list items with 'text' field (no 'name') get name populated."""
        from sre_agent.quality_engine import normalize_component_spec

        spec = {
            "kind": "status_list",
            "title": "RBAC Findings",
            "items": [
                {"text": "ClusterRole: admin bound to user foo", "status": "warning"},
                {"title": "Exposed NodePort: my-svc", "status": "error"},
                {"name": "Already has name", "status": "ok"},
            ],
        }
        normalize_component_spec(spec)
        items = spec["items"]
        assert items[0]["name"] == "ClusterRole: admin bound to user foo"
        assert items[1]["name"] == "Exposed NodePort: my-svc"
        assert items[2]["name"] == "Already has name"  # untouched
