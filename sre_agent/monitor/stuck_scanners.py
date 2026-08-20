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
from .scanner_health import report_failure

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


# A long window is right for deciding something is real, and wrong for deciding
# it is over. increase(...[1h]) keeps reporting a problem for a full hour after
# it stops: a finding raised at 16:49 was still "true" at 17:22 with zero
# failures in the preceding fifteen minutes, so an operator whose cluster had
# already recovered had no way to make the card go away.
#
# Every windowed check is therefore two. The long window says the problem is
# real; a short one says it is still happening. Detect slowly, clear quickly.
_RECENT_WINDOW = "15m"


def _sustained_and_current(long_expr: str, short_expr: str) -> str:
    """Fire only while the sustained and the recent views agree.

    PromQL `and` matches on labels, so both sides carry the same grouping.
    """
    return f"({long_expr}) and ({short_expr})"


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
            report_failure(e)
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

    def retries(window: str) -> str:
        return f"sum by (name, namespace) (rate(workqueue_retries_total[{window}]))"

    query = _sustained_and_current(
        f"{retries('1h')} > {_RETRY_RATE_WARNING}",
        f"{retries(_RECENT_WINDOW)} > {_RETRY_RATE_WARNING}",
    )
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

    # Built with an f-string per window rather than .format(): the label
    # selector contains braces of its own and .format() reads them as fields.
    def writes(window: str) -> str:
        return (
            f"sum by (resource, verb, group) (rate(apiserver_request_total{{"
            f'verb=~"POST|PUT|PATCH|DELETE",resource!~"{excluded}"}}[{window}]))'
        )

    query = _sustained_and_current(
        f"{writes('1h')} > {_WRITE_RATE_WARNING}",
        f"{writes(_RECENT_WINDOW)} > {_WRITE_RATE_WARNING}",
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

    def client_errors(window: str) -> str:
        return f'sum by (namespace, pod, code) (rate(rest_client_requests_total{{code=~"4..|5.."}}[{window}]))'

    query = _sustained_and_current(
        f"{client_errors('1h')} > {_CLIENT_ERROR_RATE_WARNING}",
        f"{client_errors(_RECENT_WINDOW)} > {_CLIENT_ERROR_RATE_WARNING}",
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
            report_failure(e)
    return findings


# ── Control-plane stalls ──────────────────────────────────────────────────
# Added after an incident the two scanners above walked straight past. etcd
# peer latency spiked, the API server's p99 went from 20ms to the 60s ceiling
# for fifteen minutes, liveness probes timed out, and the kubelet SIGKILLed
# 135 containers across all six nodes in thirteen minutes. Every workload-level
# scanner saw the restarts; none could say why, because the cause was a layer
# below anything they measure.
#
# Every threshold below is the midpoint of a measured healthy value and a
# measured incident value on the same cluster, recorded in the comments.

# Healthy: 0 in 6h. Incident: 12 in 6h. Any leader change stalls writes
# cluster-wide, so a couple in an hour is already worth knowing about.
_ETCD_LEADER_CHANGES_WARNING = 2.0

# Healthy: 0. Incident: 1,667 in 6h across three members. A failed proposal is
# a write etcd refused — it surfaces to clients as a 500 on a lease renewal,
# which is how leader election and node heartbeats start failing.
_ETCD_FAILED_PROPOSALS_WARNING = 10.0

# Healthy: 12ms across AZs. Incident: 3,280ms. etcd's own heartbeat interval is
# 100ms, so anything approaching that costs elections.
_ETCD_PEER_RTT_WARNING = 0.25
_ETCD_PEER_RTT_CRITICAL = 1.0

# Healthy: 45ms. Incident: 2,048ms. This is the disk, and on cloud volumes it
# is usually IOPS throttling rather than a failing device.
_ETCD_COMMIT_WARNING = 0.5
_ETCD_COMMIT_CRITICAL = 1.5

# Healthy: 0.02 to 0.07s. Incident: pegged at 60s, the request timeout ceiling.
_APISERVER_LATENCY_WARNING = 5.0
_APISERVER_LATENCY_CRITICAL = 30.0

# A cluster-scoped LIST returns every object of its kind, which is why it costs
# so much more than the namespaced form. Healthy ceiling on the reference
# cluster is 1.0/s (projects); the two admission-webhook configs sit at
# 2.4 to 5.2/s around the clock, which is a controller re-listing instead of
# watching. 2/s is above everything legitimate and below that pair.
_CLUSTER_LIST_RATE_WARNING = 2.0


def _scan_etcd_consensus() -> list[dict]:
    """etcd losing or re-running leader elections, and writes it would not accept.

    Checked before latency because a member that cannot hold leadership makes
    every other control-plane number meaningless.
    """
    findings: list[dict[str, Any]] = []

    def leader(window: str) -> str:
        return f"sum(increase(etcd_server_leader_changes_seen_total[{window}]))"

    for result in _query(
        _sustained_and_current(
            f"{leader('1h')} > {_ETCD_LEADER_CHANGES_WARNING}",
            f"{leader(_RECENT_WINDOW)} > 0",
        )
    ):
        rate = _rate(result)
        if rate is None:
            continue
        findings.append(
            _make_finding(
                severity=SEVERITY_CRITICAL,
                category="control_plane",
                title=f"etcd changed leader {int(rate)} times in an hour",
                summary=(
                    f"A healthy etcd cluster elects a leader once and keeps it. {int(rate)} elections in "
                    f"an hour means members are failing to reach each other or to commit in time; every "
                    f"election stalls writes cluster-wide, which surfaces as API server timeouts and "
                    f"controllers losing leadership. Check peer latency and disk commit times first."
                ),
                resources=[{"kind": "Etcd", "name": "cluster"}],
                runbook_id="control-plane-stall",
                confidence=0.9,
            )
        )

    for result in _query(
        _sustained_and_current(
            f"sum by (instance) (increase(etcd_server_proposals_failed_total[1h])) > {_ETCD_FAILED_PROPOSALS_WARNING}",
            f"sum by (instance) (increase(etcd_server_proposals_failed_total[{_RECENT_WINDOW}])) > 0",
        )
    ):
        rate = _rate(result)
        if rate is None:
            continue
        instance = result.get("metric", {}).get("instance", "unknown")
        findings.append(
            _make_finding(
                severity=SEVERITY_CRITICAL,
                category="control_plane",
                title=f"etcd member {instance} refused {int(rate)} writes in an hour",
                summary=(
                    "Failed proposals are writes etcd would not accept, normally because leadership was "
                    "in flux while they were in flight. Clients see them as HTTP 500 and 504, and the "
                    "first casualties are lease renewals — leader election and node heartbeats."
                ),
                resources=[{"kind": "Etcd", "name": instance}],
                runbook_id="control-plane-stall",
                confidence=0.85,
            )
        )

    return findings


def _scan_etcd_latency() -> list[dict]:
    """The two etcd latencies that cause elections: peer network and disk commit."""
    findings: list[dict[str, Any]] = []

    peer_query = (
        "histogram_quantile(0.99, sum by (instance, le) "
        f"(rate(etcd_network_peer_round_trip_time_seconds_bucket[15m]))) > {_ETCD_PEER_RTT_WARNING}"
    )
    for result in _query(peer_query):
        seconds = _rate(result)
        if seconds is None:
            continue
        instance = result.get("metric", {}).get("instance", "unknown")
        findings.append(
            _make_finding(
                severity=SEVERITY_CRITICAL if seconds >= _ETCD_PEER_RTT_CRITICAL else SEVERITY_WARNING,
                category="control_plane",
                title=f"etcd peer latency {int(seconds * 1000)}ms from {instance}",
                summary=(
                    f"Round-trip time to this member's peers is {seconds * 1000:.0f}ms at p99. etcd's "
                    f"heartbeat interval is 100ms, so at this latency members declare each other dead and "
                    f"elect a new leader. Across availability zones, tens of milliseconds is normal and "
                    f"hundreds is not — look at the network path, not at etcd."
                ),
                resources=[{"kind": "Etcd", "name": instance}],
                runbook_id="control-plane-stall",
                confidence=0.85,
            )
        )

    commit_query = (
        "histogram_quantile(0.99, sum by (instance, le) "
        f"(rate(etcd_disk_backend_commit_duration_seconds_bucket[15m]))) > {_ETCD_COMMIT_WARNING}"
    )
    for result in _query(commit_query):
        seconds = _rate(result)
        if seconds is None:
            continue
        instance = result.get("metric", {}).get("instance", "unknown")
        findings.append(
            _make_finding(
                severity=SEVERITY_CRITICAL if seconds >= _ETCD_COMMIT_CRITICAL else SEVERITY_WARNING,
                category="control_plane",
                title=f"etcd disk commit {int(seconds * 1000)}ms on {instance}",
                summary=(
                    f"Backend commit p99 is {seconds * 1000:.0f}ms, against tens of milliseconds on healthy "
                    f"hardware. etcd cannot acknowledge a write until it commits, so this becomes API "
                    f"server latency directly. On cloud volumes the usual cause is IOPS throttling — a "
                    f"burst balance running out — rather than a failing disk."
                ),
                resources=[{"kind": "Etcd", "name": instance}],
                runbook_id="control-plane-stall",
                confidence=0.85,
            )
        )

    return findings


def _scan_apiserver_latency() -> list[dict]:
    """The API server taking so long that probes and heartbeats give up.

    WATCH and CONNECT are excluded because they are long-lived by design and
    would sit above any threshold that means anything for the rest.
    """
    findings: list[dict[str, Any]] = []
    query = (
        "histogram_quantile(0.99, sum by (le) (rate(apiserver_request_duration_seconds_bucket"
        f'{{verb!~"WATCH|WATCHLIST|CONNECT"}}[5m]))) > {_APISERVER_LATENCY_WARNING}'
    )
    for result in _query(query):
        seconds = _rate(result)
        if seconds is None:
            continue
        findings.append(
            _make_finding(
                severity=SEVERITY_CRITICAL if seconds >= _APISERVER_LATENCY_CRITICAL else SEVERITY_WARNING,
                category="control_plane",
                title=f"API server p99 latency {seconds:.0f}s",
                summary=(
                    f"Requests are taking {seconds:.0f}s at p99, against tens of milliseconds normally. "
                    f"Liveness probes and lease renewals time out well before this, so the kubelet starts "
                    f"killing containers and controllers start losing leadership — a burst of restarts "
                    f"across unrelated namespaces is the symptom, not the cause. Check etcd first."
                ),
                resources=[{"kind": "APIServer", "name": "kube-apiserver"}],
                runbook_id="control-plane-stall",
                confidence=0.9,
            )
        )
    return findings


def _scan_read_amplification() -> list[dict]:
    """Cluster-scoped LISTs at a rate no controller should need.

    The write-amplification check misses this entirely, and reads are what
    actually cost an API server: a cluster-scoped LIST returns every object of
    its kind, decoded into memory, every time.
    """
    findings: list[dict[str, Any]] = []

    def lists(window: str) -> str:
        return f'sum by (resource, group) (rate(apiserver_request_total{{verb="LIST",scope="cluster"}}[{window}]))'

    query = _sustained_and_current(
        f"{lists('1h')} > {_CLUSTER_LIST_RATE_WARNING}",
        f"{lists(_RECENT_WINDOW)} > {_CLUSTER_LIST_RATE_WARNING}",
    )
    for result in _query(query):
        rate = _rate(result)
        if rate is None:
            continue
        metric = result.get("metric", {})
        resource = metric.get("resource", "unknown")
        group = metric.get("group", "")
        qualified = f"{resource}.{group}" if group else resource
        findings.append(
            _make_finding(
                severity=SEVERITY_WARNING,
                category="control_plane",
                title=f"{qualified} listed cluster-wide {rate:.1f}/s",
                summary=(
                    f"Something is fetching every {qualified} in the cluster {rate:.1f} times a second — "
                    f"{int(rate * 3600):,} full collection reads an hour. A controller that needs current "
                    f"state should hold a watch, not re-list; re-listing this often is a bug in the "
                    f"controller and a standing cost on API server memory and CPU."
                ),
                resources=[{"kind": "APIResource", "name": qualified}],
                runbook_id="control-plane-stall",
                confidence=0.75,
            )
        )
    return findings


def scan_control_plane_stalls() -> list[dict]:
    """Find the control plane failing underneath workloads that look merely flaky."""
    findings: list[dict[str, Any]] = []
    for label, sub_scan in (
        ("etcd consensus", _scan_etcd_consensus),
        ("etcd latency", _scan_etcd_latency),
        ("API server latency", _scan_apiserver_latency),
        ("read amplification", _scan_read_amplification),
    ):
        try:
            findings.extend(sub_scan())
        except Exception as e:
            logger.error("Control-plane scan failed for %s: %s", label, e)
            report_failure(e)
    return findings
