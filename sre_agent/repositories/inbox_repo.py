"""Inbox repository -- all inbox-related database operations.

Extracted from ``inbox.py`` to keep domain logic cohesive.  The original
module-level functions in ``inbox.py`` now delegate here for backward
compatibility.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .base import BaseRepository

logger = logging.getLogger("pulse_agent.inbox")


class InboxRepository(BaseRepository):
    """Database operations for the ops inbox."""

    # -- Single-item reads ---------------------------------------------------

    def fetch_item(self, item_id: str) -> dict[str, Any] | None:
        """Fetch a single inbox item by id (raw row)."""
        return self.db.fetchone("SELECT * FROM inbox_items WHERE id = ?", (item_id,))

    # -- Insert --------------------------------------------------------------

    def insert_item(
        self,
        item_id: str,
        item: dict[str, Any],
        priority: float,
        cluster_id: str | None,
        now: int,
    ) -> None:
        resources = item.get("resources", [])
        metadata = item.get("metadata", {})
        self.db.execute(
            """INSERT INTO inbox_items
            (id, item_type, status, title, summary, severity, priority_score,
             confidence, noise_score, namespace, resources, correlation_key,
             created_by, due_date, finding_id, view_id, cluster_id,
             pinned_by, metadata, created_at, updated_at)
            VALUES (?, ?, 'new', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?)""",
            (
                item_id,
                item["item_type"],
                item["title"],
                item.get("summary", ""),
                item.get("severity"),
                priority,
                item.get("confidence", 0),
                item.get("noise_score", 0),
                item.get("namespace"),
                json.dumps(resources),
                item.get("correlation_key"),
                item["created_by"],
                item.get("due_date"),
                item.get("finding_id"),
                item.get("view_id"),
                cluster_id,
                json.dumps(metadata),
                now,
                now,
            ),
        )
        self.db.commit()

    # -- List / query --------------------------------------------------------

    def query_items(
        self,
        where: str,
        params: tuple[Any, ...],
    ) -> list[Any]:
        return self.db.fetchall(
            f"SELECT * FROM inbox_items WHERE {where} ORDER BY priority_score DESC LIMIT ? OFFSET ?",
            params,
        )

    async def async_query_items(
        self,
        where: str,
        params: tuple[Any, ...],
    ) -> list[Any]:
        db = await self.get_async_db()
        return await db.fetchall(
            f"SELECT * FROM inbox_items WHERE {where} ORDER BY priority_score DESC LIMIT ? OFFSET ?",
            *params,
        )

    def get_stats_rows(self, now: int) -> list[Any]:
        return self.db.fetchall(
            """SELECT status, COUNT(*) as cnt,
            COUNT(DISTINCT correlation_key) as unique_cnt
            FROM inbox_items
            WHERE (snoozed_until IS NULL OR snoozed_until <= ?)
            GROUP BY status""",
            (now,),
        )

    async def async_get_stats_rows(self, now: int) -> list[Any]:
        db = await self.get_async_db()
        return await db.fetchall(
            """SELECT status, COUNT(*) as cnt,
            COUNT(DISTINCT correlation_key) as unique_cnt
            FROM inbox_items
            WHERE (snoozed_until IS NULL OR snoozed_until <= ?)
            GROUP BY status""",
            now,
        )

    def fetch_stale_agent_reviewing(self, stale_cutoff: int) -> list[Any]:
        return self.db.fetchall(
            """SELECT * FROM inbox_items
            WHERE status IN ('agent_reviewing', 'agent_review_failed')
            AND updated_at < ?""",
            (stale_cutoff,),
        )

    def fetch_untouched_open_items(self, cutoff: int) -> list[Any]:
        """Open items last updated before the cutoff and never claimed or pinned.

        Filtered here rather than in Python so an inbox with thousands of rows
        does not have to be deserialised to find the handful that are stale.
        """
        return self.db.fetchall(
            """SELECT * FROM inbox_items
            WHERE status IN ('new', 'triaged')
            AND updated_at < ?
            AND claimed_by IS NULL
            -- pinned_by is a JSON list and defaults to '[]', never NULL. An
            -- IS NULL test here matched no row in the table, so expiry could
            -- never fire; the empty-list case is the one that means unpinned.
            AND coalesce(pinned_by, '[]') IN ('[]', '')""",
            (cutoff,),
        )

    def fetch_items_by_category_window(self, categories: tuple[str, ...], start: int, end: int) -> list[Any]:
        """Items in these categories created inside a time window, oldest first.

        Used to answer "what changed" for an episode. `category` is not a
        column — it lives in the item's metadata — so this matches on the
        correlation key, which is built as `category:namespace:resource`.
        """
        prefixes = " OR ".join("correlation_key LIKE ?" for _ in categories)
        params: list[Any] = [f"{c}:%" for c in categories]
        params.extend([start, end])
        return self.db.fetchall(
            f"""SELECT id, title, namespace, correlation_key, created_at
            FROM inbox_items
            WHERE ({prefixes})
            AND created_at >= ? AND created_at <= ?
            ORDER BY created_at""",
            tuple(params),
        )

    # -- Status updates ------------------------------------------------------

    def archive_with_reason(self, item_id: str, reason: str, metadata: dict[str, Any], now: int) -> None:
        """Archive an item and record why, so it never just disappears.

        The reason goes in metadata rather than a new column: it is read by the
        UI to explain the row and by nothing else, and a migration for one
        string is not worth the schema churn.
        """
        metadata = dict(metadata or {})
        metadata["archived_reason"] = reason
        metadata["archived_at"] = now
        self.db.execute(
            "UPDATE inbox_items SET status = 'archived', metadata = ?, updated_at = ? WHERE id = ?",
            (json.dumps(metadata), now, item_id),
        )

    def update_status(
        self,
        item_id: str,
        new_status: str,
        now: int,
        resolved_at: int | None = None,
    ) -> None:
        self.db.execute(
            "UPDATE inbox_items SET status = ?, updated_at = ?, resolved_at = ? WHERE id = ?",
            (new_status, now, resolved_at, item_id),
        )
        self.db.commit()

    # -- Claim / unclaim -----------------------------------------------------

    def update_claim(
        self,
        item_id: str,
        username: str,
        now: int,
    ) -> None:
        self.db.execute(
            "UPDATE inbox_items SET claimed_by = ?, claimed_at = ?, updated_at = ? WHERE id = ?",
            (username, now, now, item_id),
        )
        self.db.commit()

    def update_claim_and_status(
        self,
        item_id: str,
        username: str,
        target_status: str,
        now: int,
    ) -> None:
        self.db.execute(
            "UPDATE inbox_items SET claimed_by = ?, claimed_at = ?, status = ?, updated_at = ? WHERE id = ? AND (claimed_by IS NULL OR claimed_by = ?)",
            (username, now, target_status, now, item_id, username),
        )
        self.db.commit()

    def clear_claim(self, item_id: str, now: int) -> None:
        self.db.execute(
            "UPDATE inbox_items SET claimed_by = NULL, claimed_at = NULL, updated_at = ? WHERE id = ?",
            (now, item_id),
        )
        self.db.commit()

    # -- Metadata updates ----------------------------------------------------

    def update_metadata(self, item_id: str, metadata: dict[str, Any], now: int) -> None:
        self.db.execute(
            "UPDATE inbox_items SET metadata = ?, updated_at = ? WHERE id = ?",
            (json.dumps(metadata), now, item_id),
        )
        self.db.commit()

    def update_metadata_and_view(
        self,
        item_id: str,
        view_id: str,
        metadata: dict[str, Any],
        now: int,
    ) -> None:
        self.db.execute(
            "UPDATE inbox_items SET view_id = ?, metadata = ?, updated_at = ? WHERE id = ?",
            (view_id, json.dumps(metadata), now, item_id),
        )
        self.db.commit()

    # -- Snooze --------------------------------------------------------------

    def set_snooze(
        self,
        item_id: str,
        snoozed_until: int,
        metadata: dict[str, Any],
        now: int,
    ) -> None:
        self.db.execute(
            "UPDATE inbox_items SET snoozed_until = ?, metadata = ?, updated_at = ? WHERE id = ?",
            (snoozed_until, json.dumps(metadata), now, item_id),
        )
        self.db.commit()

    def fetch_expired_snoozed(self, now: int) -> list[Any]:
        return self.db.fetchall(
            "SELECT id, metadata FROM inbox_items WHERE snoozed_until IS NOT NULL AND snoozed_until <= ?",
            (now,),
        )

    def clear_snooze(
        self,
        item_id: str,
        status: str,
        metadata: dict[str, Any],
        now: int,
    ) -> None:
        self.db.execute(
            "UPDATE inbox_items SET snoozed_until = NULL, status = ?, metadata = ?, updated_at = ? WHERE id = ?",
            (status, json.dumps(metadata), now, item_id),
        )

    def commit(self) -> None:
        self.db.commit()

    # -- Upsert / dedup ------------------------------------------------------

    def find_active_by_correlation(
        self,
        corr_key: str,
        item_type: str,
    ) -> Any | None:
        return self.db.fetchone(
            "SELECT * FROM inbox_items WHERE correlation_key = ? AND item_type = ? AND status NOT IN ('resolved', 'archived')",
            (corr_key, item_type),
        )

    def find_recently_resolved(
        self,
        corr_key: str,
        item_type: str | None = None,
        since: int | None = None,
    ) -> Any | None:
        if since is None:
            since = int(__import__("time").time()) - 3600
        if item_type:
            return self.db.fetchone(
                "SELECT * FROM inbox_items WHERE correlation_key = ? AND item_type = ? AND status IN ('resolved', 'archived') AND updated_at > ?",
                (corr_key, item_type, since),
            )
        return self.db.fetchone(
            "SELECT * FROM inbox_items WHERE correlation_key = ? AND status IN ('resolved', 'archived') AND updated_at > ? ORDER BY updated_at DESC LIMIT 1",
            (corr_key, since),
        )

    def update_resources_and_priority(
        self,
        item_id: str,
        resources: list[dict[str, Any]],
        priority: float,
        now: int,
        title: str | None = None,
        summary: str | None = None,
    ) -> None:
        """Refresh a correlated item.

        title/summary are optional so existing callers keep their behaviour, but
        the generator path should pass them. They were previously never updated,
        so an item's wording was frozen at creation while its resources were
        pruned and re-pointed underneath it — leaving titles naming pods that
        had been deleted days earlier, and restart counts stuck at their
        first-seen value. The item was still valid; only its label was stale.
        """
        fields = ["resources = ?", "priority_score = ?", "updated_at = ?"]
        values: list[Any] = [json.dumps(resources), priority, now]
        if title:
            fields.append("title = ?")
            values.append(title)
        if summary:
            fields.append("summary = ?")
            values.append(summary)
        values.append(item_id)
        self.db.execute(f"UPDATE inbox_items SET {', '.join(fields)} WHERE id = ?", tuple(values))
        self.db.commit()

    # -- Escalation ----------------------------------------------------------

    def resolve_item(self, item_id: str, now: int, metadata: dict[str, Any] | None = None) -> None:
        if metadata is not None:
            self.db.execute(
                "UPDATE inbox_items SET status = 'resolved', resolved_at = ?, metadata = ?, updated_at = ? WHERE id = ?",
                (now, json.dumps(metadata), now, item_id),
            )
        else:
            self.db.execute(
                "UPDATE inbox_items SET status = 'resolved', updated_at = ?, resolved_at = ? WHERE id = ?",
                (now, now, item_id),
            )
        self.db.commit()

    # -- Pin -----------------------------------------------------------------

    def update_pinned_by(self, item_id: str, pinned_by: list[str], now: int) -> None:
        self.db.execute(
            "UPDATE inbox_items SET pinned_by = ?, updated_at = ? WHERE id = ?",
            (json.dumps(pinned_by), now, item_id),
        )
        self.db.commit()

    # -- Dismiss -------------------------------------------------------------

    def archive_item(self, item_id: str, now: int) -> None:
        self.db.execute(
            "UPDATE inbox_items SET status = 'archived', updated_at = ?, resolved_at = ? WHERE id = ?",
            (now, now, item_id),
        )
        self.db.commit()

    # -- Prune ---------------------------------------------------------------

    def delete_old_resolved(self, cutoff: int) -> int:
        cur = self.db.execute(
            "DELETE FROM inbox_items WHERE status IN ('resolved', 'archived') AND resolved_at IS NOT NULL AND resolved_at < ?",
            (cutoff,),
        )
        self.db.commit()
        return cur.rowcount if hasattr(cur, "rowcount") else 0

    # -- Finding resolution --------------------------------------------------

    def find_active_by_finding_id(self, finding_id: str) -> Any | None:
        return self.db.fetchone(
            "SELECT * FROM inbox_items WHERE finding_id = ? AND status NOT IN ('resolved', 'archived')",
            (finding_id,),
        )

    # -- Bridge (monitor integration) ----------------------------------------

    def find_active_by_correlation_task(self, corr_key: str) -> Any | None:
        return self.db.fetchone(
            "SELECT * FROM inbox_items WHERE correlation_key = ? AND item_type = 'task' AND status NOT IN ('resolved', 'archived')",
            (corr_key,),
        )

    # -- Agent pipeline (triage / investigate / plan) ------------------------

    def fetch_new_for_triage(self, limit: int = 5) -> list[Any]:
        return self.db.fetchall(
            """SELECT * FROM inbox_items
            WHERE status IN ('new', 'agent_review_failed')
            AND (metadata NOT LIKE ? OR metadata NOT LIKE ?)
            ORDER BY priority_score DESC
            LIMIT ?""",
            ('%"triaged"%', "%true%", limit),
        )

    def update_triage_result(
        self,
        item_id: str,
        new_status: str,
        metadata: dict[str, Any],
        summary: str,
        now: int,
    ) -> None:
        self.db.execute(
            "UPDATE inbox_items SET status = ?, metadata = ?, summary = ?, updated_at = ? WHERE id = ?",
            (new_status, json.dumps(metadata), summary, now, item_id),
        )
        self.db.commit()

    def fetch_agent_reviewing(self, limit: int = 3) -> list[Any]:
        return self.db.fetchall(
            """SELECT * FROM inbox_items
            WHERE status = 'agent_reviewing'
            ORDER BY priority_score DESC
            LIMIT ?""",
            (limit,),
        )

    def update_resources(
        self,
        item_id: str,
        resources: list[dict[str, Any]],
        now: int,
    ) -> None:
        self.db.execute(
            "UPDATE inbox_items SET resources = ?, updated_at = ? WHERE id = ?",
            (json.dumps(resources), now, item_id),
        )
        self.db.commit()

    def update_investigation_result(
        self,
        item_id: str,
        new_status: str,
        metadata: dict[str, Any],
        now: int,
    ) -> None:
        self.db.execute(
            "UPDATE inbox_items SET status = ?, metadata = ?, updated_at = ? WHERE id = ?",
            (new_status, json.dumps(metadata), now, item_id),
        )
        self.db.commit()

    def fetch_triaged_without_plan(self, limit: int = 3) -> list[Any]:
        return self.db.fetchall(
            """SELECT * FROM inbox_items
            WHERE status = 'triaged'
            AND metadata NOT LIKE ?
            ORDER BY priority_score DESC
            LIMIT ?""",
            ("%action_plan%", limit),
        )

    def update_plan_metadata(
        self,
        item_id: str,
        metadata: dict[str, Any],
        now: int,
    ) -> None:
        self.db.execute(
            "UPDATE inbox_items SET metadata = ?, updated_at = ? WHERE id = ?",
            (json.dumps(metadata), now, item_id),
        )
        self.db.commit()

    # -- Generator cycle -----------------------------------------------------

    def fetch_generator_items(self) -> list[Any]:
        return self.db.fetchall(
            """SELECT id, correlation_key, metadata FROM inbox_items
            WHERE item_type = 'task'
            AND status IN ('new', 'triaged')""",
        )

    def auto_resolve_generator_item(self, item_id: str, now: int) -> None:
        self.db.execute(
            "UPDATE inbox_items SET status = 'resolved', resolved_at = ?, updated_at = ? WHERE id = ?",
            (now, now, item_id),
        )

    # -- Mutes ---------------------------------------------------------------

    def set_mute(self, correlation_key: str, muted_until: int | None, muted_by: str, reason: str, now: int) -> None:
        """Silence a correlation key. muted_until=None means indefinitely."""
        self.db.execute(
            """INSERT INTO inbox_mutes (correlation_key, muted_until, muted_by, reason, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (correlation_key) DO UPDATE SET
                 muted_until = EXCLUDED.muted_until,
                 muted_by = EXCLUDED.muted_by,
                 reason = EXCLUDED.reason,
                 created_at = EXCLUDED.created_at""",
            (correlation_key, muted_until, muted_by, reason, now),
        )
        self.db.commit()

    def clear_mute(self, correlation_key: str) -> None:
        self.db.execute("DELETE FROM inbox_mutes WHERE correlation_key = ?", (correlation_key,))
        self.db.commit()

    def is_muted(self, correlation_key: str, now: int) -> bool:
        """True while a mute is active. Expired mutes are treated as absent."""
        if not correlation_key:
            return False
        row = self.db.fetchone(
            "SELECT muted_until FROM inbox_mutes WHERE correlation_key = ?",
            (correlation_key,),
        )
        if row is None:
            return False
        until = row["muted_until"]
        return until is None or until > now

    def list_mutes(self) -> list[Any]:
        return self.db.fetchall("SELECT * FROM inbox_mutes ORDER BY created_at DESC")

    def fetch_open_items_with_resources(self) -> list[Any]:
        return self.db.fetchall(
            "SELECT id, resources FROM inbox_items WHERE status NOT IN ('resolved', 'archived')",
        )

    def update_item_resources_raw(
        self,
        item_id: str,
        resources_json: str,
    ) -> None:
        self.db.execute(
            "UPDATE inbox_items SET resources = ? WHERE id = ?",
            (resources_json, item_id),
        )

    def record_interaction(
        self,
        actor: str,
        interaction_type: str,
        item_id: str | None,
        action_id: str | None,
        decision: str,
        metadata_json: str,
    ) -> None:
        self.db.execute(
            "INSERT INTO user_interactions (actor, interaction_type, item_id, action_id, decision, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (actor, interaction_type, item_id, action_id, decision, metadata_json),
        )
        self.db.commit()


# -- Singleton ---------------------------------------------------------------

_inbox_repo: InboxRepository | None = None


def get_inbox_repo() -> InboxRepository:
    """Return the module-level InboxRepository singleton."""
    global _inbox_repo
    if _inbox_repo is None:
        _inbox_repo = InboxRepository()
    return _inbox_repo
