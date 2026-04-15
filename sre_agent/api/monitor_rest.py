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


@router.get("/kpi")
async def get_kpi_dashboard(
    days: int = Query(7, ge=1, le=90),
    _auth=Depends(verify_token),
):
    """Operational KPIs — 9 metrics aligned with ORCA targets."""
    from .. import db

    kpis: dict[str, dict] = {}

    try:
        database = db.get_database()

        # 1. MTTD — Mean Time to Detect (scan interval proxy)
        from ..config import get_settings

        kpis["mttd"] = {
            "label": "Mean Time to Detect",
            "value": get_settings().scan_interval,
            "unit": "seconds",
            "target": 30,
            "status": "pass" if get_settings().scan_interval <= 60 else "fail",
        }

        # 2. MTTR — Mean Time to Remediate
        mttr_row = database.fetchone(
            "SELECT AVG(duration_ms) as avg_ms FROM actions "
            "WHERE status = 'completed' "
            "AND timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * ?)::BIGINT * 1000",
            (days,),
        )
        mttr_seconds = int((mttr_row["avg_ms"] or 0) / 1000) if mttr_row else 0
        kpis["mttr"] = {
            "label": "Mean Time to Remediate",
            "value": mttr_seconds,
            "unit": "seconds",
            "target": 300,
            "status": "pass" if mttr_seconds <= 300 else "warn" if mttr_seconds <= 600 else "fail",
        }

        # 3. Auto-remediation success rate
        fix_row = database.fetchone(
            "SELECT COUNT(*) FILTER (WHERE status = 'completed') AS good, "
            "COUNT(*) AS total FROM actions "
            "WHERE timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * ?)::BIGINT * 1000",
            (days,),
        )
        fix_rate = round(fix_row["good"] / max(fix_row["total"], 1), 3) if fix_row else 0
        kpis["auto_fix_success"] = {
            "label": "Auto-Remediation Success",
            "value": fix_rate,
            "unit": "ratio",
            "target": 0.85,
            "status": "pass" if fix_rate >= 0.85 else "warn" if fix_rate >= 0.70 else "fail",
        }

        # 4. False positive rate (noise)
        noise_row = database.fetchone(
            "SELECT COUNT(*) FILTER (WHERE noise_score > 0.7) AS noise, "
            "COUNT(*) AS total FROM findings "
            "WHERE timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * ?)::BIGINT * 1000",
            (days,),
        )
        fp_rate = round(noise_row["noise"] / max(noise_row["total"], 1), 3) if noise_row else 0
        kpis["false_positive_rate"] = {
            "label": "False Positive Rate",
            "value": fp_rate,
            "unit": "ratio",
            "target": 0.02,
            "status": "pass" if fp_rate <= 0.02 else "warn" if fp_rate <= 0.05 else "fail",
        }

        # 5. Selector recall@5 (from latest eval or selection log)
        selector_row = database.fetchone(
            "SELECT COUNT(*) FILTER (WHERE skill_overridden IS NULL) AS correct, "
            "COUNT(*) AS total FROM skill_selection_log "
            "WHERE session_id != '__weight_snapshot__' "
            "AND timestamp > NOW() - INTERVAL '%s days'",
            (days,),
        )
        recall = round(selector_row["correct"] / max(selector_row["total"], 1), 3) if selector_row else 0
        kpis["selector_recall"] = {
            "label": "Selector Recall",
            "value": recall,
            "unit": "ratio",
            "target": 0.92,
            "status": "pass" if recall >= 0.92 else "warn" if recall >= 0.85 else "fail",
        }

        # 6. Selector latency p99
        latency_row = database.fetchone(
            "SELECT PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY selection_ms) AS p99 "
            "FROM skill_selection_log "
            "WHERE session_id != '__weight_snapshot__' "
            "AND timestamp > NOW() - INTERVAL '%s days'",
            (days,),
        )
        p99_ms = int(latency_row["p99"] or 0) if latency_row else 0
        kpis["selector_latency_p99"] = {
            "label": "Selector Latency p99",
            "value": p99_ms,
            "unit": "ms",
            "target": 80,
            "status": "pass" if p99_ms <= 80 else "warn" if p99_ms <= 200 else "fail",
        }

        # 7. Agent-caused incidents (actions with status='rolled_back')
        rollback_row = database.fetchone(
            "SELECT COUNT(*) as cnt FROM actions WHERE status = 'rolled_back' "
            "AND timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * ?)::BIGINT * 1000",
            (days,),
        )
        agent_incidents = rollback_row["cnt"] if rollback_row else 0
        kpis["agent_caused_incidents"] = {
            "label": "Agent-Caused Incidents",
            "value": agent_incidents,
            "unit": "count",
            "target": 0,
            "status": "pass" if agent_incidents == 0 else "fail",
        }

        # 8. Time-to-Resolution (finding detected → verified fixed)
        ttr_row = database.fetchone(
            "SELECT AVG(a.timestamp - f.timestamp) / 1000 as avg_seconds "
            "FROM actions a JOIN findings f ON a.finding_id = f.id "
            "WHERE a.status = 'completed' "
            "AND a.verification_status = 'verified' "
            "AND a.timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * %s)::BIGINT * 1000",
            (days,),
        )
        ttr_seconds = int(ttr_row["avg_seconds"] or 0) if ttr_row else 0
        kpis["time_to_resolution"] = {
            "label": "Time to Resolution",
            "value": ttr_seconds,
            "unit": "seconds",
            "target": 600,
            "status": "pass" if ttr_seconds <= 600 else "warn" if ttr_seconds <= 1800 else "fail",
            "description": "Finding detected → fix verified",
        }

        # 9. Self-heal rate (findings that resolved without any action)
        self_heal_row = database.fetchone(
            "SELECT "
            "COUNT(*) FILTER (WHERE resolved = 1 AND id NOT IN (SELECT finding_id FROM actions WHERE finding_id IS NOT NULL)) AS self_healed, "
            "COUNT(*) FILTER (WHERE resolved = 1) AS total_resolved "
            "FROM findings "
            "WHERE timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * %s)::BIGINT * 1000",
            (days,),
        )
        self_heal_rate = (
            round((self_heal_row["self_healed"] or 0) / max(self_heal_row["total_resolved"] or 1, 1), 3)
            if self_heal_row
            else 0
        )
        kpis["self_heal_rate"] = {
            "label": "Self-Heal Rate",
            "value": self_heal_rate,
            "unit": "ratio",
            "target": None,
            "status": "info",
            "description": "Findings that resolved without agent intervention",
        }

        # 10. Token cost per resolution
        token_cost_row = database.fetchone(
            "SELECT AVG(total_tokens) as avg_tokens FROM ("
            "  SELECT pe.finding_id, "
            "  SUM(COALESCE((phase->>'evidence_length')::int, 0)) as total_tokens "
            "  FROM plan_executions pe, jsonb_array_elements(pe.phase_details) as phase "
            "  WHERE pe.status IN ('complete', 'partial') "
            "  AND pe.timestamp > NOW() - INTERVAL '%s days' "
            "  AND pe.finding_id IS NOT NULL AND pe.finding_id != '' "
            "  GROUP BY pe.finding_id"
            ") sub",
            (days,),
        )
        avg_tokens = int(token_cost_row["avg_tokens"] or 0) if token_cost_row else 0
        kpis["tokens_per_resolution"] = {
            "label": "Evidence per Resolution",
            "value": avg_tokens,
            "unit": "chars",
            "target": 5000,
            "status": "pass" if avg_tokens <= 5000 else "warn" if avg_tokens <= 10000 else "fail",
            "description": "Average evidence gathered per incident resolution",
        }

        # 11. Routing accuracy (% of routing decisions not overridden by user)
        override_row = database.fetchone(
            "SELECT "
            "COUNT(*) FILTER (WHERE routing_skill IS NOT NULL AND feedback = 'positive') AS confirmed, "
            "COUNT(*) FILTER (WHERE routing_skill IS NOT NULL AND feedback = 'negative') AS rejected, "
            "COUNT(*) FILTER (WHERE routing_skill IS NOT NULL) AS total "
            "FROM tool_turns "
            "WHERE timestamp > NOW() - INTERVAL '%s days'",
            (days,),
        )
        if override_row and (override_row["total"] or 0) > 0:
            routing_accuracy = round(
                (override_row["total"] - (override_row["rejected"] or 0)) / override_row["total"], 3
            )
        else:
            routing_accuracy = 1.0
        kpis["routing_accuracy"] = {
            "label": "Routing Accuracy",
            "value": routing_accuracy,
            "unit": "ratio",
            "target": 0.95,
            "status": "pass" if routing_accuracy >= 0.95 else "warn" if routing_accuracy >= 0.85 else "fail",
            "description": "Skill routing decisions not rejected by user feedback",
        }

    except Exception as e:
        logger.debug("KPI computation failed: %s", e)

    # Count pass/warn/fail
    statuses = [k["status"] for k in kpis.values()]
    return {
        "kpis": kpis,
        "summary": {
            "pass": statuses.count("pass"),
            "warn": statuses.count("warn"),
            "fail": statuses.count("fail"),
            "total": len(kpis),
        },
        "days": days,
    }


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


