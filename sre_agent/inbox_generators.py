"""Proactive task generators for the Ops Inbox.

Each generator is a function returning list[dict] of inbox item dicts.
Generators are registered in TASK_GENERATORS and called each scan cycle.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

logger = logging.getLogger("pulse_agent.inbox_generators")


class GeneratorUnavailableError(Exception):
    """A generator could not check its condition this cycle.

    Distinct from "checked, found nothing": the cycle auto-resolves a
    generator's open items when it stops producing them, so a fetcher that
    turned a 403 or a timeout into ``[]`` resolved every item it owned and hid
    them for a day.
    """


def _checked(result: Any, what: str) -> Any:
    """Raise if ``safe()`` returned an error instead of a result."""
    from .errors import ToolError

    if isinstance(result, (str, ToolError)):
        raise GeneratorUnavailableError(f"{what}: {result}")
    return result


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
    ui_link: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {"generator": generator, "urgency_hours": urgency_hours}
    if ui_link:
        # The UI page where the item is acted on (a path inside the console).
        metadata["ui_link"] = ui_link
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
        # Urgency only ranks an item through its due date; without one an
        # expiring cert and a week-old posture note sorted the same.
        "due_date": int(time.time() + urgency_hours * 3600),
        "metadata": metadata,
    }


# -- Data fetchers --
#
# A fetcher that cannot answer raises (GeneratorUnavailableError, or whatever the
# client raised) rather than returning []. run_all_generators records the
# generator as failed and the cycle leaves its open items alone.


def _parse_k8s_time(value: Any) -> float | None:
    from datetime import datetime

    if not value:
        return None
    if isinstance(value, datetime):
        return value.timestamp()
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _get_tls_secrets() -> list[dict]:
    from .k8s_client import get_core_client, safe

    result = _checked(
        safe(lambda: get_core_client().list_secret_for_all_namespaces(field_selector="type=kubernetes.io/tls")),
        "list TLS secrets",
    )
    secrets = []
    for s in result.items:
        annotations = s.metadata.annotations or {}
        expiry = _parse_k8s_time(annotations.get("cert-manager.io/certificate-expiry-time"))
        if expiry is None:
            # Not cert-manager managed: read the certificate itself.
            expiry = _cert_not_after((s.data or {}).get("tls.crt"), base64_encoded=True)
        if expiry is not None:
            secrets.append(
                {"name": s.metadata.name, "namespace": s.metadata.namespace, "expiry_timestamp": int(expiry)}
            )
    return secrets


def _cert_not_after(pem: str | None, *, base64_encoded: bool = False) -> float | None:
    """Expiry of the first certificate in a PEM bundle, or None if unreadable."""
    if not pem:
        return None
    try:
        import base64

        from cryptography import x509

        raw = base64.b64decode(pem) if base64_encoded else pem.encode()
        cert = x509.load_pem_x509_certificate(raw)
        return cert.not_valid_after_utc.timestamp()
    except Exception:
        logger.debug("Unreadable certificate", exc_info=True)
        return None


# How far ahead each trend scanner forecasts, read from its title ("in 3 days").
_TREND_HORIZON = re.compile(r"in (\d+) days?")


def _get_trend_findings() -> list[dict]:
    """The monitor's standing trend findings.

    This read ``monitor._trend_findings``, which nothing ever set, so trend
    items never appeared. Trend findings live in ``_last_findings`` with
    ``findingType == "trend"`` like everything else the monitor holds.
    """
    from .monitor.cluster_monitor import get_cluster_monitor_sync

    monitor = get_cluster_monitor_sync()
    if monitor is None or getattr(monitor, "_last_scanner_results", None) is None:
        # No completed scan yet: nothing is known, which is not "no trends".
        raise GeneratorUnavailableError("monitor has not completed a scan")
    threshold = getattr(monitor, "_noise_threshold", 1.0)
    return [
        f
        for f in list(monitor._last_findings.values())
        if f.get("findingType") == "trend" and float(f.get("noiseScore") or 0) < threshold
    ]


def _get_degraded_operators() -> list[dict]:
    from .k8s_client import get_custom_client, safe

    result = _checked(
        safe(lambda: get_custom_client().list_cluster_custom_object("config.openshift.io", "v1", "clusteroperators")),
        "list ClusterOperators",
    )
    now = time.time()
    degraded = []
    for op in result.get("items", []):
        conditions = {c["type"]: c for c in op.get("status", {}).get("conditions", [])}
        deg = conditions.get("Degraded", {})
        if deg.get("status") == "True":
            since = _parse_k8s_time(deg.get("lastTransitionTime"))
            degraded.append(
                {
                    "name": op["metadata"]["name"],
                    # Measured from the condition, not a constant 1h.
                    "degraded_duration_hours": round((now - since) / 3600, 1) if since else None,
                    "message": deg.get("message", ""),
                }
            )
    return degraded


def _get_available_updates() -> list[dict]:
    from .k8s_client import get_custom_client, safe

    result = _checked(
        safe(
            lambda: get_custom_client().get_cluster_custom_object(
                "config.openshift.io", "v1", "clusterversions", "version"
            )
        ),
        "get ClusterVersion",
    )
    updates = result.get("status", {}).get("availableUpdates") or []
    return [{"version": u.get("version"), "channel": u.get("channel", "")} for u in updates]


# The generator cycle runs every scan; listing APIRequestCount is one object
# per served resource version with 24h of per-node, per-user counts, and the
# answer moves on the scale of hours. A verdict is reused for this long.
_UPGRADE_API_CACHE_SECONDS = 45 * 60
_upgrade_api_cache: tuple[float, dict | None] | None = None


def _reset_upgrade_api_cache() -> None:
    global _upgrade_api_cache
    _upgrade_api_cache = None


def _get_upgrade_api_removals() -> dict | None:
    """The API-removal verdict for the next minor, or None when there is nothing to check.

    The target is the next minor on offer, else current + 1 (a ``stable-4.N``
    channel offers only z-streams until the admin switches channel, which is
    exactly when to plan). None means checked and nothing to say: not
    OpenShift. A read that failed raises, so an open "upgrade blocked" item is
    left alone rather than resolved by a 403; failures are not cached.
    """
    global _upgrade_api_cache
    from .upgrade_api_removals import read_upgrade_api_removals

    now = time.time()
    if _upgrade_api_cache and now - _upgrade_api_cache[0] < _UPGRADE_API_CACHE_SECONDS:
        return _upgrade_api_cache[1]
    data = read_upgrade_api_removals()
    if data["status"] == "unknown":
        # Nothing to say (not OpenShift, or a major the check has no version
        # map for, e.g. after an upgrade to OpenShift 5): open items resolve.
        if data["cause"] not in ("not_openshift", "unsupported_version"):
            raise GeneratorUnavailableError(f"upgrade API removals: {data['reason']}")
        result = None
    else:
        result = data
    _upgrade_api_cache = (now, result)
    return result


# Hours of error budget left below which an SLO becomes an inbox item.
_SLO_HORIZON_HOURS = 72


def _get_slo_burn_rates() -> list[dict]:
    """SLOs burning toward exhaustion, from one live evaluation of all of them.

    This read ``slo["name"]`` (the dicts have no such key, so every call raised
    and the generator never produced anything), and the budget it compared was
    a constant derived from the target rather than a measurement.
    """
    from .slo_registry import get_slo_registry

    registry = get_slo_registry()
    if not registry.list_all():
        return []
    statuses = registry.evaluate_with_prometheus()
    if not statuses:
        # SLOs are defined but none could be measured (Prometheus unreachable
        # or no data): that is not "no SLO is burning", and returning [] here
        # resolved every open SLO item on a Prometheus outage.
        raise GeneratorUnavailableError("no SLO could be evaluated from Prometheus")
    burning = []
    for status in statuses:
        hours = status.budget_remaining_hours
        if status.alert_level == "ok" and (hours is None or hours >= _SLO_HORIZON_HOURS):
            continue
        slo = status.definition
        burning.append(
            {
                "name": f"{slo.service_name} {slo.slo_type}",
                "service": slo.service_name,
                "budget_remaining_hours": hours,
                "budget_remaining": status.error_budget_remaining,
                "burn_rate": status.burn_rate,
                "alert_level": status.alert_level,
            }
        )
    return burning


def _get_node_capacity() -> list[dict]:
    """Nodes reporting memory, disk or PID pressure right now.

    Each used to be reported as "95% CPU, full in 4h" — constants, attached to
    any node with MemoryPressure. The condition is what is actually known.
    """
    from .k8s_client import get_core_client, safe

    nodes = _checked(safe(lambda: get_core_client().list_node()), "list nodes")
    pressured = []
    for node in nodes.items:
        conditions = {c.type: c for c in (node.status.conditions or [])}
        kinds = [
            k
            for k in ("MemoryPressure", "DiskPressure", "PIDPressure")
            if getattr(conditions.get(k), "status", "") == "True"
        ]
        if kinds:
            pressured.append({"node": node.metadata.name, "pressure": kinds})
    return pressured


def _get_stale_findings() -> list[dict]:
    from .repositories import get_monitor_repo

    cutoff = int(time.time()) - 72 * 3600
    rows = get_monitor_repo().fetch_stale_inbox_items(cutoff)
    return [
        {"title": r["title"], "hours_stale": (time.time() - r["created_at"]) / 3600, "finding_id": r["id"]}
        for r in rows
    ]


def _get_privileged_workloads() -> list[dict]:
    from .k8s_client import get_core_client, safe

    pods = _checked(safe(lambda: get_core_client().list_pod_for_all_namespaces()), "list pods")
    privileged = []
    for pod in pods.items:
        for c in pod.spec.containers:
            sc = c.security_context
            if sc and (getattr(sc, "privileged", False) or getattr(sc, "run_as_user", None) == 0):
                privileged.append({"pod": pod.metadata.name, "namespace": pod.metadata.namespace, "container": c.name})
                break
    return privileged


def _get_rbac_drift() -> list[dict]:
    from .k8s_client import get_rbac_client, safe

    bindings = _checked(safe(lambda: get_rbac_client().list_cluster_role_binding()), "list ClusterRoleBindings")
    drift = []
    for b in bindings.items:
        if b.role_ref.name == "cluster-admin":
            for subject in b.subjects or []:
                if subject.kind == "User":
                    drift.append({"binding": b.metadata.name, "user": subject.name})
    return drift


def _get_network_policy_gaps() -> list[dict]:
    from .k8s_client import get_core_client, get_networking_client, is_system_namespace, safe

    namespaces = _checked(safe(lambda: get_core_client().list_namespace()), "list namespaces")
    # Fetch all network policies cluster-wide in a single call
    all_policies = _checked(
        safe(lambda: get_networking_client().list_network_policy_for_all_namespaces()), "list NetworkPolicies"
    )
    ns_with_policies = {p.metadata.namespace for p in all_policies.items}

    gaps = []
    for ns in namespaces.items:
        name = ns.metadata.name
        if is_system_namespace(name, include_default=False):
            continue
        if name not in ns_with_policies:
            gaps.append({"namespace": name})
    return gaps


# Routes whose certificate expires within this many hours become an item.
_ROUTE_CERT_HORIZON_HOURS = 30 * 24


def _get_route_cert_expiry() -> list[dict]:
    """Routes whose inline certificate expires soon, read from the certificate.

    Every route with an inline certificate was reported as expiring in 168h —
    a constant, so a cert good for two years raised the same item as one due
    on Friday.
    """
    from .k8s_client import get_custom_client, safe

    routes = _checked(
        safe(lambda: get_custom_client().list_cluster_custom_object("route.openshift.io", "v1", "routes")),
        "list Routes",
    )
    now = time.time()
    expiring = []
    for r in routes.get("items", []):
        tls = r.get("spec", {}).get("tls") or {}
        not_after = _cert_not_after(tls.get("certificate"))
        if not_after is None:
            continue
        hours = (not_after - now) / 3600
        if hours <= _ROUTE_CERT_HORIZON_HOURS:
            expiring.append(
                {"name": r["metadata"]["name"], "namespace": r["metadata"]["namespace"], "hours_until_expiry": hours}
            )
    return expiring


def _get_service_endpoint_gaps() -> list[dict]:
    from .k8s_client import get_core_client, is_system_namespace, safe

    endpoints = _checked(safe(lambda: get_core_client().list_endpoints_for_all_namespaces()), "list Endpoints")
    gaps = []
    for ep in endpoints.items:
        ns = ep.metadata.namespace
        if is_system_namespace(ns, include_default=False):
            continue
        subsets = ep.subsets or []
        ready = sum(len(s.addresses or []) for s in subsets)
        if ready == 0:
            gaps.append({"service": ep.metadata.name, "namespace": ns})
    return gaps


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
        # The forecast horizon the scanner used ("exhaustion predicted in 3
        # days"): the condition is expected no later than that.
        m = _TREND_HORIZON.search(str(f.get("title", "")))
        hours = int(m.group(1)) * 24 if m else 168
        items.append(
            _make_assessment(
                title=f["title"],
                summary=f.get("summary", ""),
                severity=f.get("severity", "warning"),
                urgency_hours=hours,
                generator="trend_prediction",
                resources=f.get("resources", []),
                confidence=f.get("confidence", 0.7),
                # One item per forecast, not one per generator.
                correlation_key=f"trend_prediction:{f.get('category', '')}:{f.get('title', '')}",
            )
        )
    return items


def gen_degraded_operator() -> list[dict[str, Any]]:
    operators = _get_degraded_operators()
    if not operators:
        return []

    def _describe(op: dict) -> str:
        hours = op.get("degraded_duration_hours")
        return f"{op['name']} ({hours:.0f}h)" if hours is not None else op["name"]

    operators.sort(key=lambda op: -(op.get("degraded_duration_hours") or 0))
    names = ", ".join(_describe(op) for op in operators[:10])
    if len(operators) > 10:
        names += f" (+{len(operators) - 10} more)"
    resources = [{"kind": "ClusterOperator", "name": op["name"], "namespace": ""} for op in operators[:20]]
    summary = f"Degraded operators: {names}"
    if any(op["name"] == "cloud-credential" for op in operators):
        # Its cause is outside the cluster; say where it is explained.
        summary += (
            ". cloud-credential: a component lacks cloud permissions. The Cloud Credentials items name each "
            "affected component, the permissions it needs and the fix for this cluster's credentials mode; or ask "
            "Pulse to explain cloud credentials."
        )
    return [
        _make_assessment(
            title=f"{len(operators)} ClusterOperators degraded",
            summary=summary,
            severity="critical",
            urgency_hours=0,
            generator="degraded_operator",
            resources=resources,
        )
    ]


# Upgrade items are acted on in Administration -> Updates. The ClusterVersion
# resource links to the object itself in drawers that only render resources;
# ``ui_link`` names the Updates tab for drawers that render it.
UPDATES_UI_LINK = "/admin?tab=updates"
_CLUSTER_VERSION_RESOURCE = {"kind": "ClusterVersion", "name": "version", "namespace": ""}


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
            resources=[dict(_CLUSTER_VERSION_RESOURCE)],
            confidence=1.0,
            ui_link=UPDATES_UI_LINK,
        )
    ]


def gen_upgrade_api_removals() -> list[dict[str, Any]]:
    """One item while the next minor is blocked: removed APIs in use, an unacked gate, or Upgradeable=False.

    Stops being produced (and is auto-resolved) once the verdict is go. A new
    target minor gets its own item, so the old one resolves with it.
    """
    data = _get_upgrade_api_removals()
    if not data or data["status"] != "no_go":
        return []
    target = data["target"]["ocp"]
    in_use = data["inUse"]
    unacked = [g for g in data["gates"] if g["acked"] is False]
    if in_use:
        n = len(in_use)
        title = f"Upgrade to {target} blocked: {n} removed API{'s' if n != 1 else ''} still in use"
    elif unacked:
        n = len(unacked)
        title = f"Upgrade to {target} blocked: {n} admin gate{'s' if n != 1 else ''} not acknowledged"
    else:
        reason = data["upgradeable"]["reason"] or "Upgradeable=False"
        title = f"Upgrade to {target} blocked: ClusterVersion not upgradeable ({reason})"

    lines = [data["reason"]]
    for api in in_use[:10]:
        lines.append(
            f"{api['name']} (removed in Kubernetes {api['removedInRelease']}): {api['requestCount']} requests/24h"
        )
        for c in api.get("callers", [])[:3]:
            platform = " [platform]" if c["platform"] else ""
            agent = f" ({c['userAgent']})" if c["userAgent"] else ""
            lines.append(f"  - {c['username'] or '(unknown user)'}{agent}: {c['count']}{platform}")
        more = api.get("callerCount", 0) - min(3, len(api.get("callers", [])))
        if more > 0:
            lines.append(f"  - +{more} more callers")
    if len(in_use) > 10:
        lines.append(f"+{len(in_use) - 10} more APIs")
    for g in unacked:
        lines.append(f"Admin gate not acknowledged: {g['key']} — {g['description']}")
    resources = [dict(_CLUSTER_VERSION_RESOURCE)]
    resources += [{"kind": "APIRequestCount", "name": api["name"], "namespace": ""} for api in in_use[:20]]
    return [
        _make_assessment(
            title=title,
            summary="\n".join(lines),
            severity="warning",
            urgency_hours=72,
            generator="upgrade_api_removals",
            resources=resources,
            correlation_key=f"upgrade_api_removals:{target}",
            confidence=1.0,
            ui_link=UPDATES_UI_LINK,
        )
    ]


def gen_slo_burn() -> list[dict[str, Any]]:
    burns = _get_slo_burn_rates()
    items = []
    for b in burns:
        hours = b["budget_remaining_hours"]
        severity = "critical" if b["alert_level"] == "critical" or (hours is not None and hours < 24) else "warning"
        when = f"exhausting in {int(hours)}h" if hours is not None else f"{b['budget_remaining']:.0%} budget left"
        items.append(
            _make_assessment(
                title=f"SLO '{b['name']}' error budget {when}",
                summary=(
                    f"Burning at {b['burn_rate']:.1f}x the sustainable rate over the last hour, "
                    f"{b['budget_remaining']:.0%} of the budget left. Reduce the error rate or revisit the SLO."
                ),
                severity=severity,
                urgency_hours=hours if hours is not None else 24,
                generator="slo_burn",
                resources=[{"kind": "Deployment", "name": b["service"], "namespace": ""}],
                correlation_key=f"slo_burn:{b['name']}",
            )
        )
    return items


def gen_capacity_projection() -> list[dict[str, Any]]:
    nodes = _get_node_capacity()
    if not nodes:
        return []
    node_list = ", ".join(f"{n['node']} ({', '.join(n['pressure'])})" for n in nodes[:10])
    if len(nodes) > 10:
        node_list += f" (+{len(nodes) - 10} more)"
    resources = [{"kind": "Node", "name": n["node"], "namespace": ""} for n in nodes[:20]]
    return [
        _make_assessment(
            title=f"{len(nodes)} node{'s' if len(nodes) != 1 else ''} under resource pressure",
            summary=f"Kubelet reports pressure on: {node_list}. Pods may be evicted until it clears.",
            severity="warning",
            urgency_hours=0,
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


def gen_skill_curation() -> list[dict[str, Any]]:
    """The forgetting half of skill learning, surfaced as proposals.

    Agent-created skills that never route still cost selector attention and
    reviewer trust, and near-duplicate lessons split traffic between two
    half-trained skills. The curator (skill_curator.py) finds both cases; this
    generator turns them into inbox items pointing at the archive endpoint —
    the human act. Archiving is recoverable and pinning a skill exempts it.
    """
    from .skill_curator import STALE_AFTER_DAYS, find_overlapping_skills, find_stale_skills

    items: list[dict[str, Any]] = []
    for stale in find_stale_skills(strict=True)[:3]:
        name = stale["name"]
        state = "unreviewed" if not stale["reviewed"] else ("quarantined" if stale["quarantined"] else "active")
        items.append(
            _make_assessment(
                title=f"Agent-created skill '{name}' has not routed in {STALE_AFTER_DAYS} days",
                summary=(
                    f"'{name}' ({state}, {stale['age_days']}d old) received no traffic in the last "
                    f"{STALE_AFTER_DAYS} days. Archive it to keep the portfolio honest — archiving is "
                    f"recoverable (POST /admin/skills/{name}/archive, restore any time), or pin it "
                    f"(POST /admin/skills/{name}/pin) if it covers a rare case worth keeping."
                ),
                severity="low",
                urgency_hours=336,
                generator="skill_curation",
                correlation_key=f"skill-stale:{name}",
                confidence=0.8,
            )
        )
    for pair in find_overlapping_skills()[:2]:
        a, b = pair["names"]
        items.append(
            _make_assessment(
                title=f"Skills '{a}' and '{b}' look like the same lesson learned twice",
                summary=(
                    f"Keyword overlap {pair['overlap']:.0%} (shared: {', '.join(pair['shared_keywords'])}). "
                    "Two near-duplicate skills split routing traffic and neither deepens. Consider merging "
                    "their distilled cases into one and archiving the other — the agent can draft the merge "
                    "if you ask it to."
                ),
                severity="low",
                urgency_hours=336,
                generator="skill_curation",
                correlation_key=f"skill-overlap:{min(a, b)}:{max(a, b)}",
                confidence=0.7,
            )
        )
    return items


def gen_skill_gaps() -> list[dict[str, Any]]:
    """Recurring queries no skill handles well become proposals, not log lines.

    ``identify_skill_gaps`` used to run in the daily flywheel and log its
    findings, which meant nobody ever saw them. As an inbox item the gap is
    visible, deduplicated on its pattern, and auto-resolves once a skill
    starts covering those queries.
    """
    from .selector_learning import identify_skill_gaps

    gaps = identify_skill_gaps(days=30, strict=True)
    items: list[dict[str, Any]] = []
    for gap in gaps[:3]:  # top recurring patterns only — this is a suggestion queue, not a backlog
        pattern = str(gap.get("pattern", "")).strip()
        if not pattern:
            continue
        count = int(gap.get("occurrences", 0))
        slug = re.sub(r"[^a-z0-9]+", "-", pattern.lower())[:60].strip("-")
        items.append(
            _make_assessment(
                title=f"No skill covers recurring queries about '{pattern}' ({count}x in 30d)",
                summary=(
                    f"Users asked about '{pattern}' {count} times in the last 30 days and "
                    "routing found no skill with the right tools. Consider drafting a skill "
                    "for it — ask the agent to create one, or add it in the Toolbox UI. "
                    "Agent-drafted skills stay out of routing until a person approves them."
                ),
                severity="low",
                urgency_hours=168,
                generator="skill_gaps",
                correlation_key=f"skill-gap:{slug}",
                confidence=0.7,
            )
        )
    return items


def gen_skill_low_performers() -> list[dict[str, Any]]:
    """Skills people keep overriding become quarantine proposals with evidence.

    ``prune_low_performers`` used to log the names and drop them; the skill kept
    routing. This surfaces each flagged skill with its override rate and points
    at the quarantine endpoint, which is the human act that actually pulls it.
    The item auto-resolves if the skill's override rate recovers.
    """
    from .selector_learning import prune_low_performers

    flagged = prune_low_performers(days=30, strict=True)
    items: list[dict[str, Any]] = []
    for entry in flagged:
        skill = str(entry.get("skill", "")).strip()
        if not skill:
            continue
        rate = float(entry.get("override_rate", 0))
        items.append(
            _make_assessment(
                title=f"Skill '{skill}' overridden in {rate:.0%} of routes — consider quarantine",
                summary=(
                    f"Routing picked '{skill}' {entry.get('total', 0)} times in the last 30 days "
                    f"and users overrode it {entry.get('overrides', 0)} times ({rate:.0%}). "
                    "If the skill is misrouting, quarantine it: "
                    f"POST /admin/skills/{skill}/quarantine pulls it from automatic routing "
                    "(reversible via /unquarantine; it stays loadable by name for debugging)."
                ),
                severity="low",
                urgency_hours=168,
                generator="skill_low_performers",
                correlation_key=f"skill-quarantine:{skill}",
                confidence=0.75,
            )
        )
    return items


#: Last successful answer, returned when Temporal cannot be asked. An empty
#: list on an outage would auto-resolve every waiting-approval item, telling
#: people the question went away when only the server did.
_last_durable_approvals: list[dict[str, Any]] = []


def gen_durable_approvals() -> list[dict[str, Any]]:
    """A durable plan run waiting for a human becomes an inbox item.

    Approval gates on Temporal wait up to a day, but the only place to answer
    one was the plan panel that started the run, and only while it stayed
    open. Navigate away and the run timed out to needs_escalation with nobody
    asked. The item auto-resolves when the gate is answered or times out.
    """
    global _last_durable_approvals
    from .config import get_settings

    if not get_settings().temporal.host:
        return []
    import asyncio

    from .temporal.client import list_awaiting_approvals

    try:
        # Generators run in a worker thread with no event loop of their own.
        waiting = asyncio.run(asyncio.wait_for(list_awaiting_approvals(), timeout=10))
    except Exception:
        logger.debug("Could not list durable runs awaiting approval", exc_info=True)
        return list(_last_durable_approvals)

    items: list[dict[str, Any]] = []
    for run in waiting:
        memo = run.get("memo") or {}
        plan = str(memo.get("incident_type") or "plan")
        target = "/".join(str(memo[k]) for k in ("resource_namespace", "resource_name") if memo.get(k))
        item = _make_assessment(
            title=f"Durable {plan} run is waiting for your approval of '{run['phase_id']}'"
            + (f" on {target}" if target else ""),
            summary=(
                f"Run {run['workflow_id']} reached phase '{run['phase_id']}', which needs a person to approve "
                "before it runs. Approve or deny it here or in Pulse Agent → Runs. Unanswered, it times out and "
                "the phase is recorded as needs_escalation."
            ),
            severity="warning",
            urgency_hours=24,
            generator="durable_approvals",
            namespace=memo.get("resource_namespace") or None,
            correlation_key=f"durable-approval:{run['workflow_id']}:{run['phase_id']}",
            confidence=1.0,
        )
        item["metadata"].update(workflow_id=run["workflow_id"], phase_id=run["phase_id"])
        items.append(item)
    _last_durable_approvals = items
    return items


# Pending InstallPlans are acted on in Operators -> Needs approval.
OPERATOR_APPROVALS_UI_LINK = "/operators?view=approvals"
_MAX_APPROVAL_ITEMS = 25


def _get_pending_installplans() -> list[Any]:
    """Unapproved InstallPlans across namespaces, as the agent's service account.

    The operator's agent ClusterRole has no rule for operators.coreos.com, so
    on a cluster deployed by it this listing is forbidden and the generator
    reports itself unavailable every cycle (its items are left alone) instead
    of claiming nothing is pending.
    """
    from .k8s_tools.olm import OLMReadError, read_pending_installplans

    try:
        plans, _subs_error = read_pending_installplans("ALL")
    except OLMReadError as e:
        raise GeneratorUnavailableError(str(e)) from e
    return plans


def gen_operator_approvals() -> list[dict[str, Any]]:
    """One item per operator update waiting for approval; resolved once it is approved or gone.

    A plan whose Subscription points at a newer plan is superseded and gets no
    item: approving it is not the way to update. OLM has no deny, so the item
    says how to decline (leave it unapproved) instead of offering one.
    """
    from .k8s_tools.olm import csv_version

    items: list[dict[str, Any]] = []
    for plan in _get_pending_installplans():
        if plan.current is False:
            continue
        to_text = ", ".join(plan.installs) or "a new version"
        to_ver = ", ".join(csv_version(c) for c in plan.installs) or "?"
        from_ver = ", ".join(csv_version(c) for c in plan.replaces)
        versions = f"{from_ver} → {to_ver}" if from_ver else to_ver
        replaces = f", replacing {', '.join(plan.replaces)}" if plan.replaces else ""
        channel = f" on channel {plan.channel}" if plan.channel else ""
        resources = [{"kind": "InstallPlan", "name": plan.name, "namespace": plan.namespace}]
        if plan.subscription:
            resources.append({"kind": "Subscription", "name": plan.subscription, "namespace": plan.namespace})
        item = _make_assessment(
            title=f"Operator update waiting for approval: {plan.operator} {versions}",
            summary=(
                f"InstallPlan {plan.namespace}/{plan.name}{channel} installs {to_text}{replaces} once someone "
                "approves it. Review and approve it in Operators → Needs approval, or ask Pulse. To decline, "
                "leave it unapproved: OLM has no deny, and the installed version keeps running. While it waits, "
                "OLM holds other updates in the same namespace."
            ),
            severity="warning",
            urgency_hours=72,
            generator="operator_approvals",
            namespace=plan.namespace,
            resources=resources,
            correlation_key=f"operator_approval:{plan.namespace}/{plan.name}",
            confidence=1.0,
            ui_link=OPERATOR_APPROVALS_UI_LINK,
        )
        item["metadata"].update(installplan=plan.name, installs=plan.installs, replaces=plan.replaces)
        items.append(item)
        if len(items) >= _MAX_APPROVAL_ITEMS:
            break
    return items


# -- Registry --

TASK_GENERATORS: list[tuple[str, Any]] = [
    ("cert_expiry", gen_cert_expiry),
    ("trend_prediction", gen_trend_prediction),
    ("degraded_operator", gen_degraded_operator),
    ("upgrade_available", gen_upgrade_available),
    ("upgrade_api_removals", gen_upgrade_api_removals),
    ("slo_burn", gen_slo_burn),
    ("capacity_projection", gen_capacity_projection),
    ("stale_finding", gen_stale_finding),
    ("privileged_workloads", gen_privileged_workloads),
    ("rbac_drift", gen_rbac_drift),
    ("network_policy_gaps", gen_network_policy_gaps),
    ("route_cert_expiry", gen_route_cert_expiry),
    ("service_endpoint_gaps", gen_service_endpoint_gaps),
    ("skill_gaps", gen_skill_gaps),
    ("skill_low_performers", gen_skill_low_performers),
    ("skill_curation", gen_skill_curation),
    ("durable_approvals", gen_durable_approvals),
    ("operator_approvals", gen_operator_approvals),
]


def run_all_generators(failed: set[str] | None = None) -> list[dict[str, Any]]:
    """Run all registered generators and return combined items.

    Names of generators that could not check their condition are added to
    ``failed``; their absence from the output is not a reason to resolve
    their open items.
    """
    all_items: list[dict[str, Any]] = []
    for name, gen_fn in TASK_GENERATORS:
        try:
            items = gen_fn()
            all_items.extend(items)
        except Exception as e:
            if failed is not None:
                failed.add(name)
            logger.info("Generator %s could not run this cycle: %s", name, e)
            logger.debug("Generator %s failure", name, exc_info=True)
    return all_items
