"""Tests for sre_agent.db — PostgreSQL database layer with connection pooling."""

from __future__ import annotations

import asyncio
import threading

from sre_agent.db import Database, get_database, reset_database, set_database

from .conftest import _TEST_DB_URL

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_db() -> Database:
    db = Database(_TEST_DB_URL)
    db.execute("DROP TABLE IF EXISTS t CASCADE")
    db.commit()
    return db


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------


class TestDatabasePostgres:
    def test_create_table_and_insert(self):
        db = _make_test_db()
        db.execute("CREATE TABLE IF NOT EXISTS t (id TEXT PRIMARY KEY, val INTEGER)")
        db.execute("INSERT INTO t (id, val) VALUES (%s, %s)", ("a", 1))
        db.commit()
        row = db.fetchone("SELECT * FROM t WHERE id = %s", ("a",))
        assert row is not None
        assert row["id"] == "a"
        assert row["val"] == 1
        db.execute("DROP TABLE t")
        db.commit()
        db.close()

    def test_fetchall(self):
        db = _make_test_db()
        db.execute("CREATE TABLE t (id TEXT, n INTEGER)")
        for i in range(5):
            db.execute("INSERT INTO t VALUES (%s, %s)", (f"r{i}", i))
        db.commit()
        rows = db.fetchall("SELECT * FROM t ORDER BY n")
        assert len(rows) == 5
        assert rows[0]["n"] == 0
        assert rows[4]["n"] == 4
        db.execute("DROP TABLE t")
        db.commit()
        db.close()

    def test_fetchone_returns_none(self):
        db = _make_test_db()
        db.execute("CREATE TABLE t (id TEXT)")
        db.commit()
        assert db.fetchone("SELECT * FROM t") is None
        db.execute("DROP TABLE t")
        db.commit()
        db.close()

    def test_executescript(self):
        db = _make_test_db()
        db.execute("DROP TABLE IF EXISTS a CASCADE")
        db.execute("DROP TABLE IF EXISTS b CASCADE")
        db.commit()
        db.executescript(
            "CREATE TABLE IF NOT EXISTS a (id SERIAL PRIMARY KEY, v TEXT);"
            "CREATE TABLE IF NOT EXISTS b (id SERIAL PRIMARY KEY, v TEXT);"
        )
        db.execute("INSERT INTO a (v) VALUES (%s)", ("x",))
        db.execute("INSERT INTO b (v) VALUES (%s)", ("y",))
        db.commit()
        assert db.fetchone("SELECT v FROM a")["v"] == "x"
        assert db.fetchone("SELECT v FROM b")["v"] == "y"
        db.execute("DROP TABLE a, b")
        db.commit()
        db.close()


# ---------------------------------------------------------------------------
# Query translation
# ---------------------------------------------------------------------------


class TestQueryTranslation:
    """Tests for _PARAM_RE regex — JSONB operator preservation."""

    def _translate(self, query: str) -> str:
        from sre_agent.db import _PARAM_RE

        return _PARAM_RE.sub("%s", query)

    def test_replaces_question_marks(self):
        result = self._translate("INSERT INTO t VALUES (?, ?, ?)")
        assert result == "INSERT INTO t VALUES (%s, %s, %s)"

    def test_single_placeholder(self):
        assert self._translate("SELECT * FROM t WHERE id = ?") == "SELECT * FROM t WHERE id = %s"

    def test_jsonb_has_any_preserved(self):
        result = self._translate("SELECT * FROM t WHERE data ?| array['a','b']")
        assert "?|" in result

    def test_jsonb_has_all_preserved(self):
        result = self._translate("SELECT * FROM t WHERE data ?& array['a','b']")
        assert "?&" in result

    def test_jsonb_path_exists_preserved(self):
        result = self._translate("SELECT * FROM t WHERE data @? '$.name'")
        assert "@?" in result

    def test_jsonb_double_question_preserved(self):
        result = self._translate("SELECT * FROM t WHERE data ?? 'key'")
        assert "??" in result

    def test_mixed_jsonb_and_placeholder(self):
        result = self._translate("SELECT * FROM t WHERE data ?| array['a'] AND id = ?")
        assert "?|" in result
        assert result.endswith("id = %s")

    def test_mixed_path_exists_and_placeholder(self):
        result = self._translate("SELECT * FROM t WHERE data @? '$.x' AND id = ?")
        assert "@?" in result
        assert result.endswith("id = %s")


# ---------------------------------------------------------------------------
# Connection pool
# ---------------------------------------------------------------------------


