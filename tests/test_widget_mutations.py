"""Tests for dashboard widget mutation actions."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest


def _mock_view(widgets):
    return {
        "id": "cv-test",
        "title": "Test View",
        "description": "Test",
        "layout": list(widgets),
        "owner": "test-user",
    }


def _make_table_widget():
    return {
        "kind": "data_table",
        "title": "Pods",
        "columns": [
            {"id": "name", "header": "Name"},
            {"id": "namespace", "header": "Namespace"},
            {"id": "status", "header": "Status"},
            {"id": "restarts", "header": "Restarts"},
            {"id": "age", "header": "Age"},
        ],
        "rows": [
            {
                "name": "nginx",
                "namespace": "production",
                "status": "Running",
                "restarts": 0,
                "age": "5d",
                "_gvr": "v1~pods",
            },
            {
                "name": "api-server",
                "namespace": "production",
                "status": "CrashLoop",
                "restarts": 12,
                "age": "2h",
                "_gvr": "v1~pods",
            },
            {
                "name": "redis",
                "namespace": "staging",
                "status": "Running",
                "restarts": 1,
                "age": "3d",
                "_gvr": "v1~pods",
            },
        ],
    }


def _make_chart_widget():
    return {
        "kind": "chart",
        "title": "CPU Usage",
        "chartType": "line",
        "query": "rate(container_cpu_usage_seconds_total[5m])",
    }


@pytest.fixture
def mock_db():
    with (
        patch("sre_agent.db.get_view") as mock_get,
        patch("sre_agent.db.update_view") as mock_update,
        patch("sre_agent.view_tools.get_current_user", return_value="test-user"),
    ):
        yield mock_get, mock_update


class TestUpdateColumns:
    def test_removes_columns(self, mock_db):
        mock_get, mock_update = mock_db
        mock_get.return_value = _mock_view([_make_table_widget()])

        from sre_agent.view_tools import update_view_widgets

        result = update_view_widgets.call(
            {
                "view_id": "cv-test",
                "action": "update_columns",
                "widget_index": 0,
                "params_json": json.dumps({"columns": ["name", "status", "age"]}),
            }
        )
        assert "Updated columns" in result
        layout = mock_update.call_args[1]["layout"]
        col_ids = [c["id"] for c in layout[0]["columns"]]
        assert "namespace" not in col_ids
        assert "name" in col_ids

    def test_filters_rows(self, mock_db):
        mock_get, mock_update = mock_db
        mock_get.return_value = _mock_view([_make_table_widget()])

        from sre_agent.view_tools import update_view_widgets

        update_view_widgets.call(
            {
                "view_id": "cv-test",
                "action": "update_columns",
                "widget_index": 0,
                "params_json": json.dumps({"columns": ["name", "status"]}),
            }
        )
        layout = mock_update.call_args[1]["layout"]
        row = layout[0]["rows"][0]
        assert "name" in row
        assert "namespace" not in row
        assert "_gvr" in row

    def test_rejects_non_table(self, mock_db):
        mock_get, _ = mock_db
        mock_get.return_value = _mock_view([_make_chart_widget()])

        from sre_agent.view_tools import update_view_widgets

        result = update_view_widgets.call(
            {
                "view_id": "cv-test",
                "action": "update_columns",
                "widget_index": 0,
                "params_json": json.dumps({"columns": ["name"]}),
            }
        )
        assert "not a data_table" in result


class TestSortBy:
    def test_sorts_ascending(self, mock_db):
        mock_get, mock_update = mock_db
        mock_get.return_value = _mock_view([_make_table_widget()])

        from sre_agent.view_tools import update_view_widgets

        result = update_view_widgets.call(
            {
                "view_id": "cv-test",
                "action": "sort_by",
                "widget_index": 0,
                "params_json": json.dumps({"column": "restarts", "direction": "asc"}),
            }
        )
        assert "Sorted" in result
        layout = mock_update.call_args[1]["layout"]
        restarts = [r["restarts"] for r in layout[0]["rows"]]
        assert restarts == sorted(restarts)

    def test_sorts_descending(self, mock_db):
        mock_get, mock_update = mock_db
        mock_get.return_value = _mock_view([_make_table_widget()])

        from sre_agent.view_tools import update_view_widgets

        update_view_widgets.call(
            {
                "view_id": "cv-test",
                "action": "sort_by",
                "widget_index": 0,
                "params_json": json.dumps({"column": "restarts", "direction": "desc"}),
            }
        )
        layout = mock_update.call_args[1]["layout"]
        restarts = [r["restarts"] for r in layout[0]["rows"]]
        assert restarts == sorted(restarts, reverse=True)


class TestFilterBy:
    def test_adds_filter(self, mock_db):
        mock_get, mock_update = mock_db
        mock_get.return_value = _mock_view([_make_table_widget()])

        from sre_agent.view_tools import update_view_widgets

        result = update_view_widgets.call(
            {
                "view_id": "cv-test",
                "action": "filter_by",
                "widget_index": 0,
                "params_json": json.dumps({"column": "status", "operator": "!=", "value": "Running"}),
            }
        )
        assert "Added filter" in result
        layout = mock_update.call_args[1]["layout"]
        filters = layout[0].get("_filters", [])
        assert len(filters) == 1
        assert filters[0]["column"] == "status"


class TestChangeKind:
    def test_changes_kind(self, mock_db):
        mock_get, mock_update = mock_db
        mock_get.return_value = _mock_view([_make_table_widget()])

        from sre_agent.view_tools import update_view_widgets

        result = update_view_widgets.call(
            {
                "view_id": "cv-test",
                "action": "change_kind",
                "widget_index": 0,
                "params_json": json.dumps({"new_kind": "chart"}),
            }
        )
        assert "Changed widget" in result
        layout = mock_update.call_args[1]["layout"]
        assert layout[0]["kind"] == "chart"

    def test_rejects_invalid_kind(self, mock_db):
        mock_get, _ = mock_db
        mock_get.return_value = _mock_view([_make_table_widget()])

        from sre_agent.view_tools import update_view_widgets

        result = update_view_widgets.call(
            {
                "view_id": "cv-test",
                "action": "change_kind",
                "widget_index": 0,
                "params_json": json.dumps({"new_kind": "invalid_xyz"}),
            }
        )
        assert "Invalid kind" in result


class TestUpdateQuery:
    def test_updates_query(self, mock_db):
        mock_get, mock_update = mock_db
        mock_get.return_value = _mock_view([_make_chart_widget()])

        from sre_agent.view_tools import update_view_widgets

        result = update_view_widgets.call(
            {
                "view_id": "cv-test",
                "action": "update_query",
                "widget_index": 0,
                "params_json": json.dumps({"query": "container_memory_working_set_bytes"}),
            }
        )
        assert "Updated query" in result
        layout = mock_update.call_args[1]["layout"]
        assert layout[0]["query"] == "container_memory_working_set_bytes"


class TestSetRenderOverride:
    def test_sets_override(self, mock_db):
        mock_get, mock_update = mock_db
        mock_get.return_value = _mock_view([_make_table_widget()])

        from sre_agent.view_tools import update_view_widgets

        result = update_view_widgets.call(
            {
                "view_id": "cv-test",
                "action": "set_render_override",
                "widget_index": 0,
                "params_json": json.dumps({"render_as": "bar_list", "render_options": {"label_column": "name"}}),
            }
        )
        assert "Set render override" in result
        layout = mock_update.call_args[1]["layout"]
        assert layout[0]["render_as"] == "bar_list"

    def test_rejects_invalid_render_as(self, mock_db):
        mock_get, _ = mock_db
        mock_get.return_value = _mock_view([_make_table_widget()])

        from sre_agent.view_tools import update_view_widgets

        result = update_view_widgets.call(
            {
                "view_id": "cv-test",
                "action": "set_render_override",
                "widget_index": 0,
                "params_json": json.dumps({"render_as": "invalid_xyz"}),
            }
        )
        assert "Invalid render_as" in result
