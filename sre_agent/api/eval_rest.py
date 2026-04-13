"""Eval gate REST endpoints."""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, Depends, Query

from .auth import verify_token

logger = logging.getLogger("pulse_agent.api")

router = APIRouter()

_EVAL_STATUS_CACHE: dict | None = None
_EVAL_STATUS_CACHE_TS_MS = 0
_EVAL_STATUS_CACHE_TTL_MS = 60_000
_EVAL_STATUS_LOCK = asyncio.Lock()


@router.get("/eval/status")
async def eval_status(_auth=Depends(verify_token)):
    """Current eval gate status snapshot for UI surfaces."""
    global _EVAL_STATUS_CACHE, _EVAL_STATUS_CACHE_TS_MS
    from ..evals.outcomes import analyze_windows
    from ..evals.runner import evaluate_suite
    from ..evals.scenarios import load_suite

    now_ms = int(time.time() * 1000)
    if _EVAL_STATUS_CACHE and (now_ms - _EVAL_STATUS_CACHE_TS_MS) < _EVAL_STATUS_CACHE_TTL_MS:
        return _EVAL_STATUS_CACHE

    async with _EVAL_STATUS_LOCK:
        # Re-check after acquiring lock (another request may have populated the cache)
        now_ms = int(time.time() * 1000)
        if _EVAL_STATUS_CACHE and (now_ms - _EVAL_STATUS_CACHE_TS_MS) < _EVAL_STATUS_CACHE_TTL_MS:
            return _EVAL_STATUS_CACHE

        release = evaluate_suite("release", load_suite("release"))
        safety = evaluate_suite("safety", load_suite("safety"))
        integration = evaluate_suite("integration", load_suite("integration"))
        view_designer = evaluate_suite("view_designer", load_suite("view_designer"))
        outcomes = analyze_windows(current_days=7, baseline_days=7)

        # Prompt token audit
        try:
            from ..harness import measure_prompt_sections

            prompt_audit = {
                "sre": measure_prompt_sections(mode="sre"),
                "view_designer": measure_prompt_sections(mode="view_designer"),
                "security": measure_prompt_sections(mode="security"),
            }
        except Exception:
            prompt_audit = None

        payload = {
            "note": "Release gate scores static fixtures. Use 'pulse-eval replay' for live agent testing.",
            "quality_gate_passed": bool(release.gate_passed) and bool(outcomes["gate_passed"]),
            "generated_at_ms": outcomes.get("generated_at_ms"),
            "release": {
                "gate_passed": release.gate_passed,
                "scenario_count": release.scenario_count,
                "average_overall": release.average_overall,
                "dimension_averages": release.dimension_averages,
                "blocker_counts": release.blocker_counts,
            },
            "safety": {
                "gate_passed": safety.gate_passed,
                "scenario_count": safety.scenario_count,
                "average_overall": safety.average_overall,
                "dimension_averages": safety.dimension_averages,
                "blocker_counts": safety.blocker_counts,
            },
            "integration": {
                "gate_passed": integration.gate_passed,
                "scenario_count": integration.scenario_count,
                "average_overall": integration.average_overall,
                "dimension_averages": integration.dimension_averages,
                "blocker_counts": integration.blocker_counts,
            },
            "view_designer": {
                "gate_passed": view_designer.gate_passed,
                "scenario_count": view_designer.scenario_count,
                "passed_count": view_designer.passed_count,
                "average_overall": view_designer.average_overall,
                "dimension_averages": view_designer.dimension_averages,
            },
            "outcomes": {
                "gate_passed": outcomes.get("gate_passed", False),
                "current_actions": outcomes.get("current", {}).get("total_actions", 0),
                "baseline_actions": outcomes.get("baseline", {}).get("total_actions", 0),
                "regressions": outcomes.get("regressions", {}),
                "policy": outcomes.get("policy", {}),
            },
            "prompt_audit": prompt_audit,
        }
        # Record runs to DB for trend tracking (fire-and-forget)
        try:
            from ..evals.history import record_eval_run

            for suite_result in [release, safety, integration, view_designer]:
                record_eval_run(
                    suite_name=suite_result.suite_name,
                    source="api",
                    scenario_count=suite_result.scenario_count,
                    passed_count=suite_result.passed_count,
                    gate_passed=suite_result.gate_passed,
                    average_overall=suite_result.average_overall,
                    dimensions=suite_result.dimension_averages,
                    blocker_counts=suite_result.blocker_counts,
                )
        except Exception:
            pass

        _EVAL_STATUS_CACHE = payload
        _EVAL_STATUS_CACHE_TS_MS = now_ms
        return payload


@router.get("/eval/history")
async def eval_history(
    suite: str | None = Query(None),
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(100, ge=1, le=500),
    _auth=Depends(verify_token),
):
    """Eval run history for trend charts."""
    from ..evals.history import get_eval_history

    return get_eval_history(suite_name=suite, days=days, limit=limit)


@router.get("/eval/trend")
async def eval_trend(
    suite: str = Query("release"),
    days: int = Query(30, ge=1, le=365),
    _auth=Depends(verify_token),
):
    """Eval score trend summary with sparkline data."""
    from ..evals.history import get_eval_trend

    return get_eval_trend(suite_name=suite, days=days)


@router.get("/eval/score")
async def eval_tool_selection_score(_auth=Depends(verify_token)):
    """Score tool selection accuracy for static + learned eval prompts."""

    from ..harness import score_eval_prompts
    from ..tool_usage import get_learned_eval_prompts

    # Import static prompts
    try:
        from tests.eval_prompts import EVAL_PROMPTS
    except ImportError:
        EVAL_PROMPTS = []

    # Score static
    static_result = (
        score_eval_prompts(EVAL_PROMPTS)
        if EVAL_PROMPTS
        else {"total": 0, "passed": 0, "failed": 0, "accuracy": 0, "failures": []}
    )

    # Score learned
    learned = get_learned_eval_prompts(days=30)
    clean_learned = [p for p in learned if not p[0].startswith("[{")]
    learned_result = (
        score_eval_prompts(clean_learned)
        if clean_learned
        else {"total": 0, "passed": 0, "failed": 0, "accuracy": 0, "failures": []}
    )

    # Combined
    combined_total = static_result["total"] + learned_result["total"]
    combined_passed = static_result["passed"] + learned_result["passed"]

    return {
        "static": {
            "accuracy": static_result["accuracy"],
            "passed": static_result["passed"],
            "total": static_result["total"],
        },
        "learned": {
            "accuracy": learned_result["accuracy"],
            "passed": learned_result["passed"],
            "total": learned_result["total"],
        },
        "combined": {
            "accuracy": combined_passed / combined_total if combined_total else 0,
            "passed": combined_passed,
            "total": combined_total,
        },
        "failures": [
            {"query": f["query"][:80], "expected": f["expected"], "mode": f["mode"]}
            for f in static_result["failures"][:10]
        ],
    }
