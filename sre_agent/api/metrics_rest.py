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
        rows = database.fetchall(
            "SELECT duration_ms FROM tool_usage "
            "WHERE timestamp >= NOW() - INTERVAL '1 day' * ? "
            "AND duration_ms > 0 "
            "ORDER BY duration_ms",
            (period,),
        )
        if not rows:
            return {"period_days": period, "p50_ms": None, "p95_ms": None, "p99_ms": None, "count": 0}

        durations = [r["duration_ms"] for r in rows]
        n = len(durations)

        return {
            "period_days": period,
            "p50_ms": durations[n // 2],
            "p95_ms": durations[int(n * 0.95)],
            "p99_ms": durations[int(n * 0.99)],
            "count": n,
            "avg_ms": round(sum(durations) / n, 1),
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
