"""Fix history REST endpoints."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, Query

from ..monitor import (
    execute_rollback,
    get_action_detail,
    get_fix_history,
)
from .auth import verify_token

logger = logging.getLogger("pulse_agent.api")

router = APIRouter(tags=["fix-history"])


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
    return await asyncio.to_thread(get_fix_history, page=page, page_size=page_size, filters=filters or None)


@router.get("/fix-history/summary")
async def rest_fix_history_summary(
    days: int = Query(7, ge=1, le=90),
    _auth=Depends(verify_token),
):
    """Aggregated fix history statistics for the last N days. Requires token auth."""
    return await asyncio.to_thread(get_fix_history_summary, days)


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
    result = await asyncio.to_thread(get_action_detail, action_id)
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
