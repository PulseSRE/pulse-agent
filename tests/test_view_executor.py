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


# ---------------------------------------------------------------------------
# Tool-backed widgets
# ---------------------------------------------------------------------------

from unittest.mock import MagicMock, patch


class TestToolBackedWidgets:
    def test_tool_returns_tuple_extracts_component(self):
        from sre_agent.view_executor import execute_view_plan

        mock_tool = MagicMock()
        mock_tool.call.return_value = (
            "2 pods found",
            {"kind": "status_list", "items": [{"name": "pod-1", "status": "healthy"}]},
        )

        with patch("sre_agent.view_executor._resolve_tool", return_value=mock_tool):
            plan = [{"kind": "status_list", "title": "Pods", "tool": "list_pods", "args": {"namespace": "prod"}}]
            layout = execute_view_plan(plan, {"id": "x", "title": "x", "metadata": {}})

        pods = [w for w in layout if w.get("title") == "Pods"]
        assert len(pods) == 1
        mock_tool.call.assert_called_once_with({"namespace": "prod"})

    def test_tool_returns_string_wraps_in_info_card(self):
        from sre_agent.view_executor import execute_view_plan

        mock_tool = MagicMock()
        mock_tool.call.return_value = "No events found"

        with patch("sre_agent.view_executor._resolve_tool", return_value=mock_tool):
            plan = [{"kind": "data_table", "title": "Events", "tool": "get_events", "args": {"namespace": "prod"}}]
            layout = execute_view_plan(plan, {"id": "x", "title": "x", "metadata": {}})

        events = [w for w in layout if w.get("title") == "Events"]
        assert len(events) == 1
        assert events[0]["kind"] == "info_card_grid"

    def test_write_tool_rejected(self):
        from sre_agent.view_executor import execute_view_plan

        plan = [{"kind": "action_button", "title": "Delete", "tool": "delete_pod", "args": {"name": "x"}}]
        with patch("sre_agent.view_executor.WRITE_TOOL_NAMES", {"delete_pod"}):
            layout = execute_view_plan(plan, {"id": "x", "title": "x", "metadata": {}})
        assert not any(w.get("title") == "Delete" for w in layout)

    def test_unknown_tool_skipped(self):
        from sre_agent.view_executor import execute_view_plan

        with patch("sre_agent.view_executor._resolve_tool", return_value=None):
            plan = [{"kind": "data_table", "title": "X", "tool": "fake_tool", "args": {}}]
            layout = execute_view_plan(plan, {"id": "x", "title": "x", "metadata": {}})
        assert not any(w.get("title") == "X" for w in layout)

    def test_tool_timeout_skips_widget(self):
        from sre_agent.view_executor import execute_view_plan

        mock_tool = MagicMock()

        def slow_call(args):
            import time

            time.sleep(30)

        mock_tool.call.side_effect = slow_call

        with patch("sre_agent.view_executor._resolve_tool", return_value=mock_tool):
            with patch("sre_agent.view_executor._TOOL_TIMEOUT", 0.1):
                plan = [{"kind": "data_table", "title": "Slow", "tool": "slow_tool", "args": {}}]
                layout = execute_view_plan(plan, {"id": "x", "title": "x", "metadata": {}})
        assert not any(w.get("title") == "Slow" for w in layout)

    def test_tool_exception_skips_widget(self):
        from sre_agent.view_executor import execute_view_plan

        mock_tool = MagicMock()
        mock_tool.call.side_effect = RuntimeError("connection refused")

        with patch("sre_agent.view_executor._resolve_tool", return_value=mock_tool):
            plan = [
                {"kind": "data_table", "title": "Fail", "tool": "fail_tool", "args": {}},
                {"kind": "resolution_tracker", "title": "Good", "props": {"steps": []}},
            ]
            layout = execute_view_plan(plan, {"id": "x", "title": "x", "metadata": {}})
        assert not any(w.get("title") == "Fail" for w in layout)
        assert any(w.get("title") == "Good" for w in layout)

    def test_non_dict_args_skipped(self):
        from sre_agent.view_executor import execute_view_plan

        plan = [{"kind": "data_table", "title": "Bad", "tool": "get_events", "args": "not a dict"}]
        layout = execute_view_plan(plan, {"id": "x", "title": "x", "metadata": {}})
        assert not any(w.get("title") == "Bad" for w in layout)


