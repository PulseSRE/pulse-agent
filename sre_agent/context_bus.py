"""Shared context bus for cross-agent communication.

Allows the Monitor, SRE Agent, and Security Agent to share recent
findings, investigations, fixes, and diagnoses so each component
can make better-informed decisions.

Storage is database-backed via the Database abstraction, with an
in-memory fallback for the publish path protected by a thread lock.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field

from .db import get_database
from . import db_schema

logger = logging.getLogger("pulse_agent.context_bus")


@dataclass
class ContextEntry:
    source: str  # 'monitor', 'sre_agent', 'security_agent'
    category: str  # 'finding', 'investigation', 'fix', 'diagnosis', 'user_resolution', 'verification'
    summary: str
    details: dict
    timestamp: float = field(default_factory=time.time)
    namespace: str = ""
    resources: list = field(default_factory=list)


_tables_ensured = False


def _ensure_tables() -> None:
    global _tables_ensured
    if _tables_ensured:
        return
    try:
        db = get_database()
        db.executescript(db_schema.CONTEXT_ENTRIES_SCHEMA)
        db.executescript(
            "CREATE INDEX IF NOT EXISTS idx_context_entries_ts ON context_entries(timestamp DESC);\n"
            "CREATE INDEX IF NOT EXISTS idx_context_entries_ns ON context_entries(namespace);\n"
        )
        _tables_ensured = True
    except Exception as e:
        logger.warning("Failed to ensure context_entries table: %s", e)


class ContextBus:
    """Shared context between Monitor, SRE Agent, and Security Agent."""

    def __init__(self, max_entries: int = 100, ttl_seconds: int = 3600):
        self._lock = threading.Lock()
        self._ttl = ttl_seconds
        self._max_entries = max_entries

    def publish(self, entry: ContextEntry) -> None:
        """Publish a context entry from any agent."""
        with self._lock:
            try:
                _ensure_tables()
                db = get_database()
                timestamp_ms = int(entry.timestamp * 1000)
                db.execute(
                    """INSERT INTO context_entries
                       (source, category, summary, details, timestamp, namespace, resources)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        entry.source,
                        entry.category,
                        entry.summary,
                        json.dumps(entry.details),
                        timestamp_ms,
                        entry.namespace,
                        json.dumps(entry.resources),
                    ),
                )
                db.commit()
                # Prune old entries beyond max_entries (use id for stable ordering)
                db.execute(
                    """DELETE FROM context_entries WHERE id NOT IN (
                       SELECT id FROM context_entries ORDER BY id DESC LIMIT ?
                    )""",
                    (self._max_entries,),
                )
                db.commit()
            except Exception as e:
                logger.warning("Failed to publish context entry: %s", e)

    def get_context_for(self, namespace: str = "", category: str = "", limit: int = 5) -> list[ContextEntry]:
        """Get recent context entries, optionally filtered."""
        with self._lock:
            try:
                _ensure_tables()
                db = get_database()
                now_ms = int(time.time() * 1000)
                cutoff_ms = now_ms - self._ttl * 1000

                where_parts = ["timestamp > ?"]
                params: list = [cutoff_ms]

                if namespace:
                    where_parts.append("(namespace = ? OR namespace = '')")
                    params.append(namespace)
                if category:
                    where_parts.append("category = ?")
                    params.append(category)

                where = " AND ".join(where_parts)
                rows = db.fetchall(
                    f"SELECT * FROM context_entries WHERE {where} ORDER BY timestamp DESC LIMIT ?",
                    tuple(params + [limit]),
                )

                entries = []
                for r in rows:
                    details = r["details"]
                    if isinstance(details, str):
                        try:
                            details = json.loads(details)
                        except Exception:
                            details = {}
                    resources = r["resources"]
                    if isinstance(resources, str):
                        try:
                            resources = json.loads(resources)
                        except Exception:
                            resources = []
                    entries.append(ContextEntry(
                        source=r["source"],
                        category=r["category"],
                        summary=r["summary"],
                        details=details,
                        timestamp=r["timestamp"] / 1000.0,  # convert ms back to seconds
                        namespace=r["namespace"],
                        resources=resources,
                    ))
                return entries
            except Exception as e:
                logger.warning("Failed to get context entries: %s", e)
                return []

    def build_context_prompt(self, namespace: str = "", limit: int = 5) -> str:
        """Build a context injection string for agent system prompts."""
        entries = self.get_context_for(namespace=namespace, limit=limit)
        if not entries:
            return ""
        lines = ["## Recent Agent Activity (shared context)"]
        for e in entries:
            age = int(time.time() - e.timestamp)
            age_str = f"{age}s ago" if age < 60 else f"{age // 60}m ago"
            lines.append(f"- [{e.source}] {e.summary} ({age_str})")
            if e.details.get("suspected_cause"):
                lines.append(f"  Suspected cause: {e.details['suspected_cause']}")
            if e.details.get("fix_applied"):
                lines.append(f"  Fix applied: {e.details['fix_applied']}")
        return "\n".join(lines)


# Singleton
_bus = ContextBus()


def get_context_bus() -> ContextBus:
    return _bus
