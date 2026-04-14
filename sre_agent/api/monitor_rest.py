"""Monitor-related REST endpoints."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..config import get_settings
from ..monitor import (
    execute_rollback,
    get_action_detail,
    get_fix_history,
)
from .auth import verify_token

logger = logging.getLogger("pulse_agent.api")

router = APIRouter()


# ── Scanner Categories ─────────────────────────────────────────────────────

_SCANNER_CATEGORIES = {
    "pod_health": ["crashloop", "pending", "oom", "image_pull"],
    "node_pressure": ["nodes"],
    "workload_health": ["workloads", "daemonsets", "hpa"],
    "security_audit": ["audit_rbac", "audit_auth", "audit_config"],
    "certificate_expiry": ["cert_expiry"],
    "alerts": ["alerts"],
    "deployment_audit": ["audit_deployment", "audit_events"],
    "operator_health": ["operators"],
}


def get_scanner_coverage(days: int = 7) -> dict:
    """Get scanner coverage statistics.

    Args:
        days: Number of days to look back for finding stats (1-90).

    Returns:
        Dictionary with coverage metrics:
        - active_scanners: count of enabled scanners
        - total_scanners: total available scanners
        - coverage_pct: percentage of categories covered (0.0-1.0)
        - categories: list of {name, covered, scanners}
        - per_scanner: list of {name, enabled, finding_count, actionable_count, noise_pct}
    """
    from ..monitor import _get_all_scanners

    # Get all scanners
    all_scanners = _get_all_scanners()
    scanner_ids = {scanner_id for scanner_id, _ in all_scanners}
    total_scanners = len(all_scanners)

    # All scanners are currently always enabled (no toggle mechanism yet)
    active_scanners = total_scanners

    # Compute category coverage
    categories = []
    covered_count = 0
    total_categories = len(_SCANNER_CATEGORIES)

    for category_name, scanner_list in _SCANNER_CATEGORIES.items():
        # A category is covered if at least one of its scanners is enabled
        covered = any(s in scanner_ids for s in scanner_list)
        if covered:
            covered_count += 1

        # Get the list of enabled scanners for this category
        enabled_scanners = [s for s in scanner_list if s in scanner_ids]

        categories.append(
            {
                "name": category_name,
                "covered": covered,
                "scanners": enabled_scanners,
            }
        )

    coverage_pct = round(covered_count / total_categories * 100, 1) if total_categories > 0 else 0.0

    # Try to get per-scanner finding stats from the database
    per_scanner = []
    try:
        from .. import db

        database = db.get_database()

        # Single batch query instead of N+1 per-scanner queries
        stats_rows = database.fetchall(
            "SELECT category, "
            "  COUNT(*) AS total_count, "
            "  COUNT(*) FILTER (WHERE severity IN ('critical', 'warning')) AS actionable_count "
            "FROM findings "
            "WHERE timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * ?)::BIGINT * 1000 "
            "GROUP BY category",
            (days,),
        )
        stats_by_cat = {r["category"]: r for r in stats_rows} if stats_rows else {}

        for scanner_id, _ in all_scanners:
            row = stats_by_cat.get(scanner_id, {})
            finding_count = row.get("total_count", 0)
            actionable_count = row.get("actionable_count", 0)
            noise_pct = round((finding_count - actionable_count) / finding_count, 2) if finding_count > 0 else 0.0

            per_scanner.append(
                {
                    "name": scanner_id,
                    "enabled": True,
                    "finding_count": finding_count,
                    "actionable_count": actionable_count,
                    "noise_pct": noise_pct,
                }
            )
    except Exception as e:
        logger.debug("Failed to get per-scanner stats: %s", e)
        for scanner_id, _ in all_scanners:
            per_scanner.append(
                {
                    "name": scanner_id,
                    "enabled": True,
                    "finding_count": 0,
                    "actionable_count": 0,
                    "noise_pct": 0.0,
                }
            )

    return {
        "active_scanners": active_scanners,
        "total_scanners": total_scanners,
        "coverage_pct": coverage_pct,
        "categories": categories,
        "per_scanner": per_scanner,
    }


def get_fix_history_summary(days: int = 7) -> dict:
    """Aggregate fix history statistics for the last N days."""
    from .. import db

    try:
        database = db.get_database()

        # Get all actions within the time window using SQL interval
        actions = database.fetchall(
            "SELECT status, category, duration_ms, verification_status FROM actions "
            "WHERE timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * ?)::BIGINT * 1000",
            (days,),
        )

        # Single-pass aggregation
        total_actions = len(actions)
        completed = 0
        failed = 0
        rolled_back = 0
        durations: list[int] = []
        resolved = 0
        still_failing = 0
        improved = 0
        categories: dict[str, dict[str, Any]] = {}

        for action in actions:
            status = action["status"]
            if status == "completed":
                completed += 1
                if action["duration_ms"]:
                    durations.append(action["duration_ms"])
            elif status == "failed":
                failed += 1
            elif status == "rolled_back":
                rolled_back += 1

            cat = action["category"] or "unknown"
            if cat not in categories:
                categories[cat] = {
                    "category": cat,
                    "count": 0,
                    "success_count": 0,
                    "auto_fixed": 0,
                    "confirmation_required": 0,
                }
            categories[cat]["count"] += 1
            if status == "completed":
                categories[cat]["success_count"] += 1
                categories[cat]["auto_fixed"] += 1

            vs = action.get("verification_status")
            if vs == "verified":
                resolved += 1
            elif vs == "still_failing":
                still_failing += 1
            elif vs == "improved":
                improved += 1

        success_rate = completed / total_actions if total_actions > 0 else 0.0
        rollback_rate = rolled_back / total_actions if total_actions > 0 else 0.0
        avg_resolution_ms = int(sum(durations) / len(durations)) if durations else 0
        by_category = sorted(categories.values(), key=lambda x: x["count"], reverse=True)

        verification = {
            "resolved": resolved,
            "still_failing": still_failing,
            "improved": improved,
            "pending": total_actions - resolved - still_failing - improved,
            "resolution_rate": round(resolved / total_actions, 2) if total_actions > 0 else 0.0,
        }

        # Calculate trend (current week vs previous week) using SQL intervals
        current_week_count_row = database.fetchone(
            "SELECT COUNT(*) as cnt FROM actions "
            "WHERE timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '7 days')::BIGINT * 1000"
        )
        current_week_count = current_week_count_row["cnt"] if current_week_count_row else 0

        previous_week_count_row = database.fetchone(
            "SELECT COUNT(*) as cnt FROM actions "
            "WHERE timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '14 days')::BIGINT * 1000 "
            "  AND timestamp < EXTRACT(EPOCH FROM NOW() - INTERVAL '7 days')::BIGINT * 1000"
        )
        previous_week_count = previous_week_count_row["cnt"] if previous_week_count_row else 0

        trend = {
            "current_week": current_week_count,
            "previous_week": previous_week_count,
            "delta": current_week_count - previous_week_count,
        }

        return {
            "total_actions": total_actions,
            "completed": completed,
            "failed": failed,
            "rolled_back": rolled_back,
            "success_rate": round(success_rate, 2),
            "rollback_rate": round(rollback_rate, 2),
            "avg_resolution_ms": avg_resolution_ms,
            "by_category": by_category,
            "trend": trend,
            "verification": verification,
        }
    except Exception as e:
        logger.debug("Failed to get fix history summary: %s", e)
        return {
            "total_actions": 0,
            "completed": 0,
            "failed": 0,
            "rolled_back": 0,
            "success_rate": 0.0,
            "rollback_rate": 0.0,
            "avg_resolution_ms": 0,
            "by_category": [],
            "trend": {"current_week": 0, "previous_week": 0, "delta": 0},
            "verification": {
                "resolved": 0,
                "still_failing": 0,
                "improved": 0,
                "pending": 0,
                "resolution_rate": 0.0,
            },
        }


@router.get("/fix-history")
async def rest_fix_history(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: str | None = Query(None),
    category: str | None = Query(None),
    since: int | None = Query(None),
    search: str | None = Query(None),
    _auth=Depends(verify_token),
):
    """Paginated fix history (Protocol v2). Requires token auth."""
    filters: dict[str, str | int] = {}
    if status:
        filters["status"] = status
    if category:
        filters["category"] = category
    if since:
        filters["since"] = since
    if search:
        filters["search"] = search
    return get_fix_history(page=page, page_size=page_size, filters=filters or None)


@router.get("/fix-history/summary")
async def rest_fix_history_summary(
    days: int = Query(7, ge=1, le=90),
    _auth=Depends(verify_token),
):
    """Aggregated fix history statistics for the last N days. Requires token auth."""
    return get_fix_history_summary(days)


@router.get("/fix-history/resolutions")
async def rest_fix_history_resolutions(
    days: int = 7,
    limit: int = 50,
    _auth=Depends(verify_token),
):
    """Recent resolution outcomes — what was fixed, how, and whether it worked."""
    try:
        from .. import db

        database = db.get_database()
        rows = database.fetchall(
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

        resolutions = []
        for r in rows:
            time_to_verify_ms = None
            if r.get("verification_timestamp") and r.get("timestamp"):
                time_to_verify_ms = int(r["verification_timestamp"]) - int(r["timestamp"])

            resolutions.append(
                {
                    "id": r["id"],
                    "findingId": r.get("finding_id", ""),
                    "category": r.get("category", ""),
                    "tool": r.get("tool", ""),
                    "status": r.get("status", ""),
                    "reasoning": r.get("reasoning", ""),
                    "outcome": r.get("verification_status", ""),
                    "evidence": r.get("verification_evidence", ""),
                    "timestamp": r.get("timestamp"),
                    "verifiedAt": r.get("verification_timestamp"),
                    "durationMs": r.get("duration_ms"),
                    "timeToVerifyMs": time_to_verify_ms,
                }
            )

        return {"resolutions": resolutions, "total": len(resolutions)}
    except Exception as e:
        logger.debug("Failed to get resolutions: %s", e)
        return {"resolutions": [], "total": 0}


@router.get("/fix-history/{action_id}")
async def rest_action_detail(action_id: str, _auth=Depends(verify_token)):
    """Single action detail with before/after state (Protocol v2). Requires token auth."""
    result = get_action_detail(action_id)
    if result is None:
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=404, content={"error": "Action not found"})
    return result


@router.post("/fix-history/{action_id}/rollback")
async def rollback_action(action_id: str, _auth=Depends(verify_token)):
    """Rollback a completed action (Protocol v2). Requires token auth."""
    result = execute_rollback(action_id)
    if "error" in result:
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=400, content=result)
    return result


@router.get("/briefing")
async def rest_briefing(hours: int = Query(12, ge=1, le=72), _auth=Depends(verify_token)):
    """Cluster activity briefing for the last N hours. Requires token auth."""
    from ..monitor import get_briefing

    return get_briefing(hours)


@router.get("/monitor/scanners")
async def rest_list_scanners(_auth=Depends(verify_token)):
    """List all scanners with metadata and current configuration."""
    from ..monitor import SCANNER_REGISTRY

    return {"scanners": [{"id": k, **v} for k, v in SCANNER_REGISTRY.items()]}


@router.get("/monitor/history")
async def rest_scan_history(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    _auth=Depends(verify_token),
):
    """Get paginated scan run history."""
    from .. import db

    database = db.get_database()
    rows = database.fetchall(
        "SELECT id, timestamp, duration_ms, total_findings, scanner_results "
        "FROM scan_runs ORDER BY timestamp DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    total_row = database.fetchone("SELECT COUNT(*) as cnt FROM scan_runs")
    total = total_row["cnt"] if total_row else 0

    results = []
    for row in rows:
        entry = dict(row)
        if isinstance(entry.get("scanner_results"), str):
            entry["scanner_results"] = json.loads(entry["scanner_results"])
        results.append(entry)

    return {"runs": results, "total": total, "limit": limit, "offset": offset}


@router.get("/predictions")
async def rest_predictions(_auth=Depends(verify_token)):
    """Active predictions -- currently only available via /ws/monitor WebSocket stream."""
    raise HTTPException(status_code=501, detail="Predictions are only available via the /ws/monitor WebSocket stream.")


@router.post("/simulate")
async def rest_simulate(request: Request, _auth=Depends(verify_token)):
    """Predict the impact of a tool action without executing it. Requires token auth."""
    body = await request.json()
    tool = body.get("tool", "")
    inp = body.get("input", {})
    from ..monitor import simulate_action

    result = simulate_action(tool, inp)
    return result


@router.get("/monitor/capabilities")
async def monitor_capabilities(_auth=Depends(verify_token)):
    """Expose monitor trust/capability limits so UI can align controls."""
    from ..monitor import AUTO_FIX_HANDLERS

    max_trust_level = get_settings().max_trust_level
    return {
        "max_trust_level": max(0, min(max_trust_level, 4)),
        "supported_auto_fix_categories": sorted(AUTO_FIX_HANDLERS.keys()),
    }


@router.post("/monitor/pause")
async def pause_autofix(_auth=Depends(verify_token)):
    """Emergency kill switch -- pause all auto-fix actions."""
    from ..monitor import set_autofix_paused

    set_autofix_paused(True)
    logger.warning("Auto-fix PAUSED via /monitor/pause")
    return {"autofix_paused": True}


@router.post("/monitor/resume")
async def resume_autofix(_auth=Depends(verify_token)):
    """Resume auto-fix actions after a pause."""
    from ..monitor import set_autofix_paused

    set_autofix_paused(False)
    logger.info("Auto-fix RESUMED via /monitor/resume")
    return {"autofix_paused": False}


@router.get("/monitor/coverage")
async def scanner_coverage(
    days: int = Query(7, ge=1, le=90),
    _auth=Depends(verify_token),
):
    """Scanner coverage statistics showing which failure modes are monitored. Requires token auth."""
    return get_scanner_coverage(days)
