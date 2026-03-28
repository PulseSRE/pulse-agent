"""SQLite-backed persistence for the self-improving agent."""

from __future__ import annotations

import functools
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("pulse_agent")


def db_safe(default=None):
    """Decorator that catches sqlite3 errors and returns a default value."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            try:
                return fn(self, *args, **kwargs)
            except sqlite3.Error as e:
                logger.error("SQLite error in %s: %s", fn.__name__, type(e).__name__)
                try:
                    from ..error_tracker import get_tracker
                    from ..errors import ToolError
                    get_tracker().record(ToolError(
                        message=f"Memory system error: {type(e).__name__}",
                        category="server",
                        operation=f"memory.{fn.__name__}",
                    ))
                except Exception:
                    pass
                return default
        return wrapper
    return decorator

DEFAULT_DB_PATH = os.path.expanduser("~/.pulse_agent/memory.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    query TEXT NOT NULL,
    query_keywords TEXT NOT NULL,
    tool_sequence TEXT NOT NULL,
    resolution TEXT NOT NULL,
    outcome TEXT DEFAULT 'unknown',
    namespace TEXT DEFAULT '',
    resource_type TEXT DEFAULT '',
    error_type TEXT DEFAULT '',
    tool_count INTEGER DEFAULT 0,
    rejected_tools INTEGER DEFAULT 0,
    duration_seconds REAL DEFAULT 0,
    score REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS runbooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    trigger_keywords TEXT NOT NULL,
    tool_sequence TEXT NOT NULL,
    success_count INTEGER DEFAULT 1,
    failure_count INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source_incident_id INTEGER,
    FOREIGN KEY (source_incident_id) REFERENCES incidents(id)
);

CREATE TABLE IF NOT EXISTS patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_type TEXT NOT NULL,
    description TEXT NOT NULL,
    keywords TEXT NOT NULL,
    incident_ids TEXT NOT NULL,
    frequency INTEGER DEFAULT 1,
    last_seen TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    metadata TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    value REAL NOT NULL,
    window TEXT DEFAULT 'session'
);

CREATE INDEX IF NOT EXISTS idx_incidents_keywords ON incidents(query_keywords);
CREATE INDEX IF NOT EXISTS idx_incidents_error_type ON incidents(error_type);
CREATE INDEX IF NOT EXISTS idx_runbooks_keywords ON runbooks(trigger_keywords);
CREATE INDEX IF NOT EXISTS idx_patterns_keywords ON patterns(keywords);
"""

_STOP_WORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "in", "on", "at",
    "to", "for", "of", "with", "by", "from", "it", "this", "that",
    "what", "why", "how", "can", "do", "does", "my", "me", "i",
    "all", "and", "or", "not", "no", "be", "been", "being", "have",
    "has", "had", "will", "would", "could", "should", "may", "might",
})


def extract_keywords(text: str) -> str:
    """Extract searchable keywords from text."""
    words = text.lower().split()
    keywords = [
        w.strip("?.,!\"'()[]{}:;")
        for w in words
        if w.strip("?.,!\"'()[]{}:;") not in _STOP_WORDS and len(w.strip("?.,!\"'()[]{}:;")) > 1
    ]
    return " ".join(keywords)


