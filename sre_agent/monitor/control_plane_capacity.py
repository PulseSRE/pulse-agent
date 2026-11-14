"""Control-plane capacity: warn about masters and etcd before they break.

The trend scanners forecast workload nodes and volumes; ``control_plane``
(stuck_scanners) reports etcd and the API server once they have already
stalled. Between the two sat the case seen on a live 3-master cluster: masters
at 73% and 77% memory and etcd "slow" alerts firing, with no Pulse finding at
all, because nothing was past an incident threshold yet.

This scanner reports the early bands:

* control-plane node memory in use against allocatable, now and on a
  ``predict_linear`` forecast a day ahead;
* etcd WAL fsync and backend commit p99 against etcd's own hardware guidance;
* leader elections in the last hour (below the stall scanner's churn line);
* etcd database size against its backend quota, and how much of it is free
  space a defragmentation would give back.

Read-only: every input is a Prometheus instant query. A query that fails makes
the scan a failure (``report_failure``), never a clean result; a cluster with
no control-plane nodes visible (a hosted control plane) has nothing here to
watch and reports nothing. Findings never set ``auto_fixable``, and their
category (``control_plane``) has no auto-fix handler or fast-path strategy.
"""

from __future__ import annotations

import logging
from typing import Any

from .findings import _make_finding
from .registry import SEVERITY_CRITICAL, SEVERITY_WARNING
from .scanner_health import report_failure

logger = logging.getLogger("pulse_agent.monitor")

CATEGORY = "control_plane"

# ── Thresholds ────────────────────────────────────────────────────────────

# Control-plane node memory in use / allocatable. OpenShift's kube-apiserver
# operator ships ExtremelyHighIndividualControlPlaneMemory at 90% of a single
# master, so 90% is critical here too. 80% is Pulse's own early line: on a
# three-master cluster a master that fails hands its API and etcd client load
# to the other two, and at 80% they have no room to take it.
CP_MEMORY_WARNING = 0.80
CP_MEMORY_CRITICAL = 0.90
# Warn early when the last day's trend reaches the critical line within a day.
CP_MEMORY_FORECAST_THRESHOLD = 0.90
CP_MEMORY_FORECAST_HORIZON_SECONDS = 24 * 3600
# A full day of history, so a daily peak is part of the slope rather than the
# whole of it; 10-minute resolution keeps the subquery cheap.
CP_MEMORY_FORECAST_LOOKBACK = "24h:10m"

# etcd's documented disk guidance (etcd.io, "Hardware recommendations" /
# FAQ "What does the etcd warning 'failed to send out heartbeat on time'
# mean?", and OpenShift's "Recommended etcd practices"): the p99 of
# etcd_disk_wal_fsync_duration_seconds should stay below 10ms and the p99 of
# etcd_disk_backend_commit_duration_seconds below 25ms.
ETCD_FSYNC_WARNING = 0.010
# 5x the guidance. Pulse's own line, chosen because at 50ms a WAL fsync alone
# takes half of etcd's default 100ms heartbeat interval -- the upstream
# etcdHighFsyncDurations alert waits for 500ms, by which time elections have
# usually started.
ETCD_FSYNC_CRITICAL = 0.050
ETCD_COMMIT_WARNING = 0.025

# One election in an hour is worth saying out loud: a healthy etcd elects a
# leader once and keeps it. More than stuck_scanners._ETCD_LEADER_CHANGES_WARNING
# while still happening is a stall, and that scanner reports it.
ETCD_LEADER_CHANGES_WARNING = 1

# Database size / backend quota. etcd raises a NOSPACE alarm and refuses writes
# at 100%. 70% leaves time to act; 95% is where the upstream etcd-mixin
# etcdDatabaseQuotaLowSpace alert goes critical.
ETCD_DB_QUOTA_WARNING = 0.70
ETCD_DB_QUOTA_CRITICAL = 0.95
# At least this share of the file is free pages: defragmenting gives it back.
ETCD_DB_MOSTLY_FREE = 0.50

# Non-platform pods named when a master is short of memory.
_TOP_PODS = 3

# Firing alerts each check corresponds to. The firing-alerts scanner already
# raises these; this scanner only says which measurement is behind one.
_RELATED_ALERTS: dict[str, tuple[str, ...]] = {
    "memory": ("ExtremelyHighIndividualControlPlaneMemory", "HighOverallControlPlaneMemory"),
    "fsync": ("etcdHighFsyncDurations",),
    "commit": ("etcdHighCommitDurations",),
    "leader": ("etcdHighNumberOfLeaderChanges", "etcdMemberCommunicationSlow", "etcdNoLeader"),
    "db": ("etcdDatabaseQuotaLowSpace", "etcdExcessiveDatabaseGrowth", "etcdDatabaseHighFragmentationRatio"),
}

