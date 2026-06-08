"""Tests for sre_agent.async_db — async PostgreSQL layer with asyncpg."""

from __future__ import annotations

import asyncio

import pytest

from sre_agent.async_db import AsyncDatabase, _translate_placeholders


class TestPlaceholderTranslation:
    """Verify ? → $N conversion with JSONB safety."""

    def test_simple_placeholder(self):
        assert _translate_placeholders("SELECT * FROM t WHERE id = ?") == "SELECT * FROM t WHERE id = $1"

    def test_multiple_placeholders(self):
        result = _translate_placeholders("INSERT INTO t VALUES (?, ?, ?)")
        assert result == "INSERT INTO t VALUES ($1, $2, $3)"

    def test_jsonb_has_any_preserved(self):
        result = _translate_placeholders("SELECT * FROM t WHERE data ?| array['a']")
        assert "?|" in result

    def test_jsonb_has_all_preserved(self):
        result = _translate_placeholders("SELECT * FROM t WHERE data ?& array['a']")
        assert "?&" in result

    def test_jsonb_path_exists_preserved(self):
        result = _translate_placeholders("SELECT * FROM t WHERE data @? '$.name'")
        assert "@?" in result

    def test_mixed_jsonb_and_placeholder(self):
        result = _translate_placeholders("SELECT * FROM t WHERE data ?| array['a'] AND id = ?")
        assert "?|" in result
        assert result.endswith("id = $1")

    def test_sequential_numbering(self):
        result = _translate_placeholders("WHERE a = ? AND b = ? AND c = ?")
        assert "$1" in result
        assert "$2" in result
        assert "$3" in result


class TestAsyncDatabaseUnit:
    """Unit tests that don't require a running PostgreSQL."""

    def test_init(self):
        db = AsyncDatabase()
        assert db._pool is None
        assert db._url == ""

    @pytest.mark.asyncio
    async def test_health_check_without_pool(self):
        db = AsyncDatabase()
        db._pool = None
        db._url = "postgresql://invalid:5432/nodb"
        result = await db.health_check()
        assert result is False

    @pytest.mark.asyncio
    async def test_close_without_pool(self):
        db = AsyncDatabase()
        await db.close()
        assert db._pool is None


