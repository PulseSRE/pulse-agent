"""Scanners for what SREs get paged for and Pulse could not see.

Four areas the existing scanners skip entirely:

    storage      PVCs that never bind, volumes that will not attach or mount,
                 volumes nearly full
    ingress      routes answering 5xx (router metrics, so it works for apps
                 that export none) and cluster DNS failing lookups
    webhooks     admission webhooks failing or slow — every create and update
                 they intercept stalls or fails with them
    maintenance  PodDisruptionBudgets that allow no disruption (a node drain
                 or upgrade will hang on them) and MachineConfigPools that are
                 degraded or stuck updating

Each reads the cluster or Prometheus through the same plumbing as every other
scanner, so a denied read or an unreachable Prometheus marks the scanner failed
instead of reporting clean.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from ..errors import ToolError
from ..k8s_client import get_core_client, get_custom_client, get_policy_client, safe
from .findings import _make_finding, _skip_namespace
from .registry import SEVERITY_CRITICAL, SEVERITY_WARNING
from .scanner_health import optional_access, report_failure
from .trend_scanners import _query_prometheus

logger = logging.getLogger("pulse_agent.monitor")

PVC_PENDING_SECONDS = 5 * 60
MOUNT_STUCK_SECONDS = 5 * 60
VOLUME_FULL_RATIO = 0.90
VOLUME_CRITICAL_RATIO = 0.97
ROUTE_5XX_SHARE = 0.05
ROUTE_MIN_RPS = 0.5
DNS_FAILURE_SHARE = 0.01
WEBHOOK_P99_SECONDS = 5.0
WEBHOOK_FAILURES_PER_SEC = 0.1
MCP_STUCK_SECONDS = 60 * 60


def _age(ts: Any, now: datetime | None = None) -> float:
    if ts is None:
        return 0.0
    now = now or datetime.now(UTC)
    try:
        return (now - ts).total_seconds()
    except TypeError:
        return 0.0


def _value(result: dict) -> float | None:
    try:
        return float(result["value"][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


# ── storage ──────────────────────────────────────────────────────────────────


def _volume_events() -> dict[tuple[str, str], str]:
    """(namespace, pod) -> latest FailedMount/FailedAttachVolume message."""
    events = safe(lambda: get_core_client().list_event_for_all_namespaces(field_selector="type=Warning"))
    if isinstance(events, ToolError):
        return {}
    out: dict[tuple[str, str], str] = {}
    for e in events.items:
        if e.reason not in ("FailedMount", "FailedAttachVolume"):
            continue
        obj = e.involved_object
        if obj is not None and obj.kind == "Pod":
            out[(obj.namespace or "", obj.name or "")] = f"{e.reason}: {(e.message or '')[:200]}"
    return out


def scan_storage(pods: Any = None) -> list[dict]:
    findings: list[dict] = []
    try:
        core = get_core_client()
        pvcs = safe(lambda: core.list_persistent_volume_claim_for_all_namespaces())
        if not isinstance(pvcs, ToolError):
            for pvc in pvcs.items:
                ns, name = pvc.metadata.namespace, pvc.metadata.name
                if _skip_namespace(ns) or (pvc.status.phase or "") != "Pending":
                    continue
                if _age(pvc.metadata.creation_timestamp) < PVC_PENDING_SECONDS:
                    continue
                sc = pvc.spec.storage_class_name or "(cluster default)"
                findings.append(
                    _make_finding(
                        severity=SEVERITY_WARNING,
                        category="storage",
                        title=f"PersistentVolumeClaim {name} is not binding",
                        summary=(
                            f"PVC {ns}/{name} (storage class {sc}) has been Pending for "
                            f"{int(_age(pvc.metadata.creation_timestamp) // 60)}m. Pods that mount it cannot start. "
                            "Usual causes: no default StorageClass, a provisioner that is down, or no volume "
                            "matching the request in the pod's zone."
                        ),
                        resources=[{"kind": "PersistentVolumeClaim", "name": name, "namespace": ns}],
                    )
                )

        if pods is None:
            pods = safe(lambda: core.list_pod_for_all_namespaces(field_selector="status.phase=Pending"))
        if not isinstance(pods, (ToolError, str)) and pods is not None:
            mount_errors = _volume_events()
            for pod in pods.items:
                ns, name = pod.metadata.namespace, pod.metadata.name
                if _skip_namespace(ns) or (pod.status.phase or "") != "Pending":
                    continue
                message = mount_errors.get((ns, name))
                if not message or _age(pod.metadata.creation_timestamp) < MOUNT_STUCK_SECONDS:
                    continue
                findings.append(
                    _make_finding(
                        severity=SEVERITY_CRITICAL,
                        category="storage",
                        title=f"Pod {name} cannot mount its volumes",
                        summary=f"Pod {ns}/{name} is stuck starting. {message}",
                        resources=[{"kind": "Pod", "name": name, "namespace": ns}],
                    )
                )

        usage = _query_prometheus(
            "max by (namespace, persistentvolumeclaim) "
            "(kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes)"
        )
        for r in usage:
            m = r.get("metric", {})
            ns, name, ratio = m.get("namespace", ""), m.get("persistentvolumeclaim", ""), _value(r)
            if not ns or not name or ratio is None or _skip_namespace(ns) or ratio < VOLUME_FULL_RATIO:
                continue
            findings.append(
                _make_finding(
                    severity=SEVERITY_CRITICAL if ratio >= VOLUME_CRITICAL_RATIO else SEVERITY_WARNING,
                    category="storage",
                    title=f"Volume {name} is nearly full",
                    summary=f"PVC {ns}/{name} is {ratio:.0%} full. Writes fail when it reaches 100%.",
                    resources=[{"kind": "PersistentVolumeClaim", "name": name, "namespace": ns}],
                )
            )
    except Exception as e:
        logger.error("Storage scan failed: %s", e)
        report_failure(e)
    return findings


# ── ingress and DNS ──────────────────────────────────────────────────────────

_ROUTE_5XX = (
    'sum by (exported_namespace, route) (rate(haproxy_backend_http_responses_total{code="5xx"}[10m]))'
    " / sum by (exported_namespace, route) (rate(haproxy_backend_http_responses_total[10m]))"
)
_ROUTE_RPS = "sum by (exported_namespace, route) (rate(haproxy_backend_http_responses_total[10m]))"
_DNS_FAILURES = (
    'sum(rate(coredns_dns_responses_total{rcode="SERVFAIL"}[10m])) / sum(rate(coredns_dns_responses_total[10m]))'
)


def scan_ingress() -> list[dict]:
    findings: list[dict] = []
    try:
        rps = {
            (r["metric"].get("exported_namespace", ""), r["metric"].get("route", "")): _value(r) or 0.0
            for r in _query_prometheus(_ROUTE_RPS)
            if "metric" in r
        }
        for r in _query_prometheus(_ROUTE_5XX):
            m = r.get("metric", {})
            ns, route, share = m.get("exported_namespace", ""), m.get("route", ""), _value(r)
            if not ns or not route or share is None or _skip_namespace(ns):
                continue
            traffic = rps.get((ns, route), 0.0)
            # A route serving one request a minute is 100% errors after one
            # failure. Below this rate there is nothing to say yet.
            if share < ROUTE_5XX_SHARE or traffic < ROUTE_MIN_RPS:
                continue
            findings.append(
                _make_finding(
                    severity=SEVERITY_CRITICAL if share >= 0.25 else SEVERITY_WARNING,
                    category="ingress",
                    title=f"Route {route} is returning errors",
                    summary=(
                        f"{share:.0%} of requests to route {ns}/{route} returned 5xx over the last 10 minutes "
                        f"({traffic:.1f} req/s), measured at the router — the users' view, whatever the app reports."
                    ),
                    resources=[{"kind": "Route", "name": route, "namespace": ns}],
                )
            )

        dns = _query_prometheus(_DNS_FAILURES)
        share = _value(dns[0]) if dns else None
        if share is not None and share >= DNS_FAILURE_SHARE:
            findings.append(
                _make_finding(
                    severity=SEVERITY_CRITICAL if share >= 0.05 else SEVERITY_WARNING,
                    category="ingress",
                    title="Cluster DNS is failing lookups",
                    summary=(
                        f"{share:.1%} of DNS queries answered SERVFAIL over the last 10 minutes. Anything that "
                        "resolves a service name — most of the cluster — fails intermittently with it."
                    ),
                    resources=[{"kind": "Service", "name": "dns-default", "namespace": "openshift-dns"}],
                )
            )
    except Exception as e:
        logger.error("Ingress scan failed: %s", e)
        report_failure(e)
    return findings


# ── admission webhooks ───────────────────────────────────────────────────────

_WEBHOOK_P99 = (
    "histogram_quantile(0.99, sum by (name, le) "
    "(rate(apiserver_admission_webhook_admission_duration_seconds_bucket[10m])))"
)
_WEBHOOK_FAILURES = (
    "sum by (name) (rate(apiserver_admission_webhook_rejection_count"
    '{error_type=~"calling_webhook_error|apiserver_internal_error"}[10m]))'
)


def scan_webhooks() -> list[dict]:
    findings: list[dict] = []
    try:
        for r in _query_prometheus(_WEBHOOK_FAILURES):
            name, rate = r.get("metric", {}).get("name", ""), _value(r)
            if not name or rate is None or rate < WEBHOOK_FAILURES_PER_SEC:
                continue
            findings.append(
                _make_finding(
                    severity=SEVERITY_CRITICAL,
                    category="webhooks",
                    title=f"Admission webhook {name} is failing",
                    summary=(
                        f"The API server cannot reach webhook {name} ({rate:.1f} failed calls/s). Every create "
                        "or update it intercepts fails or is let through unchecked, depending on its "
                        "failurePolicy — deployments and scaling can stall cluster-wide."
                    ),
                    resources=[{"kind": "Webhook", "name": name}],
                )
            )
        for r in _query_prometheus(_WEBHOOK_P99):
            name, p99 = r.get("metric", {}).get("name", ""), _value(r)
            if not name or p99 is None or p99 != p99 or p99 < WEBHOOK_P99_SECONDS:
                continue
            findings.append(
                _make_finding(
                    severity=SEVERITY_WARNING,
                    category="webhooks",
                    title=f"Admission webhook {name} is slow",
                    summary=(
                        f"Webhook {name} takes {p99:.1f}s at p99. Every request it intercepts waits that long, "
                        "and past its timeout the API server gives up on it."
                    ),
                    resources=[{"kind": "Webhook", "name": name}],
                )
            )
    except Exception as e:
        logger.error("Webhook scan failed: %s", e)
        report_failure(e)
    return findings


# ── maintenance blockers ─────────────────────────────────────────────────────


def _condition(obj: dict, kind: str) -> dict | None:
    for c in (obj.get("status") or {}).get("conditions") or []:
        if c.get("type") == kind:
            return c
    return None


def scan_maintenance_blockers() -> list[dict]:
    findings: list[dict] = []
    try:
        pdbs = safe(lambda: get_policy_client().list_pod_disruption_budget_for_all_namespaces())
        if not isinstance(pdbs, ToolError):
            for pdb in pdbs.items:
                ns, name = pdb.metadata.namespace, pdb.metadata.name
                st = pdb.status
                if _skip_namespace(ns) or st is None:
                    continue
                expected = st.expected_pods or 0
                healthy = st.current_healthy or 0
                # Healthy and still allowing nothing: no drain can ever evict
                # these pods. When pods are unhealthy, zero is the PDB doing
                # its job, not a misconfiguration.
                if expected == 0 or (st.disruptions_allowed or 0) > 0 or healthy < (st.desired_healthy or 0):
                    continue
                findings.append(
                    _make_finding(
                        severity=SEVERITY_WARNING,
                        category="maintenance",
                        title=f"PodDisruptionBudget {name} blocks node drains",
                        summary=(
                            f"PDB {ns}/{name} allows 0 disruptions while all {healthy} pods are healthy, so no "
                            "node running them can be drained. Upgrades and node maintenance will hang there. "
                            "Usually minAvailable equals the replica count, or maxUnavailable is 0."
                        ),
                        resources=[{"kind": "PodDisruptionBudget", "name": name, "namespace": ns}],
                    )
                )

        # OpenShift only; a 404 elsewhere is not a failure. Not in the
        # default agent role yet, so a 403 must not fail the PDB half.
        with optional_access():
            pools = safe(
                lambda: get_custom_client().list_cluster_custom_object(
                    "machineconfiguration.openshift.io", "v1", "machineconfigpools"
                )
            )
        if isinstance(pools, dict):
            now = datetime.now(UTC)
            for pool in pools.get("items", []):
                name = pool.get("metadata", {}).get("name", "")
                degraded = _condition(pool, "Degraded")
                updating = _condition(pool, "Updating")
                if degraded and degraded.get("status") == "True":
                    findings.append(
                        _make_finding(
                            severity=SEVERITY_CRITICAL,
                            category="maintenance",
                            title=f"MachineConfigPool {name} is degraded",
                            summary=(
                                f"Pool {name} cannot apply its configuration: {degraded.get('message', '')[:200]}. "
                                "Upgrades will not finish on these nodes until it is cleared."
                            ),
                            resources=[{"kind": "MachineConfigPool", "name": name}],
                        )
                    )
                elif updating and updating.get("status") == "True":
                    since = updating.get("lastTransitionTime")
                    try:
                        started = datetime.fromisoformat(str(since).replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if (now - started).total_seconds() < MCP_STUCK_SECONDS:
                        continue
                    st = pool.get("status") or {}
                    findings.append(
                        _make_finding(
                            severity=SEVERITY_WARNING,
                            category="maintenance",
                            title=f"MachineConfigPool {name} has been updating for over an hour",
                            summary=(
                                f"Pool {name}: {st.get('updatedMachineCount', '?')} of "
                                f"{st.get('machineCount', '?')} nodes updated. A node that will not drain "
                                "(often a PodDisruptionBudget) is the usual reason."
                            ),
                            resources=[{"kind": "MachineConfigPool", "name": name}],
                        )
                    )
    except Exception as e:
        logger.error("Maintenance scan failed: %s", e)
        report_failure(e)
    return findings
