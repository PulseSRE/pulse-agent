"""What normal looks like for each workload, and findings measured against it.

``WorkloadBaseline`` and its table have existed since migration 032, but nothing
ever recorded one, so every scanner judged every workload against the same fixed
thresholds. 2 GiB is an emergency for a sidecar and a quiet afternoon for a JVM.
This module fills the table from Prometheus and raises a finding only when a
workload is well outside its *own* range.

Recording: every ``REFRESH_SECONDS``, one quantile query per metric over the
last ``WINDOW`` at ``STEP`` resolution, per pod, folded into one row per
workload (pod-template hash stripped, so replicas and recreated pods share it).

Detection: a pod is anomalous when the current value is above the workload's
p95 *and* at least ``RATIO`` times its p50, on a baseline with enough samples
to mean something. Both, because p95 alone flags every routine peak and a
ratio alone flags a workload that is always spiky.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from .confidence import _strip_pod_hash
from .findings import _make_finding
from .registry import SEVERITY_WARNING
from .scanner_health import report_failure

logger = logging.getLogger("pulse_agent.monitor")

WINDOW = "7d"
STEP = "1h"
REFRESH_SECONDS = 6 * 3600
RATIO = 2.0


@dataclass(frozen=True)
class BaselineMetric:
    name: str
    expr: str
    unit: str
    # Below this an observation is not worth a finding however far it is from
    # normal: a pod going from 2m to 9m CPU is "4.5x" and means nothing.
    floor: float

    def render(self, value: float) -> str:
        if self.unit == "bytes":
            for suffix, size in (("GiB", 1 << 30), ("MiB", 1 << 20), ("KiB", 1 << 10)):
                if value >= size:
                    return f"{value / size:.1f} {suffix}"
            return f"{value:.0f} B"
        if self.unit == "cores":
            return f"{value * 1000:.0f}m CPU" if value < 1 else f"{value:.2f} cores"
        return f"{value:.1f} {self.unit}"


METRICS: tuple[BaselineMetric, ...] = (
    BaselineMetric(
        "memory_bytes",
        'sum by (namespace, pod) (container_memory_working_set_bytes{container!="",container!="POD"})',
        "bytes",
        floor=64 * (1 << 20),
    ),
    BaselineMetric(
        "cpu_cores",
        'sum by (namespace, pod) (rate(container_cpu_usage_seconds_total{container!="",container!="POD"}[5m]))',
        "cores",
        floor=0.05,
    ),
    BaselineMetric(
        "restarts_per_hour",
        "sum by (namespace, pod) (increase(kube_pod_container_status_restarts_total[1h]))",
        "restarts/h",
        floor=3,
    ),
)
_BY_NAME = {m.name: m for m in METRICS}

_last_refresh = 0.0
_refresh_lock = threading.Lock()


def _client():
    from ..prometheus import get_prometheus_client

    return get_prometheus_client()


def _vector(client: Any, promql: str, timeout: int = 60) -> list[dict]:
    data = client.query(promql, timeout=timeout)
    if data.get("status") != "success":
        raise RuntimeError(f"Prometheus query failed: {data.get('error', 'unknown')}")
    return data.get("data", {}).get("result", [])


def _per_workload(series: list[dict]) -> dict[tuple[str, str], list[float]]:
    out: dict[tuple[str, str], list[float]] = {}
    for s in series:
        metric = s.get("metric", {})
        ns, pod = metric.get("namespace", ""), metric.get("pod", "")
        if not ns or not pod:
            continue
        try:
            value = float(s["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if value != value:  # NaN
            continue
        out.setdefault((ns, _strip_pod_hash(pod)), []).append(value)
    return out


def _median(values: list[float]) -> float:
    v = sorted(values)
    mid = len(v) // 2
    return v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2


def refresh_baselines(client: Any = None, memory: Any = None) -> int:
    """Recompute every workload's baseline. Returns how many rows were written."""
    from ..memory.environment import ClusterMemory, WorkloadBaseline
    from .scanners import _skip_namespace

    client = client or _client()
    memory = memory or ClusterMemory()
    written = 0
    window_hours = int(WINDOW.rstrip("d")) * 24
    for metric in METRICS:
        sub = f"({metric.expr})[{WINDOW}:{STEP}]"
        p50 = _per_workload(_vector(client, f"quantile_over_time(0.5, {sub})"))
        p95 = _per_workload(_vector(client, f"quantile_over_time(0.95, {sub})"))
        counts = _per_workload(_vector(client, f"count_over_time({sub})"))
        for key, medians in p50.items():
            ns, workload = key
            if _skip_namespace(ns):
                continue
            baseline = WorkloadBaseline(
                namespace=ns,
                workload=workload,
                metric=metric.name,
                # Replicas of one workload: the typical pod's median, and the
                # highest p95 any of them reached.
                p50=_median(medians),
                p95=max(p95.get(key, medians)),
                sample_count=int(max(counts.get(key, [0]))),
                window_hours=window_hours,
            )
            if memory.record_baseline(baseline):
                written += 1
    logger.info("Workload baselines refreshed: %d rows", written)
    return written


