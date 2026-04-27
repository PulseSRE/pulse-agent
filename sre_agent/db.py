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
import threading
from typing import Any

import psycopg2
import psycopg2.pool

logger = logging.getLogger("pulse_agent.db")


class Database:
    """PostgreSQL database interface backed by a threaded connection pool."""

    def __init__(self, url: str):
        self.url = url
        from .config import get_settings

        _s = get_settings()
        minconn = _s.database.pool_min
        maxconn = _s.database.pool_max
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
                logger.debug("Failed to return connection to pool", exc_info=True)
            self._local.conn = None
        if hasattr(self, "_pool") and self._pool is not None:
            self._pool.closeall()
            self._pool = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            logger.debug("Error during Database.__del__ cleanup", exc_info=True)

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
        from .config import get_settings

        url = get_settings().database.url
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
# View persistence — thin wrappers delegating to ViewRepository
# ---------------------------------------------------------------------------


def save_view(
    owner: str,
    view_id: str,
    title: str,
    description: str,
    layout: list,
    positions: dict | None = None,
    icon: str = "",
    *,
    view_type: str = "custom",
    status: str = "active",
    trigger_source: str = "user",
    finding_id: str | None = None,
    visibility: str = "private",
) -> str | None:
    """Save a new view for a user. Returns the view ID."""
    from .repositories.view_repo import get_view_repo

    return get_view_repo().save_view(
        owner,
        view_id,
        title,
        description,
        layout,
        positions,
        icon,
        view_type=view_type,
        status=status,
        trigger_source=trigger_source,
        finding_id=finding_id,
        visibility=visibility,
    )


def list_views(
    owner: str,
    limit: int = 50,
    *,
    view_type: str | None = None,
    visibility: str | None = None,
    exclude_status: str | None = None,
) -> list[dict]:
    """List views. By default returns owner's views. With visibility='team', returns all team-visible views."""
    from .repositories.view_repo import get_view_repo

    return get_view_repo().list_views(
        owner, limit, view_type=view_type, visibility=visibility, exclude_status=exclude_status
    )


def get_view_by_title(owner: str, title: str) -> dict | None:
    """Find a view by title -- returns full view data for merging."""
    from .repositories.view_repo import get_view_repo

    return get_view_repo().get_view_by_title(owner, title)


def get_view(view_id: str, owner: str | None = None) -> dict | None:
    """Get a single view by ID. If owner is provided, checks ownership."""
    from .repositories.view_repo import get_view_repo

    return get_view_repo().get_view(view_id, owner)


def update_view(view_id: str, owner: str, **updates) -> bool:
    """Update a view's fields. Only the owner can update.

    Pass _snapshot=True to create a version snapshot (explicit save).
    Auto-saves from drag/resize should NOT create versions.
    """
    from .repositories.view_repo import get_view_repo

    return get_view_repo().update_view(view_id, owner, **updates)


def delete_view(view_id: str, owner: str) -> bool:
    """Delete a view. Only the owner can delete. Returns False if not found."""
    from .repositories.view_repo import get_view_repo

    return get_view_repo().delete_view(view_id, owner)


def clone_view(view_id: str, new_owner: str) -> str | None:
    """Clone a view to another user's account. Returns the new view ID."""
    from .repositories.view_repo import get_view_repo

    return get_view_repo().clone_view(view_id, new_owner)


def clone_view_at_version(view_id: str, new_owner: str, version: int) -> str | None:
    """Clone a view from a specific version snapshot. Returns the new view ID."""
    from .repositories.view_repo import get_view_repo

    return get_view_repo().clone_view_at_version(view_id, new_owner, version)


# ---------------------------------------------------------------------------
# View Version History
# ---------------------------------------------------------------------------


def snapshot_view(view_id: str, action: str) -> int | None:
    """Save a snapshot of the current view state before a change. Returns version number."""
    from .repositories.view_repo import get_view_repo

    return get_view_repo().snapshot_view(view_id, action)


def list_view_versions(view_id: str, limit: int = 20) -> list[dict]:
    """List version history for a view."""
    from .repositories.view_repo import get_view_repo

    return get_view_repo().list_view_versions(view_id, limit)


def restore_view_version(view_id: str, owner: str, version: int) -> bool:
    """Restore a view to a specific version. Returns True on success."""
    from .repositories.view_repo import get_view_repo

    return get_view_repo().restore_view_version(view_id, owner, version)


def migrate_view_ownership(new_owner: str) -> int:
    """Migrate views from hash-based owners (user-*) to a real username.

    Called once when a real username is first resolved via X-Forwarded-User.
    Only migrates if there are hash-based views and no views for the real owner yet.
    Returns the number of migrated views.
    """
    from .repositories.view_repo import get_view_repo

    return get_view_repo().migrate_view_ownership(new_owner)


# ---------------------------------------------------------------------------
# View lifecycle — status transitions, claims, finding lookup
# ---------------------------------------------------------------------------


def transition_view_status(view_id: str, actor: str, new_status: str) -> bool:
    """Transition a view's status. Validates the transition is legal. Creates a version snapshot."""
    from .repositories.view_repo import get_view_repo

    return get_view_repo().transition_view_status(view_id, actor, new_status)


def claim_view(view_id: str, username: str) -> bool:
    """Claim a team-visible view. Only team views can be claimed."""
    from .repositories.view_repo import get_view_repo

    return get_view_repo().claim_view(view_id, username)


def unclaim_view(view_id: str, username: str) -> bool:
    """Release a claim on a view. Only the claimant can unclaim."""
    from .repositories.view_repo import get_view_repo

    return get_view_repo().unclaim_view(view_id, username)


def expire_stale_claims() -> int:
    """Clear claims older than 30 minutes. Returns count of expired claims."""
    from .repositories.view_repo import get_view_repo

    return get_view_repo().expire_stale_claims()


def find_similar_views(title: str, view_type: str = "incident", limit: int = 3) -> list[dict]:
    """Find resolved/completed views with similar titles. Uses trigram-like word overlap."""
    from .repositories.view_repo import get_view_repo

    return get_view_repo().find_similar_views(title, view_type, limit)


def get_view_by_finding(finding_id: str) -> dict | None:
    """Find a view linked to a monitor finding. Returns the most recent match."""
    from .repositories.view_repo import get_view_repo

    return get_view_repo().get_view_by_finding(finding_id)


def reopen_view_for_finding(finding_id: str) -> str | None:
    """Reopen a resolved/archived view when its finding recurs.

    Returns the view_id if reopened, None if no matching resolved view exists.
    """
    from .repositories.view_repo import get_view_repo

    return get_view_repo().reopen_view_for_finding(finding_id)


def escalate_assessment_to_incident(view_id: str) -> bool:
    """Escalate an assessment view to an incident when the predicted issue materializes."""
    from .repositories.view_repo import get_view_repo

    return get_view_repo().escalate_assessment_to_incident(view_id)


def extract_resolution_tools(view_id: str) -> list[dict]:
    """Extract action_button tool sequences from a view's layout.

    Returns list of {action, action_input, label} dicts.
    """
    from .repositories.view_repo import get_view_repo

    return get_view_repo().extract_resolution_tools(view_id)
