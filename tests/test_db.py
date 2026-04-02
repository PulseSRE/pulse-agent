"""Tests for sre_agent.db — PostgreSQL database layer with connection pooling."""

from __future__ import annotations

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
    def test_replaces_question_marks(self):
        db = Database(_TEST_DB_URL)
        result = db._translate_query("INSERT INTO t VALUES (?, ?, ?)")
        assert result == "INSERT INTO t VALUES (%s, %s, %s)"
        db.close()


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
