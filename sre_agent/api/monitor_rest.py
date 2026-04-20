"""Monitor-related REST endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel as _BaseModel
from pydantic import Field

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


# ── Topology / Dependency Graph ────────────────────────────────────────────


@router.get("/topology")
async def get_topology(
    namespace: str | None = Query(None),
    kinds: str = Query(""),
    relationships: str = Query(""),
    layout_hint: str = Query(""),
    include_metrics: bool = Query(False),
    group_by: str = Query(""),
    _auth=Depends(verify_token),
):
    """Return the dependency graph as nodes + edges for visualization.

    Supports perspective filtering via kinds/relationships/layout_hint params.
    Used by the perspective quick-launch pills for instant view switching.
    """
    from ..view_tools import VALID_LAYOUT_HINTS, VALID_TOPOLOGY_KINDS, VALID_TOPOLOGY_RELATIONSHIPS

    kind_set: set[str] | None = None
    if kinds:
        kind_set = {k.strip() for k in kinds.split(",") if k.strip()}
        invalid = kind_set - VALID_TOPOLOGY_KINDS
        if invalid:
            return {"error": f"Invalid kinds: {', '.join(sorted(invalid))}"}

    rel_set: set[str] | None = None
    if relationships:
        rel_set = {r.strip() for r in relationships.split(",") if r.strip()}
        invalid = rel_set - VALID_TOPOLOGY_RELATIONSHIPS
        if invalid:
            return {"error": f"Invalid relationships: {', '.join(sorted(invalid))}"}

    if layout_hint and layout_hint not in VALID_LAYOUT_HINTS:
        return {"error": f"Invalid layout_hint: {layout_hint}"}

    from ..dependency_graph import get_dependency_graph

    graph = get_dependency_graph()
    nodes = []
    edges = []

    finding_status: dict[str, str] = {}
    try:
        from ..db import get_database

        db = get_database()
        active_findings = db.fetchall("SELECT category, severity, resources FROM findings WHERE resolved = 0")
        for f in active_findings or []:
            sev = f.get("severity", "")
            for res_str in (f.get("resources") or "").split(","):
                res_str = res_str.strip()
                if res_str:
                    finding_status[res_str] = "error" if sev in ("critical", "warning") else "warning"
    except Exception:
        pass

    risk_scores: dict[str, int] = {}
    try:
        from ..change_risk import score_deployment_change

        risk_findings = db.fetchall(
            "SELECT resources, category FROM findings "
            "WHERE category = 'audit_deployment' AND resolved = 0 "
            "AND timestamp > EXTRACT(EPOCH FROM NOW() - INTERVAL '2 hours')::BIGINT * 1000"
        )
        for f in risk_findings or []:
            for res_str in (f.get("resources") or "").split(","):
                res_str = res_str.strip()
                if not res_str:
                    continue
                parts = res_str.split("/", 1)
                ns = parts[0] if len(parts) == 2 else ""
                name = parts[1] if len(parts) == 2 else parts[0]
                assessment = score_deployment_change(deployment_name=name, namespace=ns)
                risk_scores[res_str] = assessment.score
    except Exception:
        pass

    recent_changes: set[str] = set()
    try:
        recent = db.fetchall(
            "SELECT resources FROM findings "
            "WHERE category = 'audit_deployment' "
            "AND timestamp > EXTRACT(EPOCH FROM NOW() - INTERVAL '15 minutes')::BIGINT * 1000"
        )
        for f in recent or []:
            for res_str in (f.get("resources") or "").split(","):
                if res_str.strip():
                    recent_changes.add(res_str.strip())
    except Exception:
        pass

    cluster_scoped = {"Node", "HPA"}
    for key, node in graph.get_nodes().items():
        if namespace and node.namespace != namespace:
            if not (kind_set and node.kind in kind_set and node.kind in cluster_scoped):
                continue
        if kind_set and node.kind not in kind_set:
            continue

        resource_key = f"{node.kind}:{node.namespace}:{node.name}"
        status = finding_status.get(resource_key, "healthy")
        risk = risk_scores.get(resource_key, 0)

        node_data: dict[str, Any] = {
            "id": key,
            "kind": node.kind,
            "name": node.name,
            "namespace": node.namespace,
            "status": status,
        }
        if risk > 0:
            node_data["risk"] = risk
            node_data["riskLevel"] = (
                "critical" if risk >= 70 else "high" if risk >= 50 else "medium" if risk >= 25 else "low"
            )
        if resource_key in recent_changes:
            node_data["recentlyChanged"] = True
        if group_by:
            if group_by == "namespace":
                node_data["group"] = node.namespace or "cluster-scoped"
            elif group_by == "node":
                if node.kind == "Node":
                    node_data["group"] = node.name
                else:
                    parent_node = None
                    for edge in graph.get_edges():
                        if edge.target == key and edge.relationship == "schedules":
                            src = graph.get_node(edge.source)
                            if src and src.kind == "Node":
                                parent_node = src.name
                                break
                    node_data["group"] = parent_node or "unscheduled"
            else:
                node_data["group"] = node.labels.get(group_by, "unlabeled")

        nodes.append(node_data)

    # Metrics enrichment
    if include_metrics:
        from ..dependency_graph import _fetch_metrics

        node_met, pod_met = _fetch_metrics(namespace or "")
        for n in nodes:
            if n["kind"] == "Node":
                m = node_met.get(n["name"])
                if m:
                    cpu_pct = round(m["cpu_usage_m"] * 100 / m["cpu_capacity_m"]) if m["cpu_capacity_m"] else 0
                    mem_pct = round(m["memory_usage_b"] * 100 / m["memory_capacity_b"]) if m["memory_capacity_b"] else 0
                    n["metrics"] = {
                        "cpu_usage": m["cpu_usage"],
                        "cpu_capacity": m["cpu_capacity"],
                        "cpu_percent": cpu_pct,
                        "memory_usage": m["memory_usage"],
                        "memory_capacity": m["memory_capacity"],
                        "memory_percent": mem_pct,
                    }
            elif n["kind"] == "Pod":
                key = f"{n['namespace']}/{n['name']}"
                m = pod_met.get(key)
                if m:
                    n["metrics"] = {
                        "cpu_usage": m["cpu_usage"],
                        "memory_usage": m["memory_usage"],
                        "cpu_percent": 0,
                        "memory_percent": 0,
                    }

    # Group size capping
    _MAX_GROUP_SIZE = 20
    if group_by:
        groups: dict[str, list[dict]] = {}
        for n in nodes:
            g = n.get("group", "")
            if g not in groups:
                groups[g] = []
            groups[g].append(n)
        capped: list[dict] = []
        for g, members in groups.items():
            if len(members) <= _MAX_GROUP_SIZE:
                capped.extend(members)
            else:
                capped.extend(members[:_MAX_GROUP_SIZE])
                overflow = len(members) - _MAX_GROUP_SIZE
                capped.append(
                    {
                        "id": f"_summary/{g}",
                        "kind": "Summary",
                        "name": f"+ {overflow} more",
                        "namespace": members[0]["namespace"],
                        "status": "healthy",
                        "group": g,
                    }
                )
        nodes = capped

    node_ids = {n["id"] for n in nodes}
    node_kinds = {n["kind"] for n in nodes}

    for edge in graph.get_edges():
        if edge.source not in node_ids or edge.target not in node_ids:
            continue
        if rel_set and edge.relationship not in rel_set:
            continue
        if kind_set and not rel_set:
            src_node = graph.get_node(edge.source)
            tgt_node = graph.get_node(edge.target)
            if src_node and tgt_node:
                if src_node.kind not in node_kinds or tgt_node.kind not in node_kinds:
                    continue
        edges.append(
            {
                "source": edge.source,
                "target": edge.target,
                "relationship": edge.relationship,
            }
        )

    kind_counts: dict[str, int] = {}
    for n in nodes:
        kind_counts[n["kind"]] = kind_counts.get(n["kind"], 0) + 1

    return {
        "kind": "topology",
        "title": f"Topology — {namespace}" if namespace else "Topology",
        "description": f"{len(nodes)} resources, {len(edges)} relationships",
        "layout_hint": layout_hint or "top-down",
        "include_metrics": include_metrics,
        "group_by": group_by,
        "nodes": nodes,
        "edges": edges,
        "summary": {
            "nodes": len(nodes),
            "edges": len(edges),
            "kinds": kind_counts,
            "last_refresh": graph._last_refresh,
        },
    }


@router.get("/topology/blast-radius")
async def get_blast_radius(
    node_id: str = Query(..., description="Node ID from topology graph"),
    _auth=Depends(verify_token),
):
    """Compute blast radius tree for a selected node — 'What if this goes down?'"""
    from ..dependency_graph import get_dependency_graph

    graph = get_dependency_graph()
    parts = node_id.split(":", 2)
    if len(parts) == 3:
        kind, ns, name = parts
    else:
        kind, ns, name = node_id, "", ""
    downstream = graph.downstream_blast_radius(kind, ns, name)

    # Build tree structure grouped by impact type
    tree: list[dict] = []
    for dep_id in downstream:
        dep_node = graph.get_node(dep_id)
        if not dep_node:
            continue
        # Find the edge connecting to this node
        edge_label = ""
        for e in graph.get_edges():
            if e.source == node_id and e.target == dep_id:
                edge_label = e.relationship
                break
            if e.target == node_id and e.source == dep_id:
                edge_label = e.relationship
                break
        tree.append(
            {
                "id": dep_id,
                "kind": dep_node.kind,
                "name": dep_node.name,
                "namespace": dep_node.namespace,
                "relationship": edge_label,
            }
        )

    return {
        "source": node_id,
        "affected": len(tree),
        "resources": tree,
    }


# ── Incident Center ──────────────────────────────────────────────────────


def _parse_dep_id(graph, dep_id: str) -> dict:
    """Parse a dependency graph key into a structured resource dict.

    Tries ``graph.get_node()`` first; falls back to splitting the ID string
    (format ``Kind/namespace/name``).
    """
    node = graph.get_node(dep_id)
    if node:
        return {"id": dep_id, "kind": node.kind, "name": node.name, "namespace": node.namespace}
    parts = dep_id.split("/", 2)
    if len(parts) == 3:
        return {"id": dep_id, "kind": parts[0], "name": parts[2], "namespace": parts[1]}
    return {"id": dep_id, "kind": dep_id, "name": "", "namespace": ""}


def _get_finding_from_db(finding_id: str) -> dict | None:
    """Look up a finding by ID from the database.  Returns ``None`` if missing."""
    from ..db import get_database

    try:
        db = get_database()
        return db.fetchone("SELECT * FROM findings WHERE id = ?", (finding_id,))
    except Exception:
        logger.debug("Failed to fetch finding %s", finding_id)
        return None


@router.get("/incidents/{finding_id}/impact")
async def get_finding_impact(
    finding_id: str,
    kind: str = Query("", description="Resource kind (fallback if finding not in DB)"),
    name: str = Query("", description="Resource name"),
    namespace: str = Query("", description="Resource namespace"),
    _auth=Depends(verify_token),
):
    """Blast radius and dependency analysis for a single finding."""
    from fastapi.responses import JSONResponse

    from ..dependency_graph import get_dependency_graph

    res_kind, res_ns, res_name = kind, namespace, name

    if not res_kind:
        finding = _get_finding_from_db(finding_id)
        if finding:
            resources_raw = finding.get("resources") or finding.get("resource") or ""
            if isinstance(resources_raw, str):
                try:
                    import json as _json

                    parsed = _json.loads(resources_raw)
                    if isinstance(parsed, list) and parsed:
                        first = parsed[0]
                        res_kind = first.get("kind", "")
                        res_ns = first.get("namespace", "")
                        res_name = first.get("name", "")
                except (ValueError, TypeError):
                    first_str = resources_raw.split(",")[0].strip()
                    if first_str:
                        sep = ":" if ":" in first_str else "/"
                        parts = first_str.split(sep, 2)
                        if len(parts) == 3:
                            res_kind, res_ns, res_name = parts

    if not res_kind:
        return JSONResponse(
            status_code=404,
            content={"error": "Finding has no parseable resource for impact analysis"},
        )

    affected_resource = {"kind": res_kind, "name": res_name, "namespace": res_ns}

    try:
        graph = get_dependency_graph()
        downstream_ids = graph.downstream_blast_radius(res_kind, res_ns, res_name)
        upstream_ids = graph.upstream_dependencies(res_kind, res_ns, res_name)
    except Exception:
        downstream_ids = []
        upstream_ids = []

    blast_radius = [_parse_dep_id(graph, d) for d in downstream_ids]
    upstream_deps = [_parse_dep_id(graph, u) for u in upstream_ids]

    affected_pods = sum(1 for r in blast_radius if r.get("kind") == "Pod")
    namespaces = {r.get("namespace", "") for r in blast_radius if r.get("namespace")}
    scope = "cross-namespace" if len(namespaces) > 1 else "namespace-scoped"
    risk_level = "high" if len(downstream_ids) > 10 else "medium" if len(downstream_ids) > 3 else "low"

    return {
        "finding_id": finding_id,
        "affected_resource": affected_resource,
        "blast_radius": blast_radius,
        "upstream_dependencies": upstream_deps,
        "affected_pods": affected_pods,
        "scope": scope,
        "risk_level": risk_level,
    }


@router.get("/incidents/{finding_id}/learning")
async def get_finding_learning(
    finding_id: str,
    category: str = Query("", description="Finding category (fallback if finding not in DB)"),
    _auth=Depends(verify_token),
):
    """Aggregate all learning artifacts linked to a finding."""
    cat = category
    if not cat:
        finding = _get_finding_from_db(finding_id)
        cat = (finding.get("category") or "") if finding else ""

    from pathlib import Path

    from ..db import get_database

    result: dict[str, Any] = {"finding_id": finding_id}
    base_dir = Path(__file__).parent.parent

    # (a) Scaffolded skill
    result["scaffolded_skill"] = None
    if cat:
        try:
            skill_path = base_dir / "skills" / cat / "skill.md"
            if skill_path.exists():
                content = skill_path.read_text(encoding="utf-8")
                if "generated_by:" in content and "auto" in content:
                    result["scaffolded_skill"] = {
                        "name": cat,
                        "path": f"sre_agent/skills/{cat}/skill.md",
                    }
        except OSError:
            pass

    # (b) Scaffolded plan template
    result["scaffolded_plan"] = None
    if cat:
        try:
            import yaml

            plan_path = base_dir / "plan_templates" / f"{cat}.yaml"
            if plan_path.exists():
                data = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
                if data:
                    result["scaffolded_plan"] = {
                        "name": data.get("name", cat),
                        "incident_type": data.get("incident_type", cat),
                        "phases": len(data.get("phases", [])),
                    }
        except (OSError, ValueError):
            pass

    # (c) Scaffolded eval
    result["scaffolded_eval"] = None
    if cat:
        try:
            scaffolded_path = base_dir / "evals" / "scenarios_data" / "scaffolded.json"
            if scaffolded_path.exists():
                scenarios = json.loads(scaffolded_path.read_text(encoding="utf-8"))
                for sc in scenarios:
                    if cat in sc.get("scenario_id", ""):
                        result["scaffolded_eval"] = {
                            "scenario_id": sc["scenario_id"],
                            "tool_calls": len(sc.get("tool_calls", sc.get("expected_tools", []))),
                        }
                        break
        except (OSError, ValueError):
            pass

    # (d) Learned runbook + (e) Detected patterns — single store instance
    result["learned_runbook"] = None
    result["detected_patterns"] = None
    if cat:
        try:
            from ..memory.store import IncidentStore

            store = IncidentStore()
            runbooks = store.find_runbooks(cat, limit=1)
            if runbooks:
                rb = runbooks[0]
                tool_seq = rb.get("tool_sequence", "[]")
                if isinstance(tool_seq, str):
                    tool_seq = json.loads(tool_seq)
                result["learned_runbook"] = {
                    "name": rb.get("name", ""),
                    "success_count": rb.get("success_count", 0),
                    "tool_sequence": [t.get("tool", t) if isinstance(t, dict) else t for t in tool_seq][:10],
                }
            patterns = store.search_patterns(cat, limit=5)
            if patterns:
                result["detected_patterns"] = [
                    {
                        "type": p.get("pattern_type", ""),
                        "description": p.get("description", ""),
                        "frequency": p.get("frequency", 0),
                    }
                    for p in patterns
                ]
        except Exception:
            logger.debug("Failed to query memory store for category %s", cat)

    # (f) Confidence delta + (g) Weight impact — batched DB access
    result["confidence_delta"] = None
    result["weight_impact"] = None
    try:
        db = get_database()

        inv_row = db.fetchone(
            "SELECT confidence FROM investigations WHERE finding_id = ? ORDER BY timestamp DESC LIMIT 1",
            (finding_id,),
        )
        if inv_row and inv_row.get("confidence") is not None:
            before_conf = float(inv_row["confidence"])
            ver_row = db.fetchone(
                "SELECT verification_status FROM actions WHERE finding_id = ? AND verification_status IS NOT NULL "
                "ORDER BY timestamp DESC LIMIT 1",
                (finding_id,),
            )
            if ver_row:
                after_conf = (
                    min(1.0, before_conf + 0.05) if ver_row["verification_status"] == "verified" else before_conf
                )
                result["confidence_delta"] = {
                    "before": round(before_conf, 2),
                    "after": round(after_conf, 2),
                    "delta": round(after_conf - before_conf, 2),
                }

        if cat:
            weight_row = db.fetchone(
                "SELECT channel_weights FROM skill_selection_log "
                "WHERE session_id = '__weight_snapshot__' "
                "ORDER BY timestamp DESC LIMIT 1"
            )
            if weight_row and weight_row.get("channel_weights"):
                weights = weight_row["channel_weights"]
                if isinstance(weights, str):
                    weights = json.loads(weights)
                from ..skill_selector import DEFAULT_WEIGHTS

                best_ch = None
                best_delta = 0.0
                for ch, w in weights.items():
                    default = DEFAULT_WEIGHTS.get(ch, 0.0)
                    delta = abs(w - default)
                    if delta > best_delta:
                        best_delta = delta
                        best_ch = ch
                if best_ch and best_delta > 0.001:
                    result["weight_impact"] = {
                        "channel": best_ch,
                        "old_weight": round(DEFAULT_WEIGHTS.get(best_ch, 0.0), 4),
                        "new_weight": round(weights.get(best_ch, 0.0), 4),
                    }
    except Exception:
        logger.debug("Failed to compute confidence/weight data for %s", finding_id)

    return result


class _SimulateRequest(_BaseModel):
    model_config = {"populate_by_name": True}
    tool: str
    tool_input: dict = Field(default={}, alias="input")
    target_resource: dict | None = None


@router.post("/monitor/simulate")
async def simulate_with_blast_radius(body: _SimulateRequest, _auth=Depends(verify_token)):
    """Simulate a tool action and enrich with fix blast radius analysis."""
    from ..monitor.investigations import simulate_action

    sim = simulate_action(body.tool, body.tool_input)

    fix_blast_radius: list[dict] = []
    fix_upstream_deps: list[dict] = []

    if body.target_resource:
        kind = body.target_resource.get("kind", "")
        ns = body.target_resource.get("namespace", "")
        name = body.target_resource.get("name", "")
        if kind and name:
            try:
                from ..dependency_graph import get_dependency_graph

                graph = get_dependency_graph()
                downstream_ids = graph.downstream_blast_radius(kind, ns, name)
                upstream_ids = graph.upstream_dependencies(kind, ns, name)
                fix_blast_radius = [_parse_dep_id(graph, d) for d in downstream_ids]
                fix_upstream_deps = [_parse_dep_id(graph, u) for u in upstream_ids]
            except Exception:
                pass

    sim["fixBlastRadius"] = fix_blast_radius
    sim["fixUpstreamDeps"] = fix_upstream_deps
    return sim


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
