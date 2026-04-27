"""Database-backed action persistence: save, query, rollback, briefing."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from ..repositories.monitor_repo import get_monitor_repo
from .findings import _ts

logger = logging.getLogger("pulse_agent.monitor")


def save_action(
    action: dict, category: str = "", resources: list[dict] | None = None, finding: dict | None = None
) -> None:
    """Persist an action report to the database."""
    from .findings import _make_rollback_info

    try:
        rollback_available, rollback_action_json = _make_rollback_info(action, finding)
        get_monitor_repo().save_action(
            action=action,
            category=category,
            resources_json=json.dumps(resources or []),
            rollback_available=rollback_available,
            rollback_action_json=rollback_action_json,
            timestamp=action.get("timestamp", _ts()),
        )
    except Exception as e:
        logger.error("Failed to save action: %s", e)


def get_fix_history(page: int = 1, page_size: int = 20, filters: dict | None = None) -> dict:
    """Retrieve paginated fix history from the database."""
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    try:
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
        offset = (page - 1) * page_size

        total, rows = get_monitor_repo().list_actions_paginated(where, tuple(params), page_size, offset)

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
        repo = get_monitor_repo()
        since = _ts() - (hours * 3600 * 1000)

        # Recent actions
        actions = repo.get_actions_since(since)
        total_actions = len(actions)
        completed = sum(1 for a in actions if a["status"] == "completed")
        failed = sum(1 for a in actions if a["status"] == "failed")
        categories_fixed = list({a["category"] for a in actions if a["status"] == "completed"})

        # Recent investigations
        investigations = repo.get_investigations_since(since)
        total_investigations = len(investigations)

        # Live scanner runs: fast current state scanners
        current_findings: list[dict] = []
        try:
            from .scanners import (
                scan_crashlooping_pods,
                scan_firing_alerts,
                scan_oom_killed_pods,
                scan_pending_pods,
            )

            scanners: list[Callable[[], list[dict]]] = [
                scan_crashlooping_pods,
                scan_pending_pods,
                scan_oom_killed_pods,
                scan_firing_alerts,
            ]
            for scanner in scanners:
                try:
                    current_findings.extend(scanner())
                except Exception as e:
                    logger.debug("Scanner %s failed in briefing: %s", scanner.__name__, e)
        except ImportError:
            logger.debug("Scanners not available for briefing")

        # Live trend scanner runs: predictive findings
        trend_findings: list[dict] = []
        try:
            from .trend_scanners import (
                scan_disk_pressure_forecast,
                scan_error_rate_acceleration,
                scan_hpa_exhaustion_trend,
                scan_memory_pressure_forecast,
            )

            trend_scanners: list[Callable[[], list[dict]]] = [
                scan_memory_pressure_forecast,
                scan_disk_pressure_forecast,
                scan_hpa_exhaustion_trend,
                scan_error_rate_acceleration,
            ]
            for scanner in trend_scanners:
                try:
                    trend_findings.extend(scanner())
                except Exception as e:
                    logger.debug("Trend scanner %s failed in briefing: %s", scanner.__name__, e)
        except ImportError:
            logger.debug("Trend scanners not available for briefing")

        # Priority ranking: sort by severity weight
        all_findings = current_findings + trend_findings
        severity_weights = {"critical": 4, "warning": 2, "info": 1}
        all_findings.sort(key=lambda f: severity_weights.get(f.get("severity", "info"), 0), reverse=True)
        priority_items = all_findings[:10]

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
            "currentFindings": current_findings,
            "trendFindings": trend_findings,
            "priorityItems": priority_items,
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
            "currentFindings": [],
            "trendFindings": [],
            "priorityItems": [],
        }


def get_action_detail(action_id: str) -> dict | None:
    """Get a single action by ID."""
    try:
        row = get_monitor_repo().get_action_by_id(action_id)
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
        get_monitor_repo().save_investigation(
            report=report,
            finding=finding,
            timestamp=report.get("timestamp", _ts()),
        )
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
            get_monitor_repo().update_action_status(action_id, "rolled_back", "rolled_back")
            return {"status": "rolled_back", "actionId": action_id, "detail": result_text}
        except Exception as e:
            return {"error": f"Rollback failed: {e}"}

    return {"error": f"Rollback not supported for tool '{tool}'"}


def update_action_verification(action_id: str, status: str, evidence: str) -> None:
    """Persist verification result for an action."""
    try:
        get_monitor_repo().update_action_verification(action_id, status, evidence, _ts())
    except Exception as e:
        logger.error("Failed to update action verification: %s", e)


# ── Outcome tracking ─────────────────────────────────────────────────────

_VALID_OUTCOMES = frozenset({"resolved", "rolled_back", "escalated", "unknown"})


def update_action_outcome(action_id: str, outcome: str) -> bool:
    """Set the outcome of an action (resolved, rolled_back, escalated)."""
    if outcome not in _VALID_OUTCOMES:
        return False
    try:
        get_monitor_repo().update_action_outcome(action_id, outcome)
        return True
    except Exception as e:
        logger.error("Failed to update action outcome: %s", e)
        return False


def mark_finding_actions_resolved(finding_id: str) -> int:
    """Mark all actions for a finding as resolved. Returns count updated."""
    try:
        return get_monitor_repo().mark_finding_actions_resolved(finding_id)
    except Exception as e:
        logger.error("Failed to mark finding actions resolved: %s", e)
        return 0


def get_fix_success_rate(days: int = 30) -> dict:
    """Calculate fix success rate over a time period."""
    try:
        rows = get_monitor_repo().get_fix_success_rate_rows(days)
        totals = {r["outcome"]: r["cnt"] for r in rows}
        total = sum(totals.values())
        resolved = totals.get("resolved", 0)
        return {
            "period_days": days,
            "total_with_outcome": total,
            "resolved": resolved,
            "rolled_back": totals.get("rolled_back", 0),
            "escalated": totals.get("escalated", 0),
            "success_rate": round(resolved / total, 4) if total > 0 else None,
        }
    except Exception as e:
        logger.error("Failed to get fix success rate: %s", e)
        return {"period_days": days, "total_with_outcome": 0, "success_rate": None}


# ── Cost / usage tracking ──────────────────────────────────────────────────

_investigation_tokens_used = 0
_investigation_calls = 0


def get_investigation_stats() -> dict:
    """Return investigation usage counters for observability."""
    return {
        "total_calls": _investigation_calls,
        "estimated_tokens": _investigation_tokens_used,
    }
