"""Liveness scanners — resources that should have finished, but never did.

Every other scanner in this package measures the *health of state*: is this pod
crashing, is this deployment short of replicas, is this node under pressure.
None of them measures the *liveness of a process*: should this thing have
finished by now?

That gap is why a Kuadrant CRD finalizer hammered the API server for four
months without a single finding. A stuck finalizer produces no crashing pod, no
degraded deployment, no firing alert — the cluster is perfectly healthy by
every state-based measure while a controller spins forever. The two scanners
here close that gap from both ends:

  * ``scan_stuck_deletions``     — the cause: deletions that never complete.
  * ``scan_hot_reconcile_loops`` — the symptom: request volume with no progress.

The symptom side matters independently. A hot loop caused by something other
than a deletion still shows up as sustained retries, and a deletion nobody
noticed still shows up as write amplification.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from ..errors import ToolError
from ..k8s_client import get_core_client, get_custom_client, safe
from .findings import _make_finding
from .registry import SEVERITY_CRITICAL, SEVERITY_WARNING

logger = logging.getLogger("pulse_agent.monitor")

# ── Stuck-deletion thresholds ─────────────────────────────────────────────
# A normal namespace deletion finishes in seconds; a normal pod termination is
# bounded by terminationGracePeriodSeconds (30s by default, minutes at worst).
# 15 minutes is far outside both, so anything past it is genuinely wedged
# rather than merely slow — this is a scanner that should almost never fire.
_STUCK_AFTER_SECONDS = 15 * 60
# Past six hours nobody is going to clear it by waiting.
_STUCK_CRITICAL_AFTER_SECONDS = 6 * 3600

# ── Hot-loop thresholds ───────────────────────────────────────────────────
# Calibrated against a healthy production cluster, where the busiest controller
# (open_api_v3_aggregation_controller) sustains ~10 retries/s and every other
# controller sits below 2/s. 20/s leaves headroom above the noisiest legitimate
# controller; a genuinely wedged reconcile loop runs orders of magnitude higher.
_RETRY_RATE_WARNING = 20.0
_RETRY_RATE_CRITICAL = 100.0

# Same cluster's busiest non-lease write is ~0.7 writes/s. Leases are excluded
# outright: lease renewal is inherently high-rate (~20/s here) and carries no
# signal. 5/s is roughly 7x the observed ceiling for real writes.
_WRITE_RATE_WARNING = 5.0
_WRITE_RATE_CRITICAL = 25.0

# Client-side failures: the healthy ceiling is ~0.35/s of 404s from operators
# probing for optional resources. A controller retrying a failing call forever
# runs far above that.
_CLIENT_ERROR_RATE_WARNING = 5.0

# Resources whose write rate is structurally high and says nothing about loops.
_WRITE_RATE_EXCLUDED_RESOURCES = (
    "leases",
    "subjectaccessreviews",
    "selfsubjectaccessreviews",
    "selfsubjectrulesreviews",
    "tokenreviews",
    "events",
)


def _age_seconds(timestamp: Any) -> float | None:
    """Seconds since an API timestamp, or None if it is missing/unparseable."""
    if timestamp is None:
        return None
    if isinstance(timestamp, str):
        try:
            timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return (datetime.now(UTC) - timestamp).total_seconds()


def _humanise(seconds: float) -> str:
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _severity_for_age(seconds: float) -> str:
    return SEVERITY_CRITICAL if seconds >= _STUCK_CRITICAL_AFTER_SECONDS else SEVERITY_WARNING


# ── Stuck deletions ───────────────────────────────────────────────────────


def _namespace_blockers(ns: Any) -> str:
    """Describe what is holding a namespace open, from its own status.

    The API server has already computed this: NamespaceContentRemaining names
    the resource kinds that still exist, and NamespaceFinalizersRemaining names
    the finalizers that have not been cleared. Reading them is free and far
    more useful than listing every custom resource in the cluster ourselves.
    """
    parts: list[str] = []
    for cond in getattr(ns.status, "conditions", None) or []:
        if cond.type in ("NamespaceContentRemaining", "NamespaceFinalizersRemaining") and cond.status == "True":
            message = (cond.message or "").strip()
            if message:
                parts.append(message)
    spec_finalizers = list(getattr(ns.spec, "finalizers", None) or [])
    if spec_finalizers:
        parts.append(f"spec.finalizers: {', '.join(spec_finalizers)}")
    return " ".join(parts) if parts else "no blocking condition reported by the API server"


def _scan_stuck_namespaces() -> list[dict]:
    findings: list[dict[str, Any]] = []
    core = get_core_client()
    namespaces = safe(lambda: core.list_namespace())
    if isinstance(namespaces, ToolError):
        return findings
    for ns in namespaces.items:
        age = _age_seconds(ns.metadata.deletion_timestamp)
        if age is None or age < _STUCK_AFTER_SECONDS:
            continue
        name = ns.metadata.name
        # Deliberately not filtered through _skip_namespace: a wedged
        # openshift-* or kube-* namespace is an operator problem the cluster
        # admin owns, and it is exactly the case the other scanners hide.
        findings.append(
            _make_finding(
                severity=_severity_for_age(age),
                category="stuck",
                title=f"Namespace {name} stuck terminating for {_humanise(age)}",
                summary=(
                    f"Deletion was requested {_humanise(age)} ago and has not completed. "
                    f"Blocked by: {_namespace_blockers(ns)}"
                ),
                resources=[{"kind": "Namespace", "name": name}],
                runbook_id="stuck-namespace-deletion",
                confidence=0.95,
            )
        )
    return findings


def _scan_stuck_pods(pods: Any = None) -> list[dict]:
    findings: list[dict[str, Any]] = []
    if pods is None:
        core = get_core_client()
        pods = safe(lambda: core.list_pod_for_all_namespaces())
        if isinstance(pods, ToolError):
            return findings
    for pod in pods.items:
        age = _age_seconds(pod.metadata.deletion_timestamp)
        if age is None or age < _STUCK_AFTER_SECONDS:
            continue
        ns = pod.metadata.namespace
        name = pod.metadata.name
        finalizers = list(pod.metadata.finalizers or [])
        grace = getattr(pod.spec, "termination_grace_period_seconds", None)
        if finalizers:
            cause = f"Remaining finalizers: {', '.join(finalizers)}."
        else:
            cause = "No finalizers remain — the kubelet is not confirming termination (node or runtime problem)."
        findings.append(
            _make_finding(
                severity=_severity_for_age(age),
                category="stuck",
                title=f"Pod {name} stuck terminating for {_humanise(age)}",
                summary=(f"Deletion was requested {_humanise(age)} ago; grace period is {grace or 30}s. {cause}"),
                resources=[{"kind": "Pod", "name": name, "namespace": ns}],
                runbook_id="stuck-pod-termination",
                confidence=0.9,
            )
        )
    return findings


def _scan_stuck_pvcs() -> list[dict]:
    findings: list[dict[str, Any]] = []
    core = get_core_client()
    claims = safe(lambda: core.list_persistent_volume_claim_for_all_namespaces())
    if isinstance(claims, ToolError):
        return findings
    for pvc in claims.items:
        age = _age_seconds(pvc.metadata.deletion_timestamp)
        if age is None or age < _STUCK_AFTER_SECONDS:
            continue
        ns = pvc.metadata.namespace
        name = pvc.metadata.name
        finalizers = list(pvc.metadata.finalizers or [])
        # kubernetes.io/pvc-protection is the usual one, and it means a pod is
        # still mounting the claim — actionable, and not a finalizer to force.
        cause = f"Remaining finalizers: {', '.join(finalizers)}." if finalizers else "No finalizers remain."
        findings.append(
            _make_finding(
                severity=_severity_for_age(age),
                category="stuck",
                title=f"PVC {name} stuck terminating for {_humanise(age)}",
                summary=f"Deletion was requested {_humanise(age)} ago and has not completed. {cause}",
                resources=[{"kind": "PersistentVolumeClaim", "name": name, "namespace": ns}],
                runbook_id="stuck-pvc-deletion",
                confidence=0.85,
            )
        )
    return findings


def _scan_stuck_crds() -> list[dict]:
    """CRDs mid-deletion — the Kuadrant case, caught directly.

    A CRD cannot be removed until every instance of it is gone, and every
    instance's finalizers are cleared. When one controller's finalizer never
    completes, the CRD sits in deletion indefinitely while the garbage
    collector retries — which is precisely what four months of API server load
    looked like.
    """
    findings: list[dict[str, Any]] = []
    custom = get_custom_client()
    result = safe(
        lambda: custom.list_cluster_custom_object(
            group="apiextensions.k8s.io", version="v1", plural="customresourcedefinitions"
        )
    )
    if isinstance(result, ToolError):
        return findings
    for crd in (result or {}).get("items", []):
        meta = crd.get("metadata", {})
        age = _age_seconds(meta.get("deletionTimestamp"))
        if age is None or age < _STUCK_AFTER_SECONDS:
            continue
        name = meta.get("name", "unknown")
        finalizers = list(meta.get("finalizers") or [])
        cause = (
            f"Remaining finalizers: {', '.join(finalizers)}."
            if finalizers
            else "No finalizers remain on the CRD; instances of it are still blocking removal."
        )
        findings.append(
            _make_finding(
                severity=_severity_for_age(age),
                category="stuck",
                title=f"CRD {name} stuck deleting for {_humanise(age)}",
                summary=(
                    f"Deletion was requested {_humanise(age)} ago. Until it completes, the garbage "
                    f"collector keeps retrying and every instance of this CRD stays live. {cause}"
                ),
                resources=[{"kind": "CustomResourceDefinition", "name": name}],
                runbook_id="stuck-crd-deletion",
                confidence=0.95,
            )
        )
    return findings


def scan_stuck_deletions(pods: Any = None) -> list[dict]:
    """Find resources whose deletion was requested but never completed.

    Each sub-scan is isolated: a permissions error listing CRDs must not cost
    us the namespace findings, which are the ones an operator can act on
    immediately.
    """
    findings: list[dict[str, Any]] = []
    for label, sub_scan in (
        ("namespaces", _scan_stuck_namespaces),
        ("pods", lambda: _scan_stuck_pods(pods)),
        ("PVCs", _scan_stuck_pvcs),
        ("CRDs", _scan_stuck_crds),
    ):
        try:
            findings.extend(sub_scan())
        except Exception as e:
            logger.error("Stuck-deletion scan failed for %s: %s", label, e)
    return findings


# ── Hot reconcile loops ───────────────────────────────────────────────────


def _query(promql: str) -> list[dict]:
    """Run a PromQL instant query, returning [] on any failure.

    Shares trend_scanners' client so a Prometheus outage is reported once by
    the existing trend_degraded finding rather than once per scanner.
    """
    from .trend_scanners import _query_prometheus

    return _query_prometheus(promql)


def _rate(result: dict) -> float | None:
    try:
        return float(result.get("value", [None, None])[1])
    except (TypeError, ValueError, IndexError):
        return None


def _scan_controller_retries() -> list[dict]:
    """Controllers whose work queue is retrying far faster than anything healthy.

    ``workqueue_retries_total`` counts re-queues after a failed reconcile. A
    busy-but-working controller adds items and finishes them; a wedged one
    retries the same item forever. Measured over an hour so a brief burst
    during a rollout cannot trip it.
    """
    findings: list[dict[str, Any]] = []
    query = f"sum by (name, namespace) (rate(workqueue_retries_total[1h])) > {_RETRY_RATE_WARNING}"
    for result in _query(query):
        rate = _rate(result)
        if rate is None:
            continue
        metric = result.get("metric", {})
        controller = metric.get("name", "unknown")
        ns = metric.get("namespace", "")
        findings.append(
            _make_finding(
                severity=SEVERITY_CRITICAL if rate >= _RETRY_RATE_CRITICAL else SEVERITY_WARNING,
                category="hot_loop",
                title=f"Controller {controller} retrying {int(rate)}/s",
                summary=(
                    f"The {controller} work queue has sustained {rate:.1f} retries/s for an hour. "
                    f"Retries at this rate mean reconciles are failing and being re-queued, not "
                    f"completing — the controller is spending API server capacity without making "
                    f"progress. Common causes: a finalizer that never clears, a missing dependency, "
                    f"or an RBAC denial on one object."
                ),
                # Not a Pod: this is a work-queue name from the controller's own
                # metrics. Labelling it Pod would send the UI hunting for an object
                # that does not exist, and would collide in the correlation key with
                # a real pod of the same name.
                resources=[{"kind": "Controller", "name": controller, "namespace": ns}],
                runbook_id="controller-hot-loop",
                confidence=0.8,
            )
        )
    return findings


def _scan_write_amplification() -> list[dict]:
    """Resource kinds being written far faster than a real workload would.

    This is the view that would have caught the Kuadrant finalizer from the
    symptom side: whatever the cause, a hot loop shows up here as write volume
    against one resource kind long before anyone traces it back.
    """
    findings: list[dict[str, Any]] = []
    excluded = "|".join(_WRITE_RATE_EXCLUDED_RESOURCES)
    query = (
        f"sum by (resource, verb, group) (rate(apiserver_request_total{{"
        f'verb=~"POST|PUT|PATCH|DELETE",resource!~"{excluded}"}}[1h])) > {_WRITE_RATE_WARNING}'
    )
    for result in _query(query):
        rate = _rate(result)
        if rate is None:
            continue
        metric = result.get("metric", {})
        resource = metric.get("resource", "unknown")
        verb = metric.get("verb", "")
        group = metric.get("group", "")
        qualified = f"{resource}.{group}" if group else resource
        findings.append(
            _make_finding(
                severity=SEVERITY_CRITICAL if rate >= _WRITE_RATE_CRITICAL else SEVERITY_WARNING,
                category="hot_loop",
                title=f"{verb} {qualified} at {int(rate)}/s against the API server",
                summary=(
                    f"{qualified} has sustained {rate:.1f} {verb} requests/s for an hour. No steady-state "
                    f"workload writes one resource kind this fast; this is a controller looping. "
                    f"Check for a stuck deletion or a reconcile that never reaches a stable state."
                ),
                resources=[{"kind": "APIResource", "name": qualified}],
                runbook_id="controller-hot-loop",
                confidence=0.75,
            )
        )
    return findings


def _scan_client_error_loops() -> list[dict]:
    """Pods retrying API calls that keep failing.

    A controller that loops on a 403 or a 404 never converges, and unlike a
    crash it leaves no restart count behind to notice.
    """
    findings: list[dict[str, Any]] = []
    query = (
        f'sum by (namespace, pod, code) (rate(rest_client_requests_total{{code=~"4..|5.."}}[1h])) '
        f"> {_CLIENT_ERROR_RATE_WARNING}"
    )
    for result in _query(query):
        rate = _rate(result)
        if rate is None:
            continue
        metric = result.get("metric", {})
        pod = metric.get("pod", "unknown")
        ns = metric.get("namespace", "")
        code = metric.get("code", "?")
        findings.append(
            _make_finding(
                severity=SEVERITY_WARNING,
                category="hot_loop",
                title=f"Pod {pod} retrying failed API calls at {int(rate)}/s",
                summary=(
                    f"{pod} is receiving {rate:.1f} HTTP {code} responses/s from the API server and "
                    f"retrying. The pod is running and not restarting, so no availability scanner "
                    f"sees it — but it is not making progress either. "
                    f"{'Check its RBAC.' if code.startswith('40') else 'Check API server health.'}"
                ),
                resources=[{"kind": "Pod", "name": pod, "namespace": ns}],
                runbook_id="controller-hot-loop",
                confidence=0.7,
            )
        )
    return findings


def scan_hot_reconcile_loops() -> list[dict]:
    """Find controllers burning API server capacity without making progress."""
    findings: list[dict[str, Any]] = []
    for label, sub_scan in (
        ("controller retries", _scan_controller_retries),
        ("write amplification", _scan_write_amplification),
        ("client error loops", _scan_client_error_loops),
    ):
        try:
            findings.extend(sub_scan())
        except Exception as e:
            logger.error("Hot-loop scan failed for %s: %s", label, e)
    return findings
