"""Tests for view_critic.py — dashboard quality scoring rubric."""

from __future__ import annotations

import re

import pytest

from sre_agent import db as db_module
from sre_agent.db import Database, reset_database, set_database
from sre_agent.db_schema import ALL_SCHEMAS
from sre_agent.view_critic import critique_view

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _view_db():
    """Create a fresh PostgreSQL schema for each test."""
    from tests.conftest import _TEST_DB_URL

    test_db = Database(_TEST_DB_URL)
    test_db.execute("DROP TABLE IF EXISTS view_versions CASCADE")
    test_db.execute("DROP TABLE IF EXISTS views CASCADE")
    test_db.commit()
    test_db.executescript(ALL_SCHEMAS)
    set_database(test_db)
    yield test_db
    reset_database()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_score(result: str) -> int:
    m = re.search(r"(\d+)/10", result)
    return int(m.group(1)) if m else -1


_counter = 0


def _save_and_critique(layout, *, positions=None, view_id="cv-test", title=None):
    global _counter
    _counter += 1
    if title is None:
        title = f"Test View {_counter}"
    pos = positions if positions is not None else {}
    db_module.save_view("alice", view_id, title, "", layout, positions=pos)
    return critique_view.call({"view_id": view_id})


# ---------------------------------------------------------------------------
# Golden / bad fixture data
# ---------------------------------------------------------------------------

