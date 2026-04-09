"""Database-backed action persistence: save, query, rollback, briefing."""

from __future__ import annotations

import json
import logging
from typing import Any

from ..db import get_database
from .findings import _ensure_tables, _ts

logger = logging.getLogger("pulse_agent.monitor")


def save_action(
    action: dict, category: str = "", resources: list[dict] | None = None, finding: dict | None = None
) -> None:
    """Persist an action report to the database."""
    from .findings import _make_rollback_info

    try:
        _ensure_tables()
        db = get_database()

        rollback_available, rollback_action_json = _make_rollback_info(action, finding)

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
                action.get("timestamp", _ts()),
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
                json.dumps(resources or []),
                action.get("verificationStatus"),
                action.get("verificationEvidence"),
                action.get("verificationTimestamp"),
            ),
        )
        db.commit()
    except Exception as e:
        logger.error("Failed to save action: %s", e)


def get_fix_history(page: int = 1, page_size: int = 20, filters: dict | None = None) -> dict:
    """Retrieve paginated fix history from the database."""
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    try:
        _ensure_tables()
        db = get_database()
        where_parts = []
        params: list[Any] = []

        if filters:
            if filters.get("status"):
                where_parts.append("status = ?")
                params.append(filters["status"])
            if filters.get("category"):
                where_parts.append("category = ?")
                params.append(filters["category"])
            if filters.get("since"):
                where_parts.append("timestamp >= ?")
                params.append(filters["since"])
            if filters.get("search"):
                where_parts.append("(tool LIKE ? OR reasoning LIKE ?)")
                params.extend([f"%{filters['search']}%", f"%{filters['search']}%"])

        where = "WHERE " + " AND ".join(where_parts) if where_parts else ""

        count_row = db.fetchone(f"SELECT COUNT(*) as cnt FROM actions {where}", tuple(params))
        total = count_row["cnt"] if count_row else 0
        offset = (page - 1) * page_size
        rows = db.fetchall(
            f"SELECT * FROM actions {where} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            tuple(params + [page_size, offset]),
        )

        actions = []
        for r in rows:
            actions.append(
                {
                    "id": r["id"],
                    "findingId": r["finding_id"],
                    "timestamp": r["timestamp"],
                    "category": r["category"],
                    "tool": r["tool"],
                    "input": json.loads(r["input"]) if r["input"] else {},
                    "status": r["status"],
                    "beforeState": r["before_state"],
                    "afterState": r["after_state"],
                    "error": r["error"],
                    "reasoning": r["reasoning"],
                    "durationMs": r["duration_ms"],
                    "rollbackAvailable": bool(r["rollback_available"]),
                    "rollbackAction": json.loads(r["rollback_action"]) if r["rollback_action"] else None,
                    "resources": json.loads(r["resources"]) if r["resources"] else [],
                    "verificationStatus": r["verification_status"],
                    "verificationEvidence": r["verification_evidence"],
                    "verificationTimestamp": r["verification_timestamp"],
                }
            )

        return {"actions": actions, "total": total, "page": page, "pageSize": page_size}
    except Exception as e:
        logger.error("Failed to get fix history: %s", e)
        return {"actions": [], "total": 0, "page": page, "pageSize": page_size}


def get_briefing(hours: int = 12) -> dict:
    """Build a briefing summary of recent cluster activity."""
    try:
        _ensure_tables()
        db = get_database()
        since = _ts() - (hours * 3600 * 1000)

        # Recent actions
        actions = db.fetchall("SELECT status, category, tool FROM actions WHERE timestamp >= ?", (since,))
        total_actions = len(actions)
        completed = sum(1 for a in actions if a["status"] == "completed")
        failed = sum(1 for a in actions if a["status"] == "failed")
        categories_fixed = list({a["category"] for a in actions if a["status"] == "completed"})

        # Recent investigations
        investigations = db.fetchall("SELECT status FROM investigations WHERE timestamp >= ?", (since,))
        total_investigations = len(investigations)

        # Determine greeting
        import datetime as _dtmod

        hour = _dtmod.datetime.now().hour
        if hour < 12:
            greeting = "Good morning"
        elif hour < 17:
            greeting = "Good afternoon"
        else:
            greeting = "Good evening"

        # Build summary sentence
        if total_actions == 0 and total_investigations == 0:
            summary = "All quiet — no issues detected."
        else:
            parts = []
            if completed:
                parts.append(f"{completed} issue{'s' if completed != 1 else ''} auto-fixed")
            if failed:
                parts.append(f"{failed} fix{'es' if failed != 1 else ''} failed")
            if total_investigations:
                parts.append(
                    f"{total_investigations} investigation{'s' if total_investigations != 1 else ''} completed"
                )
            summary = ", ".join(parts) + "."

        return {
            "greeting": greeting,
            "summary": summary,
            "hours": hours,
            "actions": {"total": total_actions, "completed": completed, "failed": failed},
            "investigations": total_investigations,
            "categoriesFixed": categories_fixed,
        }
    except Exception as e:
        logger.error("Failed to build briefing: %s", e)
        return {
            "greeting": "Hello",
            "summary": "Unable to load briefing.",
            "hours": hours,
            "actions": {"total": 0, "completed": 0, "failed": 0},
            "investigations": 0,
            "categoriesFixed": [],
        }


