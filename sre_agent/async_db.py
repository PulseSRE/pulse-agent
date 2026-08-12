"""Async PostgreSQL database layer using asyncpg.

Provides ``AsyncDatabase`` as an async counterpart to the sync ``Database``
class in ``db.py``.  Both can coexist — the sync path (psycopg2) remains
the default; async is opt-in for modules that run in an async context
(e.g., cluster_monitor, agent loop).

Usage::

    db = await get_async_database()
    row = await db.fetchone("SELECT * FROM views WHERE id = $1", view_id)
    rows = await db.fetchall("SELECT * FROM actions LIMIT $1", 50)
    await db.execute("INSERT INTO events (id, type) VALUES ($1, $2)", evt_id, "scan")

Note: asyncpg uses ``$1, $2, ...`` positional placeholders (not ``?`` or ``%s``).
A ``translate_query()`` helper converts ``?`` placeholders for migration ease.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from .db import _PARAM_RE

logger = logging.getLogger("pulse_agent.async_db")

try:
    import asyncpg

    ASYNC_DB_ERRORS: tuple[type[Exception], ...] = (asyncpg.PostgresError, OSError)
except ImportError:
    ASYNC_DB_ERRORS = (OSError,)


def _translate_placeholders(query: str) -> str:
    """Convert ``?`` placeholders to asyncpg-style ``$1, $2, ...``.

    Preserves JSONB operators ``?``, ``?|``, ``?&``, ``@?``.
    For bare JSONB ``?`` (key-existence), prefer ``jsonb_exists()`` to avoid
    ambiguity with parameter placeholders.
    """
    counter = 0

    def _replace(_match: re.Match) -> str:
        nonlocal counter
        counter += 1
        return f"${counter}"

    return _PARAM_RE.sub(_replace, query)


class AsyncDatabase:
    """Async PostgreSQL interface backed by an asyncpg connection pool.

    Call :meth:`connect` before use.  The pool is created lazily on first
    query if not connected explicitly.
    """

    def __init__(self) -> None:
        self._pool: Any = None
        self._url: str = ""
        self._loop: asyncio.AbstractEventLoop | None = None

    async def connect(self, url: str | None = None, min_size: int = 2, max_size: int = 20) -> None:
        """Create the connection pool.  Safe to call multiple times."""
        if self._pool is not None:
            return
        if url is None:
            from .config import get_settings

            s = get_settings()
            url = s.database.url
            min_size = s.database.pool_min
            max_size = s.database.pool_max
        self._url = url
        import asyncpg

        # Bound lock waits and runaway statements so a stuck/held lock (e.g. an
        # uncommitted transaction on another pooled connection) fails fast with
        # a catchable error instead of blocking the caller indefinitely — mirrors
        # the equivalent guard on the sync pool in db.py.
        self._pool = await asyncpg.create_pool(
            url,
            min_size=min_size,
            max_size=max_size,
            server_settings={"lock_timeout": "10000", "statement_timeout": "30000"},
        )
        self._loop = asyncio.get_running_loop()

    async def _ensure_pool(self) -> Any:
        if self._pool is not None:
            try:
                current_loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None
            if current_loop is not None and self._loop is not None and current_loop is not self._loop:
                # Pool was created on a different (likely already-closed) event
                # loop — e.g. a singleton reused across tests/processes that each
                # spin up their own loop. Closing it here would itself run on the
                # wrong loop, so just drop the stale reference; the old loop owns
                # its own cleanup. A fresh pool is created below for this loop.
                logger.warning("AsyncDatabase pool bound to a stale event loop; recreating")
                self._pool = None
        if self._pool is None:
            await self.connect(url=self._url or None)
        return self._pool

    async def fetchone(self, query: str, *args: Any) -> dict[str, Any] | None:
        """Execute and fetch one row as a dict."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(_translate_placeholders(query), *args)
            return dict(row) if row else None

    async def fetchall(self, query: str, *args: Any) -> list[dict[str, Any]]:
        """Execute and fetch all rows as dicts."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(_translate_placeholders(query), *args)
            return [dict(r) for r in rows]

    async def execute(self, query: str, *args: Any) -> str:
        """Execute a statement (INSERT/UPDATE/DELETE). Returns status string."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            return await conn.execute(_translate_placeholders(query), *args)

    async def executemany(self, query: str, args_list: list[tuple]) -> None:
        """Execute a statement with multiple parameter sets."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.executemany(_translate_placeholders(query), args_list)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Any]:
        """Async transaction context manager.

        Usage::

            async with db.transaction() as conn:
                await conn.execute("INSERT INTO t VALUES ($1)", val)
                await conn.execute("UPDATE t SET x = $1 WHERE id = $2", x, id)
            # auto-committed on exit, rolled back on exception
        """
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                yield conn

    async def execute_in_tx(self, conn: Any, query: str, *args: Any) -> str:
        """Execute a statement within an existing transaction connection."""
        return await conn.execute(_translate_placeholders(query), *args)

    async def fetchone_in_tx(self, conn: Any, query: str, *args: Any) -> dict[str, Any] | None:
        """Fetch one row within an existing transaction connection."""
        row = await conn.fetchrow(_translate_placeholders(query), *args)
        return dict(row) if row else None

    async def fetchall_in_tx(self, conn: Any, query: str, *args: Any) -> list[dict[str, Any]]:
        """Fetch all rows within an existing transaction connection."""
        rows = await conn.fetch(_translate_placeholders(query), *args)
        return [dict(r) for r in rows]

    async def close(self) -> None:
        """Close the connection pool."""
        if self._pool is not None:
            try:
                current_loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None
            if self._loop is not None and current_loop is not None and current_loop is not self._loop:
                # Pool belongs to a different (likely already-closed) event loop —
                # closing it here would run against the wrong loop and raise, same
                # as in _ensure_pool(). Drop the reference without attempting a
                # graceful close; the original loop owns its own cleanup.
                logger.warning("AsyncDatabase.close() called on a pool bound to a stale event loop; discarding")
            else:
                await self._pool.close()
            self._pool = None
            self._loop = None

    async def health_check(self) -> bool:
        """Check if the pool can serve a connection."""
        try:
            pool = await self._ensure_pool()
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return True
        except Exception:
            logger.debug("Async health check failed", exc_info=True)
            return False


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_async_db: AsyncDatabase | None = None
_async_db_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _async_db_lock
    if _async_db_lock is None:
        _async_db_lock = asyncio.Lock()
    return _async_db_lock


async def get_async_database() -> AsyncDatabase:
    """Return the async database singleton, creating the pool on first call."""
    global _async_db
    if _async_db is not None:
        return _async_db
    async with _get_lock():
        if _async_db is None:
            _async_db = AsyncDatabase()
            await _async_db.connect()
    return _async_db


async def reset_async_database() -> None:
    """Close and reset the async database singleton."""
    global _async_db
    if _async_db is not None:
        await _async_db.close()
        _async_db = None
