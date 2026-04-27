"""Monitor-related REST endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .auth import verify_token

logger = logging.getLogger("pulse_agent.api")

router = APIRouter()


@router.get("/briefing")
async def rest_briefing(hours: int = Query(12, ge=1, le=72), _auth=Depends(verify_token)):
    """Cluster activity briefing for the last N hours. Requires token auth."""
    from ..monitor import get_briefing

    return await asyncio.to_thread(get_briefing, hours)


def _compute_kpi_dashboard_sync(days: int) -> dict:
    """Sync helper for KPI computation — runs off the event loop via to_thread."""
    from .. import db

    kpis: dict[str, dict] = {}

    try:
        database = db.get_database()

        # 1. MTTD — Mean Time to Detect (scan interval proxy)
        from ..config import get_settings

        kpis["mttd"] = {
            "label": "Mean Time to Detect",
            "value": get_settings().monitor.scan_interval,
            "unit": "seconds",
            "target": 30,
            "status": "pass" if get_settings().monitor.scan_interval <= 60 else "fail",
        }

        # 2. MTTR — Mean Time to Remediate
        mttr_row = database.fetchone(
            "SELECT AVG(duration_ms) as avg_ms FROM actions "
            "WHERE status = 'completed' "
            "AND timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * ?)::BIGINT * 1000",
            (days,),
        )
        mttr_seconds = int((mttr_row["avg_ms"] or 0) / 1000) if mttr_row else 0
        has_remediations = mttr_row and mttr_row["avg_ms"] is not None
        kpis["mttr"] = {
            "label": "Mean Time to Remediate",
            "value": mttr_seconds,
            "unit": "seconds",
            "target": 300,
            "status": "info"
            if not has_remediations
            else "pass"
            if mttr_seconds <= 300
            else "warn"
            if mttr_seconds <= 600
            else "fail",
        }

        # 3. Auto-remediation success rate
        fix_row = database.fetchone(
            "SELECT COUNT(*) FILTER (WHERE status = 'completed') AS good, "
            "COUNT(*) AS total FROM actions "
            "WHERE timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * ?)::BIGINT * 1000",
            (days,),
        )
        fix_total = fix_row["total"] if fix_row else 0
        fix_rate = round(fix_row["good"] / max(fix_total, 1), 3) if fix_row else 0
        kpis["auto_fix_success"] = {
            "label": "Auto-Remediation Success",
            "value": fix_rate,
            "unit": "ratio",
            "target": 0.85,
            "status": "info"
            if fix_total == 0
            else "pass"
            if fix_rate >= 0.85
            else "warn"
            if fix_rate >= 0.70
            else "fail",
            "sample_count": fix_total,
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


@router.get("/kpi")
async def get_kpi_dashboard(
    days: int = Query(7, ge=1, le=90),
    _auth=Depends(verify_token),
):
    """Operational KPIs — 9 metrics aligned with ORCA targets."""
    return await asyncio.to_thread(_compute_kpi_dashboard_sync, days)


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


_VALID_SLO_TYPES = {"availability", "latency", "error_rate"}


@router.post("/slo")
async def register_slo(request: Request, _auth=Depends(verify_token)):
    """Register a new SLO definition."""
    from ..slo_registry import SLODefinition, get_slo_registry

    body = await request.json()

    service_name = body.get("service", "")
    if not service_name:
        raise HTTPException(status_code=400, detail="service name required")

    # Validate slo_type
    slo_type = body.get("type", "")
    if slo_type not in _VALID_SLO_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"slo_type must be one of: {', '.join(sorted(_VALID_SLO_TYPES))}",
        )

    # Validate target (must be a float in (0.0, 100.0) exclusive)
    try:
        target = float(body.get("target", ""))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="target must be a number between 0.0 and 100.0 (exclusive)")
    if target <= 0.0 or target >= 100.0:
        raise HTTPException(status_code=422, detail="target must be between 0.0 and 100.0 (exclusive)")

    # Validate window_days (positive integer, max 90)
    try:
        window_days = int(body.get("window_days", 30))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="window_days must be a positive integer (max 90)")
    if window_days < 1 or window_days > 90:
        raise HTTPException(status_code=422, detail="window_days must be between 1 and 90")

    slo = SLODefinition(
        service_name=service_name,
        slo_type=slo_type,
        target=target,
        window_days=window_days,
        description=body.get("description", ""),
    )

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
                logger.debug("Failed to parse routing decision for learning feed", exc_info=True)

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
        logger.debug("Failed to scan for scaffolded skills", exc_info=True)

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


def _build_activity_events(database, days: int = 7) -> list[dict]:
    """Aggregate recent agent activity into plain-English events."""
    try:
        events: list[dict] = []

        actions = database.fetchall(
            "SELECT "
            "COALESCE(category, 'unknown') as category, "
            "COALESCE(namespace, '') as namespace, "
            "COUNT(*) as cnt, "
            "status "
            "FROM actions "
            "WHERE timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * %s)::BIGINT * 1000 "
            "AND status IN ('completed', 'failed', 'rolled_back') "
            "GROUP BY category, namespace, status "
            "ORDER BY cnt DESC",
            (days,),
        )
        for row in actions or []:
            cat = row["category"].replace("_", " ")
            ns = row["namespace"] or "cluster-wide"
            status = row["status"]
            verb = "Auto-fixed" if status == "completed" else "Failed to fix" if status == "failed" else "Rolled back"
            events.append(
                {
                    "type": "auto_fix" if status == "completed" else "fix_failed" if status == "failed" else "rollback",
                    "description": f"{verb} {row['cnt']} {cat} issue{'s' if row['cnt'] != 1 else ''} in {ns}",
                    "link": "/incidents?tab=actions",
                    "count": row["cnt"],
                    "category": row["category"],
                    "namespace": row["namespace"],
                }
            )

        healed = database.fetchall(
            "SELECT COUNT(*) as cnt FROM findings "
            "WHERE resolved = 1 "
            "AND id NOT IN (SELECT finding_id FROM actions WHERE finding_id IS NOT NULL) "
            "AND timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * %s)::BIGINT * 1000",
            (days,),
        )
        healed_count = healed[0]["cnt"] if healed and healed[0]["cnt"] else 0
        if healed_count > 0:
            events.append(
                {
                    "type": "self_healed",
                    "description": f"{healed_count} finding{'s' if healed_count != 1 else ''} resolved without intervention",
                    "link": "/incidents",
                    "count": healed_count,
                }
            )

        postmortems = database.fetchall(
            "SELECT COUNT(*) as cnt, "
            "MAX(summary) as latest_summary "
            "FROM postmortems "
            "WHERE created_at >= NOW() - INTERVAL '%s days'",
            (days,),
        )
        pm_count = postmortems[0]["cnt"] if postmortems and postmortems[0]["cnt"] else 0
        if pm_count > 0:
            summary = postmortems[0].get("latest_summary", "")
            desc = f"Generated {pm_count} postmortem{'s' if pm_count != 1 else ''}"
            if summary:
                desc += f" \u2014 latest: {summary[:60]}"
            events.append(
                {
                    "type": "postmortem",
                    "description": desc,
                    "link": "/incidents?tab=postmortems",
                    "count": pm_count,
                }
            )

        investigations = database.fetchall(
            "SELECT finding_type, target, COUNT(*) as cnt "
            "FROM findings "
            "WHERE investigated = 1 "
            "AND timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * %s)::BIGINT * 1000 "
            "GROUP BY finding_type, target "
            "ORDER BY cnt DESC "
            "LIMIT 5",
            (days,),
        )
        for row in investigations or []:
            ft = (row["finding_type"] or "issue").replace("_", " ")
            target = row["target"] or ""
            desc = f"Investigated {ft}"
            if target:
                desc += f" on {target}"
            events.append(
                {
                    "type": "investigation",
                    "description": desc,
                    "link": "/incidents",
                    "count": row["cnt"],
                }
            )

        return events

    except Exception:
        logger.debug("Activity event aggregation failed", exc_info=True)
        return []


def _get_agent_activity_sync(days: int) -> dict:
    """Sync helper for activity endpoint."""
    from .. import db

    try:
        database = db.get_database()
        events = _build_activity_events(database, days)
        return {"events": events, "period_days": days}
    except Exception:
        return {"events": [], "period_days": days}


@router.get("/activity")
async def get_agent_activity(
    days: int = Query(7, ge=1, le=90),
    _auth=Depends(verify_token),
):
    """Recent agent activity for the Overview tab."""
    return await asyncio.to_thread(_get_agent_activity_sync, days)
