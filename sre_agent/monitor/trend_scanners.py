"""Trend-based predictive scanners using Prometheus predict_linear() and derivatives."""

from __future__ import annotations

import logging
import time
from typing import Any

from ..prometheus import PrometheusConfigError, get_prometheus_client
from .findings import _make_finding
from .registry import SEVERITY_WARNING
from .scanner_health import report_failure

logger = logging.getLogger("pulse_agent.monitor")

_last_prometheus_error_time: float = 0.0
_PROMETHEUS_ERROR_COOLDOWN = 300
# Trend scanners whose most recent query failed. Per scanner, not one global
# flag: the four trend scanners and trend_degraded run concurrently, so a single
# boolean was whatever the last query to finish happened to say -- one success
# anywhere cleared a failure everywhere, and trend_degraded read it mid-cycle.
_degraded_scanners: set[str] = set()


def _mark(ok: bool) -> None:
    from .scanner_health import _current_scanner

    name = _current_scanner.get() or "trend"
    if ok:
        _degraded_scanners.discard(name)
    else:
        _degraded_scanners.add(name)


def _query_prometheus(query: str) -> list[dict]:
    """Query Prometheus via unified client and return results."""
    global _last_prometheus_error_time
    try:
        data = get_prometheus_client().query(query, timeout=15)
        if data.get("status") != "success":
            logger.debug("Prometheus query failed: %s", data.get("error", "unknown"))
            # Without this the scanner reported "clean" with Prometheus down.
            report_failure(f"Prometheus query failed: {data.get('error', 'unknown')}")
            _mark(False)
            return []
        _mark(True)
        return data.get("data", {}).get("result", [])
    except PrometheusConfigError as e:
        logger.warning("Prometheus config error: %s", e)
        _mark(False)
        report_failure(f"Prometheus not configured: {e}")
        return []
    except Exception as e:
        now = time.time()
        if now - _last_prometheus_error_time > _PROMETHEUS_ERROR_COOLDOWN:
            _last_prometheus_error_time = now
            logger.warning("Prometheus query error (suppressing for %ds): %s", _PROMETHEUS_ERROR_COOLDOWN, e)
        else:
            logger.debug("Prometheus query error (suppressed): %s", e)
        _mark(False)
        report_failure(f"Prometheus unreachable: {e}")
        return []


def get_trend_degraded_finding() -> list[dict]:
    """Return a degraded finding while any trend scanner's latest query failed."""
    if not _degraded_scanners:
        return []
    return [
        _make_finding(
            severity="info",
            category="monitoring",
            title="Trend monitoring degraded",
            summary=("Prometheus trend queries failed — no forecast from: " + ", ".join(sorted(_degraded_scanners))),
            resources=[],
            auto_fixable=False,
        )
    ]


def scan_memory_pressure_forecast() -> list[dict]:
    """Predict node memory exhaustion within 3 days using 7-day linear trends."""
    findings: list[dict[str, Any]] = []
    try:
        query = "predict_linear(node_memory_MemAvailable_bytes[7d], 3*86400) < 0"
        results = _query_prometheus(query)

        for result in results:
            metric = result.get("metric", {})
            node = metric.get("instance", metric.get("node", "unknown"))
            value = result.get("value", [None, None])[1]

            if value is not None:
                try:
                    predicted_bytes = float(value)
                    # If prediction is negative, memory will be exhausted
                    if predicted_bytes < 0:
                        findings.append(
                            _make_finding(
                                severity=SEVERITY_WARNING,
                                category="memory_pressure",
                                title=f"Node {node} memory exhaustion predicted in 3 days",
                                summary="Linear projection shows available memory reaching 0 bytes in ~3 days. Current 7-day trend indicates exhaustion.",
                                resources=[{"kind": "Node", "name": node}],
                                auto_fixable=False,
                                confidence=0.75,
                                finding_type="trend",
                            )
                        )
                except (ValueError, TypeError):
                    logger.debug("Failed to parse memory pressure value for node", exc_info=True)
    except Exception as e:
        logger.error("Memory pressure forecast scan failed: %s", e)
        report_failure(e)
    return findings


def scan_disk_pressure_forecast() -> list[dict]:
    """Predict volume capacity exhaustion within 7 days using 7-day linear trends."""
    findings: list[dict[str, Any]] = []
    try:
        # Query for volumes where predicted usage will exceed capacity
        query = "predict_linear(kubelet_volume_stats_used_bytes[7d], 7*86400) > kubelet_volume_stats_capacity_bytes"
        results = _query_prometheus(query)

        for result in results:
            metric = result.get("metric", {})
            namespace = metric.get("namespace", "")
            pvc = metric.get("persistentvolumeclaim", "")
            pod = metric.get("pod", "")

            if pvc:
                findings.append(
                    _make_finding(
                        severity=SEVERITY_WARNING,
                        category="disk_pressure",
                        title=f"Volume {pvc} exhaustion predicted in 7 days",
                        summary="Linear projection shows volume capacity will be exceeded in ~7 days. Current 7-day trend indicates exhaustion.",
                        resources=[
                            {"kind": "PersistentVolumeClaim", "name": pvc, "namespace": namespace},
                        ],
                        auto_fixable=False,
                        confidence=0.75,
                        finding_type="trend",
                    )
                )
            elif pod:
                findings.append(
                    _make_finding(
                        severity=SEVERITY_WARNING,
                        category="disk_pressure",
                        title=f"Pod {pod} disk exhaustion predicted in 7 days",
                        summary="Linear projection shows pod volume capacity will be exceeded in ~7 days.",
                        resources=[{"kind": "Pod", "name": pod, "namespace": namespace}],
                        auto_fixable=False,
                        confidence=0.75,
                        finding_type="trend",
                    )
                )
    except Exception as e:
        logger.error("Disk pressure forecast scan failed: %s", e)
        report_failure(e)
    return findings


