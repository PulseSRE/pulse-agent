"""Tests for ORCA skill selector."""

from __future__ import annotations

from sre_agent.db import Database, set_database
from sre_agent.db_migrations import run_migrations

from .conftest import _TEST_DB_URL


def _make_test_db() -> Database:
    db = Database(_TEST_DB_URL)
    db.execute("DROP TABLE IF EXISTS skill_selection_log CASCADE")
    db.commit()
    return db


class TestMigration:
    def test_migration_creates_table(self):
        db = _make_test_db()
        db.execute("DELETE FROM schema_migrations WHERE version >= 14")
        db.commit()
        set_database(db)
        run_migrations(db)

        row = db.fetchone(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'skill_selection_log') AS exists"
        )
        assert row["exists"] is True

    def test_skill_selection_log_columns(self):
        db = _make_test_db()
        db.execute("DELETE FROM schema_migrations WHERE version >= 14")
        db.commit()
        set_database(db)
        run_migrations(db)
        row = db.fetchone(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'skill_selection_log' AND column_name = 'channel_scores'"
        )
        assert row is not None

    def test_skill_selection_log_jsonb_columns(self):
        db = _make_test_db()
        db.execute("DELETE FROM schema_migrations WHERE version >= 14")
        db.commit()
        set_database(db)
        run_migrations(db)
        row = db.fetchone(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'skill_selection_log' AND column_name = 'fused_scores'"
        )
        assert row is not None
        assert row["data_type"] == "jsonb"
