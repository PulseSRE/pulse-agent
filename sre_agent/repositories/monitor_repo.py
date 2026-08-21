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
               -- A proposal approved later transitions in place. Without these
               -- three the row keeps the empty tool and the proposal's own
               -- reasoning, so fix history shows a completed action that never
               -- says what it actually did.
               tool = EXCLUDED.tool, before_state = EXCLUDED.before_state,
               reasoning = EXCLUDED.reasoning,
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

    def claim_proposed_action(self, action_id: str, approver: str, timestamp: int) -> bool:
        """Take ownership of a pending proposal. False if it was not still pending.

        The status check lives in the WHERE clause rather than in a read-then-write
        so that two operators approving at the same instant produce one fix and
        one conflict, not two fixes against the same cluster.
        """
        self.ensure_tables()
        cur = self.db.execute(
            "UPDATE actions SET status = 'approved', approved_by = ?, approved_at = ? "
            "WHERE id = ? AND status = 'proposed'",
            (approver, timestamp, action_id),
        )
        claimed = cur.rowcount == 1
        self.db.commit()
        return claimed

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
                report.get("suspected_cause", "") or report.get("suspectedCause", ""),
                report.get("recommended_fix", "") or report.get("recommendedFix", ""),
                float(report.get("confidence") or 0.0),
                report.get("error"),
                json.dumps(finding.get("resources", [])),
                json.dumps(report.get("evidence", [])),
                json.dumps(report.get("alternatives_considered", []) or report.get("alternativesConsidered", [])),
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
        """Persist a scan run record (sync)."""
        db = self.db
        db.execute(
            "INSERT INTO scan_runs (duration_ms, total_findings, scanner_results, session_id) VALUES (?, ?, ?, ?)",
            (duration_ms, total_findings, scanner_results_json, session_id),
        )
        db.commit()

    async def async_save_scan_run(
        self, duration_ms: int, total_findings: int, scanner_results_json: str, session_id: str
    ) -> None:
        """Persist a scan run record (async — uses asyncpg)."""
        adb = await self.get_async_db()
        await adb.execute(
            "INSERT INTO scan_runs (duration_ms, total_findings, scanner_results, session_id) VALUES (?, ?, ?, ?)",
            duration_ms,
            total_findings,
            scanner_results_json,
            session_id,
        )

    # ── Async: Actions ──────────────────────────────────────────────────

    async def async_save_action(
        self,
        action: dict,
        category: str,
        resources_json: str,
        rollback_available: int,
        rollback_action_json: str,
        timestamp: int,
    ) -> None:
        """Persist an action report (async — uses asyncpg)."""
        adb = await self.get_async_db()
        await adb.execute(
            """INSERT INTO actions
               (id, finding_id, timestamp, category, tool, input, status,
                before_state, after_state, error, reasoning, duration_ms,
                rollback_available, rollback_action, resources, verification_status,
                verification_evidence, verification_timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (id) DO UPDATE SET
               status = EXCLUDED.status, after_state = EXCLUDED.after_state,
               error = EXCLUDED.error, duration_ms = EXCLUDED.duration_ms,
               -- A proposal approved later transitions in place. Without these
               -- three the row keeps the empty tool and the proposal's own
               -- reasoning, so fix history shows a completed action that never
               -- says what it actually did.
               tool = EXCLUDED.tool, before_state = EXCLUDED.before_state,
               reasoning = EXCLUDED.reasoning,
               verification_status = EXCLUDED.verification_status,
               verification_evidence = EXCLUDED.verification_evidence,
               verification_timestamp = EXCLUDED.verification_timestamp""",
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
        )

    async def async_update_action_verification(
        self, action_id: str, status: str, evidence: str, timestamp: int
    ) -> None:
        """Persist verification result for an action (async — uses asyncpg)."""
        adb = await self.get_async_db()
        await adb.execute(
            """UPDATE actions
               SET verification_status = ?, verification_evidence = ?, verification_timestamp = ?
               WHERE id = ?""",
            status,
            evidence,
            timestamp,
            action_id,
        )

    async def async_mark_finding_actions_resolved(self, finding_id: str) -> int:
        """Mark all actions for a finding as resolved (async — uses asyncpg). Returns count updated."""
        adb = await self.get_async_db()
        result = await adb.execute(
            "UPDATE actions SET outcome = 'resolved' WHERE finding_id = ? AND outcome = 'unknown'",
            finding_id,
        )
        # asyncpg execute returns status string like 'UPDATE N'
        try:
            return int(result.split()[-1]) if result else 0
        except (ValueError, IndexError, AttributeError):
            return 0

    # ── Async: Investigations ───────────────────────────────────────────

    async def async_save_investigation(
        self,
        report: dict,
        finding: dict,
        timestamp: int,
    ) -> None:
        """Persist a proactive investigation report (async — uses asyncpg)."""
        adb = await self.get_async_db()
        await adb.execute(
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
            report.get("id"),
            report.get("findingId", ""),
            timestamp,
            finding.get("category", ""),
            finding.get("severity", ""),
            report.get("status", ""),
            report.get("summary", ""),
            report.get("suspected_cause", "") or report.get("suspectedCause", ""),
            report.get("recommended_fix", "") or report.get("recommendedFix", ""),
            float(report.get("confidence") or 0.0),
            report.get("error"),
            json.dumps(finding.get("resources", [])),
            json.dumps(report.get("evidence", [])),
            json.dumps(report.get("alternatives_considered", []) or report.get("alternativesConsidered", [])),
        )

    async def async_get_investigation_by_finding_id(self, finding_id: str) -> dict | None:
        """Get investigation id and confidence for a finding (async — uses asyncpg)."""
        adb = await self.get_async_db()
        return await adb.fetchone("SELECT id, confidence FROM investigations WHERE finding_id = ?", finding_id)

    async def async_update_investigation_confidence(self, investigation_id: str, confidence: float) -> None:
        """Update the confidence score of an investigation (async — uses asyncpg)."""
        adb = await self.get_async_db()
        await adb.execute("UPDATE investigations SET confidence = ? WHERE id = ?", confidence, investigation_id)

    # ── Async: Handoffs ─────────────────────────────────────────────────

    async def async_get_pending_handoffs(self, cutoff: int) -> list[dict]:
        """Return pending handoff context_entries since cutoff timestamp (async — uses asyncpg)."""
        adb = await self.get_async_db()
        return await adb.fetchall(
            "SELECT * FROM context_entries WHERE category = ? AND timestamp > ? ORDER BY timestamp DESC LIMIT 5",
            "handoff_request",
            cutoff,
        )

    async def async_delete_processed_handoffs(self, cutoff: int) -> None:
        """Delete processed handoff requests (async — uses asyncpg)."""
        adb = await self.get_async_db()
        await adb.execute(
            "DELETE FROM context_entries WHERE category = ? AND timestamp > ?",
            "handoff_request",
            cutoff,
        )

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

    # ── KPI queries (monitor_rest.py) ────────────────────────────────────────

    def fetch_mttr(self, days: int) -> dict | None:
        """Mean time to remediate from completed actions."""
        return self.db.fetchone(
            "SELECT AVG(duration_ms) as avg_ms FROM actions "
            "WHERE status = 'completed' "
            "AND timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * ?)::BIGINT * 1000",
            (days,),
        )

    def fetch_fix_rate(self, days: int) -> dict | None:
        """Auto-remediation success rate from actions."""
        return self.db.fetchone(
            "SELECT COUNT(*) FILTER (WHERE status = 'completed') AS good, "
            "COUNT(*) AS total FROM actions "
            "WHERE timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * ?)::BIGINT * 1000",
            (days,),
        )

    def fetch_false_positive_rate(self, days: int) -> dict | None:
        """False positive rate from findings noise_score."""
        return self.db.fetchone(
            "SELECT COUNT(*) FILTER (WHERE noise_score > 0.7) AS noise, "
            "COUNT(*) AS total FROM findings "
            "WHERE timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * ?)::BIGINT * 1000",
            (days,),
        )

    def fetch_selector_recall(self, days: int) -> dict | None:
        """Selector recall from skill_selection_log."""
        return self.db.fetchone(
            "SELECT COUNT(*) FILTER (WHERE skill_overridden IS NULL) AS correct, "
            "COUNT(*) AS total FROM skill_selection_log "
            "WHERE session_id != '__weight_snapshot__' "
            "AND timestamp > NOW() - INTERVAL '%s days'",
            (days,),
        )

    def fetch_selector_latency_p99(self, days: int) -> dict | None:
        """Selector latency p99 from skill_selection_log."""
        return self.db.fetchone(
            "SELECT PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY selection_ms) AS p99 "
            "FROM skill_selection_log "
            "WHERE session_id != '__weight_snapshot__' "
            "AND timestamp > NOW() - INTERVAL '%s days'",
            (days,),
        )

    def fetch_rollback_count(self, days: int) -> dict | None:
        """Count of rolled-back actions."""
        return self.db.fetchone(
            "SELECT COUNT(*) as cnt FROM actions WHERE status = 'rolled_back' "
            "AND timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * ?)::BIGINT * 1000",
            (days,),
        )

    def fetch_time_to_resolution(self, days: int) -> dict | None:
        """Average time from finding detection to verified fix."""
        return self.db.fetchone(
            "SELECT AVG(a.timestamp - f.timestamp) / 1000 as avg_seconds "
            "FROM actions a JOIN findings f ON a.finding_id = f.id "
            "WHERE a.status = 'completed' "
            "AND a.verification_status = 'verified' "
            "AND a.timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * %s)::BIGINT * 1000",
            (days,),
        )

    def fetch_self_heal_rate(self, days: int) -> dict | None:
        """Self-heal rate from findings vs actions."""
        return self.db.fetchone(
            "SELECT "
            "COUNT(*) FILTER (WHERE resolved = 1 AND id NOT IN (SELECT finding_id FROM actions WHERE finding_id IS NOT NULL)) AS self_healed, "
            "COUNT(*) FILTER (WHERE resolved = 1) AS total_resolved "
            "FROM findings "
            "WHERE timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * %s)::BIGINT * 1000",
            (days,),
        )

    def fetch_token_cost_per_resolution(self, days: int) -> dict | None:
        """Average evidence gathered per incident resolution."""
        return self.db.fetchone(
            "SELECT AVG(total_tokens) as avg_tokens FROM ("
            "  SELECT pe.finding_id, "
            "  SUM(COALESCE((phase->>'evidence_length')::int, 0)) as total_tokens "
            "  FROM plan_executions pe, jsonb_array_elements(pe.phase_details) as phase "
            "  WHERE pe.status IN ('complete', 'partial') "
            "  AND pe.timestamp > NOW() - INTERVAL '%s days' "
            "  AND pe.finding_id IS NOT NULL AND pe.finding_id != '' "
            "  GROUP BY pe.finding_id"
            ") sub",
            (days,),
        )

    def fetch_routing_accuracy(self, days: int) -> dict | None:
        """Routing accuracy from tool_turns feedback."""
        return self.db.fetchone(
            "SELECT "
            "COUNT(*) FILTER (WHERE routing_skill IS NOT NULL AND feedback = 'positive') AS confirmed, "
            "COUNT(*) FILTER (WHERE routing_skill IS NOT NULL AND feedback = 'negative') AS rejected, "
            "COUNT(*) FILTER (WHERE routing_skill IS NOT NULL) AS total "
            "FROM tool_turns "
            "WHERE timestamp > NOW() - INTERVAL '%s days'",
            (days,),
        )

    # ── Postmortem listing (monitor_rest.py) ──────────────────────────────────

    def fetch_postmortems(self, limit: int) -> list[dict]:
        """List postmortems newest first."""
        return self.db.fetchall(
            "SELECT id, incident_type, plan_id, timeline, root_cause, "
            "contributing_factors, blast_radius, actions_taken, prevention, "
            "metrics_impact, confidence, generated_at "
            "FROM postmortems ORDER BY generated_at DESC LIMIT ?",
            (limit,),
        )

    async def async_fetch_postmortems(self, limit: int) -> list[dict]:
        """Async version: list postmortems newest first."""
        db = await self.get_async_db()
        return await db.fetchall(
            "SELECT id, incident_type, plan_id, timeline, root_cause, "
            "contributing_factors, blast_radius, actions_taken, prevention, "
            "metrics_impact, confidence, generated_at "
            "FROM postmortems ORDER BY generated_at DESC LIMIT ?",
            limit,
        )

    # ── Fix strategy analytics (monitor_rest.py) ──────────────────────────────

    def fetch_fix_strategies(self, days: int) -> list[dict]:
        """Fetch action category/tool/status rows for strategy analysis."""
        return self.db.fetchall(
            "SELECT category, tool, status, verification_status "
            "FROM actions "
            "WHERE timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * ?)::BIGINT * 1000",
            (days,),
        )

    # ── Learning feed (monitor_rest.py) ───────────────────────────────────────

    def fetch_weight_snapshots(self, limit: int = 5) -> list[dict]:
        """Recent channel weight snapshots."""
        return self.db.fetchall(
            "SELECT channel_weights, timestamp FROM skill_selection_log "
            "WHERE session_id = '__weight_snapshot__' "
            "ORDER BY timestamp DESC LIMIT %s",
            (limit,),
        )

    def fetch_recent_routing_decisions(self, days: int, limit: int = 5) -> list[dict]:
        """Recent routing decisions with channel scores."""
        return self.db.fetchall(
            "SELECT query_summary, selected_skill, channel_scores, fused_scores "
            "FROM skill_selection_log "
            "WHERE session_id != '__weight_snapshot__' "
            "AND timestamp > NOW() - INTERVAL '%s days' "
            "ORDER BY timestamp DESC LIMIT %s",
            (days, limit),
        )

    def fetch_selection_summary(self, days: int) -> dict | None:
        """Selection log summary for learning feed."""
        return self.db.fetchone(
            "SELECT COUNT(*) as total, "
            "COUNT(DISTINCT selected_skill) as skills_used, "
            "SUM(CASE WHEN skill_overridden IS NOT NULL THEN 1 ELSE 0 END) as overrides "
            "FROM skill_selection_log "
            "WHERE timestamp > NOW() - INTERVAL '%s days'",
            (days,),
        )

    def fetch_postmortem_count(self, days: int) -> dict | None:
        """Count recent postmortems."""
        return self.db.fetchone(
            "SELECT COUNT(*) as cnt FROM postmortems "
            "WHERE generated_at > EXTRACT(EPOCH FROM NOW() - INTERVAL '%s days')::BIGINT * 1000",
            (days,),
        )

    # ── Plan analytics (monitor_rest.py) ──────────────────────────────────────

    def fetch_plan_stats(self, days: int) -> list[dict]:
        """Plan execution stats by template."""
        return self.db.fetchall(
            "SELECT template_name, incident_type, status, "
            "COUNT(*) as count, "
            "AVG(total_duration_ms) as avg_duration_ms, "
            "AVG(phases_completed::float / NULLIF(phases_total, 0)) as avg_completion_rate, "
            "AVG(confidence) as avg_confidence "
            "FROM plan_executions "
            "WHERE timestamp > NOW() - INTERVAL '%s days' "
            "GROUP BY template_name, incident_type, status "
            "ORDER BY count DESC",
            (days,),
        )

    def fetch_plan_phase_stats(self, days: int) -> list[dict]:
        """Phase-level breakdown for plan analytics."""
        return self.db.fetchall(
            "SELECT "
            "template_name, "
            "phase->>'phase_id' as phase_id, "
            "phase->>'status' as phase_status, "
            "COUNT(*) as count, "
            "AVG((phase->>'confidence')::float) as avg_confidence "
            "FROM plan_executions, jsonb_array_elements(phase_details) as phase "
            "WHERE timestamp > NOW() - INTERVAL '%s days' "
            "GROUP BY template_name, phase->>'phase_id', phase->>'status' "
            "ORDER BY template_name, phase_id",
            (days,),
        )

    # ── Activity events (monitor_rest.py) ─────────────────────────────────────

    def fetch_activity_actions(self, days: int) -> list[dict]:
        """Aggregated actions by category/namespace/status."""
        return self.db.fetchall(
            "SELECT "
            "COALESCE(category, 'unknown') as category, "
            "COALESCE(namespace, '') as namespace, "
            "COUNT(*) as cnt, "
            "status "
            "FROM actions "
            "WHERE timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * %s)::BIGINT * 1000 "
            "AND status IN ('completed', 'failed', 'rolled_back') "
            "GROUP BY category, namespace, status "
            "ORDER BY cnt DESC",
            (days,),
        )

    def fetch_self_healed_count(self, days: int) -> list[dict]:
        """Count of self-healed findings."""
        return self.db.fetchall(
            "SELECT COUNT(*) as cnt FROM findings "
            "WHERE resolved = 1 "
            "AND id NOT IN (SELECT finding_id FROM actions WHERE finding_id IS NOT NULL) "
            "AND timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * %s)::BIGINT * 1000",
            (days,),
        )

    def fetch_postmortem_activity(self, days: int) -> list[dict]:
        """Recent postmortem count and latest summary."""
        return self.db.fetchall(
            "SELECT COUNT(*) as cnt, "
            "MAX(summary) as latest_summary "
            "FROM postmortems "
            "WHERE created_at >= NOW() - INTERVAL '%s days'",
            (days,),
        )

    def fetch_investigated_findings(self, days: int, limit: int = 5) -> list[dict]:
        """Recently investigated findings by type/target."""
        return self.db.fetchall(
            "SELECT finding_type, target, COUNT(*) as cnt "
            "FROM findings "
            "WHERE investigated = 1 "
            "AND timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * %s)::BIGINT * 1000 "
            "GROUP BY finding_type, target "
            "ORDER BY cnt DESC "
            "LIMIT %s",
            (days, limit),
        )

    # ── Fix history (fix_rest.py) ─────────────────────────────────────────────

    def fetch_actions_for_summary(self, days: int) -> list[dict]:
        """Fetch action rows for fix history summary aggregation."""
        return self.db.fetchall(
            "SELECT status, category, duration_ms, verification_status FROM actions "
            "WHERE timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * ?)::BIGINT * 1000",
            (days,),
        )

    def fetch_current_week_action_count(self) -> dict | None:
        """Count actions in the current week."""
        return self.db.fetchone(
            "SELECT COUNT(*) as cnt FROM actions "
            "WHERE timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '7 days')::BIGINT * 1000"
        )

    def fetch_previous_week_action_count(self) -> dict | None:
        """Count actions in the previous week."""
        return self.db.fetchone(
            "SELECT COUNT(*) as cnt FROM actions "
            "WHERE timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '14 days')::BIGINT * 1000 "
            "  AND timestamp < EXTRACT(EPOCH FROM NOW() - INTERVAL '7 days')::BIGINT * 1000"
        )

    def fetch_resolutions(self, days: int, limit: int) -> list[dict]:
        """Fetch recent resolution outcomes."""
        return self.db.fetchall(
            "SELECT id, finding_id, category, tool, status, reasoning, "
            "verification_status, verification_evidence, verification_timestamp, "
            "timestamp, duration_ms "
            "FROM actions "
            "WHERE timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * ?)::BIGINT * 1000 "
            "AND verification_status IS NOT NULL "
            "ORDER BY timestamp DESC "
            "LIMIT ?",
            (days, limit),
        )

    # ── Scanner stats (scanner_rest.py) ───────────────────────────────────────

    def fetch_scanner_finding_stats(self, days: int) -> list[dict]:
        """Per-category finding stats for scanner status."""
        return self.db.fetchall(
            "SELECT category, "
            "  COUNT(*) AS total_count, "
            "  COUNT(*) FILTER (WHERE severity IN ('critical', 'warning')) AS actionable_count "
            "FROM findings "
            "WHERE timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * ?)::BIGINT * 1000 "
            "GROUP BY category",
            (days,),
        )

    # ── Topology (topology_rest.py) ───────────────────────────────────────────

    def fetch_active_findings(self) -> list[dict]:
        """Fetch active (unresolved) findings for topology overlay."""
        return self.db.fetchall("SELECT category, severity, resources FROM findings WHERE resolved = 0")

    async def async_fetch_active_findings(self) -> list[dict]:
        """Async version: fetch active (unresolved) findings for topology overlay."""
        db = await self.get_async_db()
        return await db.fetchall("SELECT category, severity, resources FROM findings WHERE resolved = 0")

    def fetch_recent_deployments(self, minutes: int = 15) -> list[dict]:
        """Fetch recent deployment audit findings."""
        return self.db.fetchall(
            "SELECT resources FROM findings "
            "WHERE category = 'audit_deployment' "
            "AND timestamp > EXTRACT(EPOCH FROM NOW() - MAKE_INTERVAL(mins => ?))::BIGINT * 1000",
            (minutes,),
        )

    def fetch_deployment_risk_findings(self) -> list[dict]:
        """Fetch deployment risk findings for topology risk scoring."""
        return self.db.fetchall(
            "SELECT resources, category FROM findings "
            "WHERE category = 'audit_deployment' AND resolved = 0 "
            "AND timestamp > EXTRACT(EPOCH FROM NOW() - INTERVAL '2 hours')::BIGINT * 1000"
        )

    def fetch_finding_by_id(self, finding_id: str) -> dict | None:
        """Look up a finding by ID."""
        return self.db.fetchone("SELECT * FROM findings WHERE id = ?", (finding_id,))

    # ── Topology learning (topology_rest.py) ──────────────────────────────────

    def fetch_investigation_confidence(self, finding_id: str) -> dict | None:
        """Fetch investigation confidence for a finding."""
        return self.db.fetchone(
            "SELECT confidence FROM investigations WHERE finding_id = ? ORDER BY timestamp DESC LIMIT 1",
            (finding_id,),
        )

    def fetch_verification_status(self, finding_id: str) -> dict | None:
        """Fetch latest verification status for a finding."""
        return self.db.fetchone(
            "SELECT verification_status FROM actions WHERE finding_id = ? AND verification_status IS NOT NULL "
            "ORDER BY timestamp DESC LIMIT 1",
            (finding_id,),
        )

    def fetch_latest_weight_snapshot(self) -> dict | None:
        """Fetch the most recent channel weight snapshot."""
        return self.db.fetchone(
            "SELECT channel_weights FROM skill_selection_log "
            "WHERE session_id = '__weight_snapshot__' "
            "ORDER BY timestamp DESC LIMIT 1"
        )

    # ── Postmortem saving (postmortem.py) ─────────────────────────────────────

    def save_postmortem(
        self,
        postmortem_id: str,
        incident_type: str,
        plan_id: str,
        timeline: str,
        root_cause: str,
        contributing_factors_json: str,
        blast_radius_json: str,
        actions_taken_json: str,
        prevention_json: str,
        metrics_impact: str,
        confidence: float,
        generated_at: int,
    ) -> None:
        """Save a postmortem to the database."""
        self.db.execute(
            "INSERT INTO postmortems (id, incident_type, plan_id, timeline, root_cause, "
            "contributing_factors, blast_radius, actions_taken, prevention, metrics_impact, "
            "confidence, generated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET "
            "timeline = EXCLUDED.timeline, root_cause = EXCLUDED.root_cause, "
            "confidence = EXCLUDED.confidence",
            (
                postmortem_id,
                incident_type,
                plan_id,
                timeline,
                root_cause,
                contributing_factors_json,
                blast_radius_json,
                actions_taken_json,
                prevention_json,
                metrics_impact,
                confidence,
                generated_at,
            ),
        )
        self.db.commit()

    # ── Change risk (change_risk.py) ──────────────────────────────────────────

    def fetch_deployment_failure_rate(self, deployment_name: str) -> dict | None:
        """Historical failure rate for a deployment."""
        return self.db.fetchone(
            "SELECT COUNT(*) as total, "
            "SUM(CASE WHEN verification_status = 'still_failing' THEN 1 ELSE 0 END) as failures "
            "FROM actions WHERE category = 'workloads' "
            "AND reasoning LIKE %s "
            "AND timestamp > EXTRACT(EPOCH FROM NOW() - INTERVAL '30 days')::BIGINT * 1000",
            (f"%{deployment_name}%",),
        )

    # ── Plan execution recording (plan_runtime.py) ────────────────────────────

    def record_plan_execution(
        self,
        execution_id: str,
        template_id: str,
        template_name: str,
        incident_type: str,
        finding_id: str,
        status: str,
        phases_total: int,
        phases_completed: int,
        total_duration_ms: int,
        phase_details_json: str,
        confidence: float,
    ) -> None:
        """Persist a plan execution record."""
        self.db.execute(
            "INSERT INTO plan_executions "
            "(id, template_id, template_name, incident_type, finding_id, status, "
            "phases_total, phases_completed, total_duration_ms, phase_details, confidence) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            (
                execution_id,
                template_id,
                template_name,
                incident_type,
                finding_id,
                status,
                phases_total,
                phases_completed,
                total_duration_ms,
                phase_details_json,
                confidence,
            ),
        )
        self.db.commit()

    def record_phase_trace(
        self,
        session_id: str,
        query_summary: str,
        selected_skill: str,
        channel_weights_json: str,
    ) -> None:
        """Store phase reasoning trace to skill_selection_log for audit."""
        self.db.execute(
            "INSERT INTO skill_selection_log "
            "(session_id, query_summary, selected_skill, threshold_used, "
            "selection_ms, channel_weights) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (session_id, query_summary, selected_skill, 0.0, 0, channel_weights_json),
        )
        self.db.commit()

    # ── Harness (harness.py) ──────────────────────────────────────────────────

    def fetch_view_count(self) -> dict | None:
        """Count of saved views."""
        return self.db.fetchone("SELECT COUNT(*) as cnt FROM views")

    # ── Finding status for view_tools.py ──────────────────────────────────────

    def fetch_unresolved_finding_status(self) -> list[dict]:
        """Fetch severity and resources for unresolved findings."""
        return self.db.fetchall("SELECT severity, resources FROM findings WHERE resolved = 0")

    # ── Inbox generators (inbox_generators.py) ────────────────────────────────

    def fetch_stale_inbox_items(self, cutoff: int) -> list[dict]:
        """Fetch stale inbox items for the stale findings generator.

        Excludes generator-produced items. _make_assessment sets
        item_type='task', which this query selects, so the
        "N findings open >72h without action" digest became stale itself after
        72h and then counted — and listed — itself on every later cycle. The
        correlation key keeps it to a single row rather than growing without
        bound, but the count was inflated by one and the summary read
        absurdly. Generator items carry metadata.generator; nothing else does.
        """
        return self.db.fetchall(
            """SELECT id, title, created_at FROM inbox_items
            WHERE item_type = 'task' AND status IN ('new', 'triaged')
            AND created_at < ?
            AND (metadata IS NULL OR metadata NOT LIKE '%"generator"%')""",
            (cutoff,),
        )


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
