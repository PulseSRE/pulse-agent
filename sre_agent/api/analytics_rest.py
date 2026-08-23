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
        from ..repositories import get_analytics_repo

        rows = get_analytics_repo().fetch_confidence_pairs(days)

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
        from ..repositories import get_analytics_repo

        override_row = get_analytics_repo().fetch_override_rate(days)

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
    from ..observability import TOKEN_PRICES

    INPUT_PRICE = TOKEN_PRICES["input"]
    OUTPUT_PRICE = TOKEN_PRICES["output"]
    CACHE_READ_PRICE = TOKEN_PRICES["cache_read"]
    CACHE_WRITE_PRICE = TOKEN_PRICES["cache_write"]

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
        from ..repositories import get_analytics_repo

        repo = get_analytics_repo()

        # Current period token totals (split by type for cost calculation)
        totals_row = repo.fetch_token_totals(days)

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
        prev_row = repo.fetch_prev_period_tokens(days)
        prev_tokens = int(prev_row["total_tokens"]) if prev_row else 0
        prev_incidents = int(prev_row["total_incidents"]) if prev_row else 0
        previous_avg = prev_tokens / prev_incidents if prev_incidents > 0 else 0

        delta_pct = round((current_avg - previous_avg) / previous_avg * 100, 1) if previous_avg > 0 else 0.0

        # By-mode breakdown
        by_mode_rows = repo.fetch_tokens_by_mode(days)

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

        forecast = None
        try:
            daily_rows = repo.fetch_daily_cost_totals(7)
            if len(daily_rows) >= 3:
                daily_costs = [
                    _estimate_cost(
                        int(r["input_t"]), int(r["output_t"]), int(r["cache_read_t"]), int(r["cache_write_t"])
                    )
                    for r in daily_rows
                ]
                avg_daily = sum(daily_costs) / len(daily_costs)
                forecast = {
                    "avg_daily_usd": round(avg_daily, 2),
                    "projected_30d_usd": round(avg_daily * 30, 2),
                    "based_on_days": len(daily_costs),
                }
        except Exception:
            logger.debug("Cost forecast failed", exc_info=True)

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
            "forecast": forecast,
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


@router.get("/budget")
def get_budget_status(_token: None = Depends(verify_token)):
    """Investigation and cost budget status."""
    from ..config import get_settings

    settings = get_settings()

    investigation = {
        "max_daily": settings.monitor.max_daily_investigations,
        "used_today": 0,
        "remaining": settings.monitor.max_daily_investigations,
    }
    try:
        from ..monitor.cluster_monitor import get_cluster_monitor_sync

        monitor = get_cluster_monitor_sync()
        if monitor:
            used, max_daily = monitor.get_investigation_budget()
            investigation["used_today"] = used
            investigation["remaining"] = max(0, max_daily - used)
    except Exception:
        logger.debug("Could not read investigation budget from monitor", exc_info=True)

    cost = None
    if settings.cost_budget_usd > 0:
        from ..observability import TOKEN_PRICES
        from ..repositories import get_analytics_repo

        totals = get_analytics_repo().fetch_token_totals(1)
        if totals and totals["total_incidents"] > 0:
            spent = round(
                (
                    int(totals["total_input"]) * TOKEN_PRICES["input"]
                    + int(totals["total_output"]) * TOKEN_PRICES["output"]
                    + int(totals["total_cache_read"]) * TOKEN_PRICES["cache_read"]
                    + int(totals["total_cache_write"]) * TOKEN_PRICES["cache_write"]
                )
                / 1_000_000,
                2,
            )
        else:
            spent = 0.0
        limit_usd = settings.cost_budget_usd
        warn_pct = settings.cost_budget_warning_pct
        cost = {
            "daily_limit_usd": limit_usd,
            "spent_today_usd": spent,
            "remaining_usd": round(max(0, limit_usd - spent), 2),
            "warning_threshold_pct": warn_pct,
            "status": ("ok" if spent < limit_usd * warn_pct / 100 else "warning" if spent < limit_usd else "exceeded"),
        }

    return {"investigation": investigation, "cost": cost}


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
    # Check for unused tool capabilities
    try:
        from ..repositories import get_analytics_repo

        repo = get_analytics_repo()
        # Check if git tools unused
        git_usage = repo.fetch_tool_usage_by_pattern("%%git%%")
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
        predict_usage = repo.fetch_tool_usage_by_pattern("%%predict%%")
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

    try:
        body = await request.json()
        events = body.get("events", [])
        if not events:
            return {"status": "ok", "recorded": 0}

        user_id = request.headers.get("x-forwarded-user", "")
        from ..repositories import get_analytics_repo

        repo = get_analytics_repo()

        for event in events[:50]:  # cap at 50 per batch
            repo.insert_user_event(
                event.get("session_id", ""),
                user_id,
                event.get("event_type", "unknown"),
                event.get("page", ""),
                json.dumps(event.get("data", {})),
            )
        repo.commit()
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
        from ..repositories import get_analytics_repo

        repo = get_analytics_repo()

        # Summary totals
        summary_row = repo.fetch_session_summary(days)
        total_queries_row = repo.fetch_total_queries(days)
        avg_duration_row = repo.fetch_avg_page_duration(days)

        # Top pages by visit count
        page_rows = repo.fetch_top_pages(days)

        # Average time on page
        duration_rows = repo.fetch_page_durations(days)

        # Agent queries by page
        query_rows = repo.fetch_queries_by_page(days)

        # Top follow-up suggestions clicked
        suggestion_rows = repo.fetch_top_suggestions(days)

        # Feature usage
        feature_rows = repo.fetch_feature_usage(days)

        return {
            "summary": {
                "total_sessions": (summary_row["total_sessions"] if summary_row else 0),
                "total_page_views": (summary_row["total_views"] if summary_row else 0),
                "unique_pages": (summary_row["unique_pages"] if summary_row else 0),
                "total_queries": (total_queries_row["total_queries"] if total_queries_row else 0),
                "avg_duration_seconds": round((avg_duration_row["avg_ms"] or 0) / 1000, 1) if avg_duration_row else 0,
            },
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
            "summary": {
                "total_sessions": 0,
                "total_page_views": 0,
                "unique_pages": 0,
                "total_queries": 0,
                "avg_duration_seconds": 0,
            },
            "pages": [],
            "time_on_page": [],
            "agent_queries_by_page": [],
            "top_suggestions": [],
            "feature_usage": [],
            "days": days,
        }