# ── Postmortems ────────────────────────────────────────────────────────────


@router.get("/postmortems")
async def list_postmortems(
    limit: int = Query(20, ge=1, le=100),
    _auth=Depends(verify_token),
):
    """List auto-generated postmortems, newest first."""
    try:
        from .. import db

        database = db.get_database()
        rows = database.fetchall(
            "SELECT id, incident_type, plan_id, timeline, root_cause, "
            "contributing_factors, blast_radius, actions_taken, prevention, "
            "metrics_impact, confidence, generated_at "
            "FROM postmortems ORDER BY generated_at DESC LIMIT ?",
            (limit,),
        )

        results = []
        for row in rows:
            entry = dict(row)
            for field in ("contributing_factors", "blast_radius", "actions_taken", "prevention"):
                if isinstance(entry.get(field), str):
                    entry[field] = json.loads(entry[field])
            results.append(entry)

        return {"postmortems": results, "total": len(results)}
    except Exception as e:
        logger.debug("Failed to list postmortems: %s", e)
        return {"postmortems": [], "total": 0}


# ── Topology / Dependency Graph ────────────────────────────────────────────


@router.get("/topology")
async def get_topology(
    namespace: str | None = Query(None),
    _auth=Depends(verify_token),
):
    """Return the dependency graph as nodes + edges for visualization."""
    from ..dependency_graph import get_dependency_graph

    graph = get_dependency_graph()
    nodes = []
    edges = []

    for key, node in graph._nodes.items():
        if namespace and node.namespace != namespace:
            continue
        nodes.append(
            {
                "id": key,
                "kind": node.kind,
                "name": node.name,
                "namespace": node.namespace,
            }
        )

    node_ids = {n["id"] for n in nodes}
    for edge in graph._edges:
        if edge.source in node_ids and edge.target in node_ids:
            edges.append(
                {
                    "source": edge.source,
                    "target": edge.target,
                    "relationship": edge.relationship,
                }
            )

    # Build filtered summary (not global)
    kinds: dict[str, int] = {}
    for n in nodes:
        kinds[n["kind"]] = kinds.get(n["kind"], 0) + 1

    return {
        "nodes": nodes,
        "edges": edges,
        "summary": {
            "nodes": len(nodes),
            "edges": len(edges),
            "kinds": kinds,
            "last_refresh": graph._last_refresh,
        },
    }