def scan_hpa_exhaustion_trend() -> list[dict]:
    """Detect HPAs consistently running at >90% of max capacity over 48 hours."""
    findings: list[dict[str, Any]] = []
    try:
        # Query for HPAs with sustained high utilization
        query = "avg_over_time((kube_horizontalpodautoscaler_status_current_replicas / kube_horizontalpodautoscaler_spec_max_replicas)[48h:]) > 0.9"
        results = _query_prometheus(query)

        for result in results:
            metric = result.get("metric", {})
            namespace = metric.get("namespace", "")
            hpa = metric.get("horizontalpodautoscaler", metric.get("hpa", ""))
            value = result.get("value", [None, None])[1]

            if hpa and value is not None:
                try:
                    avg_utilization = float(value)
                    findings.append(
                        _make_finding(
                            severity=SEVERITY_WARNING,
                            category="hpa",
                            title=f"HPA {hpa} sustained near-max capacity",
                            summary=f"HPA has averaged {avg_utilization:.0%} of max replicas over 48 hours. Consider increasing max replicas.",
                            resources=[
                                {"kind": "HorizontalPodAutoscaler", "name": hpa, "namespace": namespace},
                            ],
                            auto_fixable=False,
                            confidence=0.85,
                            finding_type="trend",
                        )
                    )
                except (ValueError, TypeError):
                    logger.debug("Failed to parse HPA utilization value", exc_info=True)
    except Exception as e:
        logger.error("HPA exhaustion trend scan failed: %s", e)
        report_failure(e)
    return findings


def _error_share(offset: str = "") -> str:
    """Share of requests returning 5xx, optionally as of ``offset`` ago.

    The offset has to be attached to each range selector rather than to the
    finished expression: PromQL only accepts the modifier after an instant or
    range vector selector (or a subquery), so `(<ratio>) offset 1d` is a parse
    error, not a shifted ratio. Prometheus rejects the whole query with 400
    Bad Request, which is how this scanner silently stopped working — the
    agent surfaced it only as "Pulse cannot query Prometheus", pointing at
    reachability and RBAC that were both fine.
    """
    off = f" offset {offset}" if offset else ""
    return (
        f'sum by (namespace, service, job) (rate(http_requests_total{{code=~"5.."}}[1h]{off}))'
        f" / sum by (namespace, service, job) (rate(http_requests_total[1h]{off}))"
    )


_ERROR_SHARE = _error_share()
# At least 1% of requests failing, and more than double the share at this time
# yesterday. The old query, deriv(...) > 0, fired on any upward slope at all —
# 0.001 errors/s becoming 0.002 was "accelerating".
_ERROR_ACCELERATION_QUERY = (
    f"({_ERROR_SHARE} > 0.01) and ({_ERROR_SHARE} > 2 * ({_error_share('1d')}))"
)


def scan_error_rate_acceleration() -> list[dict]:
    """Detect services whose share of 5xx responses has jumped since yesterday."""
    findings: list[dict[str, Any]] = []
    try:
        results = _query_prometheus(_ERROR_ACCELERATION_QUERY)

        for result in results:
            metric = result.get("metric", {})
            namespace = metric.get("namespace", "")
            service = metric.get("service", metric.get("job", ""))
            value = result.get("value", [None, None])[1]
            if value is None or not service:
                continue
            try:
                share = float(value)
            except (ValueError, TypeError):
                logger.debug("Failed to parse error share value", exc_info=True)
                continue
            findings.append(
                _make_finding(
                    severity=SEVERITY_WARNING,
                    category="errors",
                    title=f"Service {service} error rate accelerating",
                    summary=(
                        f"{share:.1%} of requests to {service} returned 5xx over the last hour, "
                        "more than double the share at the same time yesterday."
                    ),
                    resources=[{"kind": "Service", "name": service, "namespace": namespace}],
                    auto_fixable=False,
                    confidence=0.70,
                    finding_type="trend",
                )
            )
    except Exception as e:
        logger.error("Error rate acceleration scan failed: %s", e)
        report_failure(e)
    return findings


TREND_SCANNERS = [
    ("trend_memory", scan_memory_pressure_forecast),
    ("trend_disk", scan_disk_pressure_forecast),
    ("trend_hpa", scan_hpa_exhaustion_trend),
    ("trend_errors", scan_error_rate_acceleration),
]
