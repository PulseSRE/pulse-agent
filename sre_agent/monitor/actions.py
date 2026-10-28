"""Database-backed action persistence: save, query, rollback, briefing."""

from __future__ import annotations

import json
import logging
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
        # Stamp the condition's identity on the row. The action's own id and
        # its finding id are both per-sighting; this is the one thing that
        # says "the same problem, seen again" across scans.
        if finding is not None and not action.get("correlationKey"):
            from ..inbox import _finding_corr_key

            action["correlationKey"] = _finding_corr_key(finding)
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
        # sorted so the briefing reads the same way twice in a row
        categories_fixed = sorted({a["category"] for a in actions if a["status"] == "completed"})

        # Recent investigations
        investigations = repo.get_investigations_since(since)
        total_investigations = len(investigations)

        # What the monitor currently holds, not a fresh run of a few
        # scanners. The briefing re-ran 8 of 32 and swallowed their failures,
        # so it missed most standing problems and could not say when it was
        # blind.
        current_findings, trend_findings, unwatched = _standing_state()

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

        # Build summary sentence. "All quiet" was chosen from actions and
        # investigations alone, so it headed briefings listing critical items.
        critical_now = sum(1 for f in current_findings if f.get("severity") == "critical")
        warning_now = sum(1 for f in current_findings if f.get("severity") == "warning")
        parts = []
        if critical_now:
            parts.append(f"{critical_now} critical issue{'s' if critical_now != 1 else ''} open")
        if warning_now:
            parts.append(f"{warning_now} warning{'s' if warning_now != 1 else ''} open")
        if unwatched:
            parts.append(
                f"{len(unwatched)} scanner{'s' if len(unwatched) != 1 else ''} failing "
                f"({', '.join(sorted(unwatched)[:3])}{'…' if len(unwatched) > 3 else ''})"
            )
        if not parts and total_actions == 0 and total_investigations == 0:
            summary = "All quiet — no issues detected."
        else:
            if completed:
                parts.append(f"{completed} issue{'s' if completed != 1 else ''} auto-fixed")
            if failed:
                parts.append(f"{failed} fix{'es' if failed != 1 else ''} failed")
            if total_investigations:
                parts.append(
                    f"{total_investigations} investigation{'s' if total_investigations != 1 else ''} completed"
                )
            summary = ", ".join(parts) + "." if parts else "No open issues."

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
            "unwatchedScanners": sorted(unwatched),
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
            "unwatchedScanners": [],
        }


# The scanners the briefing runs itself when no monitor has scanned yet:
# (scanner name, module, function).
_FALLBACK_SCANNERS = (
    ("crashloop", "scanners", "scan_crashlooping_pods"),
    ("pending", "scanners", "scan_pending_pods"),
    ("oom", "scanners", "scan_oom_killed_pods"),
    ("alerts", "scanners", "scan_firing_alerts"),
    ("trend_memory", "trend_scanners", "scan_memory_pressure_forecast"),
    ("trend_disk", "trend_scanners", "scan_disk_pressure_forecast"),
    ("trend_hpa", "trend_scanners", "scan_hpa_exhaustion_trend"),
    ("trend_errors", "trend_scanners", "scan_error_rate_acceleration"),
)


def _standing_state() -> tuple[list[dict], list[dict], set[str]]:
    """(current findings, trend findings, failing scanner names).

    From the running monitor when it has completed a scan: everything it holds,
    minus noise-suppressed findings, and the scanners that errored on its last
    scan. Before the first scan, the briefing runs a few fast scanners itself
    and reports any that fail rather than treating them as clean.
    """
    from .cluster_monitor import get_cluster_monitor_sync

    monitor = get_cluster_monitor_sync()
    results = getattr(monitor, "_last_scanner_results", None) if monitor is not None else None
    if monitor is not None and results is not None:
        threshold = getattr(monitor, "_noise_threshold", 1.0)
        standing = [f for f in list(monitor._last_findings.values()) if float(f.get("noiseScore") or 0) < threshold]
        current = [f for f in standing if f.get("findingType", "current") == "current"]
        trend = [f for f in standing if f.get("findingType") == "trend"]
        unwatched = {r["name"] for r in results if r.get("status") == "error"}
        return current, trend, unwatched

    import importlib

    from .scanner_health import _failures_this_cycle, get_failure, scanning

    ran_current: list[dict] = []
    ran_trend: list[dict] = []
    failing: set[str] = set()
    for name, module_name, func_name in _FALLBACK_SCANNERS:
        scan = getattr(importlib.import_module(f"{__package__}.{module_name}"), func_name)
        _failures_this_cycle.pop(name, None)
        try:
            with scanning(name):
                found = scan() or []
                failed = get_failure(name)
        except Exception as e:
            logger.debug("Scanner %s failed in briefing: %s", name, e)
            failing.add(name)
            continue
        if failed:
            failing.add(name)
        for f in found:
            (ran_trend if f.get("findingType") == "trend" else ran_current).append(f)
    return ran_current, ran_trend, failing


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
    if tool == "restore_snapshot":
        # The strongest undo: put the resource back the way the pre-write
        # snapshot found it. This branch was persisted by _make_rollback_info
        # long before anything could execute it — fix history offered a
        # rollback that this function then refused as "not supported".
        from ..snapshot import from_json, restore

        blob = rollback_data.get("input", {}).get("snapshot")
        snap = from_json(blob) if isinstance(blob, str) else (blob if isinstance(blob, dict) else None)
        if not snap:
            return {"error": "Rollback failed: the stored snapshot could not be parsed"}
        try:
            # A person asked for this, possibly long after the fix: never let it
            # overwrite a change made since.
            restored_text = restore(snap, require_unchanged_since_fix=True)
        except Exception as e:
            return {"error": f"Rollback failed: {e}"}
        get_monitor_repo().update_action_status(action_id, "rolled_back", "rolled_back")
        return {"status": "rolled_back", "actionId": action_id, "detail": restored_text}

    if tool == "rollback_deployment":
        inp = rollback_data.get("input", {})
        try:
            from ..k8s_tools import rollback_deployment

            name = inp["name"]
            ns = inp["namespace"]
            revision = int(inp.get("revision", 0)) if inp.get("revision") else 0
            result_text: str = rollback_deployment.func(ns, name, revision)

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

# "recurred": the fix verified on the next scan, then the same condition came
# back inside the recurrence window. It is not "resolved" — counting it as a
# success would teach calibration exactly the wrong lesson.
_VALID_OUTCOMES = frozenset({"resolved", "rolled_back", "escalated", "recurred", "unknown"})


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
            "recurred": totals.get("recurred", 0),
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
