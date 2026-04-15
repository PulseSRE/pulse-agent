"""Memory system REST endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from .auth import verify_token

logger = logging.getLogger("pulse_agent.api")

router = APIRouter()


@router.get("/memory/export")
async def export_memory(_auth=Depends(verify_token)):
    """Export learned runbooks and patterns for cross-pod sharing."""
    from ..memory import get_manager

    manager = get_manager()
    if not manager:
        return {"runbooks": [], "patterns": []}
    return {
        "runbooks": manager.store.export_runbooks(),
        "patterns": manager.store.export_patterns(),
    }


@router.post("/memory/import")
async def import_memory(body: dict, _auth=Depends(verify_token)):
    """Import runbooks and patterns from another pod's export."""
    from ..memory import get_manager

    manager = get_manager()
    if not manager:
        return {"imported_runbooks": 0, "imported_patterns": 0, "error": "Memory system not enabled"}
    runbooks = body.get("runbooks", [])
    patterns = body.get("patterns", [])
    imported_rb = manager.store.import_runbooks(runbooks) if runbooks else 0
    imported_pat = manager.store.import_patterns(patterns) if patterns else 0
    return {"imported_runbooks": imported_rb, "imported_patterns": imported_pat}


@router.get("/memory/stats")
async def memory_stats(_auth=Depends(verify_token)):
    """Memory system stats: incident count, runbook count, pattern count, top metrics."""
    from ..memory import get_manager

    manager = get_manager()
    if not manager:
        return {"enabled": False, "incidents": 0, "runbooks": 0, "patterns": 0, "metrics": {}}
    return {
        "enabled": True,
        "incidents": manager.store.get_incident_count(),
        "runbooks": len(manager.store.list_runbooks()),
        "patterns": len(manager.store.list_patterns()),
        "metrics": manager.store.get_metrics_summary(),
    }


@router.get("/memory/runbooks")
async def memory_runbooks(limit: int = Query(20, ge=1, le=100), _auth=Depends(verify_token)):
    """List learned runbooks sorted by success rate."""
    from ..memory import get_manager

    manager = get_manager()
    if not manager:
        return {"runbooks": []}
    runbooks = manager.store.list_runbooks()[:limit]
    # Normalize trigger_keywords from space-separated string to array
    for rb in runbooks:
        kw = rb.get("trigger_keywords", "")
        if isinstance(kw, str):
            rb["trigger_keywords"] = kw.split() if kw else []
    return {"runbooks": runbooks}


@router.get("/memory/incidents")
async def memory_incidents(
    search: str = Query("", max_length=200),
    limit: int = Query(10, ge=1, le=50),
    _auth=Depends(verify_token),
):
    """Search past incidents by query similarity."""
    from ..memory import get_manager

    manager = get_manager()
    if not manager:
        return {"incidents": []}
    if search:
        incidents = manager.store.search_incidents(search, limit=limit)
    else:
        # No search query -- return most recent incidents
        rows = manager.store.db.fetchall("SELECT * FROM incidents ORDER BY timestamp DESC LIMIT ?", (limit,))
        incidents = [dict(r) for r in rows] if rows else []
    return {"incidents": incidents}


@router.get("/memory/patterns")
async def memory_patterns(_auth=Depends(verify_token)):
    """List detected recurring patterns."""
    from ..memory import get_manager

    manager = get_manager()
    if not manager:
        return {"patterns": []}
    patterns = manager.store.list_patterns()
    # Convert keywords from space-separated string to array for frontend
    for p in patterns:
        kw = p.get("keywords", "")
        p["keywords"] = kw.split() if isinstance(kw, str) else kw
    return {"patterns": patterns}


@router.get("/memory/summary")
async def memory_summary(_auth=Depends(verify_token)):
    """Get summary stats for the agent intelligence page."""
    from ..memory import get_manager

    manager = get_manager()
    if not manager:
        return {"incidents_count": 0, "runbooks_count": 0, "patterns_count": 0, "avg_score": 0, "top_namespaces": []}

    db = manager.store.db
    incidents_row = db.fetchone("SELECT COUNT(*) as cnt, COALESCE(AVG(score), 0) as avg FROM incidents")
    runbooks_row = db.fetchone("SELECT COUNT(*) as cnt FROM runbooks")
    patterns_row = db.fetchone("SELECT COUNT(*) as cnt FROM patterns")
    ns_rows = db.fetchall(
        "SELECT namespace, COUNT(*) as cnt FROM incidents WHERE namespace != '' GROUP BY namespace ORDER BY cnt DESC LIMIT 5"
    )

    # Get eval accuracy if available
    eval_accuracy = 0.0
    try:
        from ..harness import score_eval_prompts

        # Only import eval prompts if available (won't be in production)
        try:
            from tests.eval_prompts import EVAL_PROMPTS

            result = score_eval_prompts(EVAL_PROMPTS)
            eval_accuracy = result["accuracy"]
        except ImportError:
            pass
    except Exception:
        pass

    return {
        "incidents_count": incidents_row["cnt"] if incidents_row else 0,
        "avg_score": round(float(incidents_row["avg"] if incidents_row else 0), 1),
        "runbooks_count": runbooks_row["cnt"] if runbooks_row else 0,
        "patterns_count": patterns_row["cnt"] if patterns_row else 0,
        "eval_accuracy": round(eval_accuracy, 3),
        "top_namespaces": [r["namespace"] for r in (ns_rows or [])],
    }
