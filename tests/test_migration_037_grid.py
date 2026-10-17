"""Migration 037 — dashboards move from the 4-column grid to 12 columns.

A saved position that is not rescaled renders at a quarter of its width the
moment the UI switches grids, and so does every version a user restores with
undo. Both tables are covered, and the migration must never apply twice.
"""

from __future__ import annotations

import json

import pytest

from sre_agent.db import get_database
from sre_agent.db_migrations import _migrate_037_grid_12, _rescale_view_json

OLD_POSITIONS = {
    "0": {"x": 0, "y": 0, "w": 4, "h": 3},
    "1": {"x": 0, "y": 3, "w": 2, "h": 10},
    "2": {"x": 2, "y": 3, "w": 2, "h": 10},
    "3": {"x": 3, "y": 13, "w": 1, "h": 3},
}
NEW_POSITIONS = {
    "0": {"x": 0, "y": 0, "w": 12, "h": 3},
    "1": {"x": 0, "y": 3, "w": 6, "h": 10},
    "2": {"x": 6, "y": 3, "w": 6, "h": 10},
    "3": {"x": 9, "y": 13, "w": 3, "h": 3},
}


@pytest.fixture(autouse=True)
def _clean_views():
    db = get_database()
    db.execute("DELETE FROM view_versions")
    db.execute("DELETE FROM views")
    db.commit()
    yield
    db.execute("DELETE FROM view_versions")
    db.execute("DELETE FROM views")
    db.commit()


def _insert_view(view_id: str, positions: dict, layout: list | None = None) -> None:
    db = get_database()
    db.execute(
        "INSERT INTO views (id, owner, title, layout, positions, created_at, updated_at) "
        "VALUES (%s, 'alice', 'Prod', %s, %s, '2026-10-01', '2026-10-01')",
        (view_id, json.dumps(layout or [{"kind": "chart"}]), json.dumps(positions)),
    )
    db.execute(
        "INSERT INTO view_versions (view_id, version, action, layout, positions, title, created_at) "
        "VALUES (%s, 1, 'agent:create', %s, %s, 'Prod', '2026-10-01')",
        (view_id, json.dumps(layout or [{"kind": "chart"}]), json.dumps(positions)),
    )
    db.commit()


def _run() -> None:
    db = get_database()
    _migrate_037_grid_12(db)
    db.commit()


def _positions(table: str, view_id: str) -> dict:
    col = "id" if table == "views" else "view_id"
    row = get_database().fetchone(f"SELECT positions FROM {table} WHERE {col} = %s", (view_id,))
    return json.loads(row["positions"])


def test_live_views_and_their_history_are_rescaled():
    _insert_view("cv-1", OLD_POSITIONS)
    _run()
    assert _positions("views", "cv-1") == NEW_POSITIONS
    assert _positions("view_versions", "cv-1") == NEW_POSITIONS  # undo restores a 12-column layout


def test_widths_never_overflow_the_grid():
    positions, _ = _rescale_view_json(json.dumps({"0": {"x": 3, "y": 0, "w": 4, "h": 2}}), None)
    cell = json.loads(positions)["0"]
    assert cell["x"] + cell["w"] <= 12


def test_positions_merged_into_widgets_are_rescaled_too():
    layout = [{"kind": "chart", "x": 2, "y": 0, "w": 2, "h": 10}, {"kind": "chart"}]
    _insert_view("cv-2", {"0": {"x": 2, "y": 0, "w": 2, "h": 10}}, layout)
    _run()
    row = get_database().fetchone("SELECT layout FROM views WHERE id = %s", ("cv-2",))
    stored = json.loads(row["layout"])
    assert (stored[0]["x"], stored[0]["w"]) == (6, 6)
    assert "x" not in stored[1]


def test_malformed_rows_are_left_alone():
    db = get_database()
    db.execute(
        "INSERT INTO views (id, owner, title, layout, positions, created_at, updated_at) "
        "VALUES ('cv-bad', 'alice', 'Bad', 'not json', '{\"0\": \"nope\"}', '2026-10-01', '2026-10-01')"
    )
    db.commit()
    _run()
    row = db.fetchone("SELECT layout, positions FROM views WHERE id = %s", ("cv-bad",))
    assert row["layout"] == "not json"
    assert json.loads(row["positions"]) == {"0": "nope"}


def test_applies_once_through_run_migrations():
    """A second startup must not scale 12-column positions to 36."""
    from sre_agent.db_migrations import run_migrations

    db = get_database()
    db.execute("DELETE FROM schema_migrations WHERE version = 37")
    db.commit()
    _insert_view("cv-3", OLD_POSITIONS)
    run_migrations(db)
    run_migrations(db)
    assert _positions("views", "cv-3") == NEW_POSITIONS
