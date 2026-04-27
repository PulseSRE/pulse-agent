"""Monitor repository — all monitor-related database operations.

Extracted from ``monitor/actions.py``, ``monitor/cluster_monitor.py``,
``monitor/findings.py``, and ``monitor/fix_planner.py`` to keep domain
logic cohesive.  The original module-level functions in the monitor package
now delegate here for backward compatibility.
"""

from __future__ import annotations

import json
import logging

from .. import db_schema
from .base import BaseRepository

logger = logging.getLogger("pulse_agent.monitor")


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class MonitorRepository(BaseRepository):
    """Database operations for the monitor domain (actions, investigations, scan runs, handoffs)."""

    _tables_ensured = False

    # ── Schema bootstrap ─────────────────────────────────────────────────

    def ensure_tables(self) -> None:
        """Create actions and investigations tables if they don't exist."""
        if MonitorRepository._tables_ensured:
            return
        db = self.db
        db.executescript(db_schema.ACTIONS_SCHEMA)
        db.executescript(db_schema.INVESTIGATIONS_SCHEMA)
        db.executescript(
            "CREATE INDEX IF NOT EXISTS idx_actions_ts ON actions(timestamp DESC);\n"
            "CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status);\n"
            "CREATE INDEX IF NOT EXISTS idx_actions_category ON actions(category);\n"
            "CREATE INDEX IF NOT EXISTS idx_investigations_ts ON investigations(timestamp DESC);\n"
            "CREATE INDEX IF NOT EXISTS idx_investigations_finding ON investigations(finding_id);\n"
        )
        MonitorRepository._tables_ensured = True

    def ensure_scan_runs_table(self) -> None:
        """Create the scan_runs table if it doesn't exist."""
        try:
            db = self.db
            db.executescript(db_schema.SCAN_RUNS_SCHEMA)
            db.commit()
        except Exception as e:
            logger.debug("Failed to initialize scan_runs schema: %s", e, exc_info=True)

    # ── Actions ──────────────────────────────────────────────────────────

    def save_action(
        self,
        action: dict,
        category: str,
        resources_json: str,
        rollback_available: int,
        rollback_action_json: str,
        timestamp: int,
    ) -> None:
        """Persist an action report to the database."""
        self.ensure_tables()
        db = self.db
        db.execute(
            """INSERT INTO actions
               (id, finding_id, timestamp, category, tool, input, status,
                before_state, after_state, error, reasoning, duration_ms,
                rollback_available, rollback_action, resources, verification_status,
                verification_evidence, verification_timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (id) DO UPDATE SET
               status = EXCLUDED.status, after_state = EXCLUDED.after_state,
               error = EXCLUDED.error, duration_ms = EXCLUDED.duration_ms,
               verification_status = EXCLUDED.verification_status,
               verification_evidence = EXCLUDED.verification_evidence,
               verification_timestamp = EXCLUDED.verification_timestamp""",
            (
                action["id"],
                action.get("findingId", ""),
                timestamp,
                category,
                action.get("tool", ""),
                json.dumps(action.get("input", {})),
                action.get("status", ""),
                action.get("beforeState", ""),
                action.get("afterState", ""),
                action.get("error"),
                action.get("reasoning", ""),
                action.get("durationMs", 0),
                rollback_available,
                rollback_action_json,
                resources_json,
                action.get("verificationStatus"),
                action.get("verificationEvidence"),
                action.get("verificationTimestamp"),
            ),
        )
        db.commit()

    def get_action_by_id(self, action_id: str) -> dict | None:
        """Get a single raw action row by ID."""
        self.ensure_tables()
        db = self.db
        return db.fetchone("SELECT * FROM actions WHERE id = ?", (action_id,))

    def list_actions_paginated(self, where: str, params: tuple, page_size: int, offset: int) -> tuple[int, list[dict]]:
        """Return (total_count, rows) for paginated action queries."""
        self.ensure_tables()
        db = self.db
        count_row = db.fetchone(f"SELECT COUNT(*) as cnt FROM actions {where}", params)
        total = count_row["cnt"] if count_row else 0
        rows = db.fetchall(
            f"SELECT * FROM actions {where} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            params + (page_size, offset),
        )
        return total, rows

    def update_action_status(self, action_id: str, status: str, outcome: str) -> None:
        """Update action status and outcome (e.g. for rollback)."""
        db = self.db
        db.execute(
            "UPDATE actions SET status = ?, outcome = ? WHERE id = ?",
            (status, outcome, action_id),
        )
        db.commit()

    def update_action_verification(self, action_id: str, status: str, evidence: str, timestamp: int) -> None:
        """Persist verification result for an action."""
        self.ensure_tables()
        db = self.db
        db.execute(
            """UPDATE actions
               SET verification_status = ?, verification_evidence = ?, verification_timestamp = ?
               WHERE id = ?""",
            (status, evidence, timestamp, action_id),
        )
        db.commit()

    def update_action_outcome(self, action_id: str, outcome: str) -> None:
        """Set the outcome of an action."""
        self.ensure_tables()
        db = self.db
        db.execute("UPDATE actions SET outcome = ? WHERE id = ?", (outcome, action_id))
        db.commit()

    def mark_finding_actions_resolved(self, finding_id: str) -> int:
        """Mark all actions for a finding as resolved. Returns count updated."""
        self.ensure_tables()
        db = self.db
        cursor = db.execute(
            "UPDATE actions SET outcome = 'resolved' WHERE finding_id = ? AND outcome = 'unknown'",
            (finding_id,),
        )
        db.commit()
        return getattr(cursor, "rowcount", 0)

    def get_fix_success_rate_rows(self, days: int) -> list[dict]:
        """Return outcome aggregation rows for a time period."""
        self.ensure_tables()
        db = self.db
        return db.fetchall(
            "SELECT outcome, COUNT(*) AS cnt FROM actions "
            "WHERE timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * ?)::BIGINT * 1000 "
            "AND outcome != 'unknown' "
            "GROUP BY outcome",
            (days,),
        )

    def check_existing_human_review(self, finding_id: str) -> dict | None:
        """Check if a human_review action already exists for a finding."""
        db = self.db
        return db.fetchone(
            "SELECT id FROM actions WHERE finding_id = ? AND tool = ? AND status = ?",
            (finding_id, "require_human_review", "proposed"),
        )

    # ── Briefing queries ─────────────────────────────────────────────────

    def get_actions_since(self, since: int) -> list[dict]:
        """Return actions since a timestamp (for briefing)."""
        self.ensure_tables()
        db = self.db
        return db.fetchall("SELECT status, category, tool FROM actions WHERE timestamp >= ?", (since,))

    def get_investigations_since(self, since: int) -> list[dict]:
        """Return investigations since a timestamp (for briefing)."""
        self.ensure_tables()
        db = self.db
        return db.fetchall("SELECT status FROM investigations WHERE timestamp >= ?", (since,))

    # ── Investigations ───────────────────────────────────────────────────

    def save_investigation(
        self,
        report: dict,
        finding: dict,
        timestamp: int,
    ) -> None:
        """Persist a proactive investigation report."""
        self.ensure_tables()
        db = self.db
        db.execute(
            """INSERT INTO investigations
               (id, finding_id, timestamp, category, severity, status, summary,
                suspected_cause, recommended_fix, confidence, error, resources,
                evidence, alternatives_considered)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (id) DO UPDATE SET
               status = EXCLUDED.status, summary = EXCLUDED.summary,
               suspected_cause = EXCLUDED.suspected_cause,
               recommended_fix = EXCLUDED.recommended_fix,
               confidence = EXCLUDED.confidence, error = EXCLUDED.error""",
            (
                report.get("id"),
                report.get("findingId", ""),
                timestamp,
                finding.get("category", ""),
                finding.get("severity", ""),
                report.get("status", ""),
                report.get("summary", ""),
                report.get("suspectedCause", ""),
                report.get("recommendedFix", ""),
                float(report.get("confidence") or 0.0),
                report.get("error"),
                json.dumps(finding.get("resources", [])),
                json.dumps(report.get("evidence", [])),
                json.dumps(report.get("alternativesConsidered", [])),
            ),
        )
        db.commit()

    def get_investigation_for_finding(self, finding_id: str) -> dict | None:
        """Look up the latest completed investigation for a finding."""
        db = self.db
        return db.fetchone(
            "SELECT suspected_cause, recommended_fix, confidence "
            "FROM investigations "
            "WHERE finding_id = %s AND status = 'completed' "
            "ORDER BY timestamp DESC LIMIT 1",
            (finding_id,),
        )

    def get_investigation_by_finding_id(self, finding_id: str) -> dict | None:
        """Get investigation id and confidence for a finding (for verification)."""
        db = self.db
        return db.fetchone("SELECT id, confidence FROM investigations WHERE finding_id = ?", (finding_id,))

    def update_investigation_confidence(self, investigation_id: str, confidence: float) -> None:
        """Update the confidence score of an investigation."""
        db = self.db
        db.execute("UPDATE investigations SET confidence = ? WHERE id = ?", (confidence, investigation_id))
        db.commit()

    # ── Scan runs ────────────────────────────────────────────────────────

    def save_scan_run(self, duration_ms: int, total_findings: int, scanner_results_json: str, session_id: str) -> None:
        """Persist a scan run record."""
        db = self.db
        db.execute(
            "INSERT INTO scan_runs (duration_ms, total_findings, scanner_results, session_id) VALUES (?, ?, ?, ?)",
            (duration_ms, total_findings, scanner_results_json, session_id),
        )
        db.commit()

    # ── Handoffs ─────────────────────────────────────────────────────────

    def get_pending_handoffs(self, cutoff: int) -> list[dict]:
        """Return pending handoff context_entries since cutoff timestamp."""
        db = self.db
        return db.fetchall(
            "SELECT * FROM context_entries WHERE category = ? AND timestamp > ? ORDER BY timestamp DESC LIMIT 5",
            ("handoff_request", cutoff),
        )

    def delete_processed_handoffs(self, cutoff: int) -> None:
        """Delete processed handoff requests."""
        db = self.db
        db.execute("DELETE FROM context_entries WHERE category = ? AND timestamp > ?", ("handoff_request", cutoff))
        db.commit()


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_monitor_repo: MonitorRepository | None = None


def get_monitor_repo() -> MonitorRepository:
    """Get or create the singleton MonitorRepository instance."""
    global _monitor_repo
    if _monitor_repo is None:
        _monitor_repo = MonitorRepository()
    return _monitor_repo