# ── Queries ───────────────────────────────────────────────────────────────

CP_NODES_QUERY = 'max by (node) (kube_node_role{role=~"master|control-plane"})'
# Memory in use as node-exporter sees it (MemTotal - MemAvailable): the figure
# OpenShift's control-plane memory alerts and the Pulse UI both use. node-exporter's
# instance label is the node name on OpenShift.
_USED = (
    "sum by (node) (label_replace(node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes, "
    '"node", "$1", "instance", "(.+)"))'
)
_ALLOCATABLE = 'sum by (node) (kube_node_status_allocatable{resource="memory"})'
CP_MEMORY_USED_QUERY = f"{_USED} and on (node) {CP_NODES_QUERY}"
CP_MEMORY_ALLOCATABLE_QUERY = f"{_ALLOCATABLE} and on (node) {CP_NODES_QUERY}"
CP_MEMORY_FORECAST_QUERY = (
    f"predict_linear((({_USED} / on (node) {_ALLOCATABLE}) and on (node) {CP_NODES_QUERY})"
    f"[{CP_MEMORY_FORECAST_LOOKBACK}], {CP_MEMORY_FORECAST_HORIZON_SECONDS})"
)


def _p99(metric: str) -> str:
    return f"histogram_quantile(0.99, sum by (instance, pod, le) (rate({metric}_bucket[15m])))"


ETCD_FSYNC_QUERY = _p99("etcd_disk_wal_fsync_duration_seconds")
ETCD_COMMIT_QUERY = _p99("etcd_disk_backend_commit_duration_seconds")
# Every member counts the same elections, so max, not sum, is the number.
ETCD_LEADER_CHANGES_QUERY = "max(increase(etcd_server_leader_changes_seen_total[1h]))"
ETCD_DB_TOTAL_QUERY = "max by (instance, pod) (etcd_mvcc_db_total_size_in_bytes)"
ETCD_DB_IN_USE_QUERY = "max by (instance, pod) (etcd_mvcc_db_total_size_in_use_in_bytes)"
ETCD_DB_QUOTA_QUERY = "max by (instance, pod) (etcd_server_quota_backend_bytes)"
RELATED_ALERTS_QUERY = 'ALERTS{alertstate="firing", alertname=~"etcd.+|.+ControlPlaneMemory"}'


def top_pods_query(node: str) -> str:
    """Heaviest non-platform pods on one node, by working set."""
    return (
        f"topk({_TOP_PODS}, sum by (namespace, pod) (container_memory_working_set_bytes"
        f'{{node="{node}", container!="", pod!="", namespace!~"openshift-.*|kube-.*|openshift"}}))'
    )


# ── Helpers ───────────────────────────────────────────────────────────────


def _query(promql: str) -> list[dict] | None:
    """Instant query; None when Prometheus did not answer (already reported)."""
    from .trend_scanners import query_prometheus_checked

    return query_prometheus_checked(promql)


def _optional_query(promql: str) -> list[dict] | None:
    """A query that only adds detail: its failure does not fail the scan."""
    from ..prometheus import get_prometheus_client

    try:
        data = get_prometheus_client().query(promql, timeout=15)
    except Exception:
        logger.debug("Optional control-plane query failed: %s", promql, exc_info=True)
        return None
    if data.get("status") != "success":
        return None
    result = data.get("data", {}).get("result", [])
    return result if isinstance(result, list) else None


def _value(sample: dict) -> float | None:
    try:
        value = float(sample.get("value", [None, None])[1])
    except (TypeError, ValueError, IndexError):
        return None
    if value != value or value in (float("inf"), float("-inf")):  # NaN / Inf
        return None
    return value


def _member(metric: dict) -> str:
    return metric.get("pod") or metric.get("instance") or "unknown"


def _by_member(results: list[dict]) -> dict[str, float]:
    out: dict[str, float] = {}
    for r in results:
        v = _value(r)
        if v is not None:
            out[_member(r.get("metric", {}))] = v
    return out


def _by_node(results: list[dict]) -> dict[str, float]:
    out: dict[str, float] = {}
    for r in results:
        node = r.get("metric", {}).get("node")
        v = _value(r)
        if node and v is not None:
            out[node] = v
    return out


