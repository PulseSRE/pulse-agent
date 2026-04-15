"""Analytics REST endpoints for Mission Control and Toolbox."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request

from .auth import verify_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analytics", tags=["analytics"])

_EMPTY_ACCURACY: dict = {
    "avg_quality_score": 0.0,
    "quality_trend": {"current": 0.0, "previous": 0.0, "delta": 0.0},
    "dimensions": {"quality": 0.0, "override_rate": 0.0},
    "anti_patterns": [],
    "learning": {
        "runbook_count": 0,
        "success_rate": 0.0,
        "pattern_count": 0,
        "pattern_types": [],
        "new_runbooks_this_month": 0,
    },
    "override_rate": {"rate": 0.0, "total_proposed": 0, "rejected_actions": 0},
}


def _compute_confidence_calibration(days: int) -> dict:
    """Compute confidence calibration statistics from investigations and actions.

    Joins investigations (which have confidence scores) with actions (which have
    verification_status) via finding_id to compute Brier score and calibration metrics.

    Args:
        days: Number of days to look back (1-365)

    Returns:
        dict with brier_score, accuracy_pct, rating, total_predictions, buckets[]
    """
    try:
        from .. import db

        database = db.get_database()
        cutoff_sql = "EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * ?) * 1000"

        # Query investigations joined with actions to get confidence + verification pairs
        query = f"""
            SELECT i.confidence, a.verification_status
            FROM investigations i
            INNER JOIN actions a ON a.finding_id = i.finding_id
            WHERE i.confidence IS NOT NULL
              AND a.verification_status IS NOT NULL
              AND a.timestamp > {cutoff_sql}
        """

        rows = database.fetchall(query, (days,))

        if len(rows) < 5:
            return {
                "brier_score": 0.0,
                "accuracy_pct": 0.0,
                "rating": "insufficient_data",
                "total_predictions": len(rows),
                "buckets": [],
            }

        # Compute Brier score
        squared_errors = []
        for row in rows:
            predicted = row["confidence"]
            actual = 1.0 if row["verification_status"] == "verified" else 0.0
            squared_errors.append((predicted - actual) ** 2)

        brier = sum(squared_errors) / len(squared_errors)
        accuracy = (1 - brier) * 100

        # Assign rating
        if accuracy >= 85:
            rating = "good"
        elif accuracy >= 70:
            rating = "fair"
        else:
            rating = "poor"

        # Compute calibration buckets (0.2 width)
        buckets = []
        for bucket_min in [0.0, 0.2, 0.4, 0.6, 0.8]:
            bucket_max = bucket_min + 0.2
            bucket_rows = [row for row in rows if bucket_min <= row["confidence"] < bucket_max]

            if not bucket_rows:
                continue

            avg_predicted = sum(r["confidence"] for r in bucket_rows) / len(bucket_rows)
            avg_actual = sum(1.0 if r["verification_status"] == "verified" else 0.0 for r in bucket_rows) / len(
                bucket_rows
            )

            buckets.append(
                {
                    "range": f"{bucket_min}-{bucket_max}",
                    "predicted": round(avg_predicted, 2),
                    "actual": round(avg_actual, 2),
                    "count": len(bucket_rows),
                }
            )

        return {
            "brier_score": round(brier, 2),
            "accuracy_pct": round(accuracy, 1),
            "rating": rating,
            "total_predictions": len(rows),
            "buckets": buckets,
        }

    except Exception as e:
        logger.exception("Failed to compute confidence calibration: %s", e)
        return {
            "brier_score": 0.0,
            "accuracy_pct": 0.0,
            "rating": "insufficient_data",
            "total_predictions": 0,
            "buckets": [],
        }


@router.get("/confidence")
def get_confidence_calibration(
    days: int = Query(30, ge=1, le=365),
    _token: None = Depends(verify_token),
):
    """Get confidence calibration statistics.

    Returns Brier score, accuracy percentage, rating, and calibration buckets
    showing how well the agent's confidence scores align with actual verification outcomes.
    """
    return _compute_confidence_calibration(days)


def _compute_accuracy_stats(days: int) -> dict:
    """Compute accuracy statistics from memory store and tool usage.

    Combines:
    - Memory store stats: quality scores, anti-patterns, learning metrics
    - Override rate from tool_usage: rejected_actions / total_proposed

    Args:
        days: Number of days to look back (1-365)

    Returns:
        dict with avg_quality_score, quality_trend, dimensions, anti_patterns, learning, override_rate
    """
    try:
        # Reuse the shared store singleton (avoids CREATE TABLE per request)
        from ..memory.memory_tools import _store as memory_store

        if memory_store is None:
            return _EMPTY_ACCURACY
        memory_stats = memory_store.get_accuracy_stats(days)

        # Get override rate from tool_usage table (PostgreSQL)
        from .. import db

        database = db.get_database()
        cutoff_sql = "EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * ?) * 1000"

        # Query for confirmation-required actions
        override_query = f"""
            SELECT
                COUNT(*) FILTER (WHERE requires_confirmation = TRUE) as total_proposed,
                COUNT(*) FILTER (WHERE requires_confirmation = TRUE AND was_confirmed = FALSE) as rejected_actions
            FROM tool_usage
            WHERE timestamp > TO_TIMESTAMP({cutoff_sql})
        """

        override_row = database.fetchone(override_query, (days,))

        total_proposed = override_row["total_proposed"] if override_row else 0
        rejected_actions = override_row["rejected_actions"] if override_row else 0
        override_rate = round(rejected_actions / total_proposed, 2) if total_proposed > 0 else 0.0

        # Compute dimensions (simplified accuracy view)
        dimensions = {
            "quality": memory_stats.get("avg_quality_score", 0.0),
            "override_rate": override_rate,
        }

        return {
            "avg_quality_score": memory_stats.get("avg_quality_score", 0.0),
            "quality_trend": memory_stats.get("quality_trend", 0.0),
            "dimensions": dimensions,
            "anti_patterns": memory_stats.get("anti_patterns", []),
            "learning": memory_stats.get("learning", {}),
            "override_rate": {
                "rate": override_rate,
                "total_proposed": total_proposed,
                "rejected_actions": rejected_actions,
            },
        }

    except Exception as e:
        logger.exception("Failed to compute accuracy stats: %s", e)
        return _EMPTY_ACCURACY.copy()


@router.get("/accuracy")
def get_accuracy_stats(
    days: int = Query(30, ge=1, le=365),
    _token: None = Depends(verify_token),
):
    """Get accuracy statistics.

    Returns quality scores, trends, anti-patterns, learning metrics, and override rate.
    Combines memory store data (incidents, runbooks, patterns) with tool usage data.
    """
    return _compute_accuracy_stats(days)


def _compute_cost_stats(days: int) -> dict:
    """Compute cost statistics from tool_turns table.

    Analyzes token usage across sessions to compute average tokens per incident,
    trends comparing current period vs previous period, breakdown by agent_mode,
    and estimated dollar cost using Vertex AI pricing.
    """
    # Vertex AI pricing (per 1M tokens) — Claude Opus on Vertex
    INPUT_PRICE = 15.0
    OUTPUT_PRICE = 75.0
    CACHE_READ_PRICE = 1.875
    CACHE_WRITE_PRICE = 18.75

    def _estimate_cost(input_t: int, output_t: int, cache_read_t: int = 0, cache_write_t: int = 0) -> float:
        return round(
            (
                input_t * INPUT_PRICE
                + output_t * OUTPUT_PRICE
                + cache_read_t * CACHE_READ_PRICE
                + cache_write_t * CACHE_WRITE_PRICE
            )
            / 1_000_000,
            2,
        )

    try:
        from .. import db

        database = db.get_database()
        interval_sql = "NOW() - INTERVAL '1 day' * ?"

        # Current period token totals (split by type for cost calculation)
        totals_row = database.fetchone(
            f"SELECT "
            f"  COALESCE(SUM(input_tokens), 0) AS total_input, "
            f"  COALESCE(SUM(output_tokens), 0) AS total_output, "
            f"  COALESCE(SUM(cache_read_tokens), 0) AS total_cache_read, "
            f"  COALESCE(SUM(cache_creation_tokens), 0) AS total_cache_write, "
            f"  COUNT(DISTINCT session_id) AS total_incidents "
            f"FROM tool_turns WHERE timestamp > {interval_sql} AND input_tokens IS NOT NULL",
            (days,),
        )

        if not totals_row or totals_row["total_incidents"] == 0:
            return {
                "avg_tokens_per_incident": 0,
                "trend": {"current": 0, "previous": 0, "delta_pct": 0.0},
                "by_mode": [],
                "total_tokens": 0,
                "total_incidents": 0,
                "cost": {
                    "total_usd": 0.0,
                    "avg_per_incident_usd": 0.0,
                    "input_usd": 0.0,
                    "output_usd": 0.0,
                    "cache_savings_usd": 0.0,
                },
            }

        total_input = int(totals_row["total_input"])
        total_output = int(totals_row["total_output"])
        total_cache_read = int(totals_row["total_cache_read"])
        total_cache_write = int(totals_row["total_cache_write"])
        total_incidents = int(totals_row["total_incidents"])
        total_tokens = total_input + total_output

        current_avg = total_tokens / total_incidents if total_incidents > 0 else 0

        # Previous period for trend
        prev_row = database.fetchone(
            f"SELECT "
            f"  COALESCE(SUM(input_tokens + output_tokens), 0) AS total_tokens, "
            f"  COUNT(DISTINCT session_id) AS total_incidents "
            f"FROM tool_turns "
            f"WHERE timestamp > {interval_sql} AND timestamp <= {interval_sql} AND input_tokens IS NOT NULL",
            (days * 2, days),
        )
        prev_tokens = int(prev_row["total_tokens"]) if prev_row else 0
        prev_incidents = int(prev_row["total_incidents"]) if prev_row else 0
        previous_avg = prev_tokens / prev_incidents if prev_incidents > 0 else 0

        delta_pct = round((current_avg - previous_avg) / previous_avg * 100, 1) if previous_avg > 0 else 0.0

        # By-mode breakdown
        by_mode_rows = database.fetchall(
            f"SELECT agent_mode, "
            f"  COUNT(DISTINCT session_id) AS incident_count, "
            f"  COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            f"  COALESCE(SUM(output_tokens), 0) AS output_tokens, "
            f"  COALESCE(SUM(input_tokens + output_tokens), 0) AS total_tokens "
            f"FROM tool_turns WHERE timestamp > {interval_sql} AND input_tokens IS NOT NULL "
            f"GROUP BY agent_mode ORDER BY total_tokens DESC",
            (days,),
        )

        by_mode = [
            {
                "mode": row["agent_mode"],
                "incident_count": row["incident_count"],
                "total_tokens": row["total_tokens"],
                "avg_tokens": round(row["total_tokens"] / row["incident_count"], 1) if row["incident_count"] > 0 else 0,
                "cost_usd": _estimate_cost(row["input_tokens"], row["output_tokens"]),
            }
            for row in by_mode_rows
        ]

        # Cost calculation
        total_cost = _estimate_cost(total_input, total_output, total_cache_read, total_cache_write)
        avg_cost = round(total_cost / total_incidents, 3) if total_incidents > 0 else 0.0
        # Cache savings: what it would have cost if cache_read tokens were charged at full input price
        cache_savings = round(total_cache_read * (INPUT_PRICE - CACHE_READ_PRICE) / 1_000_000, 2)

        return {
            "avg_tokens_per_incident": round(current_avg, 1),
            "trend": {
                "current": round(current_avg, 1),
                "previous": round(previous_avg, 1),
                "delta_pct": delta_pct,
            },
            "by_mode": by_mode,
            "total_tokens": total_tokens,
            "total_incidents": total_incidents,
            "cost": {
                "total_usd": total_cost,
                "avg_per_incident_usd": avg_cost,
                "input_usd": round(total_input * INPUT_PRICE / 1_000_000, 2),
                "output_usd": round(total_output * OUTPUT_PRICE / 1_000_000, 2),
                "cache_savings_usd": cache_savings,
            },
        }

    except Exception as e:
        logger.exception("Failed to compute cost stats: %s", e)
        return {
            "avg_tokens_per_incident": 0,
            "trend": {"current": 0, "previous": 0, "delta_pct": 0.0},
            "by_mode": [],
            "total_tokens": 0,
            "total_incidents": 0,
            "cost": {
                "total_usd": 0.0,
                "avg_per_incident_usd": 0.0,
                "input_usd": 0.0,
                "output_usd": 0.0,
                "cache_savings_usd": 0.0,
            },
        }


@router.get("/cost")
def get_cost_stats(
    days: int = Query(30, ge=1, le=365),
    _token: None = Depends(verify_token),
):
    """Get cost statistics.

    Returns average tokens per incident, trend comparison, breakdown by agent mode,
    and total token usage across all sessions.
    """
    return _compute_cost_stats(days)


@router.get("/intelligence")
def get_intelligence_analytics(
    days: int = Query(7, ge=1, le=90),
    mode: str = Query("sre", pattern="^(sre|security|view_designer)$"),
    _token: None = Depends(verify_token),
):
    """Get intelligence analytics sections.

    Returns structured intelligence data for Toolbox Analytics UI including:
    - query_reliability: preferred and unreliable PromQL queries
    - error_hotspots: tools with high error rates
    - token_efficiency: average token usage and cache hit rate
    - harness_effectiveness: tool selection accuracy and wasted tools
    - routing_accuracy: mode routing accuracy
    - feedback_analysis: tools with negative feedback
    - token_trending: token usage trends
    - dashboard_patterns: dashboard/view designer usage patterns
    """
    from ..intelligence import get_intelligence_sections

    return get_intelligence_sections(mode, days)


@router.get("/prompt")
async def analytics_prompt(
    days: int = Query(30, ge=1, le=365),
    skill: str | None = Query(None),
    _auth=Depends(verify_token),
):
    """Prompt section breakdown and version history for Toolbox Analytics."""
    return _get_prompt_analytics(days=days, skill=skill)


def _get_prompt_analytics(days: int = 30, skill: str | None = None) -> dict:
    try:
        from ..prompt_log import get_prompt_stats, get_prompt_versions

        stats = get_prompt_stats(days=days)
        versions = []
        if skill:
            versions = get_prompt_versions(skill, days=days)
        elif stats.get("skill_names"):
            versions = get_prompt_versions(stats["skill_names"][0], days=days)
        return {"stats": stats, "versions": versions}
    except Exception:
        logger.debug("Failed to get prompt analytics", exc_info=True)
        return {
            "stats": {
                "total_prompts": 0,
                "avg_tokens": 0,
                "cache_hit_rate": 0.0,
                "by_skill": [],
                "section_avg": {},
            },
            "versions": [],
        }


@router.get("/readiness")
async def analytics_readiness(_auth=Depends(verify_token)):
    """Lightweight readiness gate summary for Mission Control outcomes card."""
    return _get_readiness_summary()


def _get_readiness_summary() -> dict:
    # The readiness module may or may not exist. Handle both cases.
    try:
        from ..readiness import evaluate_gates

        gates = evaluate_gates()
        passed = sum(1 for g in gates if g.get("status") == "pass")
        failed = sum(1 for g in gates if g.get("status") == "fail")
        attention = sum(1 for g in gates if g.get("status") == "attention")
        total = len(gates)
        attention_items = [
            {"gate": g.get("id", "unknown"), "message": g.get("message", "")}
            for g in gates
            if g.get("status") in ("fail", "attention")
        ]
        return {
            "total_gates": total,
            "passed": passed,
            "failed": failed,
            "attention": attention,
            "pass_rate": round(passed / total, 3) if total > 0 else 0.0,
            "attention_items": attention_items[:5],
        }
    except ImportError:
        return {
            "total_gates": 0,
            "passed": 0,
            "failed": 0,
            "attention": 0,
            "pass_rate": 0.0,
            "attention_items": [],
        }
    except Exception:
        logger.debug("Failed to get readiness summary", exc_info=True)
        return {
            "total_gates": 0,
            "passed": 0,
            "failed": 0,
            "attention": 0,
            "pass_rate": 0.0,
            "attention_items": [],
        }


# ── Recommendations Router ─────────────────────────────────────────────────

recommendations_router = APIRouter(tags=["analytics"])


@recommendations_router.get("/recommendations")
async def get_recommendations(_auth=Depends(verify_token)):
    """Contextual capability recommendations for Mission Control."""
    return _compute_recommendations()


def _compute_recommendations() -> dict:
    recommendations = []
    # Type 1: Check for disabled scanners
    # Future: analyze scanner enablement and recommend enabling disabled ones
    # try:
    #     from ..monitor.scanners import ALL_SCANNERS
    #     ...
    # except Exception:
    #     pass

    # Type 2: Check for unused tool capabilities
    try:
        from .. import db

        database = db.get_database()
        # Check if git tools unused
        git_usage = database.fetchone(
            "SELECT COUNT(*) AS cnt FROM tool_usage "
            "WHERE tool_name LIKE '%%git%%' "
            "AND timestamp > EXTRACT(EPOCH FROM NOW() - INTERVAL '30 days') * 1000"
        )
        if git_usage and git_usage["cnt"] == 0:
            recommendations.append(
                {
                    "type": "capability",
                    "title": "Try Git PR proposals",
                    "description": "The agent can propose Git PRs for changes.",
                    "action": {"kind": "chat_prompt", "prompt": "propose a PR to fix deployment X"},
                }
            )
        # Check if predict tools unused
        predict_usage = database.fetchone(
            "SELECT COUNT(*) AS cnt FROM tool_usage "
            "WHERE tool_name LIKE '%%predict%%' "
            "AND timestamp > EXTRACT(EPOCH FROM NOW() - INTERVAL '30 days') * 1000"
        )
        if predict_usage and predict_usage["cnt"] == 0:
            recommendations.append(
                {
                    "type": "capability",
                    "title": "Try predictive analytics",
                    "description": "The agent can predict capacity issues before they happen.",
                    "action": {
                        "kind": "chat_prompt",
                        "prompt": "predict resource usage for namespace prod",
                    },
                }
            )
    except Exception:
        logger.debug("Failed to compute recommendations", exc_info=True)
    return {"recommendations": recommendations[:4]}


@router.post("/events", status_code=202)
async def record_user_events(request: Request, _auth=Depends(verify_token)):
    """Record a batch of user session events. Fire-and-forget."""
    import json

    from ..db import get_database

    try:
        body = await request.json()
        events = body.get("events", [])
        if not events:
            return {"status": "ok", "recorded": 0}

        user_id = request.headers.get("x-forwarded-user", "")
        db = get_database()

        for event in events[:50]:  # cap at 50 per batch
            db.execute(
                "INSERT INTO user_events (session_id, user_id, event_type, page, data) VALUES (%s, %s, %s, %s, %s)",
                (
                    event.get("session_id", ""),
                    user_id,
                    event.get("event_type", "unknown"),
                    event.get("page", ""),
                    json.dumps(event.get("data", {})),
                ),
            )
        db.commit()
        return {"status": "ok", "recorded": len(events)}
    except Exception:
        logger.debug("Failed to record user events", exc_info=True)
        return {"status": "ok", "recorded": 0}


@router.get("/sessions")
async def session_analytics(
    days: int = Query(7, ge=1, le=90),
    _auth=Depends(verify_token),
):
    """Aggregated session analytics — page views, time-on-page, agent queries by page."""
    try:
        from ..db import get_database

        db = get_database()

        # Top pages by visit count
        page_rows = db.fetchall(
            "SELECT page, COUNT(*) as views, COUNT(DISTINCT session_id) as sessions "
            "FROM user_events WHERE event_type = 'page_view' "
            "AND timestamp > NOW() - INTERVAL '%s days' "
            "GROUP BY page ORDER BY views DESC LIMIT 20",
            (days,),
        )

        # Average time on page
        duration_rows = db.fetchall(
            "SELECT page, AVG((data->>'duration_ms')::int) as avg_ms, "
            "COUNT(*) as sample_count "
            "FROM user_events WHERE event_type = 'page_leave' "
            "AND timestamp > NOW() - INTERVAL '%s days' "
            "GROUP BY page ORDER BY avg_ms DESC LIMIT 20",
            (days,),
        )

        # Agent queries by page
        query_rows = db.fetchall(
            "SELECT page, COUNT(*) as queries "
            "FROM user_events WHERE event_type = 'agent_query' "
            "AND timestamp > NOW() - INTERVAL '%s days' "
            "GROUP BY page ORDER BY queries DESC LIMIT 20",
            (days,),
        )

        # Top follow-up suggestions clicked
        suggestion_rows = db.fetchall(
            "SELECT data->>'text' as suggestion, COUNT(*) as clicks "
            "FROM user_events WHERE event_type = 'suggestion_click' "
            "AND timestamp > NOW() - INTERVAL '%s days' "
            "GROUP BY data->>'text' ORDER BY clicks DESC LIMIT 10",
            (days,),
        )

        # Feature usage
        feature_rows = db.fetchall(
            "SELECT data->>'feature' as feature, COUNT(*) as uses "
            "FROM user_events WHERE event_type = 'feature_use' "
            "AND timestamp > NOW() - INTERVAL '%s days' "
            "GROUP BY data->>'feature' ORDER BY uses DESC LIMIT 10",
            (days,),
        )

        return {
            "pages": [dict(r) for r in (page_rows or [])],
            "time_on_page": [
                {"page": r["page"], "avg_seconds": round((r["avg_ms"] or 0) / 1000, 1), "samples": r["sample_count"]}
                for r in (duration_rows or [])
            ],
            "agent_queries_by_page": [dict(r) for r in (query_rows or [])],
            "top_suggestions": [dict(r) for r in (suggestion_rows or [])],
            "feature_usage": [dict(r) for r in (feature_rows or [])],
            "days": days,
        }
    except Exception:
        logger.debug("Session analytics failed", exc_info=True)
        return {
            "pages": [],
            "time_on_page": [],
            "agent_queries_by_page": [],
            "top_suggestions": [],
            "feature_usage": [],
            "days": days,
        }