# ── Plan Templates ─────────────────────────────────────────────────────────


@router.get("/slo")
async def get_slo_status(_auth=Depends(verify_token)):
    """Current SLO status with burn rates from live Prometheus data."""
    from ..slo_registry import get_slo_registry

    registry = get_slo_registry()
    slos = registry.list_all()

    if not slos:
        return {"slos": [], "total": 0}

    statuses = registry.evaluate_with_prometheus()

    return {
        "slos": [
            {
                "service": s.definition.service_name,
                "type": s.definition.slo_type,
                "target": s.definition.target,
                "window_days": s.definition.window_days,
                "description": s.definition.description,
                "current_value": s.current_value,
                "error_budget_remaining": s.error_budget_remaining,
                "burn_rate": s.burn_rate,
                "alert_level": s.alert_level,
            }
            for s in statuses
        ],
        "total": len(statuses),
    }


@router.post("/slo")
async def register_slo(request: Request, _auth=Depends(verify_token)):
    """Register a new SLO definition."""
    from ..slo_registry import SLODefinition, get_slo_registry

    body = await request.json()
    slo = SLODefinition(
        service_name=body.get("service", ""),
        slo_type=body.get("type", "availability"),
        target=float(body.get("target", 0.999)),
        window_days=int(body.get("window_days", 30)),
        description=body.get("description", ""),
    )

    if not slo.service_name:
        raise HTTPException(status_code=400, detail="service name required")

    registry = get_slo_registry()
    registry.register(slo)
    return {"status": "registered", "service": slo.service_name, "type": slo.slo_type}


