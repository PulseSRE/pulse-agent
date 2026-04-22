"""Operational metrics REST endpoints for improvement tracking."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from .auth import verify_token

logger = logging.getLogger("pulse_agent.api")

router = APIRouter(tags=["metrics"])


@router.get("/metrics/fix-success-rate")
async def metrics_fix_success_rate(
    period: int = Query(30, ge=1, le=365, description="Period in days"),
    _auth=Depends(verify_token),
):
    """Auto-fix success rate over a time period."""
    from ..monitor.actions import get_fix_success_rate

    return get_fix_success_rate(period)


@router.get("/metrics/response-latency")
async def metrics_response_latency(
    period: int = Query(30, ge=1, le=365, description="Period in days"),
    _auth=Depends(verify_token),
):
    """Agent response p95 latency from tool usage data."""
    from .. import db

    try:
        database = db.get_database()
        row = database.fetchone(
            "SELECT "
            "  percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50, "
            "  percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95, "
            "  percentile_cont(0.99) WITHIN GROUP (ORDER BY duration_ms) AS p99, "
            "  AVG(duration_ms) AS avg_ms, "
            "  COUNT(*) AS cnt "
            "FROM tool_usage "
            "WHERE timestamp >= NOW() - INTERVAL '1 day' * ? "
            "AND duration_ms > 0",
            (period,),
        )
        if not row or row["cnt"] == 0:
            return {"period_days": period, "p50_ms": None, "p95_ms": None, "p99_ms": None, "count": 0}

        return {
            "period_days": period,
            "p50_ms": round(row["p50"], 1) if row["p50"] is not None else None,
            "p95_ms": round(row["p95"], 1) if row["p95"] is not None else None,
            "p99_ms": round(row["p99"], 1) if row["p99"] is not None else None,
            "count": row["cnt"],
            "avg_ms": round(row["avg_ms"], 1) if row["avg_ms"] is not None else None,
        }
    except Exception as e:
        logger.error("Failed to get response latency: %s", e)
        return {"period_days": period, "p50_ms": None, "p95_ms": None, "p99_ms": None, "count": 0}


@router.get("/metrics/eval-trend")
async def metrics_eval_trend(
    suite: str = Query("release", description="Eval suite name"),
    releases: int = Query(10, ge=1, le=50, description="Number of recent runs"),
    _auth=Depends(verify_token),
):
    """Eval score trend with sparkline data for release tracking."""
    from .. import db

    try:
        database = db.get_database()
        rows = database.fetchall(
            "SELECT score, pass, scenarios, timestamp FROM eval_runs WHERE suite = ? ORDER BY id DESC LIMIT ?",
            (suite, releases),
        )
        if not rows:
            return {"suite": suite, "sparkline": [], "current_score": None, "runs_count": 0}

        scores = [r["score"] for r in rows]
        scores.reverse()

        return {
            "suite": suite,
            "current_score": scores[-1] if scores else None,
            "sparkline": scores,
            "min": min(scores),
            "max": max(scores),
            "runs_count": len(scores),
            "trend": _trend(scores),
        }
    except Exception as e:
        logger.error("Failed to get eval trend: %s", e)
        return {"suite": suite, "sparkline": [], "current_score": None, "runs_count": 0}


def _trend(scores: list[float]) -> str:
    if len(scores) < 2:
        return "stable"
    recent = scores[-3:]
    avg_recent = sum(recent) / len(recent)
    avg_all = sum(scores) / len(scores)
    if avg_recent > avg_all + 0.02:
        return "improving"
    if avg_recent < avg_all - 0.02:
        return "declining"
    return "stable"


@router.get("/usage/summary")
async def usage_summary(
    days: int = Query(7, ge=1, le=90, description="Lookback period in days"),
    _auth=Depends(verify_token),
):
    """Tool usage summary split by agent (interactive) vs pipeline (autonomous)."""
    try:
        from ..db import get_database

        db = get_database()
        import time as _time

        cutoff_s = _time.time() - days * 86400
        rows = db.fetchall(
            "SELECT agent_mode, COUNT(*) as calls, COALESCE(SUM(duration_ms), 0) as total_ms "
            "FROM tool_usage WHERE timestamp > to_timestamp(?) GROUP BY agent_mode",
            (cutoff_s,),
        )
        agent_calls = 0
        agent_ms = 0
        pipeline_calls = 0
        pipeline_ms = 0
        breakdown: dict[str, int] = {}
        for r in rows or []:
            mode = r.get("agent_mode", "")
            calls = r.get("calls", 0)
            ms = r.get("total_ms", 0) or 0
            if mode.startswith("pipeline:"):
                pipeline_calls += calls
                pipeline_ms += ms
                breakdown[mode] = calls
            else:
                agent_calls += calls
                agent_ms += ms
        return {
            "days": days,
            "agent": {"tool_calls": agent_calls, "total_duration_ms": agent_ms},
            "pipeline": {"tool_calls": pipeline_calls, "total_duration_ms": pipeline_ms, "breakdown": breakdown},
        }
    except Exception as e:
        logger.error("Failed to get usage summary: %s", e)
        return {"days": days, "agent": {}, "pipeline": {}, "error": str(e)}


@router.get("/interactions")
async def list_interactions(
    actor: str = Query("", description="Filter by actor"),
    interaction_type: str = Query("", description="Filter by type"),
    item_id: str = Query("", description="Filter by inbox item ID"),
    limit: int = Query(50, ge=1, le=500),
    _auth=Depends(verify_token),
):
    """Query the user_interactions audit log."""
    try:
        from ..db import get_database

        db = get_database()
        where = []
        params: list = []
        if actor:
            where.append("actor = ?")
            params.append(actor)
        if interaction_type:
            where.append("interaction_type = ?")
            params.append(interaction_type)
        if item_id:
            where.append("item_id = ?")
            params.append(item_id)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        params.append(limit)
        rows = db.fetchall(
            f"SELECT * FROM user_interactions {clause} ORDER BY timestamp DESC LIMIT ?",
            tuple(params),
        )
        return {"interactions": rows or [], "count": len(rows or [])}
    except Exception as e:
        logger.error("Failed to list interactions: %s", e)
        return {"interactions": [], "count": 0, "error": str(e)}
