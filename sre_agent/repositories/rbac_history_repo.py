"""RBAC history repository: observed object versions and the Pulse write ledger.

``rbac_object_versions`` holds one row per observed state of a Role,
ClusterRole, RoleBinding, ClusterRoleBinding, SecurityContextConstraints or
admission webhook configuration (see ``sre_agent/rbac_history.py``).
``pulse_write_ledger`` holds one row per write made through Pulse.
"""

from __future__ import annotations

import json
from typing import Any

from .base import BaseRepository

_VERSION_COLS = (
    "id, kind, namespace, name, uid, resource_version, change, observed_at, manager, operation, manager_time, content"
)
_LEDGER_COLS = "id, at, actor, source, verb, kind, namespace, name, reason, outcome, before, after, detail"


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True, default=str)


def _parsed(value: Any) -> Any:
    # psycopg returns JSONB parsed; a text value (other drivers, tests) is parsed here.
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


class RbacHistoryRepository(BaseRepository):
    """Database operations for RBAC version history and the write ledger."""

    # -- Versions -------------------------------------------------------------

    def latest_per_object(self) -> list[dict]:
        """The newest row for every object ever observed (deleted ones included)."""
        rows = self.db.fetchall(
            "SELECT DISTINCT ON (kind, namespace, name) kind, namespace, name, resource_version, change, content "
            "FROM rbac_object_versions ORDER BY kind, namespace, name, id DESC",
        )
        return [{**r, "content": _parsed(r.get("content"))} for r in rows or []]

    def observed_kinds(self) -> set[str]:
        rows = self.db.fetchall("SELECT DISTINCT kind FROM rbac_object_versions")
        return {r["kind"] for r in rows or []}

    def insert_versions(self, rows: list[dict]) -> None:
        """Insert observed versions in one transaction."""
        if not rows:
            return
        for r in rows:
            self.db.execute(
                "INSERT INTO rbac_object_versions "
                "(kind, namespace, name, uid, resource_version, change, observed_at, manager, operation, manager_time, content) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    r["kind"],
                    r.get("namespace", ""),
                    r["name"],
                    r.get("uid", ""),
                    r.get("resource_version", ""),
                    r["change"],
                    r["observed_at"],
                    r.get("manager", ""),
                    r.get("operation", ""),
                    r.get("manager_time"),
                    _json(r.get("content")),
                ),
            )
        self.db.commit()

    def fetch_changes(
        self,
        since_ms: int,
        kind: str = "",
        namespace: str | None = None,
        name: str = "",
        limit: int = 200,
    ) -> list[dict]:
        """Changes (not baselines) observed since ``since_ms``, newest first, each with
        the content of the version before it (``before``) and its id (``previous_id``)."""
        where = ["v.change <> 'baseline'", "v.observed_at >= ?"]
        params: list[Any] = [since_ms]
        if kind:
            where.append("v.kind = ?")
            params.append(kind)
        if namespace is not None:
            where.append("v.namespace = ?")
            params.append(namespace)
        if name:
            where.append("v.name = ?")
            params.append(name)
        params.append(limit)
        rows = self.db.fetchall(
            f"SELECT v.id, v.kind, v.namespace, v.name, v.uid, v.resource_version, v.change, v.observed_at, "
            f"v.manager, v.operation, v.manager_time, v.content, p.id AS previous_id, p.content AS previous_content "
            f"FROM rbac_object_versions v "
            f"LEFT JOIN LATERAL (SELECT id, content FROM rbac_object_versions p "
            f"  WHERE p.kind = v.kind AND p.namespace = v.namespace AND p.name = v.name AND p.id < v.id "
            f"  ORDER BY p.id DESC LIMIT 1) p ON TRUE "
            f"WHERE {' AND '.join(where)} ORDER BY v.observed_at DESC, v.id DESC LIMIT ?",
            tuple(params),
        )
        out = []
        for r in rows or []:
            r = dict(r)
            r["content"] = _parsed(r.get("content"))
            r["previous_content"] = _parsed(r.get("previous_content"))
            out.append(r)
        return out

    def first_observed_at(self) -> int | None:
        row = self.db.fetchone("SELECT MIN(observed_at) AS t FROM rbac_object_versions")
        return int(row["t"]) if row and row.get("t") is not None else None

    def prune(self, before_ms: int) -> None:
        """Drop versions older than ``before_ms`` except each object's newest row,
        which is still the "before" of its next change."""
        self.db.execute(
            "DELETE FROM rbac_object_versions v WHERE v.observed_at < ? AND v.id <> "
            "(SELECT MAX(id) FROM rbac_object_versions n WHERE n.kind = v.kind AND n.namespace = v.namespace AND n.name = v.name)",
            (before_ms,),
        )
        self.db.commit()

    # -- Ledger ---------------------------------------------------------------

    def record_write(self, entry: dict) -> dict:
        cur = self.db.execute(
            "INSERT INTO pulse_write_ledger (at, actor, source, verb, kind, namespace, name, reason, outcome, before, after, detail) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING {_LEDGER_COLS}",
            (
                entry["at"],
                entry["actor"],
                entry["source"],
                entry["verb"],
                entry["kind"],
                entry.get("namespace", ""),
                entry["name"],
                entry.get("reason", ""),
                entry.get("outcome", "ok"),
                _json(entry.get("before")),
                _json(entry.get("after")),
                _json(entry.get("detail")),
            ),
        )
        row = cur.fetchone()
        cols = [d[0] for d in cur.description]
        self.db.commit()
        out = dict(zip(cols, row))
        for k in ("before", "after", "detail"):
            out[k] = _parsed(out.get(k))
        return out

    def fetch_ledger(self, since_ms: int, kinds: list[str] | None = None, limit: int = 200) -> list[dict]:
        where = ["at >= ?"]
        params: list[Any] = [since_ms]
        if kinds:
            where.append(f"kind IN ({', '.join('?' for _ in kinds)})")
            params.extend(kinds)
        params.append(limit)
        rows = self.db.fetchall(
            f"SELECT {_LEDGER_COLS} FROM pulse_write_ledger WHERE {' AND '.join(where)} ORDER BY at DESC, id DESC LIMIT ?",
            tuple(params),
        )
        out = []
        for r in rows or []:
            r = dict(r)
            for k in ("before", "after", "detail"):
                r[k] = _parsed(r.get(k))
            out.append(r)
        return out


# -- Singleton ---------------------------------------------------------------

_rbac_history_repo: RbacHistoryRepository | None = None


def get_rbac_history_repo() -> RbacHistoryRepository:
    """Return the module-level RbacHistoryRepository singleton."""
    global _rbac_history_repo
    if _rbac_history_repo is None:
        _rbac_history_repo = RbacHistoryRepository()
    return _rbac_history_repo
