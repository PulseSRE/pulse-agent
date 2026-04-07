"""Tests for the semantic layout engine."""

from sre_agent.layout_engine import compute_layout


class TestSingleComponents:
    def test_single_chart_full_width(self):
        components = [{"kind": "chart"}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 4
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
        # First 2 side-by-side
        assert layout[0]["w"] == 2
        assert layout[1]["w"] == 2
        assert layout[0]["y"] == layout[1]["y"]
        # 3rd full-width below
        assert layout[2]["w"] == 4
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
        assert layout[0]["h"] == 8

    def test_progress_list_full_width_when_alone(self):
        components = [{"kind": "progress_list", "items": [{"label": "a", "value": 50, "max": 100}]}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 4
        assert layout[0]["h"] == 8

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
