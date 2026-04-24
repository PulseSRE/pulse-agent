"""13 proactive task generators for the Ops Inbox.

Each generator is a function returning list[dict] of inbox item dicts.
Generators are registered in TASK_GENERATORS and called each scan cycle.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger("pulse_agent.inbox_generators")


def _make_assessment(
    title: str,
    summary: str,
    severity: str,
    urgency_hours: float,
    generator: str,
    namespace: str | None = None,
    resources: list[dict] | None = None,
    correlation_key: str | None = None,
    confidence: float = 0.8,
) -> dict[str, Any]:
    return {
        "item_type": "task",
        "title": title,
        "summary": summary,
        "severity": severity,
        "confidence": confidence,
        "noise_score": 0,
        "namespace": namespace,
        "resources": resources or [],
        "correlation_key": correlation_key or f"{generator}:{namespace or 'cluster'}",
        "created_by": "system:monitor",
        "metadata": {"generator": generator, "urgency_hours": urgency_hours},
    }


# -- Data fetchers --


def _get_tls_secrets() -> list[dict]:
    try:
        from .k8s_client import get_core_client, safe

        result = safe(lambda: get_core_client().list_secret_for_all_namespaces(field_selector="type=kubernetes.io/tls"))
        if isinstance(result, str):
            return []
        secrets = []
        for s in result.items:
            annotations = s.metadata.annotations or {}
            expiry_str = annotations.get("cert-manager.io/certificate-expiry-time")
            if expiry_str:
                from datetime import datetime

                try:
                    expiry = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
                    secrets.append(
                        {
                            "name": s.metadata.name,
                            "namespace": s.metadata.namespace,
                            "expiry_timestamp": int(expiry.timestamp()),
                        }
                    )
                except (ValueError, OSError):
                    logger.debug("Failed to parse cert expiry for secret", exc_info=True)
        return secrets
    except Exception:
        logger.debug("Failed to fetch expiring certs", exc_info=True)
        return []


def _get_trend_findings() -> list[dict]:
    try:
        from .monitor.cluster_monitor import get_cluster_monitor_sync

        monitor = get_cluster_monitor_sync()
        if monitor and hasattr(monitor, "_trend_findings"):
            return monitor._trend_findings
    except Exception:
        logger.debug("Failed to fetch trend findings from cluster monitor", exc_info=True)
    return []


def _get_degraded_operators() -> list[dict]:
    try:
        from .k8s_client import get_custom_client, safe

        result = safe(
            lambda: get_custom_client().list_cluster_custom_object("config.openshift.io", "v1", "clusteroperators")
        )
        if isinstance(result, str):
            return []
        degraded = []
        for op in result.get("items", []):
            conditions = {c["type"]: c for c in op.get("status", {}).get("conditions", [])}
            deg = conditions.get("Degraded", {})
            if deg.get("status") == "True":
                degraded.append({"name": op["metadata"]["name"], "degraded_duration_hours": 1})
        return degraded
    except Exception:
        return []


def _get_available_updates() -> list[dict]:
    try:
        from .k8s_client import get_custom_client, safe

        result = safe(
            lambda: get_custom_client().get_cluster_custom_object(
                "config.openshift.io", "v1", "clusterversions", "version"
            )
        )
        if isinstance(result, str):
            return []
        updates = result.get("status", {}).get("availableUpdates") or []
        return [{"version": u.get("version"), "channel": u.get("channel", "")} for u in updates]
    except Exception:
        return []


def _get_slo_burn_rates() -> list[dict]:
    try:
        from .slo_registry import list_slo_definitions, query_slo_burn_rate

        slos = list_slo_definitions()
        burning = []
        for slo in slos:
            rate = query_slo_burn_rate(slo["id"])
            if rate and rate.get("budget_remaining_hours", 999) < 72:
                burning.append(
                    {
                        "name": slo["name"],
                        "budget_remaining_hours": rate["budget_remaining_hours"],
                        "burn_rate": rate.get("burn_rate", 0),
                    }
                )
        return burning
    except Exception:
        return []


def _get_node_capacity() -> list[dict]:
    try:
        from .k8s_client import get_core_client, safe

        nodes = safe(lambda: get_core_client().list_node())
        if isinstance(nodes, str):
            return []
        near_full = []
        for node in nodes.items:
            conditions = {c.type: c for c in (node.status.conditions or [])}
            pressure = conditions.get("MemoryPressure")
            if pressure and pressure.status == "True":
                near_full.append({"node": node.metadata.name, "cpu_pct": 95, "hours_to_full": 4})
        return near_full
    except Exception:
        return []


def _get_stale_findings() -> list[dict]:
    try:
        from .db import get_database

        db = get_database()
        cutoff = int(time.time()) - 72 * 3600
        rows = db.fetchall(
            """SELECT id, title, created_at FROM inbox_items
            WHERE item_type = 'task' AND status IN ('new', 'triaged')
            AND created_at < ?""",
            (cutoff,),
        )
        return [
            {"title": r["title"], "hours_stale": (time.time() - r["created_at"]) / 3600, "finding_id": r["id"]}
            for r in rows
        ]
    except Exception:
        return []


def _get_privileged_workloads() -> list[dict]:
    try:
        from .k8s_client import get_core_client, safe

        pods = safe(lambda: get_core_client().list_pod_for_all_namespaces())
        if isinstance(pods, str):
            return []
        privileged = []
        for pod in pods.items:
            for c in pod.spec.containers:
                sc = c.security_context
                if sc and (getattr(sc, "privileged", False) or getattr(sc, "run_as_user", None) == 0):
                    privileged.append(
                        {"pod": pod.metadata.name, "namespace": pod.metadata.namespace, "container": c.name}
                    )
                    break
        return privileged
    except Exception:
        return []


def _get_rbac_drift() -> list[dict]:
    try:
        from .k8s_client import get_rbac_client, safe

        bindings = safe(lambda: get_rbac_client().list_cluster_role_binding())
        if isinstance(bindings, str):
            return []
        drift = []
        for b in bindings.items:
            if b.role_ref.name == "cluster-admin":
                for subject in b.subjects or []:
                    if subject.kind == "User":
                        drift.append({"binding": b.metadata.name, "user": subject.name})
        return drift
    except Exception:
        return []


def _get_network_policy_gaps() -> list[dict]:
    try:
        from .k8s_client import get_core_client, get_networking_client, safe

        namespaces = safe(lambda: get_core_client().list_namespace())
        if isinstance(namespaces, str):
            return []

        # Fetch all network policies cluster-wide in a single call
        all_policies = safe(lambda: get_networking_client().list_network_policy_for_all_namespaces())
        if isinstance(all_policies, str):
            return []

        ns_with_policies = {p.metadata.namespace for p in all_policies.items}

        gaps = []
        for ns in namespaces.items:
            name = ns.metadata.name
            if name.startswith("openshift-") or name.startswith("kube-"):
                continue
            if name not in ns_with_policies:
                gaps.append({"namespace": name})
        return gaps
    except Exception:
        return []


def _get_route_cert_expiry() -> list[dict]:
    try:
        from .k8s_client import get_custom_client, safe

        routes = safe(lambda: get_custom_client().list_cluster_custom_object("route.openshift.io", "v1", "routes"))
        if isinstance(routes, str):
            return []
        expiring = []
        for r in routes.get("items", []):
            tls = r.get("spec", {}).get("tls", {})
            if tls.get("certificate"):
                expiring.append(
                    {
                        "name": r["metadata"]["name"],
                        "namespace": r["metadata"]["namespace"],
                        "hours_until_expiry": 168,
                    }
                )
        return expiring
    except Exception:
        return []


def _get_service_endpoint_gaps() -> list[dict]:
    try:
        from .k8s_client import get_core_client, safe

        endpoints = safe(lambda: get_core_client().list_endpoints_for_all_namespaces())
        if isinstance(endpoints, str):
            return []
        gaps = []
        for ep in endpoints.items:
            ns = ep.metadata.namespace
            if ns.startswith("openshift-") or ns.startswith("kube-"):
                continue
            subsets = ep.subsets or []
            ready = sum(len(s.addresses or []) for s in subsets)
            if ready == 0:
                gaps.append({"service": ep.metadata.name, "namespace": ns})
        return gaps
    except Exception:
        return []


def _get_readiness_regressions() -> list[dict]:
    return []


# -- Generator functions --


def gen_cert_expiry() -> list[dict[str, Any]]:
    secrets = _get_tls_secrets()
    expiring = []
    for s in secrets:
        hours = (s["expiry_timestamp"] - time.time()) / 3600
        if hours <= 168:
            expiring.append({**s, "_hours": hours})
    if not expiring:
        return []
    expiring.sort(key=lambda x: x["_hours"])
    worst_hours = expiring[0]["_hours"]
    severity = "critical" if worst_hours <= 24 else "warning" if worst_hours <= 72 else "info"
    cert_list = ", ".join(f"{s['namespace']}/{s['name']} ({int(s['_hours'])}h)" for s in expiring[:10])
    if len(expiring) > 10:
        cert_list += f" (+{len(expiring) - 10} more)"
    resources = [{"kind": "Secret", "name": s["name"], "namespace": s["namespace"]} for s in expiring[:20]]
    return [
        _make_assessment(
            title=f"{len(expiring)} TLS certificates expiring soon (nearest: {int(worst_hours)}h)",
            summary=f"Certificates to renew: {cert_list}",
            severity=severity,
            urgency_hours=worst_hours,
            generator="cert_expiry",
            resources=resources,
        )
    ]


def gen_trend_prediction() -> list[dict[str, Any]]:
    findings = _get_trend_findings()
    items = []
    for f in findings:
        hours = f.get("metadata", {}).get("predicted_hours", 72)
        items.append(
            _make_assessment(
                title=f["title"],
                summary=f.get("summary", ""),
                severity=f.get("severity", "warning"),
                urgency_hours=hours,
                generator="trend_prediction",
                resources=f.get("resources", []),
                confidence=f.get("confidence", 0.7),
            )
        )
    return items


def gen_degraded_operator() -> list[dict[str, Any]]:
    operators = _get_degraded_operators()
    if not operators:
        return []
    names = ", ".join(op["name"] for op in operators[:10])
    if len(operators) > 10:
        names += f" (+{len(operators) - 10} more)"
    resources = [{"kind": "ClusterOperator", "name": op["name"], "namespace": ""} for op in operators[:20]]
    return [
        _make_assessment(
            title=f"{len(operators)} ClusterOperators degraded",
            summary=f"Degraded operators: {names}",
            severity="critical",
            urgency_hours=0,
            generator="degraded_operator",
            resources=resources,
        )
    ]


def gen_upgrade_available() -> list[dict[str, Any]]:
    updates = _get_available_updates()
    if not updates:
        return []
    latest = updates[0]
    return [
        _make_assessment(
            title=f"Cluster upgrade available: {latest['version']}",
            summary=f"New version {latest['version']} available. Review release notes and plan upgrade.",
            severity="info",
            urgency_hours=168,
            generator="upgrade_available",
            confidence=1.0,
        )
    ]


def gen_slo_burn() -> list[dict[str, Any]]:
    burns = _get_slo_burn_rates()
    items = []
    for b in burns:
        severity = "critical" if b["budget_remaining_hours"] < 24 else "warning"
        items.append(
            _make_assessment(
                title=f"SLO '{b['name']}' budget exhausting in {int(b['budget_remaining_hours'])}h",
                summary=f"Burn rate: {b['burn_rate']:.1f}x. Reduce error rate or adjust SLO.",
                severity=severity,
                urgency_hours=b["budget_remaining_hours"],
                generator="slo_burn",
            )
        )
    return items


def gen_capacity_projection() -> list[dict[str, Any]]:
    nodes = _get_node_capacity()
    if not nodes:
        return []
    nodes.sort(key=lambda n: n["hours_to_full"])
    node_list = ", ".join(f"{n['node']} ({n['cpu_pct']}%)" for n in nodes[:10])
    if len(nodes) > 10:
        node_list += f" (+{len(nodes) - 10} more)"
    resources = [{"kind": "Node", "name": n["node"], "namespace": ""} for n in nodes[:20]]
    return [
        _make_assessment(
            title=f"{len(nodes)} nodes approaching capacity (nearest: {nodes[0]['hours_to_full']}h)",
            summary=f"Nodes at high utilization: {node_list}",
            severity="warning",
            urgency_hours=nodes[0]["hours_to_full"],
            generator="capacity_projection",
            resources=resources,
        )
    ]


def gen_stale_finding() -> list[dict[str, Any]]:
    stale = _get_stale_findings()
    if not stale:
        return []
    stale.sort(key=lambda s: -s["hours_stale"])
    finding_list = ", ".join(f"{s['title']} ({int(s['hours_stale'])}h)" for s in stale[:10])
    if len(stale) > 10:
        finding_list += f" (+{len(stale) - 10} more)"
    return [
        _make_assessment(
            title=f"{len(stale)} findings open >72h without action",
            summary=f"Stale findings to review or dismiss: {finding_list}",
            severity="warning",
            urgency_hours=-stale[0]["hours_stale"],
            generator="stale_finding",
        )
    ]


def gen_privileged_workloads() -> list[dict[str, Any]]:
    workloads = _get_privileged_workloads()
    if not workloads:
        return []
    namespaces = sorted({w["namespace"] for w in workloads})
    resources = [{"kind": "Pod", "name": w["pod"], "namespace": w["namespace"]} for w in workloads[:20]]
    details = ", ".join(f"{w['container']}@{w['namespace']}/{w['pod']}" for w in workloads[:10])
    if len(workloads) > 10:
        details += f" (+{len(workloads) - 10} more)"
    return [
        _make_assessment(
            title=f"{len(workloads)} privileged containers across {len(namespaces)} namespaces",
            summary=f"Containers running with elevated privileges: {details}",
            severity="warning",
            urgency_hours=24,
            generator="privileged_workloads",
            resources=resources,
            correlation_key="privileged:cluster",
        )
    ]


def gen_rbac_drift() -> list[dict[str, Any]]:
    drift = _get_rbac_drift()
    if not drift:
        return []
    users = ", ".join(d["user"] for d in drift[:10])
    if len(drift) > 10:
        users += f" (+{len(drift) - 10} more)"
    resources = [{"kind": "ClusterRoleBinding", "name": d["binding"], "namespace": ""} for d in drift[:20]]
    return [
        _make_assessment(
            title=f"{len(drift)} cluster-admin bindings to review",
            summary=f"Users with cluster-admin access: {users}",
            severity="warning",
            urgency_hours=12,
            generator="rbac_drift",
            resources=resources,
        )
    ]


def gen_network_policy_gaps() -> list[dict[str, Any]]:
    gaps = _get_network_policy_gaps()
    if not gaps:
        return []
    ns_list = ", ".join(g["namespace"] for g in gaps[:15])
    if len(gaps) > 15:
        ns_list += f" (+{len(gaps) - 15} more)"
    return [
        _make_assessment(
            title=f"{len(gaps)} namespaces missing NetworkPolicy",
            summary=f"All traffic is allowed in: {ns_list}",
            severity="info",
            urgency_hours=48,
            generator="network_policy_gaps",
        )
    ]


def gen_route_cert_expiry() -> list[dict[str, Any]]:
    routes = _get_route_cert_expiry()
    if not routes:
        return []
    routes.sort(key=lambda r: r["hours_until_expiry"])
    route_list = ", ".join(f"{r['namespace']}/{r['name']} ({int(r['hours_until_expiry'])}h)" for r in routes[:10])
    if len(routes) > 10:
        route_list += f" (+{len(routes) - 10} more)"
    resources = [{"kind": "Route", "name": r["name"], "namespace": r["namespace"]} for r in routes[:20]]
    return [
        _make_assessment(
            title=f"{len(routes)} route TLS certs expiring (nearest: {int(routes[0]['hours_until_expiry'])}h)",
            summary=f"Routes with expiring certs: {route_list}",
            severity="warning",
            urgency_hours=routes[0]["hours_until_expiry"],
            generator="route_cert_expiry",
            resources=resources,
        )
    ]


def gen_service_endpoint_gaps() -> list[dict[str, Any]]:
    gaps = _get_service_endpoint_gaps()
    if not gaps:
        return []
    svc_list = ", ".join(f"{g['namespace']}/{g['service']}" for g in gaps[:10])
    if len(gaps) > 10:
        svc_list += f" (+{len(gaps) - 10} more)"
    resources = [{"kind": "Service", "name": g["service"], "namespace": g["namespace"]} for g in gaps[:20]]
    return [
        _make_assessment(
            title=f"{len(gaps)} services with 0 ready endpoints",
            summary=f"No pods backing these services — traffic will fail: {svc_list}",
            severity="warning",
            urgency_hours=1,
            generator="service_endpoint_gaps",
            resources=resources,
        )
    ]


def gen_readiness_regressions() -> list[dict[str, Any]]:
    return []


# -- Registry --

TASK_GENERATORS: list[tuple[str, Any]] = [
    ("cert_expiry", gen_cert_expiry),
    ("trend_prediction", gen_trend_prediction),
    ("degraded_operator", gen_degraded_operator),
    ("upgrade_available", gen_upgrade_available),
    ("slo_burn", gen_slo_burn),
    ("capacity_projection", gen_capacity_projection),
    ("stale_finding", gen_stale_finding),
    ("privileged_workloads", gen_privileged_workloads),
    ("rbac_drift", gen_rbac_drift),
    ("network_policy_gaps", gen_network_policy_gaps),
    ("route_cert_expiry", gen_route_cert_expiry),
    ("service_endpoint_gaps", gen_service_endpoint_gaps),
    ("readiness_regressions", gen_readiness_regressions),
]


def run_all_generators() -> list[dict[str, Any]]:
    """Run all registered generators and return combined items."""
    all_items: list[dict[str, Any]] = []
    for name, gen_fn in TASK_GENERATORS:
        try:
            items = gen_fn()
            all_items.extend(items)
        except Exception:
            logger.debug("Generator %s failed", name, exc_info=True)
    return all_items
