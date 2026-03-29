"""Database-backed persistence for the self-improving agent."""

from __future__ import annotations

import functools
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from ..db import Database
from .. import db_schema

logger = logging.getLogger("pulse_agent")


def db_safe(default=None):
    """Decorator that catches database errors and returns a default value."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            try:
                return fn(self, *args, **kwargs)
            except Exception as e:
                logger.error("Database error in %s: %s", fn.__name__, type(e).__name__)
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

DEFAULT_DB_PATH = os.environ.get("PULSE_AGENT_DATABASE_URL", "sqlite:///tmp/pulse_agent/pulse.db")

_STORE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_incidents_keywords ON incidents(query_keywords);
CREATE INDEX IF NOT EXISTS idx_incidents_error_type ON incidents(error_type);
CREATE INDEX IF NOT EXISTS idx_runbooks_keywords ON runbooks(trigger_keywords);
CREATE INDEX IF NOT EXISTS idx_patterns_keywords ON patterns(keywords);
"""

SCHEMA = (
    db_schema.INCIDENTS_SCHEMA
    + db_schema.RUNBOOKS_SCHEMA
    + db_schema.PATTERNS_SCHEMA
    + db_schema.METRICS_SCHEMA
    + _STORE_INDEXES
)

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
    """Database-backed persistence for incidents, runbooks, patterns, and metrics."""

    def __init__(self, db_path: str | None = None, db: Database | None = None):
        if db is not None:
            self.db = db
        else:
            url = db_path or DEFAULT_DB_PATH
            if not url.startswith(("sqlite:", "postgres")):
                url = f"sqlite:///{url}"
            self.db = Database(url)
        self.db.executescript(SCHEMA)

    @db_safe(default=-1)
    def record_incident(self, query: str, tool_sequence: list[dict],
                        resolution: str, outcome: str = "unknown",
                        namespace: str = "", resource_type: str = "",
                        error_type: str = "", tool_count: int = 0,
                        rejected_tools: int = 0, duration_seconds: float = 0,
                        score: float = 0) -> int:
        keywords = extract_keywords(query)
        self.db.execute(
            """INSERT INTO incidents (timestamp, query, query_keywords, tool_sequence,
               resolution, outcome, namespace, resource_type, error_type,
               tool_count, rejected_tools, duration_seconds, score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (datetime.now(timezone.utc).isoformat(), query, keywords,
             json.dumps(tool_sequence), resolution[:2000], outcome, namespace,
             resource_type, error_type, tool_count, rejected_tools,
             duration_seconds, score)
        )
        self.db.commit()
        return self.db.lastrowid

    @db_safe(default=None)
    def update_incident_outcome(self, incident_id: int, outcome: str, score: float) -> None:
        self.db.execute(
            "UPDATE incidents SET outcome = ?, score = ? WHERE id = ?",
            (outcome, score, incident_id)
        )
        self.db.commit()

    @db_safe(default=[])
    def search_incidents(self, query: str, limit: int = 5) -> list[dict]:
        from .retrieval import _tfidf_similarity

        incidents = self.db.fetchall(
            "SELECT * FROM incidents ORDER BY timestamp DESC LIMIT 200"
        )
        if not incidents:
            return []
        documents = [f"{inc['query']} {inc['resolution']}" for inc in incidents]
        scores = _tfidf_similarity(query, documents)

        ranked = sorted(
            zip(scores, incidents),
            key=lambda x: (-x[0], -x[1].get("score", 0)),
        )
        return [inc for sim, inc in ranked if sim > 0.1][:limit]

    @db_safe(default=[])
    def search_low_score_incidents(self, query: str, threshold: float = 0.4, limit: int = 2) -> list[dict]:
        """Find similar incidents with low scores to surface anti-patterns."""
        from .retrieval import _tfidf_similarity

        incidents = self.db.fetchall(
            "SELECT * FROM incidents WHERE score < ? AND score > 0 "
            "ORDER BY timestamp DESC LIMIT 200",
            (threshold,),
        )
        if not incidents:
            return []
        documents = [f"{inc['query']} {inc['resolution']}" for inc in incidents]
        scores = _tfidf_similarity(query, documents)

        ranked = sorted(
            zip(scores, incidents),
            key=lambda x: (-x[0],),
        )
        return [inc for sim, inc in ranked if sim > 0.1][:limit]

    def save_runbook(self, name: str, description: str, trigger_keywords: str,
                     tool_sequence: list[dict], source_incident_id: int | None = None) -> int:
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            """INSERT INTO runbooks (name, description, trigger_keywords, tool_sequence,
               created_at, updated_at, source_incident_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (name, description, trigger_keywords, json.dumps(tool_sequence),
             now, now, source_incident_id)
        )
        self.db.commit()
        return self.db.lastrowid

    @db_safe(default=[])
    def find_runbooks(self, query: str, limit: int = 3) -> list[dict]:
        keywords = extract_keywords(query).split()
        if not keywords:
            return []
        conditions = " OR ".join(["trigger_keywords LIKE ?"] * len(keywords))
        params = [f"%{kw}%" for kw in keywords]
        return self.db.fetchall(
            f"""SELECT * FROM runbooks WHERE ({conditions})
                ORDER BY success_count DESC LIMIT ?""",
            tuple(params + [limit])
        )

    def list_runbooks(self, limit: int = 10) -> list[dict]:
        return self.db.fetchall(
            "SELECT * FROM runbooks ORDER BY success_count DESC LIMIT ?", (limit,)
        )

    def record_pattern(self, pattern_type: str, description: str,
                       keywords: str, incident_ids: list[int],
                       metadata: dict | None = None) -> int:
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            """INSERT INTO patterns (pattern_type, description, keywords,
               incident_ids, last_seen, first_seen, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (pattern_type, description, keywords, json.dumps(incident_ids),
             now, now, json.dumps(metadata or {}))
        )
        self.db.commit()
        return self.db.lastrowid

    def list_patterns(self, limit: int = 10) -> list[dict]:
        return self.db.fetchall(
            "SELECT * FROM patterns ORDER BY frequency DESC, last_seen DESC LIMIT ?", (limit,)
        )

    def search_patterns(self, query: str, limit: int = 3) -> list[dict]:
        keywords = extract_keywords(query).split()
        if not keywords:
            return []
        conditions = " OR ".join(["keywords LIKE ?"] * len(keywords))
        params = [f"%{kw}%" for kw in keywords]
        return self.db.fetchall(
            f"SELECT * FROM patterns WHERE ({conditions}) ORDER BY frequency DESC LIMIT ?",
            tuple(params + [limit])
        )

    @db_safe(default=None)
    def record_metric(self, metric_name: str, value: float,
                      window: str = "session") -> None:
        self.db.execute(
            "INSERT INTO metrics (timestamp, metric_name, value, window) VALUES (?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), metric_name, value, window)
        )
        self.db.commit()

    def get_metrics_summary(self) -> dict:
        rows = self.db.fetchall(
            "SELECT metric_name, AVG(value) as avg, COUNT(*) as count FROM metrics GROUP BY metric_name"
        )
        return {r["metric_name"]: {"avg": r["avg"], "count": r["count"]} for r in rows}

    def get_incident_count(self) -> int:
        row = self.db.fetchone("SELECT COUNT(*) as c FROM incidents")
        return row["c"] if row else 0

    @db_safe(default=[])
    def export_runbooks(self) -> list[dict]:
        """Export all learned runbooks as JSON-serialisable dicts."""
        rows = self.db.fetchall(
            "SELECT name, description, trigger_keywords, tool_sequence, "
            "success_count, failure_count, created_at, updated_at FROM runbooks"
        )
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
            for r in self.db.fetchall("SELECT name FROM runbooks")
        }
        imported = 0
        now = datetime.now(timezone.utc).isoformat()
        for rb in runbooks:
            name = rb.get("name", "")
            if not name or name in existing_names:
                continue
            tool_seq = rb.get("tool_sequence", [])
            self.db.execute(
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
        self.db.commit()
        return imported

    @db_safe(default=[])
    def export_patterns(self) -> list[dict]:
        """Export all detected patterns as JSON-serialisable dicts."""
        rows = self.db.fetchall(
            "SELECT pattern_type, description, keywords, incident_ids, "
            "frequency, first_seen, last_seen, metadata FROM patterns"
        )
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
            for r in self.db.fetchall("SELECT description FROM patterns")
        }
        imported = 0
        now = datetime.now(timezone.utc).isoformat()
        for pat in patterns:
            desc = pat.get("description", "")
            if not desc or desc in existing_descs:
                continue
            self.db.execute(
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
        self.db.commit()
        return imported

    def cleanup(self, max_age_days: int = 90) -> int:
        """Delete incidents older than max_age_days. Returns number of deleted rows."""
        cutoff = datetime.now(timezone.utc)
        # SQLite datetime comparison: timestamps stored as ISO strings
        from datetime import timedelta
        cutoff_str = (cutoff - timedelta(days=max_age_days)).isoformat()
        cur = self.db.execute(
            "DELETE FROM incidents WHERE timestamp < ?", (cutoff_str,)
        )
        # Clean up orphaned metrics too
        self.db.execute(
            "DELETE FROM metrics WHERE timestamp < ?", (cutoff_str,)
        )
        self.db.execute("VACUUM")
        self.db.commit()
        return cur.rowcount

    def close(self):
        self.db.close()
