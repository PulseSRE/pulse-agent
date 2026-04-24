"""Topology and blast-radius analysis REST endpoints."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel as _BaseModel
from pydantic import Field

from ..k8s_client import user_token_context
from .auth import get_user_token, verify_token

logger = logging.getLogger("pulse_agent.api")

router = APIRouter(tags=["topology"])


# ── Topology / Dependency Graph ────────────────────────────────────────────


@router.get("/topology")
async def get_topology(
    namespace: str | None = Query(None),
    kinds: str = Query(""),
    relationships: str = Query(""),
    layout_hint: str = Query(""),
    include_metrics: bool = Query(False),
    group_by: str = Query(""),
    user_token: str | None = Depends(get_user_token),
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
    temp_nodes: list[dict] = []
    for key, node in graph.get_nodes().items():
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

        temp_nodes.append(node_data)

    # Namespace filtering: filter nodes and edges to requested namespace
    if namespace:
        # Include nodes in the requested namespace, plus cluster-scoped kinds if explicitly requested
        nodes = [
            n
            for n in temp_nodes
            if n.get("namespace", "") == namespace
            or (n.get("namespace", "") == "" and kind_set and n.get("kind") in cluster_scoped)
        ]
    else:
        nodes = temp_nodes

    # Metrics enrichment
    if include_metrics:
        import asyncio

        from ..dependency_graph import _fetch_metrics

        def _fetch_with_token():
            with user_token_context(user_token):
                return _fetch_metrics(namespace or "")

        node_met, pod_met = await asyncio.to_thread(_fetch_with_token)
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
    namespace: str = Query("", description="Filter results to namespace (optional)"),
    user_token: str | None = Depends(get_user_token),
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
        # Namespace filtering
        if namespace and dep_node.namespace != namespace and dep_node.namespace != "":
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
    user_token: str | None = Depends(get_user_token),
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
async def simulate_with_blast_radius(
    body: _SimulateRequest, user_token: str | None = Depends(get_user_token), _auth=Depends(verify_token)
):
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