GOLDEN_SRE = [
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
        "description": "Watch for spikes above 80%",
        "query": "sum by (ns) (rate(cpu[5m]))",
        "series": [{"name": "ns1", "data": [1, 2, 3]}],
    },
    {
        "kind": "chart",
        "title": "Memory by Namespace",
        "description": "Watch for steady growth",
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

BAD_ALL_TABLES = [
    {"kind": "data_table", "title": "Table A", "columns": [{"id": "a"}], "rows": []},
    {"kind": "data_table", "title": "Table B", "columns": [{"id": "b"}], "rows": []},
    {"kind": "data_table", "title": "Table C", "columns": [{"id": "c"}], "rows": []},
]

BAD_DUPLICATES = [
    {"kind": "grid", "title": "KPIs", "items": [{"kind": "metric_card", "title": "CPU", "value": "5%"}]},
    {"kind": "chart", "title": "CPU Trend", "query": "rate(cpu[5m])", "series": [{"data": [1]}]},
    {"kind": "chart", "title": "CPU Trend 2", "query": "rate(cpu[5m])", "series": [{"data": [1]}]},
    {"kind": "chart", "title": "CPU Trend 3", "query": "rate(cpu[5m])", "series": [{"data": [1]}]},
    {"kind": "data_table", "title": "Pods", "columns": [{"id": "n"}], "rows": []},
]

BAD_GENERIC = [
    {"kind": "grid", "title": "KPIs", "items": [{"kind": "metric_card", "title": "Metric Card", "value": "5"}]},
    {"kind": "chart", "title": "Chart", "series": [{"data": [1]}]},
    {"kind": "chart", "title": "Chart 2", "series": [{"data": [2]}]},
    {"kind": "data_table", "title": "Table", "columns": [{"id": "a"}], "rows": []},
]

BAD_EMPTY_CHARTS = [
    {"kind": "grid", "title": "KPIs", "items": [{"kind": "metric_card", "title": "CPU", "value": "5%"}]},
    {"kind": "chart", "title": "Empty Chart 1", "series": []},
    {"kind": "chart", "title": "Empty Chart 2", "series": []},
    {"kind": "data_table", "title": "Pods", "columns": [{"id": "n"}], "rows": []},
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGoldenAndBad:
    def test_golden_sre_scores_high(self):
        result = _save_and_critique(
            GOLDEN_SRE,
            positions={0: {"x": 0, "y": 0, "w": 12, "h": 2}},
            view_id="cv-golden",
        )
        assert _extract_score(result) >= 8

    def test_bad_all_tables_scores_low(self):
        result = _save_and_critique(BAD_ALL_TABLES, view_id="cv-tables")
        assert _extract_score(result) < 5

    def test_bad_duplicates_scores_low(self):
        result = _save_and_critique(BAD_DUPLICATES, view_id="cv-dups")
        score = _extract_score(result)
        assert score < 5
        assert "DUPLICATE" in result

    def test_bad_generic_scores_low(self):
        result = _save_and_critique(BAD_GENERIC, view_id="cv-generic")
        score = _extract_score(result)
        assert score < 5
        assert "GENERIC" in result

    def test_bad_empty_charts_penalized(self):
        result = _save_and_critique(BAD_EMPTY_CHARTS, view_id="cv-empty")
        # Empty charts should appear as issues, not just suggestions
        assert "EMPTY CHART" in result


class TestCheckADuplicates:
    def test_duplicate_query_deducts_per_extra(self):
        """3 copies of same query = 2 extra = -2 points."""
        layout_with_dupes = [
            {
                "kind": "grid",
                "title": "KPIs",
                "items": [
                    {"kind": "metric_card", "title": "CPU", "value": "5%", "query": "cpu_usage"},
                ],
            },
            {
                "kind": "chart",
                "title": "Chart A",
                "description": "d",
                "query": "rate(cpu[5m])",
                "series": [{"data": [1]}],
            },
            {
                "kind": "chart",
                "title": "Chart B",
                "description": "d",
                "query": "rate(cpu[5m])",
                "series": [{"data": [2]}],
            },
            {
                "kind": "chart",
                "title": "Chart C",
                "description": "d",
                "query": "rate(cpu[5m])",
                "series": [{"data": [3]}],
            },
            {"kind": "data_table", "title": "Pods", "columns": [{"id": "n"}], "rows": []},
        ]
        layout_no_dupes = [
            {
                "kind": "grid",
                "title": "KPIs",
                "items": [
                    {"kind": "metric_card", "title": "CPU", "value": "5%", "query": "cpu_usage"},
                ],
            },
            {
                "kind": "chart",
                "title": "Chart A",
                "description": "d",
                "query": "rate(cpu[5m])",
                "series": [{"data": [1]}],
            },
            {
                "kind": "chart",
                "title": "Chart B",
                "description": "d",
                "query": "rate(mem[5m])",
                "series": [{"data": [2]}],
            },
            {
                "kind": "chart",
                "title": "Chart C",
                "description": "d",
                "query": "rate(disk[5m])",
                "series": [{"data": [3]}],
            },
            {"kind": "data_table", "title": "Pods", "columns": [{"id": "n"}], "rows": []},
        ]
        r_dupes = _save_and_critique(
            layout_with_dupes, view_id="cv-d1", positions={0: {"x": 0, "y": 0, "w": 4, "h": 2}}
        )
        r_clean = _save_and_critique(layout_no_dupes, view_id="cv-d2", positions={0: {"x": 0, "y": 0, "w": 4, "h": 2}})
        score_dupes = _extract_score(r_dupes)
        score_clean = _extract_score(r_clean)
        # 3 copies = 2 points deducted
        assert score_clean - score_dupes >= 2


class TestCheckBGenericTitles:
    def test_generic_title_deducts(self):
        layout_generic = [
            {
                "kind": "grid",
                "title": "KPIs",
                "items": [
                    {"kind": "metric_card", "title": "CPU Usage", "value": "5%", "query": "cpu"},
                ],
            },
            {"kind": "chart", "title": "Chart", "description": "d", "query": "q1", "series": [{"data": [1]}]},
            {"kind": "chart", "title": "Mem Trend", "description": "d", "query": "q2", "series": [{"data": [2]}]},
            {"kind": "data_table", "title": "Pods List", "columns": [{"id": "n"}], "rows": []},
        ]
        layout_descriptive = [
            {
                "kind": "grid",
                "title": "KPIs",
                "items": [
                    {"kind": "metric_card", "title": "CPU Usage", "value": "5%", "query": "cpu"},
                ],
            },
            {"kind": "chart", "title": "CPU over Time", "description": "d", "query": "q1", "series": [{"data": [1]}]},
            {"kind": "chart", "title": "Mem Trend", "description": "d", "query": "q2", "series": [{"data": [2]}]},
            {"kind": "data_table", "title": "Pods List", "columns": [{"id": "n"}], "rows": []},
        ]
        r_generic = _save_and_critique(layout_generic, view_id="cv-g1", positions={0: {"x": 0, "y": 0, "w": 4, "h": 2}})
        r_good = _save_and_critique(
            layout_descriptive, view_id="cv-g2", positions={0: {"x": 0, "y": 0, "w": 4, "h": 2}}
        )
        assert _extract_score(r_good) > _extract_score(r_generic)
        assert "GENERIC" in r_generic


class TestCheckCEmptyCharts:
    def test_empty_chart_is_issue_not_suggestion(self):
        layout = [
            {
                "kind": "grid",
                "title": "KPIs",
                "items": [
                    {"kind": "metric_card", "title": "CPU", "value": "5%", "query": "cpu"},
                ],
            },
            {"kind": "chart", "title": "Empty One", "series": []},
            {"kind": "chart", "title": "Has Data", "description": "d", "query": "q1", "series": [{"data": [1]}]},
            {"kind": "data_table", "title": "Pods", "columns": [{"id": "n"}], "rows": []},
        ]
        result = _save_and_critique(layout, view_id="cv-ec", positions={0: {"x": 0, "y": 0, "w": 4, "h": 2}})
        # Should be in issues section, not suggestions
        assert "EMPTY CHART" in result
        assert "Empty One" in result

    def test_chart_with_query_not_penalized(self):
        """A chart with no series but a query is not empty — Prometheus will fill it."""
        layout = [
            {
                "kind": "grid",
                "title": "KPIs",
                "items": [
                    {"kind": "metric_card", "title": "CPU", "value": "5%", "query": "cpu"},
                ],
            },
            {"kind": "chart", "title": "Live Chart", "query": "rate(cpu[5m])", "series": []},
            {
                "kind": "chart",
                "title": "Also Live",
                "description": "d",
                "query": "rate(mem[5m])",
                "series": [{"data": [1]}],
            },
            {"kind": "data_table", "title": "Pods", "columns": [{"id": "n"}], "rows": []},
        ]
        result = _save_and_critique(layout, view_id="cv-qc", positions={0: {"x": 0, "y": 0, "w": 4, "h": 2}})
        assert "EMPTY CHART" not in result


class TestCheckDBalance:
    def test_balance_penalty(self):
        """All charts, no tables or metrics -> imbalanced."""
        layout = [
            {"kind": "chart", "title": "Chart A", "series": [{"data": [1]}]},
            {"kind": "chart", "title": "Chart B", "series": [{"data": [2]}]},
            {"kind": "chart", "title": "Chart C", "series": [{"data": [3]}]},
            {"kind": "chart", "title": "Chart D", "series": [{"data": [4]}]},
        ]
        result = _save_and_critique(layout, view_id="cv-bal")
        assert "IMBALANCED" in result


class TestCheckEDuplicateTitles:
    def test_duplicate_titles_penalized(self):
        layout = [
            {
                "kind": "grid",
                "title": "KPIs",
                "items": [
                    {"kind": "metric_card", "title": "CPU", "value": "5%", "query": "cpu"},
                ],
            },
            {"kind": "chart", "title": "CPU Trend", "description": "d", "query": "q1", "series": [{"data": [1]}]},
            {"kind": "chart", "title": "CPU Trend", "description": "d", "query": "q2", "series": [{"data": [2]}]},
            {"kind": "data_table", "title": "Pods", "columns": [{"id": "n"}], "rows": []},
        ]
        result = _save_and_critique(layout, view_id="cv-dt", positions={0: {"x": 0, "y": 0, "w": 4, "h": 2}})
        assert "DUPLICATE TITLES" in result


class TestScoreClamping:
    def test_score_clamped_at_zero(self):
        """A terrible dashboard should score 0, never negative."""
        layout = [
            {"kind": "chart", "title": "Chart", "series": []},
            {"kind": "chart", "title": "Chart 2", "series": []},
            {"kind": "chart", "title": "Chart 3", "series": []},
        ]
        result = _save_and_critique(layout, view_id="cv-clamp0")
        score = _extract_score(result)
        assert score == 0

    def test_score_capped_at_10(self):
        result = _save_and_critique(
            GOLDEN_SRE,
            positions={0: {"x": 0, "y": 0, "w": 12, "h": 2}},
            view_id="cv-cap10",
        )
        score = _extract_score(result)
        assert score <= 10


class TestEdgeCases:
    def test_nonexistent_view(self):
        result = critique_view.call({"view_id": "cv-nope"})
        assert "not found" in result.lower()

    def test_empty_view(self):
        result = _save_and_critique([], view_id="cv-empty")
        score = _extract_score(result)
        assert score < 3

    def test_no_layout_penalty(self):
        result = _save_and_critique(GOLDEN_SRE, positions={}, view_id="cv-notempl")
        assert "NO LAYOUT" in result


class TestExistingChecksPreserved:
    """Verify original checks 1-7 still work (no regression)."""

    def test_no_metric_cards_detected(self):
        layout = [
            {"kind": "chart", "title": "CPU", "description": "d", "query": "q1", "series": [{"data": [1]}]},
            {"kind": "chart", "title": "Mem", "description": "d", "query": "q2", "series": [{"data": [2]}]},
            {"kind": "data_table", "title": "Pods", "columns": [{"id": "n"}], "rows": []},
        ]
        result = _save_and_critique(layout, view_id="cv-nometrics")
        assert "NO METRIC CARDS" in result

    def test_only_one_chart_detected(self):
        layout = [
            {
                "kind": "grid",
                "title": "KPIs",
                "items": [
                    {"kind": "metric_card", "title": "CPU", "value": "5%", "query": "cpu"},
                ],
            },
            {"kind": "chart", "title": "CPU Trend", "description": "d", "query": "q", "series": [{"data": [1]}]},
            {"kind": "data_table", "title": "Pods", "columns": [{"id": "n"}], "rows": []},
        ]
        result = _save_and_critique(layout, view_id="cv-1chart")
        assert "ONLY 1 CHART" in result

    def test_no_table_detected(self):
        layout = [
            {
                "kind": "grid",
                "title": "KPIs",
                "items": [
                    {"kind": "metric_card", "title": "CPU", "value": "5%", "query": "cpu"},
                ],
            },
            {"kind": "chart", "title": "CPU Trend", "description": "d", "query": "q1", "series": [{"data": [1]}]},
            {"kind": "chart", "title": "Mem Trend", "description": "d", "query": "q2", "series": [{"data": [2]}]},
        ]
        result = _save_and_critique(layout, view_id="cv-notable")
        assert "NO TABLE" in result

    def test_untitled_widgets_detected(self):
        layout = [
            {
                "kind": "grid",
                "items": [
                    {"kind": "metric_card", "title": "CPU", "value": "5%", "query": "cpu"},
                ],
            },
            {"kind": "chart", "title": "CPU Trend", "description": "d", "query": "q1", "series": [{"data": [1]}]},
            {"kind": "chart", "title": "Mem Trend", "description": "d", "query": "q2", "series": [{"data": [2]}]},
            {"kind": "data_table", "title": "Pods", "columns": [{"id": "n"}], "rows": []},
        ]
        result = _save_and_critique(layout, view_id="cv-untitled")
        assert "UNTITLED WIDGETS" in result
