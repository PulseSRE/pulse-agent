"""Monitor-related REST endpoints."""

from __future__ import annotations

import json
import logging

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
    filters = {}
    if status:
        filters["status"] = status
    if category:
        filters["category"] = category
    if since:
        filters["since"] = since
    if search:
        filters["search"] = search
    return get_fix_history(page=page, page_size=page_size, filters=filters or None)


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
