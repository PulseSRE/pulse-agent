"""PostgreSQL database layer for Pulse Agent.

Usage:
    db = get_database()  # reads PULSE_AGENT_DATABASE_URL env var
    db.execute("INSERT INTO actions (id, status) VALUES (?, ?)", ("a-1", "completed"))
    db.commit()
    rows = db.fetchall("SELECT * FROM actions WHERE status = ?", ("completed",))

Queries use ``?`` placeholders which are auto-translated to ``%s`` for PostgreSQL.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Any

import psycopg2
import psycopg2.pool

logger = logging.getLogger("pulse_agent.db")


class Database:
    """PostgreSQL database interface backed by a threaded connection pool."""

    def __init__(self, url: str):
        self.url = url
        minconn = int(os.environ.get("PULSE_AGENT_DB_POOL_MIN", "2"))
        maxconn = int(os.environ.get("PULSE_AGENT_DB_POOL_MAX", "10"))
        self._pool = psycopg2.pool.ThreadedConnectionPool(minconn, maxconn, dsn=url)
        self._local = threading.local()

    # ------------------------------------------------------------------
    # Thread-local connection management (for execute/commit sequences)
    # ------------------------------------------------------------------

    def _get_conn(self):
        """Get the thread-local connection, checking it out from the pool if needed."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = self._pool.getconn()
            self._local.conn.autocommit = False
        return self._local.conn

    def _put_conn(self):
        """Return the thread-local connection to the pool."""
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._pool.putconn(self._local.conn)
            self._local.conn = None

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def _translate_query(self, query: str) -> str:
        """Translate ``?`` placeholders to PostgreSQL ``%s``."""
        return query.replace("?", "%s")

    def execute(self, query: str, params: tuple = ()) -> Any:
        """Execute a query with parameter translation.

        The connection is kept checked out until :meth:`commit` is called.
        On error, the connection is rolled back and returned to the pool.
        """
        conn = self._get_conn()
        try:
            translated = self._translate_query(query)
            cur = conn.cursor()
            cur.execute(translated, params)
            return cur
        except Exception:
            conn.rollback()
            self._put_conn()
            raise

    def executescript(self, script: str) -> None:
        """Execute a multi-statement schema script."""
        conn = self._pool.getconn()
        try:
            cur = conn.cursor()
            for stmt in script.split(";"):
                stmt = stmt.strip()
                if stmt:
                    try:
                        cur.execute(stmt)
                    except Exception:
                        conn.rollback()
                        continue
            conn.commit()
        finally:
            self._pool.putconn(conn)

    def fetchone(self, query: str, params: tuple = ()) -> dict | None:
        """Execute and fetch one row as dict."""
        conn = self._pool.getconn()
        try:
            translated = self._translate_query(query)
            cur = conn.cursor()
            cur.execute(translated, params)
            row = cur.fetchone()
            if row is None:
                return None
            cols = [desc[0] for desc in cur.description]
            return dict(zip(cols, row))
        finally:
            self._pool.putconn(conn)

    def fetchall(self, query: str, params: tuple = ()) -> list[dict]:
        """Execute and fetch all rows as dicts."""
        conn = self._pool.getconn()
        try:
            translated = self._translate_query(query)
            cur = conn.cursor()
            cur.execute(translated, params)
            cols = [desc[0] for desc in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            self._pool.putconn(conn)

    def commit(self) -> None:
        """Commit the current thread-local transaction and return the connection."""
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._local.conn.commit()
            self._put_conn()

    def close(self) -> None:
        """Return any thread-local connection and close all pool connections."""
        if hasattr(self, "_local") and hasattr(self._local, "conn") and self._local.conn is not None:
            try:
                self._pool.putconn(self._local.conn)
            except Exception:
                pass
            self._local.conn = None
        if hasattr(self, "_pool") and self._pool is not None:
            self._pool.closeall()
            self._pool = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def health_check(self) -> bool:
        """Check if the pool can serve a connection."""
        if self._pool is None:
            return False
        try:
            conn = self._pool.getconn()
            try:
                cur = conn.cursor()
                cur.execute("SELECT 1")
                cur.close()
            finally:
                self._pool.putconn(conn)
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Singleton management
# ---------------------------------------------------------------------------

_db: Database | None = None
_db_lock = threading.Lock()


def get_database() -> Database:
    """Get or create the singleton database connection.

    Requires PULSE_AGENT_DATABASE_URL pointing to a PostgreSQL instance.
    """
    global _db
    with _db_lock:
        if _db is not None and _db.health_check():
            return _db
        url = os.environ.get("PULSE_AGENT_DATABASE_URL", "")
        if not url:
            raise RuntimeError(
                "PULSE_AGENT_DATABASE_URL is required. "
                "Set it to a PostgreSQL connection URL (e.g. postgresql://user:pass@host/db)."
            )
        _db = Database(url)
        # Run migrations on first connect
        from .db_migrations import run_migrations

        run_migrations(_db)
        return _db


def set_database(db: Database) -> None:
    """Override the singleton (for testing)."""
    global _db
    _db = db


def reset_database() -> None:
    """Close and reset the singleton (for testing)."""
    global _db
    if _db:
        _db.close()
    _db = None


# ---------------------------------------------------------------------------
# View persistence (user-scoped custom dashboards)
# ---------------------------------------------------------------------------


def _db_safe(fn):
    """Decorator that catches database errors and returns None.

    Only catches database and serialization errors. Programming bugs
    (TypeError, KeyError, etc.) are re-raised to avoid silent failures.
    """
    import functools
    import json

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (json.JSONDecodeError, OSError):
            logger.exception("View database operation failed: %s", fn.__name__)
            return None
        except Exception:
            if isinstance(sys.exc_info()[1], psycopg2.Error):
                logger.exception("View database operation failed: %s", fn.__name__)
                return None
            raise

    return wrapper


def _deserialize_view_row(row: dict) -> dict:
    """Parse JSON fields in a view row from the database.

    Replaces NaN/Infinity with None to ensure valid JSON output.
    """
    import json
    import math

    def _sanitize(obj):
        if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
            return None
        if isinstance(obj, list):
            return [_sanitize(v) for v in obj]
        if isinstance(obj, dict):
            return {k: _sanitize(v) for k, v in obj.items()}
        return obj

    for field in ("layout", "positions"):
        val = row.get(field)
        if isinstance(val, str):
            row[field] = _sanitize(json.loads(val))
    return row


@_db_safe
def save_view(
    owner: str, view_id: str, title: str, description: str, layout: list, positions: dict | None = None, icon: str = ""
) -> str | None:
    """Save a new view for a user. Returns the view ID."""
    import json
    from datetime import UTC, datetime

    db = get_database()
    now = datetime.now(UTC).isoformat()
    # Only upsert if the existing row belongs to the same owner
    db.execute(
        "INSERT INTO views (id, owner, title, description, icon, layout, positions, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (id) DO UPDATE SET "
        "title = EXCLUDED.title, description = EXCLUDED.description, icon = EXCLUDED.icon, "
        "layout = EXCLUDED.layout, positions = EXCLUDED.positions, updated_at = EXCLUDED.updated_at "
        "WHERE views.owner = EXCLUDED.owner",
        (view_id, owner, title, description, icon, json.dumps(layout), json.dumps(positions or {}), now, now),
    )
    db.commit()

    # Snapshot the initial version so version history is never empty
    try:
        snapshot_view(view_id, "created")
    except Exception:
        pass

    return view_id


@_db_safe
def list_views(owner: str, limit: int = 50) -> list[dict]:
    """List all views owned by a user (max 50)."""
    db = get_database()
    rows = db.fetchall(
        "SELECT id, owner, title, description, icon, layout, positions, created_at, updated_at "
        "FROM views WHERE owner = ? ORDER BY updated_at DESC LIMIT ?",
        (owner, min(limit, 50)),
    )
    return [_deserialize_view_row(row) for row in rows]


@_db_safe
def get_view_by_title(owner: str, title: str) -> dict | None:
    """Find a view by title (lightweight — returns id and title only)."""
    db = get_database()
    row = db.fetchone("SELECT id, title FROM views WHERE owner = ? AND title = ? LIMIT 1", (owner, title))
    return row


@_db_safe
def get_view(view_id: str, owner: str | None = None) -> dict | None:
    """Get a single view by ID. If owner is provided, checks ownership."""
    db = get_database()
    if owner:
        row = db.fetchone("SELECT * FROM views WHERE id = ? AND owner = ?", (view_id, owner))
    else:
        row = db.fetchone("SELECT * FROM views WHERE id = ?", (view_id,))
    if row is None:
        return None
    return _deserialize_view_row(row)


@_db_safe
def update_view(view_id: str, owner: str, **updates) -> bool:
    """Update a view's fields. Only the owner can update. Auto-snapshots before changes."""
    import json
    from datetime import UTC, datetime

    # Auto-snapshot before any change (for undo/version history)
    action = updates.pop("_action", "update")
    try:
        snapshot_view(view_id, action)
    except Exception:
        pass  # Don't block the update if snapshot fails

    allowed = {"title", "description", "icon", "layout", "positions"}
    fields = []
    values = []
    for key, value in updates.items():
        if key not in allowed:
            continue
        if key in ("layout", "positions"):
            value = json.dumps(value)
        fields.append(f"{key} = ?")
        values.append(value)

    if not fields:
        return False

    fields.append("updated_at = ?")
    values.append(datetime.now(UTC).isoformat())
    values.extend([view_id, owner])

    db = get_database()
    cursor = db.execute(
        f"UPDATE views SET {', '.join(fields)} WHERE id = ? AND owner = ?",
        tuple(values),
    )
    db.commit()
    return getattr(cursor, "rowcount", 1) > 0


@_db_safe
def delete_view(view_id: str, owner: str) -> bool:
    """Delete a view. Only the owner can delete. Returns False if not found."""
    db = get_database()
    cursor = db.execute("DELETE FROM views WHERE id = ? AND owner = ?", (view_id, owner))
    db.commit()
    return getattr(cursor, "rowcount", 1) > 0


@_db_safe
def clone_view(view_id: str, new_owner: str) -> str | None:
    """Clone a view to another user's account. Returns the new view ID."""
    import json
    import uuid
    from datetime import UTC, datetime

    db = get_database()
    source = db.fetchone("SELECT * FROM views WHERE id = ?", (view_id,))
    if source is None:
        return None
    _deserialize_view_row(source)

    new_id = f"cv-{uuid.uuid4().hex[:12]}"
    now = datetime.now(UTC).isoformat()
    layout = source["layout"]
    positions = source["positions"]

    db.execute(
        "INSERT INTO views (id, owner, title, description, icon, layout, positions, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            new_id,
            new_owner,
            source["title"],
            source["description"],
            source.get("icon", ""),
            json.dumps(layout),
            json.dumps(positions),
            now,
            now,
        ),
    )
    db.commit()
    return new_id