def get_action_detail(action_id: str) -> dict | None:
    """Get a single action by ID."""
    try:
        _ensure_tables()
        db = get_database()
        row = db.fetchone("SELECT * FROM actions WHERE id = ?", (action_id,))
        if not row:
            return None
        return {
            "id": row["id"],
            "findingId": row["finding_id"],
            "timestamp": row["timestamp"],
            "category": row["category"],
            "tool": row["tool"],
            "input": json.loads(row["input"]) if row["input"] else {},
            "status": row["status"],
            "beforeState": row["before_state"],
            "afterState": row["after_state"],
            "error": row["error"],
            "reasoning": row["reasoning"],
            "durationMs": row["duration_ms"],
            "rollbackAvailable": bool(row["rollback_available"]),
            "rollbackAction": json.loads(row["rollback_action"]) if row["rollback_action"] else None,
            "resources": json.loads(row["resources"]) if row["resources"] else [],
            "verificationStatus": row["verification_status"],
            "verificationEvidence": row["verification_evidence"],
            "verificationTimestamp": row["verification_timestamp"],
        }
    except Exception as e:
        logger.error("Failed to get action detail: %s", e)
        return None


def save_investigation(report: dict, finding: dict) -> None:
    """Persist a proactive investigation report."""
    try:
        _ensure_tables()
        db = get_database()
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
                report.get("timestamp", _ts()),
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
    except Exception as e:
        logger.error("Failed to save investigation: %s", e)


def execute_rollback(action_id: str) -> dict:
    """Rollback a completed action if rollback data is available."""
    detail = get_action_detail(action_id)
    if not detail:
        return {"error": "Action not found"}
    if detail["status"] != "completed":
        return {"error": f"Cannot rollback action with status '{detail['status']}'"}

    rollback_data = detail.get("rollbackAction")
    if not rollback_data:
        return {
            "error": "Rollback not available. This action type (e.g. pod deletion) "
            "cannot be reversed — the controller recreates pods automatically.",
        }

    tool = rollback_data.get("tool")
    if tool == "rollback_deployment":
        inp = rollback_data.get("input", {})
        try:
            from ..k8s_tools import rollback_deployment

            name = inp["name"]
            ns = inp["namespace"]
            revision = int(inp.get("revision", 0)) if inp.get("revision") else 0
            result_text = rollback_deployment(ns, name, revision)

            if "error" in result_text.lower() or "not found" in result_text.lower():
                return {"error": f"Rollback failed: {result_text}"}

            # Update status in database
            db = get_database()
            db.execute("UPDATE actions SET status = 'rolled_back' WHERE id = ?", (action_id,))
            db.commit()
            return {"status": "rolled_back", "actionId": action_id, "detail": result_text}
        except Exception as e:
            return {"error": f"Rollback failed: {e}"}

    return {"error": f"Rollback not supported for tool '{tool}'"}


def update_action_verification(action_id: str, status: str, evidence: str) -> None:
    """Persist verification result for an action."""
    try:
        _ensure_tables()
        db = get_database()
        db.execute(
            """UPDATE actions
               SET verification_status = ?, verification_evidence = ?, verification_timestamp = ?
               WHERE id = ?""",
            (status, evidence, _ts(), action_id),
        )
        db.commit()
    except Exception as e:
        logger.error("Failed to update action verification: %s", e)


# ── Cost / usage tracking ──────────────────────────────────────────────────

_investigation_tokens_used = 0
_investigation_calls = 0


def get_investigation_stats() -> dict:
    """Return investigation usage counters for observability."""
    return {
        "total_calls": _investigation_calls,
        "estimated_tokens": _investigation_tokens_used,
    }