# ---------------------------------------------------------------------------
# validate_view_plan
# ---------------------------------------------------------------------------


class TestValidateViewPlan:
    def test_accepts_valid_widgets(self):
        from sre_agent.view_executor import validate_view_plan

        plan = [
            {"kind": "chart", "title": "CPU", "props": {"query": "up"}},
            {"kind": "resolution_tracker", "title": "Steps", "props": {"steps": []}},
        ]
        validated = validate_view_plan(plan)
        assert len(validated) == 2

    def test_rejects_invalid_kind(self):
        from sre_agent.view_executor import validate_view_plan

        plan = [{"kind": "fake_widget", "title": "X", "props": {}}]
        assert len(validate_view_plan(plan)) == 0

    def test_rejects_write_tool(self):
        from sre_agent.view_executor import validate_view_plan

        plan = [{"kind": "action_button", "title": "Delete", "tool": "delete_pod", "args": {}}]
        with patch("sre_agent.view_executor.WRITE_TOOL_NAMES", {"delete_pod"}):
            with patch("sre_agent.view_executor.TOOL_REGISTRY", {"delete_pod": MagicMock()}):
                assert len(validate_view_plan(plan)) == 0

    def test_rejects_unknown_tool(self):
        from sre_agent.view_executor import validate_view_plan

        plan = [{"kind": "data_table", "title": "X", "tool": "nonexistent_tool", "args": {}}]
        assert len(validate_view_plan(plan)) == 0

    def test_caps_at_max_widgets(self):
        from sre_agent.view_executor import validate_view_plan

        plan = [{"kind": "chart", "title": f"W{i}", "props": {}} for i in range(10)]
        assert len(validate_view_plan(plan)) <= 6

    def test_passes_through_valid_props_and_tool(self):
        from sre_agent.view_executor import validate_view_plan

        plan = [{"kind": "chart", "title": "CPU", "props": {"query": "up"}, "extra": "preserved"}]
        validated = validate_view_plan(plan)
        assert len(validated) == 1
        assert validated[0]["extra"] == "preserved"


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------

import time


class TestStaleness:
    def test_stale_plan_skips_tool_widgets_keeps_props(self):
        from sre_agent.view_executor import execute_view_plan

        stale_ts = int(time.time()) - 3600
        plan = [
            {"kind": "data_table", "title": "Events", "tool": "get_events", "args": {"namespace": "prod"}},
            {
                "kind": "resolution_tracker",
                "title": "Steps",
                "props": {"steps": [{"title": "Done", "status": "complete"}]},
            },
        ]
        item = {"id": "x", "title": "x", "metadata": {"view_plan_at": stale_ts}}
        layout = execute_view_plan(plan, item)
        kinds = [w["kind"] for w in layout]
        assert "resolution_tracker" in kinds
        assert "data_table" not in kinds

    def test_stale_plan_shows_outdated_notice(self):
        from sre_agent.view_executor import execute_view_plan

        stale_ts = int(time.time()) - 3600
        plan = [{"kind": "data_table", "title": "Events", "tool": "get_events", "args": {}}]
        item = {"id": "x", "title": "x", "metadata": {"view_plan_at": stale_ts}}
        layout = execute_view_plan(plan, item)
        titles = [w.get("title", "") for w in layout]
        assert any("Outdated" in t for t in titles)

    def test_fresh_plan_executes_tool_widgets(self):
        from sre_agent.view_executor import execute_view_plan

        mock_tool = MagicMock()
        mock_tool.call.return_value = ("data", {"kind": "data_table", "rows": []})
        fresh_ts = int(time.time())
        plan = [{"kind": "data_table", "title": "Events", "tool": "get_events", "args": {}}]
        item = {"id": "x", "title": "x", "metadata": {"view_plan_at": fresh_ts}}

        with patch("sre_agent.view_executor._resolve_tool", return_value=mock_tool):
            layout = execute_view_plan(plan, item)
        assert any(w.get("title") == "Events" for w in layout)

    def test_no_timestamp_assumes_fresh(self):
        from sre_agent.view_executor import execute_view_plan

        mock_tool = MagicMock()
        mock_tool.call.return_value = ("data", {"kind": "data_table", "rows": []})
        plan = [{"kind": "data_table", "title": "Events", "tool": "get_events", "args": {}}]
        item = {"id": "x", "title": "x", "metadata": {}}

        with patch("sre_agent.view_executor._resolve_tool", return_value=mock_tool):
            layout = execute_view_plan(plan, item)
        assert any(w.get("title") == "Events" for w in layout)

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


