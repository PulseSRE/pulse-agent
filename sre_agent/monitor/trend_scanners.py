"""Trend-based predictive scanners using Prometheus predict_linear() and derivatives."""

from __future__ import annotations

import logging
import time
from typing import Any

from ..prometheus import PrometheusConfigError, get_prometheus_client
from .findings import _make_finding
from .registry import SEVERITY_WARNING

logger = logging.getLogger("pulse_agent.monitor")

_last_prometheus_error_time: float = 0.0
_PROMETHEUS_ERROR_COOLDOWN = 300


def _query_prometheus(query: str) -> list[dict]:
    """Query Prometheus via unified client and return results."""
    global _last_prometheus_error_time
    try:
        data = get_prometheus_client().query(query, timeout=15)
        if data.get("status") != "success":
            logger.debug("Prometheus query failed: %s", data.get("error", "unknown"))
            return []
        return data.get("data", {}).get("result", [])
    except PrometheusConfigError as e:
        logger.warning("Prometheus config error: %s", e)
        return []
    except Exception as e:
        now = time.time()
        if now - _last_prometheus_error_time > _PROMETHEUS_ERROR_COOLDOWN:
            _last_prometheus_error_time = now
            logger.warning("Prometheus query error (suppressing for %ds): %s", _PROMETHEUS_ERROR_COOLDOWN, e)
        else:
            logger.debug("Prometheus query error (suppressed): %s", e)
        return []


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
    return findings


def scan_error_rate_acceleration() -> list[dict]:
    """Detect accelerating HTTP 5xx error rates over 24 hours."""
    findings: list[dict[str, Any]] = []
    try:
        # Query for services with increasing error rates
        query = 'deriv(rate(http_requests_total{code=~"5.."}[1h])[24h:]) > 0'
        results = _query_prometheus(query)

        for result in results:
            metric = result.get("metric", {})
            namespace = metric.get("namespace", "")
            service = metric.get("service", metric.get("job", ""))
            code = metric.get("code", "5xx")
            value = result.get("value", [None, None])[1]

            if value is not None:
                try:
                    derivative = float(value)
                    if derivative > 0:
                        findings.append(
                            _make_finding(
                                severity=SEVERITY_WARNING,
                                category="errors",
                                title=f"Service {service} error rate accelerating",
                                summary=f"HTTP {code} error rate has been increasing over 24 hours. Derivative: {derivative:.6f} errors/sec².",
                                resources=[
                                    {"kind": "Service", "name": service, "namespace": namespace},
                                ],
                                auto_fixable=False,
                                confidence=0.70,
                                finding_type="trend",
                            )
                        )
                except (ValueError, TypeError):
                    logger.debug("Failed to parse error rate derivative value", exc_info=True)
    except Exception as e:
        logger.error("Error rate acceleration scan failed: %s", e)
    return findings


TREND_SCANNERS = [
    ("trend_memory", scan_memory_pressure_forecast),
    ("trend_disk", scan_disk_pressure_forecast),
    ("trend_hpa", scan_hpa_exhaustion_trend),
    ("trend_errors", scan_error_rate_acceleration),
]