def _gib(n: float) -> str:
    return f"{n / 1024**3:.1f} GiB"


def _ms(seconds: float) -> str:
    return f"{seconds * 1000:.0f}ms"


def _alert_note(check: str, firing: list[dict], *keys: str) -> str:
    """'Prometheus is also firing X.' when an alert behind this check is firing.

    An alert that names a member or node only counts for the finding about
    that one; an alert naming neither is cluster-wide and counts everywhere,
    and a cluster-wide finding (no ``keys``) takes every related alert.
    """
    names = _RELATED_ALERTS.get(check, ())
    matched: list[str] = []
    for alert in firing:
        labels = alert.get("metric", {})
        name = labels.get("alertname", "")
        if name not in names or name in matched:
            continue
        targets = {labels[k] for k in ("pod", "instance", "node") if labels.get(k)}
        if keys and targets and not targets & set(keys):
            continue
        matched.append(name)
    if not matched:
        return ""
    return f" Prometheus is also firing {', '.join(matched)}; this is the measurement behind it."


# ── Control-plane node memory ─────────────────────────────────────────────


def _scan_memory(nodes: list[str], firing: list[dict]) -> list[dict]:
    used_r = _query(CP_MEMORY_USED_QUERY)
    alloc_r = _query(CP_MEMORY_ALLOCATABLE_QUERY)
    if used_r is None or alloc_r is None:
        return []
    forecast_r = _query(CP_MEMORY_FORECAST_QUERY)
    used, alloc = _by_node(used_r), _by_node(alloc_r)
    forecast = _by_node(forecast_r or [])

    unmeasured = sorted(n for n in nodes if n not in used or not alloc.get(n))
    if unmeasured:
        # A master Pulse cannot measure is unknown, not fine.
        report_failure(
            "Prometheus returned no memory-in-use or allocatable series for control-plane node(s) "
            + ", ".join(unmeasured)
        )

    findings: list[dict] = []
    for node in sorted(n for n in nodes if n not in unmeasured):
        ratio = used[node] / alloc[node]
        predicted = forecast.get(node)
        heading_for_critical = predicted is not None and predicted >= CP_MEMORY_FORECAST_THRESHOLD and predicted > ratio
        if ratio < CP_MEMORY_WARNING and not heading_for_critical:
            continue

        measured = (
            f"{node} has {_gib(used[node])} of memory in use against {_gib(alloc[node])} allocatable ({ratio:.0%})."
        )
        trend = ""
        if predicted is not None and predicted > ratio:
            trend = f" On the last day's trend it reaches {predicted:.0%} by this time tomorrow."
        next_step = _memory_next_step(node)
        note = _alert_note("memory", firing, node)

        if ratio >= CP_MEMORY_WARNING:
            severity = SEVERITY_CRITICAL if ratio >= CP_MEMORY_CRITICAL else SEVERITY_WARNING
            findings.append(
                _make_finding(
                    severity=severity,
                    category=CATEGORY,
                    title=f"Control-plane node {node} memory at {ratio:.0%} of allocatable",
                    summary=(
                        f"{measured}{trend} The API server and etcd on this master need headroom to take "
                        f"over a failed peer's load, and a master that runs out of memory takes API server "
                        f"and etcd capacity down with it.{note} Next step: {next_step}"
                    ),
                    resources=[{"kind": "Node", "name": node}],
                    confidence=0.9,
                )
            )
        else:
            findings.append(
                _make_finding(
                    severity=SEVERITY_WARNING,
                    category=CATEGORY,
                    title=f"Control-plane node {node} memory on course for {CP_MEMORY_FORECAST_THRESHOLD:.0%} within a day",
                    summary=(
                        f"{measured}{trend} That is a linear forecast from the last 24 hours, not a "
                        f"measurement: nothing is broken yet.{note} Next step: {next_step}"
                    ),
                    resources=[{"kind": "Node", "name": node}],
                    confidence=0.7,
                    finding_type="trend",
                    # A forecast explains nothing happening now, and is no
                    # one's symptom either.
                    posture=True,
                )
            )
    return findings