# ---------------------------------------------------------------------------
# Integration with inbox.py
# ---------------------------------------------------------------------------


class TestIntegrationWithInbox:
    def test_generate_view_uses_view_plan(self):
        from sre_agent.inbox import _generate_view_for_item

        item = {
            "id": "inb-test",
            "title": "OOM killed",
            "summary": "Pod OOM",
            "severity": "critical",
            "namespace": "prod",
            "resources": [],
            "metadata": {
                "view_plan": [
                    {"kind": "resolution_tracker", "title": "Steps", "props": {"steps": []}},
                ],
                "view_plan_at": int(time.time()),
                "investigation_summary": "OOM due to memory leak",
                "investigation_confidence": 0.85,
            },
        }

        with (
            patch("sre_agent.inbox.get_database") as mock_db,
            patch("sre_agent.db.save_view") as mock_save,
            patch("sre_agent.inbox._publish_event"),
        ):
            mock_db.return_value = MagicMock()
            _generate_view_for_item("inb-test", item)

        mock_save.assert_called_once()
        call_kwargs = mock_save.call_args
        layout = call_kwargs.kwargs.get("layout") or call_kwargs[1].get("layout")
        assert any(c["kind"] == "confidence_badge" for c in layout)
        assert any(c["kind"] == "resolution_tracker" for c in layout)

    def test_generate_view_falls_back_without_view_plan(self):
        from sre_agent.inbox import _generate_view_for_item

        item = {
            "id": "inb-old",
            "title": "Old item",
            "summary": "Legacy",
            "severity": "warning",
            "namespace": "prod",
            "resources": [],
            "metadata": {
                "investigation_summary": "Something happened",
            },
        }

        with (
            patch("sre_agent.inbox.get_database") as mock_db,
            patch("sre_agent.db.save_view") as mock_save,
            patch("sre_agent.inbox._publish_event"),
            patch(
                "sre_agent.inbox._generate_smart_layout",
                return_value=[{"kind": "info_card_grid", "title": "Old", "props": {}}],
            ),
        ):
            mock_db.return_value = MagicMock()
            _generate_view_for_item("inb-old", item)

        mock_save.assert_called_once()


class TestTokenForwarding:
    def test_execute_tool_widget_sets_contextvar(self):
        from unittest.mock import MagicMock, patch

        from sre_agent.k8s_client import _user_token_var

        captured = []

        def mock_call(args):
            captured.append(_user_token_var.get())
            return ("result", {"kind": "info_card_grid", "props": {}})

        mock_tool = MagicMock()
        mock_tool.call = mock_call

        with patch("sre_agent.view_executor._resolve_tool", return_value=mock_tool):
            with patch("sre_agent.view_executor.WRITE_TOOL_NAMES", set()):
                from sre_agent.view_executor import _execute_tool_widget

                widget = {"tool": "test_tool", "args": {}, "kind": "info_card_grid", "title": "Test"}
                _execute_tool_widget(widget, item_id="test", user_token="view-token")

        assert captured == ["view-token"]
        assert _user_token_var.get() is None

    def test_execute_tool_widget_no_token(self):
        from unittest.mock import MagicMock, patch

        from sre_agent.k8s_client import _user_token_var

        captured = []

        def mock_call(args):
            captured.append(_user_token_var.get())
            return "result"

        mock_tool = MagicMock()
        mock_tool.call = mock_call

        with patch("sre_agent.view_executor._resolve_tool", return_value=mock_tool):
            with patch("sre_agent.view_executor.WRITE_TOOL_NAMES", set()):
                from sre_agent.view_executor import _execute_tool_widget

                widget = {"tool": "test_tool", "args": {}, "kind": "info_card_grid", "title": "Test"}
                _execute_tool_widget(widget, item_id="test")

        assert captured == [None]