@router.delete("/slo/{service}/{slo_type}")
async def unregister_slo(service: str, slo_type: str, _auth=Depends(verify_token)):
    """Remove an SLO definition."""
    from ..slo_registry import get_slo_registry

    registry = get_slo_registry()
    registry.unregister(service, slo_type)
    return {"status": "removed", "service": service, "type": slo_type}


@router.get("/analytics/fix-strategies")
async def fix_strategy_effectiveness(
    days: int = Query(30, ge=1, le=90),
    _auth=Depends(verify_token),
):
    """Which fix strategies work for which root causes."""
    try:
        from .. import db

        database = db.get_database()
        rows = database.fetchall(
            "SELECT category, tool, status, verification_status "
            "FROM actions "
            "WHERE timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * ?)::BIGINT * 1000",
            (days,),
        )

        strategies: dict[str, dict] = {}
        for r in rows:
            key = f"{r.get('category', 'unknown')}:{r.get('tool', 'unknown')}"
            if key not in strategies:
                strategies[key] = {
                    "category": r.get("category", "unknown"),
                    "tool": r.get("tool", "unknown"),
                    "total": 0,
                    "success": 0,
                    "verified": 0,
                    "failed": 0,
                }
            strategies[key]["total"] += 1
            if r["status"] == "completed":
                strategies[key]["success"] += 1
            elif r["status"] == "failed":
                strategies[key]["failed"] += 1
            if r.get("verification_status") == "verified":
                strategies[key]["verified"] += 1

        result = sorted(strategies.values(), key=lambda x: -x["total"])
        for s in result:
            s["success_rate"] = round(s["success"] / s["total"], 2) if s["total"] > 0 else 0
            s["verification_rate"] = round(s["verified"] / s["total"], 2) if s["total"] > 0 else 0

        return {"strategies": result, "days": days}
    except Exception as e:
        logger.debug("Fix strategy analytics failed: %s", e)
        return {"strategies": [], "days": days}


@router.get("/analytics/learning")
async def agent_learning_feed(
    days: int = Query(7, ge=1, le=30),
    _auth=Depends(verify_token),
):
    """What the agent learned recently — weight updates, scaffolded skills, noise suppression."""
    events: list[dict] = []

    try:
        from .. import db

        database = db.get_database()

        # Weight snapshots
        weight_rows = database.fetchall(
            "SELECT channel_weights, timestamp FROM skill_selection_log "
            "WHERE session_id = '__weight_snapshot__' "
            "ORDER BY timestamp DESC LIMIT 5"
        )
        for r in weight_rows:
            events.append(
                {
                    "type": "weight_update",
                    "description": "Channel weights recomputed from selection outcomes",
                    "data": r.get("channel_weights"),
                    "timestamp": r.get("timestamp"),
                }
            )

        # Recent routing decisions (show WHY queries were routed)
        routing_rows = database.fetchall(
            "SELECT query_summary, selected_skill, channel_scores, fused_scores "
            "FROM skill_selection_log "
            "WHERE session_id != '__weight_snapshot__' "
            "AND timestamp > NOW() - INTERVAL '%s days' "
            "ORDER BY timestamp DESC LIMIT 5",
            (days,),
        )
        for r in routing_rows:
            try:
                ch = (
                    json.loads(r["channel_scores"])
                    if isinstance(r["channel_scores"], str)
                    else (r["channel_scores"] or {})
                )
                # Build a readable breakdown
                top_channels = []
                for ch_name, scores in ch.items():
                    if scores and isinstance(scores, dict):
                        best = max(scores.values()) if scores else 0
                        if best > 0.1:
                            top_channels.append(f"{ch_name}: {best:.0%}")
                events.append(
                    {
                        "type": "routing_decision",
                        "description": f'"{r["query_summary"][:60]}" → {r["selected_skill"]}',
                        "data": {"channels": ", ".join(top_channels[:4]) if top_channels else "low signal"},
                    }
                )
            except Exception:
                pass

        # Selection stats
        sel_row = database.fetchone(
            "SELECT COUNT(*) as total, "
            "COUNT(DISTINCT selected_skill) as skills_used, "
            "SUM(CASE WHEN skill_overridden IS NOT NULL THEN 1 ELSE 0 END) as overrides "
            "FROM skill_selection_log "
            "WHERE timestamp > NOW() - INTERVAL '%s days'",
            (days,),
        )
        if sel_row and sel_row["total"] > 0:
            events.append(
                {
                    "type": "selection_summary",
                    "description": f"{sel_row['total']} queries routed, {sel_row['skills_used']} skills used, {sel_row['overrides']} overrides",
                    "data": dict(sel_row),
                }
            )

        # Postmortem count
        pm_row = database.fetchone(
            "SELECT COUNT(*) as cnt FROM postmortems "
            "WHERE generated_at > EXTRACT(EPOCH FROM NOW() - INTERVAL '%s days')::BIGINT * 1000",
            (days,),
        )
        if pm_row and pm_row["cnt"] > 0:
            events.append(
                {
                    "type": "postmortems_generated",
                    "description": f"{pm_row['cnt']} postmortems auto-generated",
                    "data": {"count": pm_row["cnt"]},
                }
            )

    except Exception as e:
        logger.debug("Learning feed failed: %s", e)

    # Check for scaffolded skills (look for auto-generated files)
    try:
        from pathlib import Path

        skills_dir = Path(__file__).parent.parent / "skills"
        for skill_dir in skills_dir.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_md = skill_dir / "skill.md"
            if skill_md.exists():
                content = skill_md.read_text(encoding="utf-8")
                if "generated_by: auto" in content:
                    events.append(
                        {
                            "type": "skill_scaffolded",
                            "description": f"Auto-generated skill: {skill_dir.name}",
                            "data": {"name": skill_dir.name},
                        }
                    )
    except Exception:
        pass

    return {"events": events, "days": days}