def _memory_next_step(node: str) -> str:
    pods = _optional_query(top_pods_query(node))
    if pods is None:
        return (
            f"see what is using memory on {node} (Pulse could not list its pods' usage); move non-platform "
            "pods off the masters, or move the control plane to a larger machine size."
        )
    heavy = []
    for p in pods:
        v = _value(p)
        m = p.get("metric", {})
        if v and m.get("pod"):
            heavy.append(f"{m.get('namespace', '')}/{m['pod']} ({_gib(v)})")
    if heavy:
        return (
            f"non-platform pods are running on this master: {', '.join(heavy)}. Move them to workers "
            "(drop the toleration or nodeSelector that lets them land on masters), and check whether masters "
            "are schedulable at all (mastersSchedulable on the cluster Scheduler)."
        )
    return (
        "everything using memory here is platform, so the control plane has outgrown its machines: move to a "
        "larger control-plane machine size (on OpenShift, through the ControlPlaneMachineSet, one master at a "
        "time)."
    )


# ── etcd ──────────────────────────────────────────────────────────────────


def _scan_etcd_disk(firing: list[dict]) -> tuple[list[dict], bool]:
    """WAL fsync and backend commit p99.

    The second value is False when Prometheus answered both queries with no
    series at all: etcd is not being scraped, which is not the same as healthy.
    """
    findings: list[dict] = []
    fsync_r = _query(ETCD_FSYNC_QUERY)
    commit_r = _query(ETCD_COMMIT_QUERY)

    disk_step = (
        "check the control-plane disk class and its provisioned IOPS/throughput (on cloud volumes, a burst "
        "balance running out is the usual cause), and whether anything else on the master writes to the same "
        "disk. etcd needs a dedicated, low-latency SSD."
    )
    for member, seconds in sorted(_by_member(fsync_r or []).items()):
        if seconds <= ETCD_FSYNC_WARNING:
            continue
        severity = SEVERITY_CRITICAL if seconds > ETCD_FSYNC_CRITICAL else SEVERITY_WARNING
        findings.append(
            _make_finding(
                severity=severity,
                category=CATEGORY,
                title=f"etcd write-ahead log fsync slow on {member} ({_ms(seconds)} p99)",
                summary=(
                    f"etcd member {member} takes {_ms(seconds)} at p99 to fsync its write-ahead log over the "
                    f"last 15 minutes; etcd's guidance is under {_ms(ETCD_FSYNC_WARNING)}. Every write waits "
                    f"for this, and slow fsync is what leads to missed heartbeats and leader elections."
                    f"{_alert_note('fsync', firing, member)} Next step: {disk_step}"
                ),
                resources=[{"kind": "Etcd", "name": member}],
                runbook_id="control-plane-stall",
                confidence=0.9,
            )
        )

    from .stuck_scanners import _ETCD_COMMIT_WARNING as STALL_COMMIT

    for member, seconds in sorted(_by_member(commit_r or []).items()):
        # From the stall scanner's line up, that scanner reports it.
        if seconds <= ETCD_COMMIT_WARNING or seconds > STALL_COMMIT:
            continue
        findings.append(
            _make_finding(
                severity=SEVERITY_WARNING,
                category=CATEGORY,
                title=f"etcd backend commit slow on {member} ({_ms(seconds)} p99)",
                summary=(
                    f"etcd member {member} takes {_ms(seconds)} at p99 to commit to its database over the last "
                    f"15 minutes; etcd's guidance is under {_ms(ETCD_COMMIT_WARNING)}. The API server cannot "
                    f"acknowledge a write until it commits, so this shows up as API latency."
                    f"{_alert_note('commit', firing, member)} Next step: {disk_step}"
                ),
                resources=[{"kind": "Etcd", "name": member}],
                runbook_id="control-plane-stall",
                confidence=0.85,
            )
        )
    unscraped = fsync_r == [] and commit_r == []
    return findings, not unscraped


def _scan_etcd_leader(firing: list[dict]) -> list[dict]:
    changes_r = _query(ETCD_LEADER_CHANGES_QUERY)
    if not changes_r:
        return []
    value = _value(changes_r[0])
    count = round(value) if value is not None else 0
    if count < ETCD_LEADER_CHANGES_WARNING:
        return []

    from .stuck_scanners import etcd_leader_churn_query

    stall = _query(etcd_leader_churn_query())
    if stall is None or stall:
        # The stall scanner owns churn that is sustained and still happening
        # (or we cannot tell whether it does: no second finding on a guess).
        return []
    times = "time" if count == 1 else "times"
    return [
        _make_finding(
            severity=SEVERITY_WARNING,
            category=CATEGORY,
            title=f"etcd changed leader {count} {times} in the last hour",
            summary=(
                f"etcd elected a new leader {count} {times} in the last hour; a healthy etcd keeps one leader. "
                f"Each election pauses writes cluster-wide for a moment.{_alert_note('leader', firing)} "
                "Next step: elections follow slow disks or a slow network between masters, so look at this "
                "scanner's fsync/commit findings and etcd peer round-trip time; if both are normal, check "
                "whether a master rebooted or an etcd pod restarted in the last hour."
            ),
            resources=[{"kind": "Etcd", "name": "cluster"}],
            runbook_id="control-plane-stall",
            confidence=0.85,
        )
    ]


