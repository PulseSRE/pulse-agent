"""Every agent edit to a dashboard leaves a version to undo to.

Only the agent's whole-view update and add/remove-widget used to snapshot.
Typed mutations ("move alerts to the top", "make it a bar chart", "fix the
query") and optimize_view did not, so the UI had nothing to undo an edit to,
and the undo endpoint jumped back to an older version, discarding every edit
in between.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sre_agent import db

OWNER = "snapshot-test-user"


def _widgets():
    return [
        {"kind": "chart", "title": "CPU", "chartType": "line", "query": "up"},
        {"kind": "status_list", "title": "Alerts", "items": [{"name": "a", "status": "error"}]},
        {"kind": "metric_card", "title": "Nodes", "value": "4"},
    ]


@pytest.fixture
def view():
    view_id = "cv-snapshot-test"
    db.save_view(OWNER, view_id, "Prod", "", _widgets())
    with patch("sre_agent.view_tools.get_current_user", return_value=OWNER):
        yield view_id


def _titles(layout):
    return [w["title"] for w in layout]


def test_a_typed_mutation_snapshots_the_layout_before_it(view):
    from sre_agent.view_tools import update_view_widgets

    update_view_widgets.call({"view_id": view, "action": "move_widget", "widget_index": 1, "new_title": "0"})

    current = db.get_view(view, OWNER)
    assert _titles(current["layout"])[0] == "Alerts"
    latest = db.list_view_versions(view, limit=1)[0]
    assert latest["action"] == "agent:move_widget"
    assert _titles(latest["layout"]) == ["CPU", "Alerts", "Nodes"]  # the state to undo to


def test_undo_restores_the_state_before_the_last_agent_edit(view):
    from sre_agent.view_tools import update_view_widgets

    update_view_widgets.call({"view_id": view, "action": "change_chart_type", "widget_index": 0, "new_title": "bar"})
    update_view_widgets.call({"view_id": view, "action": "move_widget", "widget_index": 1, "new_title": "0"})
    latest = db.list_view_versions(view, limit=1)[0]
    assert db.restore_view_version(view, OWNER, latest["version"])

    restored = db.get_view(view, OWNER)
    # Only the move is undone; the earlier chart-type change survives.
    assert _titles(restored["layout"]) == ["CPU", "Alerts", "Nodes"]
    assert restored["layout"][0]["chartType"] == "bar"


def test_optimize_view_snapshots(view):
    from sre_agent.view_tools import optimize_view

    optimize_view.call({"view_id": view})
    assert db.list_view_versions(view, limit=1)[0]["action"] == "agent:optimize_view"


def test_a_failed_snapshot_does_not_block_the_edit(view):
    from sre_agent.view_tools import update_view_widgets

    with patch("sre_agent.db.snapshot_view", side_effect=RuntimeError("db hiccup")):
        update_view_widgets.call(
            {"view_id": view, "action": "rename_widget", "widget_index": 2, "new_title": "Nodes Ready"}
        )
    assert _titles(db.get_view(view, OWNER)["layout"])[2] == "Nodes Ready"
