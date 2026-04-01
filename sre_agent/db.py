"""Database abstraction -- supports SQLite (dev/test) and PostgreSQL (production).

Usage:
    db = get_database()  # reads PULSE_AGENT_DATABASE_URL env var
    db.execute("INSERT INTO actions (id, status) VALUES (?, ?)", ("a-1", "completed"))
    db.commit()
    rows = db.fetchall("SELECT * FROM actions WHERE status = ?", ("completed",))
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
from typing import Any

logger = logging.getLogger("pulse_agent.db")


class Database:
    """Unified database interface for SQLite and PostgreSQL."""

    def __init__(self, url: str):
        self.url = url
        self.is_postgres = url.startswith("postgres")
        self._lock = threading.Lock()
        self._conn: Any = None
        self._connect()

    def _connect(self) -> None:
        """Establish a new database connection."""
        if self.is_postgres:
            import psycopg2

            self._conn = psycopg2.connect(self.url)
            self._conn.autocommit = False
        else:
            path = self.url.replace("sqlite:///", "") if self.url.startswith("sqlite:///") else self.url
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            self._conn = sqlite3.connect(path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")

    def _ensure_connection(self) -> None:
        """Reconnect if the connection is stale."""
        if self._conn is None:
            self._connect()
            return
        try:
            if self.is_postgres:
                # PostgreSQL: check if connection is still alive
                cur = self._conn.cursor()
                cur.execute("SELECT 1")
                cur.close()
            else:
                self._conn.execute("SELECT 1")
        except Exception:
            logger.warning("Database connection lost, reconnecting...")
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
            self._connect()

    def _translate_query(self, query: str) -> str:
        """Translate SQLite ``?`` placeholders to PostgreSQL ``%s``."""
        if self.is_postgres:
            return query.replace("?", "%s")
        return query

    def _translate_schema(self, schema: str) -> str:
        """Translate SQLite schema DDL to PostgreSQL."""
        if self.is_postgres:
            schema = schema.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
            schema = schema.replace("INSERT OR REPLACE", "INSERT")
            schema = re.sub(r"PRAGMA\s+\w+\s*=\s*\w+\s*;?", "", schema)
        return schema

    def execute(self, query: str, params: tuple = ()) -> Any:
        """Execute a query with parameter translation."""
        self._ensure_connection()
        with self._lock:
            translated = self._translate_query(query)
            if self.is_postgres:
                return self._execute_pg(translated, params)
            return self._conn.execute(translated, params)

    def executescript(self, script: str) -> None:
        """Execute a multi-statement script with schema translation."""
        translated = self._translate_schema(script)
        with self._lock:
            if self.is_postgres:
                # Execute each statement separately to handle IF NOT EXISTS
                # correctly with SERIAL types (which create implicit sequences)
                cur = self._conn.cursor()
                for stmt in translated.split(";"):
                    stmt = stmt.strip()
                    if stmt:
                        try:
                            cur.execute(stmt)
                        except Exception:
                            # Statement already applied (e.g. table exists with SERIAL)
                            self._conn.rollback()
                            continue
                self._conn.commit()
            else:
                self._conn.executescript(translated)

    def fetchone(self, query: str, params: tuple = ()) -> dict | None:
        """Execute and fetch one row as dict."""
        self._ensure_connection()
        with self._lock:
            translated = self._translate_query(query)
            if self.is_postgres:
                cur = self._execute_pg(translated, params)
            else:
                cur = self._conn.execute(translated, params)
            row = cur.fetchone()
            if row is None:
                return None
            if self.is_postgres:
                cols = [desc[0] for desc in cur.description]
                return dict(zip(cols, row))
            return dict(row)

    def fetchall(self, query: str, params: tuple = ()) -> list[dict]:
        """Execute and fetch all rows as dicts."""
        self._ensure_connection()
        with self._lock:
            translated = self._translate_query(query)
            if self.is_postgres:
                cur = self._execute_pg(translated, params)
                cols = [desc[0] for desc in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
            cur = self._conn.execute(translated, params)
            return [dict(row) for row in cur.fetchall()]

    def _execute_pg(self, query: str, params: tuple) -> Any:
        """Execute on PostgreSQL connection."""
        cur = self._conn.cursor()
        cur.execute(query, params)
        return cur

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def __del__(self):
        self.close()

    @property
    def lastrowid(self) -> int | None:
        """Get last inserted row ID (SQLite only, PostgreSQL uses RETURNING)."""
        with self._lock:
            if not self.is_postgres:
                return self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            return None

    def health_check(self) -> bool:
        """Check if the current connection is alive (does not reconnect)."""
        if self._conn is None:
            return False
        try:
            with self._lock:
                if self.is_postgres:
                    cur = self._conn.cursor()
                    cur.execute("SELECT 1")
                    cur.close()
                else:
                    self._conn.execute("SELECT 1")
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Singleton management
# ---------------------------------------------------------------------------

_db: Database | None = None
_db_lock = threading.Lock()


def get_database() -> Database:
    """Get or create the singleton database connection."""
    global _db
    with _db_lock:
        if _db is not None and _db.health_check():
            return _db
        url = os.environ.get("PULSE_AGENT_DATABASE_URL", "sqlite:////tmp/pulse_agent/pulse.db")
        _db = Database(url)
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
    """Decorator that catches database errors and returns None/empty."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            logger.exception("View database operation failed: %s", fn.__name__)
            return None

    return wrapper


def _deserialize_view_row(row: dict) -> dict:
    """Parse JSON fields in a view row from the database."""
    import json

    for field in ("layout", "positions"):
        val = row.get(field)
        if isinstance(val, str):
            row[field] = json.loads(val)
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
    db.execute(
        "INSERT INTO views (id, owner, title, description, icon, layout, positions, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (view_id, owner, title, description, icon, json.dumps(layout), json.dumps(positions or {}), now, now),
    )
    db.commit()
    return view_id


@_db_safe
def list_views(owner: str) -> list[dict]:
    """List all views owned by a user."""
    db = get_database()
    rows = db.fetchall(
        "SELECT id, owner, title, description, icon, layout, positions, created_at, updated_at "
        "FROM views WHERE owner = ? ORDER BY updated_at DESC",
        (owner,),
    )
    return [_deserialize_view_row(row) for row in rows]


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
    """Update a view's fields. Only the owner can update."""
    import json
    from datetime import UTC, datetime

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
    db.execute(
        f"UPDATE views SET {', '.join(fields)} WHERE id = ? AND owner = ?",
        tuple(values),
    )
    db.commit()
    return True


@_db_safe
def delete_view(view_id: str, owner: str) -> bool:
    """Delete a view. Only the owner can delete."""
    db = get_database()
    db.execute("DELETE FROM views WHERE id = ? AND owner = ?", (view_id, owner))
    db.commit()
    return True


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
