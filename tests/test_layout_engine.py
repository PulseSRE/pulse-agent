"""Tests for the semantic layout engine."""

from sre_agent.layout_engine import GRID_COLS, compute_layout


class TestSingleComponents:
    def test_single_chart_default_width(self):
        components = [{"kind": "chart"}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 12  # a lone half-width chart stretches to fill its row
        assert layout[0]["x"] == 0

    def test_single_table_full_width(self):
        components = [{"kind": "data_table"}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 12

    def test_single_metric_card(self):
        components = [{"kind": "metric_card"}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 12  # Single card fills full width
        assert layout[0]["x"] == 0

    def test_two_metric_cards_half_width(self):
        components = [{"kind": "metric_card"}, {"kind": "metric_card"}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 6
        assert layout[1]["w"] == 6
        assert layout[0]["x"] == 0
        assert layout[1]["x"] == 6

    def test_empty_list(self):
        assert compute_layout([]) == {}

    def test_unknown_kind_full_width(self):
        components = [{"kind": "foobar"}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 12


class TestKPIPacking:
    def test_four_metric_cards_in_row(self):
        components = [{"kind": "metric_card"} for _ in range(4)]
        layout = compute_layout(components)
        ys = {layout[i]["y"] for i in range(4)}
        assert len(ys) == 1, "All 4 cards should share the same y"
        xs = sorted(layout[i]["x"] for i in range(4))
        assert xs == [0, 3, 6, 9]
        for i in range(4):
            assert layout[i]["w"] == 3

    def test_grid_kpi_group_full_width(self):
        components = [{"kind": "grid", "items": [{"kind": "metric_card"}, {"kind": "metric_card"}]}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 12

    def test_info_card_grid_full_width(self):
        components = [{"kind": "info_card_grid"}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 12

    def test_kpi_before_charts(self):
        components = [{"kind": "chart"}, {"kind": "metric_card"}]
        layout = compute_layout(components)
        # metric_card (index 1) should have lower y than chart (index 0)
        assert layout[1]["y"] < layout[0]["y"]


class TestChartPacking:
    def test_two_charts_side_by_side(self):
        components = [{"kind": "chart"}, {"kind": "chart"}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 6
        assert layout[1]["w"] == 6
        assert layout[0]["y"] == layout[1]["y"]
        assert layout[0]["x"] != layout[1]["x"]

    def test_three_charts(self):
        """Three charts share one row as thirds, not two halves and a lone full-width one."""
        components = [{"kind": "chart"}, {"kind": "chart"}, {"kind": "chart"}]
        layout = compute_layout(components)
        assert [(layout[i]["x"], layout[i]["w"]) for i in range(3)] == [(0, 4), (4, 4), (8, 4)]
        assert len({layout[i]["y"] for i in range(3)}) == 1

    def test_five_charts_end_in_a_row_of_thirds(self):
        layout = compute_layout([{"kind": "chart"} for _ in range(5)])
        assert [layout[i]["w"] for i in range(5)] == [6, 6, 4, 4, 4]
        assert layout[0]["y"] == layout[1]["y"] < layout[2]["y"] == layout[4]["y"]

    def test_four_charts_are_two_rows_of_halves(self):
        layout = compute_layout([{"kind": "chart"} for _ in range(4)])
        assert [layout[i]["w"] for i in range(4)] == [6, 6, 6, 6]

    def test_node_map_full_width(self):
        components = [{"kind": "node_map"}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 12


class TestDetailPairing:
    def test_log_viewer_key_value_paired(self):
        components = [{"kind": "log_viewer"}, {"kind": "key_value"}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 6
        assert layout[1]["w"] == 6
        assert layout[0]["y"] == layout[1]["y"]

    def test_key_value_relationship_tree_paired(self):
        components = [{"kind": "key_value"}, {"kind": "relationship_tree"}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 6
        assert layout[1]["w"] == 6
        assert layout[0]["y"] == layout[1]["y"]

    def test_unpaired_detail_full_width(self):
        components = [{"kind": "log_viewer"}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 12


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
        assert layout[1]["w"] == 6
        assert layout[2]["w"] == 6
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
    def test_five_metric_cards_share_a_row(self):
        """Up to six cards fit one row; five fill it as evenly as the grid allows."""
        layout = compute_layout([{"kind": "metric_card"} for _ in range(5)])
        assert len({layout[i]["y"] for i in range(5)}) == 1
        assert [layout[i]["w"] for i in range(5)] == [2, 2, 2, 3, 3]

    def test_seven_metric_cards_balance_two_rows(self):
        """Seven cards are rows of four and three, not six and a lone one."""
        layout = compute_layout([{"kind": "metric_card"} for _ in range(7)])
        first = layout[0]["y"]
        assert [layout[i]["w"] for i in range(7)] == [3, 3, 3, 3, 4, 4, 4]
        assert sum(1 for i in range(7) if layout[i]["y"] == first) == 4

    def test_grid_without_metric_items_is_container(self):
        """grid with non-metric items → container role, w=4."""
        components = [{"kind": "grid", "items": [{"kind": "chart"}]}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 12


class TestNewComponentTypes:
    def test_bar_list_full_width_when_alone(self):
        """Single detail-role component gets full width."""
        components = [{"kind": "bar_list", "items": [{"label": "a", "value": 1}]}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 12
        assert layout[0]["h"] == 3  # content-aware: 2 + min(1, 8)

    def test_progress_list_full_width_when_alone(self):
        components = [{"kind": "progress_list", "items": [{"label": "a", "value": 50, "max": 100}]}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 12
        assert layout[0]["h"] == 4  # content-aware: 2 + ceil(1*1.2) = 4

    def test_stat_card_full_width_when_alone(self):
        """Single kpi-role component gets full width."""
        components = [{"kind": "stat_card", "title": "CPU", "value": "42%"}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 12
        assert layout[0]["h"] == 3

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
        assert layout[0]["w"] == 12
        assert layout[0]["h"] == 10


class TestLayoutHints:
    def test_width_hint_half(self):
        components = [{"kind": "data_table", "layout": {"w": "half"}}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 6

    def test_width_hint_full(self):
        components = [{"kind": "chart", "layout": {"w": "full"}}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 12

    def test_width_hint_quarter(self):
        components = [{"kind": "chart", "layout": {"w": "quarter"}}]
        layout = compute_layout(components)
        assert layout[0]["w"] == 3

    def test_height_hint_compact(self):
        components = [{"kind": "chart", "layout": {"h": "compact"}}]
        layout = compute_layout(components)
        assert layout[0]["h"] < 10  # default chart height is 10

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
        assert layout[0]["w"] == 12  # lone chart fills its row
        assert layout[0]["h"] == 4  # empty chart — compact (no series)

    def test_chart_with_series_gets_full_height(self):
        components = [{"kind": "chart", "series": [{"label": "cpu", "data": [[1, 2]]}]}]
        layout = compute_layout(components)
        assert layout[0]["h"] == 10  # single series — medium

    def test_chart_multi_series_gets_tall(self):
        components = [{"kind": "chart", "series": [{"label": "a"}, {"label": "b"}, {"label": "c"}]}]
        layout = compute_layout(components)
        assert layout[0]["h"] == 10  # sized to the rendered card (~360px)

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
        assert layout[0]["w"] == 12

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
        assert layout[1]["w"] == 12


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


class TestContainerHeights:
    """Container components (section, tabs, grid) must size based on children."""

    def test_section_height_scales_with_children(self):
        empty = [{"kind": "section", "components": []}]
        full = [
            {
                "kind": "section",
                "components": [
                    {"kind": "metric_card", "title": "A", "value": "1"},
                    {"kind": "metric_card", "title": "B", "value": "2"},
                    {"kind": "metric_card", "title": "C", "value": "3"},
                ],
            }
        ]
        h_empty = compute_layout(empty)[0]["h"]
        h_full = compute_layout(full)[0]["h"]
        assert h_full > h_empty, f"Section with 3 children (h={h_full}) should be taller than empty (h={h_empty})"

    def test_section_uses_components_key(self):
        """Section children are stored under 'components', not 'items'."""
        wrong_key = [{"kind": "section", "items": [{"kind": "chart"}] * 3}]
        right_key = [{"kind": "section", "components": [{"kind": "chart"}] * 3}]
        h_wrong = compute_layout(wrong_key)[0]["h"]
        h_right = compute_layout(right_key)[0]["h"]
        assert h_right > h_wrong, "Section should read 'components' key, not 'items'"

    def test_empty_section_minimum_height(self):
        components = [{"kind": "section", "components": []}]
        layout = compute_layout(components)
        assert layout[0]["h"] >= 6

    def test_tabs_height_based_on_tallest_tab(self):
        components = [
            {
                "kind": "tabs",
                "tabs": [
                    {"label": "Small", "components": [{"kind": "metric_card", "title": "X", "value": "1"}]},
                    {
                        "label": "Large",
                        "components": [
                            {"kind": "chart", "series": [{"label": "a"}]},
                            {"kind": "data_table", "rows": [{}] * 5},
                        ],
                    },
                ],
            }
        ]
        layout = compute_layout(components)
        assert layout[0]["h"] >= 15, f"Tabs with chart+table tab should be tall, got h={layout[0]['h']}"

    def test_nested_section_in_tabs(self):
        components = [
            {
                "kind": "tabs",
                "tabs": [
                    {
                        "label": "Nested",
                        "components": [
                            {
                                "kind": "section",
                                "components": [
                                    {"kind": "metric_card", "title": "A", "value": "1"},
                                    {"kind": "metric_card", "title": "B", "value": "2"},
                                ],
                            },
                        ],
                    },
                ],
            }
        ]
        layout = compute_layout(components)
        assert layout[0]["h"] >= 12, f"Tabs with nested section should be tall, got h={layout[0]['h']}"

    def test_grid_height_recursive(self):
        components = [
            {
                "kind": "grid",
                "columns": 2,
                "items": [
                    {"kind": "chart", "series": [{"label": "a"}]},
                    {"kind": "chart", "series": [{"label": "b"}]},
                ],
            }
        ]
        layout = compute_layout(components)
        assert layout[0]["h"] >= 10, f"Grid with 2 charts should be tall, got h={layout[0]['h']}"

    def test_recursion_depth_guard(self):
        deep = {
            "kind": "section",
            "components": [
                {
                    "kind": "section",
                    "components": [
                        {
                            "kind": "section",
                            "components": [
                                {
                                    "kind": "section",
                                    "components": [
                                        {
                                            "kind": "section",
                                            "components": [
                                                {
                                                    "kind": "section",
                                                    "components": [
                                                        {"kind": "metric_card", "title": "Deep", "value": "1"},
                                                    ],
                                                }
                                            ],
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        components = [deep]
        layout = compute_layout(components)
        assert layout[0]["h"] > 0, "Deeply nested components should not error"


class TestOrphanRowFill:
    def test_six_kpis_share_one_row(self):
        layout = compute_layout([{"kind": "metric_card", "title": f"M{i}"} for i in range(6)])
        assert [(layout[i]["x"], layout[i]["w"]) for i in range(6)] == [(i * 2, 2) for i in range(6)]

    def test_three_kpis_are_thirds(self):
        layout = compute_layout([{"kind": "metric_card", "title": f"M{i}"} for i in range(3)])
        assert [layout[i]["w"] for i in range(3)] == [4, 4, 4]

    def test_every_row_spans_the_grid(self):
        for n in range(1, 13):
            layout = compute_layout([{"kind": "metric_card"} for _ in range(n)])
            rows: dict[int, int] = {}
            for pos in layout.values():
                rows[pos["y"]] = rows.get(pos["y"], 0) + pos["w"]
            assert set(rows.values()) == {GRID_COLS}, n

    def test_explicit_kpi_width_hints_untouched(self):
        comps = [{"kind": "metric_card", "layout": {"w": "half"}}, {"kind": "metric_card"}]
        layout = compute_layout(comps)
        assert [layout[0]["w"], layout[1]["w"]] == [6, 3]

    def test_third_width_hints(self):
        comps = [{"kind": "chart", "layout": {"w": "third"}}, {"kind": "chart", "layout": {"w": "two_thirds"}}]
        layout = compute_layout(comps)
        assert [(layout[i]["x"], layout[i]["w"]) for i in range(2)] == [(0, 4), (4, 8)]


_PLANNED_COMPS = (
    {"kind": "data_table", "title": "Pods", "rows": [{}] * 3},
    {"kind": "metric_card", "title": "CPU"},
    {"kind": "metric_card", "title": "Mem"},
    {"kind": "chart", "title": "Trend", "series": [{"data": [[1, 2]]}]},
    {"kind": "status_list", "title": "Alerts", "items": [{}]},
)


class TestPlannedRows:
    COMPS = _PLANNED_COMPS

    def _order(self, positions):
        return [self.COMPS[i]["title"] for i, _ in sorted(positions.items(), key=lambda kv: (kv[1]["y"], kv[1]["x"]))]

    def test_follows_planned_order_not_widget_type(self):
        pos = compute_layout(list(self.COMPS), rows="Alerts\nCPU | Mem | Trend\nPods")
        assert self._order(pos) == ["Alerts", "CPU", "Mem", "Trend", "Pods"]

    def test_row_shares_width_and_keeps_own_heights(self):
        pos = compute_layout(list(self.COMPS), rows="CPU | Mem | Trend")
        assert [pos[i]["w"] for i in (1, 2, 3)] == [4, 4, 4]  # thirds, not 1/4, 1/4, 1/2
        assert pos[1]["h"] < pos[3]["h"]  # metric card not stretched to chart height

    def test_unplanned_widgets_follow_planned_rows(self):
        pos = compute_layout(list(self.COMPS), rows="Trend")
        trend_bottom = pos[3]["y"] + pos[3]["h"]
        assert all(pos[i]["y"] >= trend_bottom for i in (0, 1, 2, 4))

    def test_titles_match_case_insensitively_and_unknowns_are_ignored(self):
        pos = compute_layout(list(self.COMPS), rows="alerts | Nonexistent\nPODS")
        assert self._order(pos)[:2] == ["Alerts", "Pods"]
        assert set(pos) == set(range(len(self.COMPS)))

    def test_no_rows_keeps_role_based_packing(self):
        assert compute_layout(list(self.COMPS)) == compute_layout(list(self.COMPS), rows="")

    def test_parse_rows_accepts_lists(self):
        from sre_agent.layout_engine import parse_rows

        assert parse_rows([["A", " B "], [], ["C"]]) == [["A", "B"], ["C"]]
        assert parse_rows("A | B\n\nC") == [["A", "B"], ["C"]]