class IncidentStore:
    """SQLite-backed persistence for incidents, runbooks, patterns, and metrics."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or os.environ.get("PULSE_AGENT_MEMORY_PATH", DEFAULT_DB_PATH)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    @db_safe(default=-1)
    def record_incident(self, query: str, tool_sequence: list[dict],
                        resolution: str, outcome: str = "unknown",
                        namespace: str = "", resource_type: str = "",
                        error_type: str = "", tool_count: int = 0,
                        rejected_tools: int = 0, duration_seconds: float = 0,
                        score: float = 0) -> int:
        keywords = extract_keywords(query)
        cur = self.conn.execute(
            """INSERT INTO incidents (timestamp, query, query_keywords, tool_sequence,
               resolution, outcome, namespace, resource_type, error_type,
               tool_count, rejected_tools, duration_seconds, score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (datetime.now(timezone.utc).isoformat(), query, keywords,
             json.dumps(tool_sequence), resolution[:2000], outcome, namespace,
             resource_type, error_type, tool_count, rejected_tools,
             duration_seconds, score)
        )
        self.conn.commit()
        return cur.lastrowid

    @db_safe(default=None)
    def update_incident_outcome(self, incident_id: int, outcome: str, score: float) -> None:
        self.conn.execute(
            "UPDATE incidents SET outcome = ?, score = ? WHERE id = ?",
            (outcome, score, incident_id)
        )
        self.conn.commit()

    @db_safe(default=[])
    def search_incidents(self, query: str, limit: int = 5) -> list[dict]:
        keywords = extract_keywords(query).split()
        if not keywords:
            return []
        conditions = " OR ".join(["query_keywords LIKE ?"] * len(keywords))
        params = [f"%{kw}%" for kw in keywords]
        rows = self.conn.execute(
            f"""SELECT * FROM incidents WHERE ({conditions})
                ORDER BY score DESC, timestamp DESC LIMIT ?""",
            params + [limit]
        ).fetchall()
        return [dict(r) for r in rows]

    @db_safe(default=[])
    def search_low_score_incidents(self, query: str, threshold: float = 0.4, limit: int = 2) -> list[dict]:
        """Find similar incidents with low scores to surface anti-patterns."""
        keywords = extract_keywords(query).split()
        if not keywords:
            return []
        conditions = " OR ".join(["query_keywords LIKE ?"] * len(keywords))
        params = [f"%{kw}%" for kw in keywords]
        rows = self.conn.execute(
            f"""SELECT * FROM incidents WHERE ({conditions}) AND score < ? AND score > 0
                ORDER BY score ASC, timestamp DESC LIMIT ?""",
            params + [threshold, limit]
        ).fetchall()
        return [dict(r) for r in rows]

    def save_runbook(self, name: str, description: str, trigger_keywords: str,
                     tool_sequence: list[dict], source_incident_id: int | None = None) -> int:
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            """INSERT INTO runbooks (name, description, trigger_keywords, tool_sequence,
               created_at, updated_at, source_incident_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (name, description, trigger_keywords, json.dumps(tool_sequence),
             now, now, source_incident_id)
        )
        self.conn.commit()
        return cur.lastrowid

    @db_safe(default=[])
    def find_runbooks(self, query: str, limit: int = 3) -> list[dict]:
        keywords = extract_keywords(query).split()
        if not keywords:
            return []
        conditions = " OR ".join(["trigger_keywords LIKE ?"] * len(keywords))
        params = [f"%{kw}%" for kw in keywords]
        rows = self.conn.execute(
            f"""SELECT * FROM runbooks WHERE ({conditions})
                ORDER BY success_count DESC LIMIT ?""",
            params + [limit]
        ).fetchall()
        return [dict(r) for r in rows]

    def list_runbooks(self, limit: int = 10) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM runbooks ORDER BY success_count DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def record_pattern(self, pattern_type: str, description: str,
                       keywords: str, incident_ids: list[int],
                       metadata: dict | None = None) -> int:
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            """INSERT INTO patterns (pattern_type, description, keywords,
               incident_ids, last_seen, first_seen, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (pattern_type, description, keywords, json.dumps(incident_ids),
             now, now, json.dumps(metadata or {}))
        )
        self.conn.commit()
        return cur.lastrowid

    def list_patterns(self, limit: int = 10) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM patterns ORDER BY frequency DESC, last_seen DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def search_patterns(self, query: str, limit: int = 3) -> list[dict]:
        keywords = extract_keywords(query).split()
        if not keywords:
            return []
        conditions = " OR ".join(["keywords LIKE ?"] * len(keywords))
        params = [f"%{kw}%" for kw in keywords]
        rows = self.conn.execute(
            f"SELECT * FROM patterns WHERE ({conditions}) ORDER BY frequency DESC LIMIT ?",
            params + [limit]
        ).fetchall()
        return [dict(r) for r in rows]

    @db_safe(default=None)
    def record_metric(self, metric_name: str, value: float,
                      window: str = "session") -> None:
        self.conn.execute(
            "INSERT INTO metrics (timestamp, metric_name, value, window) VALUES (?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), metric_name, value, window)
        )
        self.conn.commit()

    def get_metrics_summary(self) -> dict:
        rows = self.conn.execute(
            "SELECT metric_name, AVG(value) as avg, COUNT(*) as count FROM metrics GROUP BY metric_name"
        ).fetchall()
        return {r["metric_name"]: {"avg": r["avg"], "count": r["count"]} for r in rows}

    def get_incident_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) as c FROM incidents").fetchone()
        return row["c"]

    @db_safe(default=[])
    def export_runbooks(self) -> list[dict]:
        """Export all learned runbooks as JSON-serialisable dicts."""
        rows = self.conn.execute(
            "SELECT name, description, trigger_keywords, tool_sequence, "
            "success_count, failure_count, created_at, updated_at FROM runbooks"
        ).fetchall()
        return [
            {
                "name": r["name"],
                "description": r["description"],
                "trigger_keywords": r["trigger_keywords"],
                "tool_sequence": json.loads(r["tool_sequence"]),
                "success_count": r["success_count"],
                "failure_count": r["failure_count"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]

    @db_safe(default=0)
    def import_runbooks(self, runbooks: list[dict]) -> int:
        """Import runbooks from JSON. Skips duplicates by name. Returns count imported."""
        existing_names = {
            r["name"]
            for r in self.conn.execute("SELECT name FROM runbooks").fetchall()
        }
        imported = 0
        now = datetime.now(timezone.utc).isoformat()
        for rb in runbooks:
            name = rb.get("name", "")
            if not name or name in existing_names:
                continue
            tool_seq = rb.get("tool_sequence", [])
            self.conn.execute(
                """INSERT INTO runbooks (name, description, trigger_keywords, tool_sequence,
                   success_count, failure_count, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    name,
                    rb.get("description", ""),
                    rb.get("trigger_keywords", ""),
                    json.dumps(tool_seq),
                    rb.get("success_count", 1),
                    rb.get("failure_count", 0),
                    rb.get("created_at", now),
                    now,
                ),
            )
            existing_names.add(name)
            imported += 1
        self.conn.commit()
        return imported

    @db_safe(default=[])
    def export_patterns(self) -> list[dict]:
        """Export all detected patterns as JSON-serialisable dicts."""
        rows = self.conn.execute(
            "SELECT pattern_type, description, keywords, incident_ids, "
            "frequency, first_seen, last_seen, metadata FROM patterns"
        ).fetchall()
        return [
            {
                "pattern_type": r["pattern_type"],
                "description": r["description"],
                "keywords": r["keywords"],
                "incident_ids": json.loads(r["incident_ids"]),
                "frequency": r["frequency"],
                "first_seen": r["first_seen"],
                "last_seen": r["last_seen"],
                "metadata": json.loads(r["metadata"]),
            }
            for r in rows
        ]

    @db_safe(default=0)
    def import_patterns(self, patterns: list[dict]) -> int:
        """Import patterns from JSON. Skips duplicates by description. Returns count imported."""
        existing_descs = {
            r["description"]
            for r in self.conn.execute("SELECT description FROM patterns").fetchall()
        }
        imported = 0
        now = datetime.now(timezone.utc).isoformat()
        for pat in patterns:
            desc = pat.get("description", "")
            if not desc or desc in existing_descs:
                continue
            self.conn.execute(
                """INSERT INTO patterns (pattern_type, description, keywords,
                   incident_ids, frequency, first_seen, last_seen, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    pat.get("pattern_type", "imported"),
                    desc,
                    pat.get("keywords", ""),
                    json.dumps(pat.get("incident_ids", [])),
                    pat.get("frequency", 1),
                    pat.get("first_seen", now),
                    now,
                    json.dumps(pat.get("metadata", {})),
                ),
            )
            existing_descs.add(desc)
            imported += 1
        self.conn.commit()
        return imported

    def cleanup(self, max_age_days: int = 90) -> int:
        """Delete incidents older than max_age_days. Returns number of deleted rows."""
        cutoff = datetime.now(timezone.utc)
        # SQLite datetime comparison: timestamps stored as ISO strings
        from datetime import timedelta
        cutoff_str = (cutoff - timedelta(days=max_age_days)).isoformat()
        cur = self.conn.execute(
            "DELETE FROM incidents WHERE timestamp < ?", (cutoff_str,)
        )
        # Clean up orphaned metrics too
        self.conn.execute(
            "DELETE FROM metrics WHERE timestamp < ?", (cutoff_str,)
        )
        self.conn.execute("VACUUM")
        self.conn.commit()
        return cur.rowcount

    def close(self):
        self.conn.close()