def _scan_etcd_db(firing: list[dict]) -> list[dict]:
    total_r = _query(ETCD_DB_TOTAL_QUERY)
    quota_r = _query(ETCD_DB_QUOTA_QUERY)
    if total_r is None or quota_r is None:
        return []
    in_use = _by_member(_query(ETCD_DB_IN_USE_QUERY) or [])
    quota = _by_member(quota_r)
    findings: list[dict] = []
    for member, size in sorted(_by_member(total_r).items()):
        limit = quota.get(member)
        if not limit:
            continue
        ratio = size / limit
        if ratio < ETCD_DB_QUOTA_WARNING:
            continue
        live = in_use.get(member)
        if live is not None and size > 0 and (size - live) / size >= ETCD_DB_MOSTLY_FREE:
            usage = f" Only {_gib(live)} of it is live data; the rest is free pages left behind by deletes."
            next_step = (
                "defragment etcd to give the free space back. OpenShift's etcd operator defragments on its own "
                "when it judges it safe; if it has not, defragment one member at a time, the leader last, as "
                "the OpenShift etcd documentation describes."
            )
        else:
            usage = f" {_gib(live)} of it is live data, so defragmenting will not help much." if live else ""
            next_step = (
                "find which resource types hold the most objects (Events, old ReplicaSets, Secrets and "
                "ConfigMaps from old releases, custom resources a controller never cleans up) and prune them."
            )
        critical = ratio >= ETCD_DB_QUOTA_CRITICAL
        findings.append(
            _make_finding(
                severity=SEVERITY_CRITICAL if critical else SEVERITY_WARNING,
                category=CATEGORY,
                title=f"etcd database on {member} at {ratio:.0%} of its quota",
                summary=(
                    f"etcd member {member}'s database file is {_gib(size)} against a backend quota of "
                    f"{_gib(limit)} ({ratio:.0%}).{usage} At 100% etcd raises a NOSPACE alarm and refuses "
                    f"every write.{_alert_note('db', firing, member)} Next step: {next_step}"
                ),
                resources=[{"kind": "Etcd", "name": member}],
                runbook_id="control-plane-stall",
                confidence=0.9,
                # Short of the quota it has not broken anything yet.
                posture=not critical,
            )
        )
    return findings


# ── Entry point ───────────────────────────────────────────────────────────


def scan_control_plane_capacity() -> list[dict]:
    """Control-plane memory and etcd headroom, reported before either breaks."""
    findings: list[dict[str, Any]] = []
    try:
        nodes_r = _query(CP_NODES_QUERY)
        if nodes_r is None:
            return findings
        nodes = sorted({r.get("metric", {}).get("node", "") for r in nodes_r} - {""})
        if not nodes:
            # No control-plane nodes in this cluster (a hosted control plane):
            # nothing of the control plane is here to watch.
            return findings
        firing = _optional_query(RELATED_ALERTS_QUERY) or []
    except Exception as e:
        logger.error("Control-plane capacity scan failed: %s", e)
        report_failure(e)
        return findings

    for label, sub_scan in (
        ("control-plane memory", lambda: _scan_memory(nodes, firing)),
        ("etcd leader", lambda: _scan_etcd_leader(firing)),
        ("etcd database", lambda: _scan_etcd_db(firing)),
    ):
        try:
            findings.extend(sub_scan())
        except Exception as e:
            logger.error("Control-plane capacity scan failed for %s: %s", label, e)
            report_failure(e)

    try:
        disk, scraped = _scan_etcd_disk(firing)
        findings.extend(disk)
        if not scraped:
            # Masters exist and Prometheus answers, yet etcd reports nothing.
            report_failure("Prometheus returned no etcd disk latency series although control-plane nodes exist")
    except Exception as e:
        logger.error("Control-plane capacity scan failed for etcd disk: %s", e)
        report_failure(e)
    return findings
