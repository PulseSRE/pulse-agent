"""Tests for the migration list itself, independent of any database."""

from __future__ import annotations


class TestMigrationVersionsAreUnique:
    """A duplicate version is skipped forever on any cluster past that number."""

    def test_no_duplicate_versions(self):
        import collections

        from sre_agent.db_migrations import MIGRATIONS

        counts = collections.Counter(v for v, _name, _fn in MIGRATIONS)
        dupes = {v: c for v, c in counts.items() if c > 1}
        assert not dupes, (
            f"duplicate migration versions {sorted(dupes)}. run_migrations computes "
            "MAX(version) once and skips anything <= it, so on a database already past "
            "that number only one of them ever runs — and a fresh CI database applies "
            "both, so the tests pass while production silently lacks the tables."
        )

    def test_versions_are_ascending(self):
        from sre_agent.db_migrations import MIGRATIONS

        versions = [v for v, _n, _f in MIGRATIONS]
        assert versions == sorted(versions), "migrations must be listed in ascending order"

    def test_names_are_unique(self):
        import collections

        from sre_agent.db_migrations import MIGRATIONS

        counts = collections.Counter(n for _v, n, _f in MIGRATIONS)
        assert not [n for n, c in counts.items() if c > 1]