@router.get("/plan-templates")
async def list_plan_templates(_auth=Depends(verify_token)):
    """List all investigation plan templates."""
    from ..plan_templates import list_templates

    return {"templates": list_templates()}


@router.get("/plan-templates/{incident_type}")
async def get_plan_template(incident_type: str, _auth=Depends(verify_token)):
    """Get a single plan template by incident type."""
    from ..plan_templates import get_template

    template = get_template(incident_type)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    return {
        "id": template.id,
        "name": template.name,
        "incident_type": template.incident_type,
        "max_total_duration": template.max_total_duration,
        "phases": [
            {
                "id": p.id,
                "skill_name": p.skill_name,
                "required": p.required,
                "depends_on": p.depends_on,
                "timeout_seconds": p.timeout_seconds,
                "produces": p.produces,
                "branch_on": p.branch_on,
                "branches": p.branches,
                "parallel_with": p.parallel_with,
                "approval_required": p.approval_required,
                "runs": p.runs,
            }
            for p in template.phases
        ],
    }


@router.put("/plan-templates/{incident_type}")
async def update_plan_template(incident_type: str, request: Request, _auth=Depends(verify_token)):
    """Update an existing plan template. Rewrites the YAML file and reloads."""
    import re
    from pathlib import Path

    import yaml

    from ..plan_templates import get_template, load_templates

    template = get_template(incident_type)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    body = await request.json()

    # Validate incident_type for path safety
    if not re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", incident_type):
        raise HTTPException(status_code=400, detail="Invalid incident type")

    templates_dir = Path(__file__).parent.parent / "plan_templates"
    # Find the YAML file for this template
    target_path = None
    for path in templates_dir.glob("*.yaml"):
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if data.get("incident_type") == incident_type or data.get("id") == template.id:
                target_path = path
                break
        except Exception:
            continue

    if not target_path:
        raise HTTPException(status_code=404, detail="Template file not found")

    # Verify resolved path stays within templates directory
    if not str(target_path.resolve()).startswith(str(templates_dir.resolve())):
        raise HTTPException(status_code=400, detail="Invalid path")

    # Build updated YAML
    updated = {
        "id": template.id,
        "name": body.get("name", template.name),
        "incident_type": incident_type,
        "max_total_duration": body.get("max_total_duration", template.max_total_duration),
        "phases": body.get(
            "phases",
            [
                {
                    "id": p.id,
                    "skill_name": p.skill_name,
                    "required": p.required,
                    "depends_on": p.depends_on,
                    "timeout_seconds": p.timeout_seconds,
                    "produces": p.produces,
                    "branch_on": p.branch_on,
                    "branches": p.branches,
                    "parallel_with": p.parallel_with,
                    "approval_required": p.approval_required,
                    "runs": p.runs,
                }
                for p in template.phases
            ],
        ),
    }

    with open(target_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(updated, f, default_flow_style=False, sort_keys=False)

    load_templates()
    logger.info("Updated plan template: %s", incident_type)
    return {"status": "updated", "incident_type": incident_type}


@router.delete("/plan-templates/{incident_type}")
async def delete_plan_template(incident_type: str, _auth=Depends(verify_token)):
    """Delete a plan template. Only auto-generated templates can be deleted."""
    from pathlib import Path

    import yaml

    from ..plan_templates import get_template, load_templates

    template = get_template(incident_type)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    # Only allow deleting auto-generated templates
    if not template.id.startswith("auto-"):
        raise HTTPException(status_code=403, detail="Cannot delete built-in templates")

    templates_dir = Path(__file__).parent.parent / "plan_templates"
    target_path = None
    for path in templates_dir.glob("*.yaml"):
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if data.get("incident_type") == incident_type or data.get("id") == template.id:
                target_path = path
                break
        except Exception:
            continue

    if not target_path:
        raise HTTPException(status_code=404, detail="Template file not found")

    if not str(target_path.resolve()).startswith(str(templates_dir.resolve())):
        raise HTTPException(status_code=400, detail="Invalid path")

    target_path.unlink()
    load_templates()
    logger.info("Deleted plan template: %s", incident_type)
    return {"status": "deleted", "incident_type": incident_type}


@router.get("/analytics/plans")
async def plan_analytics(
    days: int = Query(30, ge=1, le=365),
    _auth=Depends(verify_token),
):
    """Plan execution analytics — template usage, phase success rates, durations."""
    try:
        from ..db import get_database

        db = get_database()

        # Overall stats
        rows = db.fetchall(
            "SELECT template_name, incident_type, status, "
            "COUNT(*) as count, "
            "AVG(total_duration_ms) as avg_duration_ms, "
            "AVG(phases_completed::float / NULLIF(phases_total, 0)) as avg_completion_rate, "
            "AVG(confidence) as avg_confidence "
            "FROM plan_executions "
            "WHERE timestamp > NOW() - INTERVAL '%s days' "
            "GROUP BY template_name, incident_type, status "
            "ORDER BY count DESC",
            (days,),
        )

        # Phase-level breakdown
        phase_rows = db.fetchall(
            "SELECT "
            "template_name, "
            "phase->>'phase_id' as phase_id, "
            "phase->>'status' as phase_status, "
            "COUNT(*) as count, "
            "AVG((phase->>'confidence')::float) as avg_confidence "
            "FROM plan_executions, jsonb_array_elements(phase_details) as phase "
            "WHERE timestamp > NOW() - INTERVAL '%s days' "
            "GROUP BY template_name, phase->>'phase_id', phase->>'status' "
            "ORDER BY template_name, phase_id",
            (days,),
        )

        # Build response
        by_template: dict[str, Any] = {}
        for r in rows or []:
            name = r["template_name"]
            if name not in by_template:
                by_template[name] = {
                    "template_name": name,
                    "incident_type": r["incident_type"],
                    "total_runs": 0,
                    "by_status": {},
                    "avg_duration_ms": 0,
                    "avg_completion_rate": 0,
                    "avg_confidence": 0,
                }
            entry = by_template[name]
            entry["by_status"][r["status"]] = r["count"]
            entry["total_runs"] += r["count"]
            entry["avg_duration_ms"] = int(r["avg_duration_ms"] or 0)
            entry["avg_completion_rate"] = round(float(r["avg_completion_rate"] or 0), 2)
            entry["avg_confidence"] = round(float(r["avg_confidence"] or 0), 2)

        # Phase breakdown per template
        phases_by_template: dict[str, list] = {}
        for r in phase_rows or []:
            name = r["template_name"]
            phases_by_template.setdefault(name, []).append(
                {
                    "phase_id": r["phase_id"],
                    "status": r["phase_status"],
                    "count": r["count"],
                    "avg_confidence": round(float(r["avg_confidence"] or 0), 2),
                }
            )

        for name, phases in phases_by_template.items():
            if name in by_template:
                by_template[name]["phases"] = phases

        return {
            "templates": list(by_template.values()),
            "total_executions": sum(t["total_runs"] for t in by_template.values()),
            "days": days,
        }
    except Exception as e:
        logger.debug("Plan analytics failed: %s", e, exc_info=True)
        return {"templates": [], "total_executions": 0, "days": days}