class TestConnectionPool:
    def test_concurrent_reads(self):
        """Multiple threads can read concurrently from the pool."""
        db = Database(_TEST_DB_URL)
        db.execute("DROP TABLE IF EXISTS pool_test CASCADE")
        db.execute("CREATE TABLE pool_test (id INTEGER PRIMARY KEY, val TEXT)")
        for i in range(10):
            db.execute("INSERT INTO pool_test VALUES (%s, %s)", (i, f"v{i}"))
        db.commit()

        results = []
        errors = []

        def reader(thread_id):
            try:
                rows = db.fetchall("SELECT * FROM pool_test ORDER BY id")
                results.append((thread_id, len(rows)))
            except Exception as e:
                errors.append((thread_id, e))

        threads = [threading.Thread(target=reader, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Errors in threads: {errors}"
        assert len(results) == 5
        assert all(count == 10 for _, count in results)

        db.execute("DROP TABLE pool_test")
        db.commit()
        db.close()

    def test_concurrent_writes(self):
        """Multiple threads can write concurrently using execute+commit."""
        db = Database(_TEST_DB_URL)
        db.execute("DROP TABLE IF EXISTS pool_write CASCADE")
        db.execute("CREATE TABLE pool_write (id TEXT PRIMARY KEY, thread_id INTEGER)")
        db.commit()

        errors = []

        def writer(thread_id):
            try:
                for i in range(3):
                    db.execute(
                        "INSERT INTO pool_write VALUES (%s, %s)",
                        (f"t{thread_id}-{i}", thread_id),
                    )
                    db.commit()
            except Exception as e:
                errors.append((thread_id, e))

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Errors in threads: {errors}"
        rows = db.fetchall("SELECT * FROM pool_write")
        assert len(rows) == 15  # 5 threads x 3 rows each

        db.execute("DROP TABLE pool_write")
        db.commit()
        db.close()

    def test_health_check_with_pool(self):
        db = Database(_TEST_DB_URL)
        assert db.health_check() is True
        db.close()
        assert db.health_check() is False


# ---------------------------------------------------------------------------
# Singleton management
# ---------------------------------------------------------------------------


class TestSingleton:
    def setup_method(self):
        reset_database()

    def teardown_method(self):
        reset_database()

    def test_get_database_returns_same_instance(self, monkeypatch):
        monkeypatch.setenv("PULSE_AGENT_DATABASE_URL", _TEST_DB_URL)
        db1 = get_database()
        db2 = get_database()
        assert db1 is db2

    def test_set_database_overrides(self):
        db = Database(_TEST_DB_URL)
        set_database(db)
        assert get_database() is db
        db.close()

    def test_reset_database_clears(self, monkeypatch):
        monkeypatch.setenv("PULSE_AGENT_DATABASE_URL", _TEST_DB_URL)
        db1 = get_database()
        reset_database()
        db2 = get_database()
        assert db1 is not db2


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


class TestHealthCheck:
    def test_healthy_connection(self):
        db = Database(_TEST_DB_URL)
        assert db.health_check() is True
        db.close()

    def test_closed_connection(self):
        db = Database(_TEST_DB_URL)
        db.close()
        assert db.health_check() is False


# ---------------------------------------------------------------------------
# Connection pool leak tests
# ---------------------------------------------------------------------------


class TestConnectionPoolLeaks:
    """Tests for database connection pool management — ensures connections
    are returned to the pool and not leaked on error or normal operation."""

    def test_execute_without_commit_returns_connection_on_error(self):
        """execute() that raises should rollback and return connection to pool."""
        db = Database(_TEST_DB_URL)
        db.execute("DROP TABLE IF EXISTS leak_test CASCADE")
        db.execute("CREATE TABLE leak_test (id INTEGER PRIMARY KEY)")
        db.commit()

        # Insert a row, then try to insert a duplicate (violates PK)
        db.execute("INSERT INTO leak_test VALUES (1)")
        db.commit()

        # This should raise due to duplicate PK — connection must be returned
        try:
            db.execute("INSERT INTO leak_test VALUES (1)")
        except Exception:
            pass  # Expected

        # The connection should have been returned to the pool.
        # Verify by successfully using the pool again.
        db.execute("INSERT INTO leak_test VALUES (2)")
        db.commit()

        row = db.fetchone("SELECT COUNT(*) AS cnt FROM leak_test")
        assert row["cnt"] == 2

        db.execute("DROP TABLE leak_test")
        db.commit()
        db.close()

    def test_fetchone_auto_returns_connection(self):
        """fetchone() should not hold connections after returning."""
        db = Database(_TEST_DB_URL)
        db.execute("DROP TABLE IF EXISTS leak_fo CASCADE")
        db.execute("CREATE TABLE leak_fo (id INTEGER)")
        db.execute("INSERT INTO leak_fo VALUES (1)")
        db.commit()

        # Call fetchone many times — should not exhaust pool
        for _ in range(25):
            row = db.fetchone("SELECT * FROM leak_fo")
            assert row is not None

        # Pool should still be healthy
        assert db.health_check() is True

        db.execute("DROP TABLE leak_fo")
        db.commit()
        db.close()

    def test_fetchall_auto_returns_connection(self):
        """fetchall() should not hold connections after returning."""
        db = Database(_TEST_DB_URL)
        db.execute("DROP TABLE IF EXISTS leak_fa CASCADE")
        db.execute("CREATE TABLE leak_fa (id INTEGER)")
        for i in range(5):
            db.execute("INSERT INTO leak_fa VALUES (%s)", (i,))
        db.commit()

        # Call fetchall many times — should not exhaust pool
        for _ in range(25):
            rows = db.fetchall("SELECT * FROM leak_fa ORDER BY id")
            assert len(rows) == 5

        assert db.health_check() is True

        db.execute("DROP TABLE leak_fa")
        db.commit()
        db.close()

    def test_multiple_execute_commit_cycles(self):
        """Multiple execute+commit cycles should not leak connections."""
        db = Database(_TEST_DB_URL)
        db.execute("DROP TABLE IF EXISTS leak_cycle CASCADE")
        db.execute("CREATE TABLE leak_cycle (id INTEGER)")
        db.commit()

        # Run many execute+commit cycles
        for i in range(30):
            db.execute("INSERT INTO leak_cycle VALUES (%s)", (i,))
            db.commit()

        row = db.fetchone("SELECT COUNT(*) AS cnt FROM leak_cycle")
        assert row["cnt"] == 30
        assert db.health_check() is True

        db.execute("DROP TABLE leak_cycle")
        db.commit()
        db.close()

    def test_fire_and_forget_pattern(self):
        """The fire-and-forget pattern (execute+commit in try/except) should not leak."""
        db = Database(_TEST_DB_URL)
        db.execute("DROP TABLE IF EXISTS leak_ff CASCADE")
        db.execute("CREATE TABLE leak_ff (id INTEGER PRIMARY KEY)")
        db.commit()

        # Simulate fire-and-forget with mixed success/failure
        for i in range(20):
            try:
                db.execute("INSERT INTO leak_ff VALUES (%s)", (i % 5,))  # duplicates after 5
                db.commit()
            except Exception:
                pass  # Fire-and-forget ignores errors

        # Should still be able to query
        rows = db.fetchall("SELECT * FROM leak_ff ORDER BY id")
        assert len(rows) == 5  # Only 0-4 succeed
        assert db.health_check() is True

        db.execute("DROP TABLE leak_ff")
        db.commit()
        db.close()


# ---------------------------------------------------------------------------
# BaseRepository async_db stale-pool regression
# ---------------------------------------------------------------------------


class TestAsyncPlaceholderTranslation:
    """Tests for async_db._translate_placeholders JSONB safety."""

    def test_simple_placeholders(self):
        from sre_agent.async_db import _translate_placeholders

        assert _translate_placeholders("SELECT * FROM t WHERE id = ? AND name = ?") == (
            "SELECT * FROM t WHERE id = $1 AND name = $2"
        )

    def test_jsonb_has_any_preserved(self):
        from sre_agent.async_db import _translate_placeholders

        result = _translate_placeholders("SELECT * FROM t WHERE data ?| array['a']")
        assert "?|" in result

    def test_jsonb_has_all_preserved(self):
        from sre_agent.async_db import _translate_placeholders

        result = _translate_placeholders("SELECT * FROM t WHERE data ?& array['a']")
        assert "?&" in result

    def test_jsonb_path_exists_preserved(self):
        from sre_agent.async_db import _translate_placeholders

        result = _translate_placeholders("SELECT * FROM t WHERE data @? '$.name'")
        assert "@?" in result

    def test_mixed_jsonb_and_placeholder(self):
        from sre_agent.async_db import _translate_placeholders

        result = _translate_placeholders("SELECT * FROM t WHERE data ?| array['a'] AND id = ?")
        assert "?|" in result
        assert result.endswith("id = $1")


class TestBaseRepositoryAsyncDbStalePool:
    """Verify BaseRepository.get_async_db() does not cache a stale pool
    after reset_async_database() is called."""

    def test_get_async_db_returns_fresh_pool_after_reset(self, monkeypatch):
        monkeypatch.setenv("PULSE_AGENT_DATABASE_URL", _TEST_DB_URL)

        from sre_agent.async_db import reset_async_database
        from sre_agent.repositories.base import BaseRepository

        async def _run():
            repo = BaseRepository()
            db1 = await repo.get_async_db()
            await reset_async_database()
            db2 = await repo.get_async_db()
            assert db1 is not db2, "get_async_db() returned stale cached pool after reset"
            await reset_async_database()

        asyncio.run(_run())
