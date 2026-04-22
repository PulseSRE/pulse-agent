"""Tests for view plan executor."""

from __future__ import annotations


class TestPropsOnlyWidgets:
    def test_resolution_tracker_from_props(self):
        from sre_agent.view_executor import execute_view_plan

        plan = [
            {
                "kind": "resolution_tracker",
                "title": "Investigation Steps",
                "props": {
                    "steps": [
                        {"title": "Checked logs", "status": "complete"},
                        {"title": "Increase memory", "status": "pending"},
                    ]
                },
            }
        ]
        item = {
            "id": "test-1",
            "title": "OOM",
            "metadata": {"investigation_confidence": 0.85, "investigation_summary": "Memory leak"},
        }
        layout = execute_view_plan(plan, item)
        tracker = [w for w in layout if w["kind"] == "resolution_tracker"]
        assert len(tracker) == 1
        assert tracker[0]["title"] == "Investigation Steps"

    def test_blast_radius_from_props(self):
        from sre_agent.view_executor import execute_view_plan

        plan = [
            {
                "kind": "blast_radius",
                "title": "Impact",
                "props": {"affected_count": 3, "affected_resources": [{"kind": "Pod", "name": "web-1"}]},
            }
        ]
        item = {"id": "test-2", "title": "Crash", "metadata": {}}
        layout = execute_view_plan(plan, item)
        blast = [w for w in layout if w["kind"] == "blast_radius"]
        assert len(blast) == 1

    def test_empty_plan_still_has_header(self):
        from sre_agent.view_executor import execute_view_plan

        item = {"id": "x", "title": "Test", "metadata": {"investigation_summary": "Something broke"}}
        layout = execute_view_plan([], item)
        assert len(layout) >= 1

    def test_max_6_viewplan_widgets(self):
        from sre_agent.view_executor import execute_view_plan

        plan = [{"kind": "info_card_grid", "title": f"W{i}", "props": {"cards": []}} for i in range(10)]
        item = {"id": "x", "title": "x", "metadata": {}}
        layout = execute_view_plan(plan, item)
        viewplan_widgets = [w for w in layout if w.get("title", "").startswith("W")]
        assert len(viewplan_widgets) <= 6

    def test_invalid_kind_skipped(self):
        from sre_agent.view_executor import execute_view_plan

        plan = [
            {"kind": "nonexistent_widget", "title": "Bad", "props": {}},
            {"kind": "resolution_tracker", "title": "Good", "props": {"steps": []}},
        ]
        item = {"id": "x", "title": "x", "metadata": {}}
        layout = execute_view_plan(plan, item)
        kinds = [w["kind"] for w in layout]
        assert "nonexistent_widget" not in kinds
        assert "resolution_tracker" in kinds

    def test_confidence_badge_always_present(self):
        from sre_agent.view_executor import execute_view_plan

        plan = [{"kind": "resolution_tracker", "title": "Steps", "props": {"steps": []}}]
        item = {"id": "x", "title": "x", "metadata": {"investigation_confidence": 0.85}}
        layout = execute_view_plan(plan, item)
        assert layout[0]["kind"] == "confidence_badge"
        assert layout[0]["props"]["score"] == 0.85

    def test_summary_header_present_when_investigation_exists(self):
        from sre_agent.view_executor import execute_view_plan

        plan = []
        item = {
            "id": "x",
            "title": "x",
            "metadata": {
                "investigation_summary": "Memory leak detected",
                "suspected_cause": "Unbounded cache",
                "recommended_fix": "Set memory limit to 512Mi",
            },
        }
        layout = execute_view_plan(plan, item)
        header = [w for w in layout if w["kind"] == "info_card_grid"]
        assert len(header) >= 1
        cards = header[0]["props"]["cards"]
        assert any("Memory leak" in c["value"] for c in cards)