@pytest.mark.requires_pg
class TestAsyncDatabaseIntegration:
    """Integration tests requiring a running PostgreSQL (pulse-test-pg)."""

    @pytest.mark.asyncio
    async def test_connect_and_health_check(self):
        from tests.conftest import _TEST_DB_URL

        db = AsyncDatabase()
        await db.connect(url=_TEST_DB_URL)
        try:
            assert await db.health_check() is True
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_execute_and_fetchone(self):
        from tests.conftest import _TEST_DB_URL

        db = AsyncDatabase()
        await db.connect(url=_TEST_DB_URL)
        try:
            await db.execute("CREATE TABLE IF NOT EXISTS _async_test (id TEXT PRIMARY KEY, val INTEGER)")
            await db.execute("DELETE FROM _async_test")
            await db.execute("INSERT INTO _async_test (id, val) VALUES ($1, $2)", "a", 42)
            row = await db.fetchone("SELECT * FROM _async_test WHERE id = $1", "a")
            assert row is not None
            assert row["id"] == "a"
            assert row["val"] == 42
        finally:
            await db.execute("DROP TABLE IF EXISTS _async_test")
            await db.close()

    @pytest.mark.asyncio
    async def test_fetchall(self):
        from tests.conftest import _TEST_DB_URL

        db = AsyncDatabase()
        await db.connect(url=_TEST_DB_URL)
        try:
            await db.execute("CREATE TABLE IF NOT EXISTS _async_test (id TEXT, val INTEGER)")
            await db.execute("DELETE FROM _async_test")
            for i in range(5):
                await db.execute("INSERT INTO _async_test VALUES ($1, $2)", f"r{i}", i)
            rows = await db.fetchall("SELECT * FROM _async_test ORDER BY val")
            assert len(rows) == 5
            assert rows[0]["val"] == 0
            assert rows[4]["val"] == 4
        finally:
            await db.execute("DROP TABLE IF EXISTS _async_test")
            await db.close()

    @pytest.mark.asyncio
    async def test_transaction_commit(self):
        from tests.conftest import _TEST_DB_URL

        db = AsyncDatabase()
        await db.connect(url=_TEST_DB_URL)
        try:
            await db.execute("CREATE TABLE IF NOT EXISTS _async_test (id TEXT PRIMARY KEY, val INTEGER)")
            await db.execute("DELETE FROM _async_test")
            async with db.transaction() as conn:
                await conn.execute("INSERT INTO _async_test (id, val) VALUES ($1, $2)", "tx1", 100)
                await conn.execute("INSERT INTO _async_test (id, val) VALUES ($1, $2)", "tx2", 200)
            rows = await db.fetchall("SELECT * FROM _async_test ORDER BY val")
            assert len(rows) == 2
            assert rows[0]["val"] == 100
            assert rows[1]["val"] == 200
        finally:
            await db.execute("DROP TABLE IF EXISTS _async_test")
            await db.close()

    @pytest.mark.asyncio
    async def test_transaction_rollback(self):
        from tests.conftest import _TEST_DB_URL

        db = AsyncDatabase()
        await db.connect(url=_TEST_DB_URL)
        try:
            await db.execute("CREATE TABLE IF NOT EXISTS _async_test (id TEXT PRIMARY KEY, val INTEGER)")
            await db.execute("DELETE FROM _async_test")
            await db.execute("INSERT INTO _async_test (id, val) VALUES ($1, $2)", "before", 1)
            with pytest.raises(ValueError):
                async with db.transaction() as conn:
                    await conn.execute("INSERT INTO _async_test (id, val) VALUES ($1, $2)", "during", 2)
                    raise ValueError("rollback test")
            rows = await db.fetchall("SELECT * FROM _async_test")
            assert len(rows) == 1
            assert rows[0]["id"] == "before"
        finally:
            await db.execute("DROP TABLE IF EXISTS _async_test")
            await db.close()

    @pytest.mark.asyncio
    async def test_concurrent_queries(self):
        from tests.conftest import _TEST_DB_URL

        db = AsyncDatabase()
        await db.connect(url=_TEST_DB_URL, min_size=2, max_size=5)
        try:
            await db.execute("CREATE TABLE IF NOT EXISTS _async_test (id TEXT, val INTEGER)")
            await db.execute("DELETE FROM _async_test")
            for i in range(10):
                await db.execute("INSERT INTO _async_test VALUES ($1, $2)", f"c{i}", i)

            async def query(n):
                return await db.fetchone("SELECT * FROM _async_test WHERE id = $1", f"c{n}")

            results = await asyncio.gather(*[query(i) for i in range(10)])
            assert all(r is not None for r in results)
            assert len(results) == 10
        finally:
            await db.execute("DROP TABLE IF EXISTS _async_test")
            await db.close()

    @pytest.mark.asyncio
    async def test_execute_in_tx_helpers(self):
        from tests.conftest import _TEST_DB_URL

        db = AsyncDatabase()
        await db.connect(url=_TEST_DB_URL)
        try:
            await db.execute("CREATE TABLE IF NOT EXISTS _async_test (id TEXT PRIMARY KEY, val INTEGER)")
            await db.execute("DELETE FROM _async_test")
            async with db.transaction() as conn:
                await db.execute_in_tx(conn, "INSERT INTO _async_test (id, val) VALUES (?, ?)", "h1", 10)
                row = await db.fetchone_in_tx(conn, "SELECT * FROM _async_test WHERE id = ?", "h1")
                assert row is not None
                assert row["val"] == 10
                rows = await db.fetchall_in_tx(conn, "SELECT * FROM _async_test")
                assert len(rows) == 1
        finally:
            await db.execute("DROP TABLE IF EXISTS _async_test")
            await db.close()