# ---------------------------------------------------------------------------
# View Version History
# ---------------------------------------------------------------------------


@_db_safe
def snapshot_view(view_id: str, action: str) -> int | None:
    """Save a snapshot of the current view state before a change. Returns version number."""
    import json
    from datetime import UTC, datetime

    db = get_database()
    view = db.fetchone("SELECT * FROM views WHERE id = ?", (view_id,))
    if not view:
        return None

    # Get the next version number
    last = db.fetchone(
        "SELECT COALESCE(MAX(version), 0) AS max_v FROM view_versions WHERE view_id = ?",
        (view_id,),
    )
    next_version = (last["max_v"] if last else 0) + 1

    layout = view["layout"] if isinstance(view["layout"], str) else json.dumps(view["layout"])
    positions = view["positions"] if isinstance(view["positions"], str) else json.dumps(view["positions"])

    db.execute(
        "INSERT INTO view_versions (view_id, version, action, layout, positions, title, description, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            view_id,
            next_version,
            action,
            layout,
            positions,
            view["title"],
            view.get("description", ""),
            datetime.now(UTC).isoformat(),
        ),
    )
    db.commit()
    return next_version


@_db_safe
def list_view_versions(view_id: str, limit: int = 20) -> list[dict]:
    """List version history for a view."""
    db = get_database()
    rows = db.fetchall(
        "SELECT version, action, title, description, layout, created_at FROM view_versions WHERE view_id = ? ORDER BY version DESC LIMIT ?",
        (view_id, limit),
    )
    import json as _json

    for row in rows:
        if isinstance(row.get("layout"), str):
            try:
                row["layout"] = _json.loads(row["layout"])
            except (ValueError, TypeError):
                pass
    return rows


