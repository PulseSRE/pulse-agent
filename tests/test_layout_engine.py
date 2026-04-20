"""Tests for the semantic layout engine."""

from sre_agent.layout_engine import compute_layout


class TestSingleComponents:
    def test_single_chart_default_width(self):
        components = [{"kind": "chart"}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 2  # charts default to half-width
        assert layout[0]["x"] == 0

    def test_single_table_full_width(self):
        components = [{"kind": "data_table"}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 4

    def test_single_metric_card(self):
        components = [{"kind": "metric_card"}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 4  # Single card fills full width
        assert layout[0]["x"] == 0

    def test_two_metric_cards_half_width(self):
        components = [{"kind": "metric_card"}, {"kind": "metric_card"}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 2
        assert layout[1]["w"] == 2
        assert layout[0]["x"] == 0
        assert layout[1]["x"] == 2

    def test_empty_list(self):
        assert compute_layout([]) == {}

    def test_unknown_kind_full_width(self):
        components = [{"kind": "foobar"}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 4


class TestKPIPacking:
    def test_four_metric_cards_in_row(self):
        components = [{"kind": "metric_card"} for _ in range(4)]
        layout = compute_layout(components)
        ys = {layout[i]["y"] for i in range(4)}
        assert len(ys) == 1, "All 4 cards should share the same y"
        xs = sorted(layout[i]["x"] for i in range(4))
        assert xs == [0, 1, 2, 3]
        for i in range(4):
            assert layout[i]["w"] == 1

    def test_grid_kpi_group_full_width(self):
        components = [{"kind": "grid", "items": [{"kind": "metric_card"}, {"kind": "metric_card"}]}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 4

    def test_info_card_grid_full_width(self):
        components = [{"kind": "info_card_grid"}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 4

    def test_kpi_before_charts(self):
        components = [{"kind": "chart"}, {"kind": "metric_card"}]
        layout = compute_layout(components)
        # metric_card (index 1) should have lower y than chart (index 0)
        assert layout[1]["y"] < layout[0]["y"]


class TestChartPacking:
    def test_two_charts_side_by_side(self):
        components = [{"kind": "chart"}, {"kind": "chart"}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 2
        assert layout[1]["w"] == 2
        assert layout[0]["y"] == layout[1]["y"]
        assert layout[0]["x"] != layout[1]["x"]

    def test_three_charts(self):
        components = [{"kind": "chart"}, {"kind": "chart"}, {"kind": "chart"}]
        layout = compute_layout(components)
        # First 2 side-by-side (w=2 each fills row)
        assert layout[0]["w"] == 2
        assert layout[1]["w"] == 2
        assert layout[0]["y"] == layout[1]["y"]
        # 3rd on next row (bin-packed, stays w=2)
        assert layout[2]["w"] == 2
        assert layout[2]["y"] > layout[0]["y"]

    def test_node_map_full_width(self):
        components = [{"kind": "node_map"}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 4


class TestDetailPairing:
    def test_log_viewer_key_value_paired(self):
        components = [{"kind": "log_viewer"}, {"kind": "key_value"}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 2
        assert layout[1]["w"] == 2
        assert layout[0]["y"] == layout[1]["y"]

    def test_key_value_relationship_tree_paired(self):
        components = [{"kind": "key_value"}, {"kind": "relationship_tree"}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 2
        assert layout[1]["w"] == 2
        assert layout[0]["y"] == layout[1]["y"]

    def test_unpaired_detail_full_width(self):
        components = [{"kind": "log_viewer"}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 4


class TestFullDashboards:
    def test_sre_dashboard(self):
        components = [
            {"kind": "grid", "items": [{"kind": "metric_card"}]},
            {"kind": "chart"},
            {"kind": "chart"},
            {"kind": "data_table"},
        ]
        layout = compute_layout(components)
        # KPI group on top
        assert layout[0]["y"] == 0
        # Charts in the middle
        chart_y = layout[1]["y"]
        assert chart_y > 0
        assert layout[2]["y"] == chart_y  # side-by-side
        # Table at bottom
        assert layout[3]["y"] > chart_y

    def test_incident_report(self):
        components = [
            {"kind": "status_list"},
            {"kind": "log_viewer"},
            {"kind": "key_value"},
            {"kind": "data_table"},
        ]
        layout = compute_layout(components)
        # status first
        status_y = layout[0]["y"]
        # detail pair next
        detail_y = layout[1]["y"]
        assert detail_y > status_y
        assert layout[1]["y"] == layout[2]["y"]  # paired
        assert layout[1]["w"] == 2
        assert layout[2]["w"] == 2
        # table last
        assert layout[3]["y"] > detail_y

    def test_no_overlapping_positions(self):
        components = [
            {"kind": "metric_card"},
            {"kind": "metric_card"},
            {"kind": "chart"},
            {"kind": "data_table"},
            {"kind": "log_viewer"},
            {"kind": "key_value"},
        ]
        layout = compute_layout(components)
        # Check no two components occupy overlapping grid cells
        occupied: set[tuple[int, int]] = set()
        for idx in layout:
            pos = layout[idx]
            for dx in range(pos["w"]):
                for dy in range(pos["h"]):
                    cell = (pos["x"] + dx, pos["y"] + dy)
                    assert cell not in occupied, f"Component {idx} overlaps at {cell}"
                    occupied.add(cell)


class TestEdgeCases:
    def test_five_metric_cards_wrap(self):
        """5 metric cards: 4 in first row, 1 in second."""
        components = [{"kind": "metric_card"} for _ in range(5)]
        layout = compute_layout(components)
        first_row_y = layout[0]["y"]
        assert sum(1 for i in range(5) if layout[i]["y"] == first_row_y) == 4
        assert sum(1 for i in range(5) if layout[i]["y"] > first_row_y) == 1

    def test_grid_without_metric_items_is_container(self):
        """grid with non-metric items → container role, w=4."""
        components = [{"kind": "grid", "items": [{"kind": "chart"}]}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 4


class TestNewComponentTypes:
    def test_bar_list_full_width_when_alone(self):
        """Single detail-role component gets full width."""
        components = [{"kind": "bar_list", "items": [{"label": "a", "value": 1}]}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 4
        assert layout[0]["h"] == 3  # content-aware: 2 + min(1, 8)

    def test_progress_list_full_width_when_alone(self):
        components = [{"kind": "progress_list", "items": [{"label": "a", "value": 50, "max": 100}]}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 4
        assert layout[0]["h"] == 4  # content-aware: 2 + ceil(1*1.2) = 4

    def test_stat_card_full_width_when_alone(self):
        """Single kpi-role component gets full width."""
        components = [{"kind": "stat_card", "title": "CPU", "value": "42%"}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 4
        assert layout[0]["h"] == 4

    def test_stat_cards_pack_like_metric_cards(self):
        """stat_card has kpi role, so 4 should pack in a row."""
        components = [{"kind": "stat_card"} for _ in range(4)]
        layout = compute_layout(components)
        ys = {layout[i]["y"] for i in range(4)}
        assert len(ys) == 1, "All 4 stat_cards should share same y"

    def test_grid_metric_height_scales_with_rows(self):
        """Grid height should increase with more rows of metric cards."""
        one_row = [{"kind": "grid", "columns": 4, "items": [{"kind": "metric_card"}] * 4}]
        two_rows = [{"kind": "grid", "columns": 2, "items": [{"kind": "metric_card"}] * 4}]
        h1 = compute_layout(one_row)[0]["h"]
        h2 = compute_layout(two_rows)[0]["h"]
        assert h2 > h1, "2-row grid should be taller than 1-row grid"

    def test_timeline_full_width(self):
        components = [{"kind": "timeline"}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 4
        assert layout[0]["h"] == 10


class TestLayoutHints:
    def test_width_hint_half(self):
        components = [{"kind": "data_table", "layout": {"w": "half"}}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 2

    def test_width_hint_full(self):
        components = [{"kind": "chart", "layout": {"w": "full"}}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 4

    def test_width_hint_quarter(self):
        components = [{"kind": "chart", "layout": {"w": "quarter"}}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 1

    def test_height_hint_compact(self):
        components = [{"kind": "chart", "layout": {"h": "compact"}}]
        layout = compute_layout(components)
        assert layout[0]["h"] < 12  # default chart height is 12

    def test_height_hint_tall(self):
        components = [
            {"kind": "chart", "series": [{"label": "a"}, {"label": "b"}, {"label": "c"}], "layout": {"h": "tall"}}
        ]
        layout = compute_layout(components)
        assert layout[0]["h"] > 12  # 12 * 1.5 = 18

    def test_group_packing(self):
        components = [
            {"kind": "chart", "layout": {"w": "half", "group": "cpu"}},
            {"kind": "chart", "layout": {"w": "half", "group": "cpu"}},
        ]
        layout = compute_layout(components)
        assert layout[0]["y"] == layout[1]["y"]  # same row
        assert layout[0]["x"] != layout[1]["x"]

    def test_priority_top(self):
        components = [
            {"kind": "data_table"},
            {"kind": "chart", "layout": {"priority": "top"}},
        ]
        layout = compute_layout(components)
        # Chart with priority=top should be above table
        assert layout[1]["y"] < layout[0]["y"]

    def test_no_hints_uses_defaults(self):
        components = [{"kind": "chart"}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 2  # default for chart
        assert layout[0]["h"] == 4  # empty chart — compact (no series)

    def test_chart_with_series_gets_full_height(self):
        components = [{"kind": "chart", "series": [{"label": "cpu", "data": [[1, 2]]}]}]
        layout = compute_layout(components)
        assert layout[0]["h"] == 8  # single series — medium

    def test_chart_multi_series_gets_tall(self):
        components = [{"kind": "chart", "series": [{"label": "a"}, {"label": "b"}, {"label": "c"}]}]
        layout = compute_layout(components)
        assert layout[0]["h"] == 12  # multi-series — full default

    def test_content_aware_table_height(self):
        components = [{"kind": "data_table", "rows": [{}] * 8}]
        layout = compute_layout(components)
        assert layout[0]["h"] == 11  # 3 + min(8, 15)

    def test_table_height_2_rows(self):
        components = [{"kind": "data_table", "rows": [{}] * 2}]
        layout = compute_layout(components)
        assert layout[0]["h"] == 5  # 3 + 2

    def test_table_height_15_rows_capped(self):
        components = [{"kind": "data_table", "rows": [{}] * 30}]
        layout = compute_layout(components)
        assert layout[0]["h"] == 18  # 3 + min(30, 15) = 18

    def test_table_height_empty(self):
        components = [{"kind": "data_table", "rows": []}]
        layout = compute_layout(components)
        assert layout[0]["h"] == 5  # empty static table

    def test_table_height_live_no_rows(self):
        components = [{"kind": "data_table", "datasources": [{"type": "k8s", "id": "x"}]}]
        layout = compute_layout(components)
        assert layout[0]["h"] == 10  # live table with no initial snapshot

    def test_table_height_live_with_rows(self):
        components = [{"kind": "data_table", "rows": [{}] * 5, "datasources": [{"type": "k8s", "id": "x"}]}]
        layout = compute_layout(components)
        assert layout[0]["h"] == 8  # 3 + 5, uses actual row count

    def test_content_aware_status_list_height(self):
        components = [{"kind": "status_list", "items": [{}] * 5}]
        layout = compute_layout(components)
        assert layout[0]["h"] == 6  # 2 + min(ceil(5*0.8), 8) = 2 + 4


class TestTopologyLayout:
    def test_topology_full_width(self):
        components = [{"kind": "topology"}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 4

    def test_topology_height(self):
        components = [{"kind": "topology"}]
        layout = compute_layout(components)
        assert layout[0]["h"] == 24

    def test_topology_below_kpi(self):
        components = [{"kind": "resource_counts"}, {"kind": "topology"}]
        layout = compute_layout(components)
        assert layout[0]["y"] < layout[1]["y"]

    def test_topology_not_paired(self):
        """Topology should never be half-width paired with another chart."""
        components = [{"kind": "chart"}, {"kind": "topology"}]
        layout = compute_layout(components)
        assert layout[1]["w"] == 4


class TestNamespaceDashboard:
    """Regression tests for namespace_summary-style dashboards."""

    def test_full_dashboard_no_overlap(self):
        components = [
            {"kind": "grid", "items": [{"kind": "metric_card"}] * 4},
            {"kind": "chart", "series": [{"label": "cpu", "data": [[1, 2]]}]},
            {"kind": "chart", "series": [{"label": "mem", "data": [[1, 2]]}]},
            {"kind": "data_table", "rows": [{}] * 5},
            {"kind": "topology"},
        ]
        layout = compute_layout(components)
        occupied: set[tuple[int, int]] = set()
        for idx in layout:
            pos = layout[idx]
            for dx in range(pos["w"]):
                for dy in range(pos["h"]):
                    cell = (pos["x"] + dx, pos["y"] + dy)
                    assert cell not in occupied, f"Component {idx} overlaps at {cell}"
                    occupied.add(cell)

    def test_topology_has_minimum_height(self):
        """Topology must be tall enough to render a graph without clipping."""
        components = [
            {"kind": "resource_counts"},
            {"kind": "chart"},
            {"kind": "topology"},
        ]
        layout = compute_layout(components)
        topo_h = layout[2]["h"]
        assert topo_h >= 16, f"Topology height {topo_h} too short, needs >= 16 (384px at 24px/row)"

    def test_all_widget_kinds_have_entries(self):
        """Every kind in _KIND_MAP should produce a valid layout."""
        from sre_agent.layout_engine import _KIND_MAP

        for kind in _KIND_MAP:
            components = [{"kind": kind}]
            layout = compute_layout(components)
            assert 0 in layout, f"Kind '{kind}' produced no layout"
            assert layout[0]["w"] > 0
            assert layout[0]["h"] > 0
