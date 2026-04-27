"""View repository — all view-related database operations.

Extracted from ``db.py`` to keep domain logic cohesive.  The original
module-level functions in ``db.py`` now delegate here for backward
compatibility.
"""

from __future__ import annotations

import functools
import json
import logging
import math
import uuid
from datetime import UTC, datetime, timedelta

import psycopg2

from .base import BaseRepository

logger = logging.getLogger("pulse_agent.db")


# ---------------------------------------------------------------------------
# Helpers (moved from db.py)
# ---------------------------------------------------------------------------


def _db_safe(fn):
    """Decorator that catches database errors and returns None.

    Only catches database and serialization errors. Programming bugs
    (TypeError, KeyError, etc.) are re-raised to avoid silent failures.

    Works as both a plain function decorator and a method decorator
    (``self`` is simply forwarded via ``*args``).
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (json.JSONDecodeError, OSError, psycopg2.Error):
            logger.exception("View database operation failed: %s", fn.__name__)
            return None

    return wrapper


def _deserialize_view_row(row: dict) -> dict:
    """Parse JSON fields in a view row from the database.

    Replaces NaN/Infinity with None to ensure valid JSON output.
    """

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


# ---------------------------------------------------------------------------
# Status machine constants
# ---------------------------------------------------------------------------

_STATUS_TRANSITIONS: dict[str, dict[str, set[str]]] = {
    "incident": {
        "investigating": {"action_taken"},
        "action_taken": {"verifying"},
        "verifying": {"resolved", "investigating"},
        "resolved": {"investigating", "archived"},
    },
    "plan": {
        "analyzing": {"ready"},
        "ready": {"executing"},
        "executing": {"ready", "completed"},
    },
    "assessment": {
        "analyzing": {"ready"},
        "ready": {"acknowledged", "investigating"},
    },
}

_CLAIM_EXPIRY_MINUTES = 30


# ---------------------------------------------------------------------------
# ViewRepository
# ---------------------------------------------------------------------------


class ViewRepository(BaseRepository):
    """All view persistence operations (CRUD, lifecycle, versioning)."""

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    @_db_safe
    def save_view(
        self,
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
        db = self.db
        now = datetime.now(UTC).isoformat()
        existing = self.get_view_by_title(owner, title)
        if existing:
            view_id = existing["id"]

        db.execute(
            "INSERT INTO views (id, owner, title, description, icon, layout, positions, "
            "view_type, status, trigger_source, finding_id, visibility, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET "
            "title = EXCLUDED.title, description = EXCLUDED.description, icon = EXCLUDED.icon, "
            "layout = EXCLUDED.layout, positions = EXCLUDED.positions, updated_at = EXCLUDED.updated_at, "
            "view_type = EXCLUDED.view_type, status = EXCLUDED.status, trigger_source = EXCLUDED.trigger_source, "
            "finding_id = EXCLUDED.finding_id, visibility = EXCLUDED.visibility "
            "WHERE views.owner = EXCLUDED.owner",
            (
                view_id,
                owner,
                title,
                description,
                icon,
                json.dumps(layout),
                json.dumps(positions or {}),
                view_type,
                status,
                trigger_source,
                finding_id,
                visibility,
                now,
                now,
            ),
        )
        db.commit()

        try:
            self.snapshot_view(view_id, "created")
        except Exception:
            logger.exception("Failed to snapshot view %s on creation", view_id)

        return view_id

    @_db_safe
    def list_views(
        self,
        owner: str,
        limit: int = 50,
        *,
        view_type: str | None = None,
        visibility: str | None = None,
        exclude_status: str | None = None,
    ) -> list[dict]:
        """List views. By default returns owner's views. With visibility='team', returns all team-visible views."""
        db = self.db
        conditions: list[str] = []
        params: list = []

        if visibility == "team":
            conditions.append("visibility = 'team'")
        else:
            conditions.append("owner = ?")
            params.append(owner)

        if view_type:
            conditions.append("view_type = ?")
            params.append(view_type)
        if exclude_status:
            conditions.append("status != ?")
            params.append(exclude_status)

        where = " AND ".join(conditions)
        params.append(min(limit, 50))

        rows = db.fetchall(
            f"SELECT * FROM views WHERE {where} ORDER BY updated_at DESC LIMIT ?",
            tuple(params),
        )
        return [_deserialize_view_row(row) for row in rows]

    @_db_safe
    def get_view_by_title(self, owner: str, title: str) -> dict | None:
        """Find a view by title -- returns full view data for merging."""
        db = self.db
        row = db.fetchone(
            "SELECT * FROM views WHERE owner = ? AND title = ? LIMIT 1",
            (owner, title),
        )
        return _deserialize_view_row(row) if row else None

    @_db_safe
    def get_view(self, view_id: str, owner: str | None = None) -> dict | None:
        """Get a single view by ID. If owner is provided, checks ownership."""
        db = self.db
        if owner:
            row = db.fetchone("SELECT * FROM views WHERE id = ? AND owner = ?", (view_id, owner))
        else:
            row = db.fetchone("SELECT * FROM views WHERE id = ?", (view_id,))
        if row is None:
            return None
        return _deserialize_view_row(row)

    @_db_safe
    def update_view(self, view_id: str, owner: str, **updates) -> bool:
        """Update a view's fields. Only the owner can update.

        Pass _snapshot=True to create a version snapshot (explicit save).
        Auto-saves from drag/resize should NOT create versions.
        """
        # Only snapshot when explicitly requested (user clicks save, agent updates)
        action = updates.pop("_action", "update")
        should_snapshot = updates.pop("_snapshot", False)
        if should_snapshot:
            try:
                self.snapshot_view(view_id, action)
            except Exception:
                logger.exception("Failed to snapshot view %s on %s", view_id, action)

        allowed = {"title", "description", "icon", "layout", "positions", "visibility", "status", "view_type"}
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

        db = self.db
        cursor = db.execute(
            f"UPDATE views SET {', '.join(fields)} WHERE id = ? AND owner = ?",
            tuple(values),
        )
        db.commit()
        return getattr(cursor, "rowcount", 1) > 0

    @_db_safe
    def delete_view(self, view_id: str, owner: str) -> bool:
        """Delete a view. Only the owner can delete. Returns False if not found."""
        db = self.db
        cursor = db.execute("DELETE FROM views WHERE id = ? AND owner = ?", (view_id, owner))
        db.commit()
        return getattr(cursor, "rowcount", 1) > 0

    @_db_safe
    def clone_view(self, view_id: str, new_owner: str) -> str | None:
        """Clone a view to another user's account. Returns the new view ID."""
        db = self.db
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

    @_db_safe
    def clone_view_at_version(self, view_id: str, new_owner: str, version: int) -> str | None:
        """Clone a view from a specific version snapshot. Returns the new view ID."""
        db = self.db
        snapshot = db.fetchone(
            "SELECT layout, positions, title, description FROM view_versions WHERE view_id = ? AND version = ?",
            (view_id, version),
        )
        if snapshot is None:
            return None

        new_id = f"cv-{uuid.uuid4().hex[:12]}"
        now = datetime.now(UTC).isoformat()

        layout = snapshot["layout"] if isinstance(snapshot["layout"], str) else json.dumps(snapshot["layout"])
        positions = (
            snapshot["positions"] if isinstance(snapshot["positions"], str) else json.dumps(snapshot["positions"])
        )

        db.execute(
            "INSERT INTO views (id, owner, title, description, icon, layout, positions, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_id,
                new_owner,
                snapshot["title"],
                snapshot.get("description", ""),
                "",
                layout,
                positions,
                now,
                now,
            ),
        )
        db.commit()
        return new_id

    # ------------------------------------------------------------------
    # Version history
    # ------------------------------------------------------------------

    @_db_safe
    def snapshot_view(self, view_id: str, action: str) -> int | None:
        """Save a snapshot of the current view state before a change. Returns version number."""
        db = self.db
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
    def list_view_versions(self, view_id: str, limit: int = 20) -> list[dict]:
        """List version history for a view."""
        db = self.db
        rows = db.fetchall(
            "SELECT version, action, title, description, layout, created_at FROM view_versions WHERE view_id = ? ORDER BY version DESC LIMIT ?",
            (view_id, limit),
        )
        for row in rows:
            if isinstance(row.get("layout"), str):
                try:
                    row["layout"] = json.loads(row["layout"])
                except (ValueError, TypeError):
                    logger.debug("Failed to parse layout JSON for view version", exc_info=True)
        return rows

    @_db_safe
    def restore_view_version(self, view_id: str, owner: str, version: int) -> bool:
        """Restore a view to a specific version. Returns True on success."""
        db = self.db
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
        self.snapshot_view(view_id, f"before_restore_to_v{version}")

        # Restore
        layout = snapshot["layout"] if isinstance(snapshot["layout"], str) else json.dumps(snapshot["layout"])
        positions = (
            snapshot["positions"] if isinstance(snapshot["positions"], str) else json.dumps(snapshot["positions"])
        )

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

    @_db_safe
    def migrate_view_ownership(self, new_owner: str) -> int:
        """Migrate views from hash-based owners (user-*) to a real username.

        Called once when a real username is first resolved via X-Forwarded-User.
        Only migrates if there are hash-based views and no views for the real owner yet.
        Returns the number of migrated views.
        """
        db = self.db

        # Don't migrate if the new owner already has views
        existing = db.fetchone("SELECT COUNT(*) as cnt FROM views WHERE owner = ?", (new_owner,))
        if existing and existing["cnt"] > 0:
            return 0

        # Find hash-based owners (user-<hex>)
        hash_owners = db.fetchall(
            "SELECT DISTINCT owner FROM views WHERE owner LIKE 'user-%' AND LENGTH(owner) = 21",
        )
        if not hash_owners:
            return 0

        # Migrate all hash-based views to the real owner
        total = 0
        for row in hash_owners:
            old_owner = row["owner"]
            result = db.execute("UPDATE views SET owner = ? WHERE owner = ?", (new_owner, old_owner))
            total += getattr(result, "rowcount", 0) if result else 0

        if total > 0:
            db.commit()

        return total

    # ------------------------------------------------------------------
    # Lifecycle — status transitions, claims, finding lookup
    # ------------------------------------------------------------------

    @_db_safe
    def transition_view_status(self, view_id: str, actor: str, new_status: str) -> bool:
        """Transition a view's status. Validates the transition is legal. Creates a version snapshot."""
        db = self.db
        row = db.fetchone("SELECT view_type, status FROM views WHERE id = ?", (view_id,))
        if not row:
            return False

        view_type = row["view_type"]
        current_status = row["status"]
        allowed = _STATUS_TRANSITIONS.get(view_type, {}).get(current_status, set())
        if new_status not in allowed:
            return False

        try:
            self.snapshot_view(view_id, f"status:{new_status}")
        except Exception:
            logger.exception("Failed to snapshot view %s on status transition to %s", view_id, new_status)

        cursor = db.execute(
            "UPDATE views SET status = ?, updated_at = ? WHERE id = ?",
            (new_status, datetime.now(UTC).isoformat(), view_id),
        )
        db.commit()
        return getattr(cursor, "rowcount", 1) > 0

    @_db_safe
    def claim_view(self, view_id: str, username: str) -> bool:
        """Claim a team-visible view. Only team views can be claimed."""
        db = self.db
        cursor = db.execute(
            "UPDATE views SET claimed_by = ?, claimed_at = ? WHERE id = ? AND visibility = 'team'",
            (username, datetime.now(UTC).isoformat(), view_id),
        )
        db.commit()
        return getattr(cursor, "rowcount", 1) > 0

    @_db_safe
    def unclaim_view(self, view_id: str, username: str) -> bool:
        """Release a claim on a view. Only the claimant can unclaim."""
        db = self.db
        cursor = db.execute(
            "UPDATE views SET claimed_by = NULL, claimed_at = NULL WHERE id = ? AND claimed_by = ?",
            (view_id, username),
        )
        db.commit()
        return getattr(cursor, "rowcount", 1) > 0

    def expire_stale_claims(self) -> int:
        """Clear claims older than 30 minutes. Returns count of expired claims."""
        db = self.db
        cutoff = (datetime.now(UTC) - timedelta(minutes=_CLAIM_EXPIRY_MINUTES)).isoformat()
        cursor = db.execute(
            "UPDATE views SET claimed_by = NULL, claimed_at = NULL WHERE claimed_by IS NOT NULL AND claimed_at < ?",
            (cutoff,),
        )
        db.commit()
        count = getattr(cursor, "rowcount", 0)
        if count:
            logger.info("Expired %d stale view claims", count)
        return count

    @_db_safe
    def find_similar_views(self, title: str, view_type: str = "incident", limit: int = 3) -> list[dict]:
        """Find resolved/completed views with similar titles. Uses trigram-like word overlap."""
        db = self.db
        rows = db.fetchall(
            "SELECT id, title, status, view_type, updated_at FROM views "
            "WHERE view_type = ? AND status IN ('resolved', 'completed', 'acknowledged', 'archived') "
            "ORDER BY updated_at DESC LIMIT 50",
            (view_type,),
        )
        if not rows:
            return []

        title_words = set(title.lower().split())
        scored: list[tuple[float, dict]] = []
        for row in rows:
            row_words = set(row["title"].lower().split())
            if not title_words or not row_words:
                continue
            overlap = len(title_words & row_words) / max(len(title_words | row_words), 1)
            if overlap >= 0.3:
                scored.append((overlap, row))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [row for _, row in scored[:limit]]

    @_db_safe
    def get_view_by_finding(self, finding_id: str) -> dict | None:
        """Find a view linked to a monitor finding. Returns the most recent match."""
        db = self.db
        row = db.fetchone(
            "SELECT * FROM views WHERE finding_id = ? ORDER BY updated_at DESC LIMIT 1",
            (finding_id,),
        )
        return _deserialize_view_row(row) if row else None

    @_db_safe
    def reopen_view_for_finding(self, finding_id: str) -> str | None:
        """Reopen a resolved/archived view when its finding recurs.

        Returns the view_id if reopened, None if no matching resolved view exists.
        """
        db = self.db
        row = db.fetchone(
            "SELECT id, status, view_type FROM views "
            "WHERE finding_id = ? AND status IN ('resolved', 'archived') "
            "ORDER BY updated_at DESC LIMIT 1",
            (finding_id,),
        )
        if not row or row["view_type"] not in ("incident", "assessment"):
            return None

        view_id = row["id"]
        try:
            self.snapshot_view(view_id, "status:recurrence")
        except Exception:
            logger.exception("Failed to snapshot view %s on recurrence", view_id)

        db.execute(
            "UPDATE views SET status = 'investigating', updated_at = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), view_id),
        )
        db.commit()
        return view_id

    @_db_safe
    def escalate_assessment_to_incident(self, view_id: str) -> bool:
        """Escalate an assessment view to an incident when the predicted issue materializes."""
        db = self.db
        row = db.fetchone(
            "SELECT view_type, status FROM views WHERE id = ?",
            (view_id,),
        )
        if not row or row["view_type"] != "assessment":
            return False

        try:
            self.snapshot_view(view_id, "escalated:assessment_to_incident")
        except Exception:
            logger.exception("Failed to snapshot view %s on assessment escalation", view_id)

        db.execute(
            "UPDATE views SET view_type = 'incident', status = 'investigating', updated_at = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), view_id),
        )
        db.commit()
        return True

    def extract_resolution_tools(self, view_id: str) -> list[dict]:
        """Extract action_button tool sequences from a view's layout.

        Returns list of {action, action_input, label} dicts.
        """
        view = self.get_view(view_id)
        if not view:
            return []
        layout = view.get("layout", [])
        tools: list[dict] = []

        def _scan(components: list[dict]) -> None:
            for comp in components:
                if comp.get("kind") == "action_button":
                    tools.append(
                        {
                            "action": comp.get("action", ""),
                            "action_input": comp.get("action_input", {}),
                            "label": comp.get("label", ""),
                        }
                    )
                for key in ("items", "components"):
                    nested = comp.get(key)
                    if isinstance(nested, list):
                        _scan(nested)
                tabs = comp.get("tabs")
                if isinstance(tabs, list):
                    for tab in tabs:
                        tab_comps = tab.get("components")
                        if isinstance(tab_comps, list):
                            _scan(tab_comps)

        _scan(layout)
        return tools


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_view_repo: ViewRepository | None = None


def get_view_repo() -> ViewRepository:
    """Return the module-level ViewRepository singleton."""
    global _view_repo
    if _view_repo is None:
        _view_repo = ViewRepository()
    return _view_repo
