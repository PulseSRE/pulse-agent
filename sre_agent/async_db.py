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

import logging
import re
from typing import Any

logger = logging.getLogger("pulse_agent.async_db")


_PLACEHOLDER_RE = re.compile(r"\?")


def _translate_placeholders(query: str) -> str:
    """Convert ``?`` placeholders to asyncpg-style ``$1, $2, ...``.

    WARNING: This does naive replacement — do NOT use PostgreSQL's jsonb ``?``
    operator in queries passed through this translator.  Use ``jsonb_exists()``
    instead, or pass pre-translated queries with ``$N`` placeholders directly.
    """
    counter = 0

    def _replace(_match: re.Match) -> str:
        nonlocal counter
        counter += 1
        return f"${counter}"

    return _PLACEHOLDER_RE.sub(_replace, query)


class AsyncDatabase:
    """Async PostgreSQL interface backed by an asyncpg connection pool.

    Call :meth:`connect` before use.  The pool is created lazily on first
    query if not connected explicitly.
    """

    def __init__(self) -> None:
        self._pool: Any = None
        self._url: str = ""

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

        self._pool = await asyncpg.create_pool(url, min_size=min_size, max_size=max_size)

    async def _ensure_pool(self) -> Any:
        if self._pool is None:
            await self.connect()
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

    async def close(self) -> None:
        """Close the connection pool."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

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


async def get_async_database() -> AsyncDatabase:
    """Return the async database singleton, creating the pool on first call."""
    global _async_db
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
