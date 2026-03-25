"""SQLite-backed persistence for the self-improving agent."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

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

    def update_incident_outcome(self, incident_id: int, outcome: str, score: float) -> None:
        self.conn.execute(
            "UPDATE incidents SET outcome = ?, score = ? WHERE id = ?",
            (outcome, score, incident_id)
        )
        self.conn.commit()

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

    def close(self):
        self.conn.close()