def _refresh_in_background() -> None:
    global _last_refresh
    if not _refresh_lock.acquire(blocking=False):
        return
    try:
        refresh_baselines()
        _last_refresh = time.time()
    except Exception:
        # Retried next cycle rather than in REFRESH_SECONDS: a failed refresh
        # leaves the table as stale as it was.
        logger.warning("Workload baseline refresh failed", exc_info=True)
    finally:
        _refresh_lock.release()


def maybe_refresh(now: float | None = None) -> bool:
    """Start a refresh off the scan path if the baselines are stale."""
    now = time.time() if now is None else now
    if now - _last_refresh < REFRESH_SECONDS or _refresh_lock.locked():
        return False
    threading.Thread(target=_refresh_in_background, name="baseline-refresh", daemon=True).start()
    return True


def is_anomalous(baseline: Any, observed: float, metric: BaselineMetric) -> bool:
    if not baseline.is_reliable or observed < metric.floor:
        return False
    if baseline.p50 <= 0:
        # Normally zero (restarts): only the p95 can say what is unusual.
        return observed > max(baseline.p95, 0) * RATIO and observed >= metric.floor
    return observed > baseline.p95 and observed >= RATIO * baseline.p50


def scan_workload_anomalies(client: Any = None, memory: Any = None) -> list[dict]:
    """Pods well outside their own workload's normal range."""
    from ..memory.environment import ClusterMemory
    from .scanners import _skip_namespace

    findings: list[dict] = []
    try:
        maybe_refresh()
        client = client or _client()
        memory = memory or ClusterMemory()
        for metric in METRICS:
            for s in _vector(client, metric.expr, timeout=30):
                m = s.get("metric", {})
                ns, pod = m.get("namespace", ""), m.get("pod", "")
                if not ns or not pod or _skip_namespace(ns):
                    continue
                try:
                    observed = float(s["value"][1])
                except (KeyError, IndexError, TypeError, ValueError):
                    continue
                workload = _strip_pod_hash(pod)
                baseline = memory.get_baseline(ns, workload, metric.name)
                if baseline is None or not is_anomalous(baseline, observed, metric):
                    continue
                normal = metric.render(baseline.p50)
                ratio = f"{observed / baseline.p50:.1f}x" if baseline.p50 > 0 else "far above"
                findings.append(
                    _make_finding(
                        severity=SEVERITY_WARNING,
                        category="anomaly",
                        title=f"{workload} {metric.name.replace('_', ' ')} well above its normal",
                        summary=(
                            f"Pod {pod} is at {metric.render(observed)}, {ratio} this workload's normal "
                            f"({normal}; its usual peak is {metric.render(baseline.p95)} over the last "
                            f"{baseline.window_hours // 24} days)."
                        ),
                        resources=[{"kind": "Pod", "name": pod, "namespace": ns}],
                        confidence=0.75,
                    )
                )
    except Exception as e:
        logger.warning("Workload anomaly scan failed: %s", e)
        report_failure(e)
    return findings


def baseline_context(resources: list[dict], memory: Any = None) -> str:
    """One line per known baseline for a finding's workloads, for an investigation prompt."""
    from ..memory.environment import ClusterMemory

    memory = memory or ClusterMemory()
    lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    for r in resources[:5]:
        ns, name = r.get("namespace", ""), r.get("name", "")
        if not ns or not name:
            continue
        workload = _strip_pod_hash(name) if r.get("kind") == "Pod" else name
        if (ns, workload) in seen:
            continue
        seen.add((ns, workload))
        for b in memory.list_baselines(ns, workload):
            if not b.is_reliable or b.metric not in _BY_NAME:
                continue
            m = _BY_NAME[b.metric]
            lines.append(f"{ns}/{workload} {b.metric}: normally {m.render(b.p50)}, usual peak {m.render(b.p95)}")
    return "\n".join(lines)