@_db_safe
def restore_view_version(view_id: str, owner: str, version: int) -> bool:
    """Restore a view to a specific version. Returns True on success."""
    import json

    db = get_database()
    # Verify ownership
    view = db.fetchone("SELECT id FROM views WHERE id = ? AND owner = ?", (view_id, owner))
    if not view:
        return False

    # Get the version snapshot
    snapshot = db.fetchone(
        "SELECT layout, positions, title, description FROM view_versions WHERE view_id = ? AND version = ?",
        (view_id, version),
    )
    if not snapshot:
        return False

    # Snapshot current state before restoring
    snapshot_view(view_id, f"before_restore_to_v{version}")

    # Restore
    layout = snapshot["layout"] if isinstance(snapshot["layout"], str) else json.dumps(snapshot["layout"])
    positions = snapshot["positions"] if isinstance(snapshot["positions"], str) else json.dumps(snapshot["positions"])

    from datetime import UTC, datetime

    db.execute(
        "UPDATE views SET layout = ?, positions = ?, title = ?, description = ?, updated_at = ? WHERE id = ? AND owner = ?",
        (
            layout,
            positions,
            snapshot["title"],
            snapshot.get("description", ""),
            datetime.now(UTC).isoformat(),
            view_id,
            owner,
        ),
    )
    db.commit()
    return True
