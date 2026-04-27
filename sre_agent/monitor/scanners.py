"""All scan_* functions for cluster health monitoring."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from ..config import get_settings
from ..errors import ToolError
from ..k8s_client import get_apps_client, get_autoscaling_client, get_core_client, get_custom_client, safe
from ..prometheus import PrometheusBackend, get_prometheus_client
from .findings import _make_finding, _skip_namespace
from .registry import SEVERITY_CRITICAL, SEVERITY_INFO, SEVERITY_WARNING

logger = logging.getLogger("pulse_agent.monitor")


# ── Scan Functions ─────────────────────────────────────────────────────────


def scan_crashlooping_pods(pods=None) -> list[dict]:
    """Find pods in CrashLoopBackOff or high restart counts."""
    crashloop_threshold = get_settings().monitor.crashloop_threshold
    findings: list[dict[str, Any]] = []
    try:
        if pods is None:
            pods = safe(lambda: get_core_client().list_pod_for_all_namespaces())
        if isinstance(pods, ToolError):
            return findings
        for pod in pods.items:
            ns = pod.metadata.namespace
            name = pod.metadata.name
            # Skip system namespaces
            if _skip_namespace(ns):
                continue
            for cs in pod.status.container_statuses or []:
                if cs.restart_count >= crashloop_threshold:
                    waiting = cs.state.waiting
                    reason = waiting.reason if waiting else "Unknown"
                    findings.append(
                        _make_finding(
                            severity=SEVERITY_CRITICAL if cs.restart_count >= 10 else SEVERITY_WARNING,
                            category="crashloop",
                            title=f"Pod {name} restarting ({cs.restart_count}x)",
                            summary=f"Container '{cs.name}' has restarted {cs.restart_count} times. Reason: {reason}",
                            resources=[{"kind": "Pod", "name": name, "namespace": ns}],
                            auto_fixable=True,
                            runbook_id="crashloop-restart",
                        )
                    )
    except Exception as e:
        logger.error("Crash loop scan failed: %s", e)
    return findings


def scan_pending_pods() -> list[dict]:
    """Find pods stuck in Pending state."""
    findings: list[dict[str, Any]] = []
    try:
        core = get_core_client()
        pods = safe(lambda: core.list_pod_for_all_namespaces(field_selector="status.phase=Pending"))
        if isinstance(pods, ToolError):
            return findings
        for pod in pods.items:
            ns = pod.metadata.namespace
            name = pod.metadata.name
            if _skip_namespace(ns):
                continue
            # Check how long it's been pending
            created = pod.metadata.creation_timestamp
            if created:
                age_minutes = (datetime.now(UTC) - created).total_seconds() / 60
                if age_minutes > 5:
                    reason = ""
                    for cond in pod.status.conditions or []:
                        if cond.type == "PodScheduled" and cond.status == "False":
                            reason = cond.message or cond.reason or "Unschedulable"
                            break
                    findings.append(
                        _make_finding(
                            severity=SEVERITY_WARNING if age_minutes < 30 else SEVERITY_CRITICAL,
                            category="scheduling",
                            title=f"Pod {name} pending for {int(age_minutes)}m",
                            summary=f"Pod has been pending for {int(age_minutes)} minutes. {reason}",
                            resources=[{"kind": "Pod", "name": name, "namespace": ns}],
                        )
                    )
    except Exception as e:
        logger.error("Pending pod scan failed: %s", e)
    return findings


def scan_failed_deployments() -> list[dict]:
    """Find deployments with unavailable replicas."""
    findings: list[dict[str, Any]] = []
    try:
        apps = get_apps_client()
        deploys = safe(lambda: apps.list_deployment_for_all_namespaces())
        if isinstance(deploys, ToolError):
            return findings
        for d in deploys.items:
            ns = d.metadata.namespace
            name = d.metadata.name
            if _skip_namespace(ns):
                continue
            desired = d.spec.replicas or 0
            available = d.status.available_replicas or 0
            if desired > 0 and available < desired:
                findings.append(
                    _make_finding(
                        severity=SEVERITY_WARNING if available > 0 else SEVERITY_CRITICAL,
                        category="workloads",
                        title=f"Deployment {name} degraded ({available}/{desired})",
                        summary=f"Only {available} of {desired} replicas available",
                        resources=[{"kind": "Deployment", "name": name, "namespace": ns}],
                        auto_fixable=True,
                        runbook_id="deployment-degraded",
                    )
                )
    except Exception as e:
        logger.error("Deployment scan failed: %s", e)
    return findings


def scan_node_pressure() -> list[dict]:
    """Find nodes with pressure conditions (DiskPressure, MemoryPressure, PIDPressure)."""
    findings: list[dict[str, Any]] = []
    try:
        core = get_core_client()
        nodes = safe(lambda: core.list_node())
        if isinstance(nodes, ToolError):
            return findings
        for node in nodes.items:
            name = node.metadata.name
            for cond in node.status.conditions or []:
                if cond.type in ("DiskPressure", "MemoryPressure", "PIDPressure") and cond.status == "True":
                    findings.append(
                        _make_finding(
                            severity=SEVERITY_CRITICAL,
                            category="nodes",
                            title=f"Node {name} has {cond.type}",
                            summary=f"{cond.type}: {cond.message or cond.reason or 'Condition active'}",
                            resources=[{"kind": "Node", "name": name}],
                        )
                    )
                if cond.type == "Ready" and cond.status != "True":
                    findings.append(
                        _make_finding(
                            severity=SEVERITY_CRITICAL,
                            category="nodes",
                            title=f"Node {name} NotReady",
                            summary=f"Node is not ready: {cond.message or cond.reason or 'Unknown'}",
                            resources=[{"kind": "Node", "name": name}],
                        )
                    )
    except Exception as e:
        logger.error("Node pressure scan failed: %s", e)
    return findings


def scan_expiring_certs() -> list[dict]:
    """Find TLS secrets with certificates expiring within 30 days."""
    findings: list[dict[str, Any]] = []
    try:
        import base64
        from datetime import timedelta

        core = get_core_client()
        secrets = safe(lambda: core.list_secret_for_all_namespaces(field_selector="type=kubernetes.io/tls"))
        if isinstance(secrets, ToolError):
            return findings
        now = datetime.now(UTC)
        warn_threshold = timedelta(days=30)

        for secret in secrets.items:
            ns = secret.metadata.namespace
            name = secret.metadata.name
            # Intentionally skips default/openshift too — certs there are cluster-managed
            if _skip_namespace(ns):
                continue
            cert_data = (secret.data or {}).get("tls.crt")
            if not cert_data:
                continue
            try:
                import ssl
                import tempfile

                cert_bytes = base64.b64decode(cert_data)
                with tempfile.NamedTemporaryFile(suffix=".crt", delete=True) as f:
                    f.write(cert_bytes)
                    f.flush()
                    try:
                        cert_info = ssl._ssl._test_decode_cert(f.name)  # type: ignore[attr-defined]
                    except (AttributeError, Exception) as cert_err:
                        logger.warning(
                            "Cannot decode cert %s/%s (CPython-specific API): %s",
                            ns,
                            name,
                            cert_err,
                        )
                        continue
                not_after_str = cert_info.get("notAfter", "")
                if not_after_str:
                    # Format: "Mon DD HH:MM:SS YYYY GMT"
                    not_after = datetime.strptime(not_after_str, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=UTC)
                    remaining = not_after - now
                    if remaining < timedelta(0):
                        findings.append(
                            _make_finding(
                                severity=SEVERITY_CRITICAL,
                                category="cert_expiry",
                                title=f"Certificate {name} EXPIRED",
                                summary=f"TLS certificate expired {abs(remaining.days)} days ago",
                                resources=[{"kind": "Secret", "name": name, "namespace": ns}],
                            )
                        )
                    elif remaining < warn_threshold:
                        findings.append(
                            _make_finding(
                                severity=SEVERITY_WARNING,
                                category="cert_expiry",
                                title=f"Certificate {name} expiring in {remaining.days}d",
                                summary=f"TLS certificate expires on {not_after.isoformat()}",
                                resources=[{"kind": "Secret", "name": name, "namespace": ns}],
                            )
                        )
            except Exception as e:
                logger.debug("Failed to parse certificate: %s", e)
    except Exception as e:
        logger.error("Certificate scan failed: %s", e)
    return findings


def scan_firing_alerts() -> list[dict]:
    """Check Prometheus for firing alerts."""
    findings: list[dict[str, Any]] = []
    try:
        data = get_prometheus_client().request("api/v1/rules", {"type": "alert"}, 15, PrometheusBackend.LOCAL)
        if data.get("status") != "success":
            return findings
        for group in data.get("data", {}).get("groups", []):
            for rule in group.get("rules", []):
                if rule.get("state") != "firing":
                    continue
                for alert in rule.get("alerts", []):
                    if alert.get("state") != "firing":
                        continue
                    labels = alert.get("labels", {})
                    severity = labels.get("severity", "warning")
                    ns = labels.get("namespace", "")
                    alertname = labels.get("alertname", rule.get("name", "Unknown"))
                    # Skip watchdog and info alerts
                    if alertname in ("Watchdog", "InfoInhibitor"):
                        continue
                    sev = (
                        SEVERITY_CRITICAL
                        if severity == "critical"
                        else SEVERITY_WARNING
                        if severity == "warning"
                        else SEVERITY_INFO
                    )
                    summary = alert.get("annotations", {}).get(
                        "summary", alert.get("annotations", {}).get("message", "")
                    )
                    resources = []
                    if labels.get("pod"):
                        resources.append({"kind": "Pod", "name": labels["pod"], "namespace": ns})
                    elif labels.get("deployment"):
                        resources.append({"kind": "Deployment", "name": labels["deployment"], "namespace": ns})
                    elif labels.get("node"):
                        resources.append({"kind": "Node", "name": labels["node"]})
                    if not resources:
                        resources.append({"kind": "Alert", "name": alertname, "namespace": ns})
                    findings.append(
                        _make_finding(
                            severity=sev,
                            category="alerts",
                            title=alertname,
                            summary=summary[:200] if summary else f"Alert {alertname} firing",
                            resources=resources,
                        )
                    )
    except Exception as e:
        logger.debug("Alert scan failed (monitoring may not be available): %s", e)
    return findings


def scan_oom_killed_pods(pods=None) -> list[dict]:
    """Find pods with OOMKilled exit code in last terminated state."""
    findings: list[dict[str, Any]] = []
    try:
        if pods is None:
            pods = safe(lambda: get_core_client().list_pod_for_all_namespaces())
        if isinstance(pods, ToolError):
            return findings
        for pod in pods.items:
            ns = pod.metadata.namespace
            name = pod.metadata.name
            if _skip_namespace(ns):
                continue
            for cs in pod.status.container_statuses or []:
                last = cs.last_state
                if last and last.terminated and last.terminated.reason == "OOMKilled":
                    findings.append(
                        _make_finding(
                            severity=SEVERITY_CRITICAL,
                            category="oom",
                            title=f"Pod {name} OOMKilled",
                            summary=f"Container '{cs.name}' was OOMKilled (exit code {last.terminated.exit_code})",
                            resources=[{"kind": "Pod", "name": name, "namespace": ns}],
                            auto_fixable=True,
                        )
                    )
    except Exception as e:
        logger.error("OOMKilled scan failed: %s", e)
    return findings


def scan_image_pull_errors(pods=None) -> list[dict]:
    """Find pods in ImagePullBackOff or ErrImagePull state."""
    findings: list[dict[str, Any]] = []
    try:
        if pods is None:
            pods = safe(lambda: get_core_client().list_pod_for_all_namespaces())
        if isinstance(pods, ToolError):
            return findings
        for pod in pods.items:
            ns = pod.metadata.namespace
            name = pod.metadata.name
            if _skip_namespace(ns):
                continue
            for cs in pod.status.container_statuses or []:
                waiting = cs.state.waiting if cs.state else None
                if waiting and waiting.reason in ("ImagePullBackOff", "ErrImagePull"):
                    findings.append(
                        _make_finding(
                            severity=SEVERITY_WARNING,
                            category="image_pull",
                            title=f"Pod {name} {waiting.reason}",
                            summary=f"Container '{cs.name}' cannot pull image: {waiting.message or waiting.reason}",
                            resources=[{"kind": "Pod", "name": name, "namespace": ns}],
                            auto_fixable=True,
                            runbook_id="image-pull-restart",
                        )
                    )
    except Exception as e:
        logger.error("Image pull scan failed: %s", e)
    return findings


def scan_degraded_operators() -> list[dict]:
    """Find ClusterOperators with Degraded=True condition."""
    findings: list[dict[str, Any]] = []
    try:
        custom = get_custom_client()
        result = safe(
            lambda: custom.list_cluster_custom_object(
                group="config.openshift.io",
                version="v1",
                plural="clusteroperators",
            )
        )
        if isinstance(result, ToolError):
            return findings
        for op in result.get("items", []):
            name = op.get("metadata", {}).get("name", "")
            for cond in op.get("status", {}).get("conditions", []):
                if cond.get("type") == "Degraded" and cond.get("status") == "True":
                    findings.append(
                        _make_finding(
                            severity=SEVERITY_CRITICAL,
                            category="operators",
                            title=f"ClusterOperator {name} degraded",
                            summary=f"Operator degraded: {cond.get('message', cond.get('reason', 'Unknown'))}",
                            resources=[{"kind": "ClusterOperator", "name": name}],
                        )
                    )
    except Exception as e:
        logger.error("Degraded operators scan failed: %s", e)
    return findings


def scan_daemonset_gaps() -> list[dict]:
    """Find DaemonSets where desiredNumberScheduled != numberReady."""
    findings: list[dict[str, Any]] = []
    try:
        apps = get_apps_client()
        dsets = safe(lambda: apps.list_daemon_set_for_all_namespaces())
        if isinstance(dsets, ToolError):
            return findings
        for ds in dsets.items:
            ns = ds.metadata.namespace
            name = ds.metadata.name
            if _skip_namespace(ns):
                continue
            desired = ds.status.desired_number_scheduled or 0
            ready = ds.status.number_ready or 0
            if desired > 0 and ready < desired:
                findings.append(
                    _make_finding(
                        severity=SEVERITY_WARNING if ready > 0 else SEVERITY_CRITICAL,
                        category="daemonsets",
                        title=f"DaemonSet {name} not fully ready ({ready}/{desired})",
                        summary=f"Only {ready} of {desired} desired pods are ready",
                        resources=[{"kind": "DaemonSet", "name": name, "namespace": ns}],
                    )
                )
    except Exception as e:
        logger.error("DaemonSet gap scan failed: %s", e)
    return findings


def scan_hpa_saturation() -> list[dict]:
    """Find HPAs at maxReplicas."""
    findings: list[dict[str, Any]] = []
    try:
        autoscaling = get_autoscaling_client()
        hpas = safe(lambda: autoscaling.list_horizontal_pod_autoscaler_for_all_namespaces())
        if isinstance(hpas, ToolError):
            return findings
        for hpa in hpas.items:
            ns = hpa.metadata.namespace
            name = hpa.metadata.name
            if _skip_namespace(ns):
                continue
            max_replicas = hpa.spec.max_replicas or 0
            current = hpa.status.current_replicas or 0
            if max_replicas > 0 and current >= max_replicas:
                findings.append(
                    _make_finding(
                        severity=SEVERITY_WARNING,
                        category="hpa",
                        title=f"HPA {name} at max replicas ({current}/{max_replicas})",
                        summary=f"HPA is at maximum capacity ({current}/{max_replicas} replicas)",
                        resources=[{"kind": "HorizontalPodAutoscaler", "name": name, "namespace": ns}],
                    )
                )
    except Exception as e:
        logger.error("HPA saturation scan failed: %s", e)
    return findings


ALL_SCANNERS: list[tuple[str, Callable[..., Any]]] = [
    ("crashloop", scan_crashlooping_pods),
    ("pending", scan_pending_pods),
    ("workloads", scan_failed_deployments),
    ("nodes", scan_node_pressure),
    ("cert_expiry", scan_expiring_certs),
    ("alerts", scan_firing_alerts),
    ("oom", scan_oom_killed_pods),
    ("image_pull", scan_image_pull_errors),
    ("operators", scan_degraded_operators),
    ("daemonsets", scan_daemonset_gaps),
    ("hpa", scan_hpa_saturation),
]


def _get_all_scanners() -> list[tuple[str, Callable[..., Any]]]:
    """Return all scanners including audit scanners (lazy import to avoid circular dependency)."""
    from ..audit_scanner import (
        scan_auth_events,
        scan_config_changes,
        scan_rbac_changes,
        scan_recent_deployments,
        scan_warning_events,
    )

    return ALL_SCANNERS + [
        ("audit_config", scan_config_changes),
        ("audit_rbac", scan_rbac_changes),
        ("audit_deployment", scan_recent_deployments),
        ("audit_events", scan_warning_events),
        ("audit_auth", scan_auth_events),
        ("slo_burn", scan_slo_burn_rate),
        ("security", scan_security_posture),
    ]


# ── Protocol-based scanner instances ──────────────────────────────────────
# Wraps existing functions as Scanner protocol instances.  The scanner_protocol
# module is the canonical interface for new code; ALL_SCANNERS and
# _get_all_scanners remain for backward compatibility.

from .registry import SCANNER_REGISTRY
from .scanner_protocol import AsyncScanner, FunctionScanner, ScannerMeta


def _meta(name: str) -> ScannerMeta:
    """Build ScannerMeta from SCANNER_REGISTRY entry."""
    return ScannerMeta.from_registry(name, SCANNER_REGISTRY[name])


class AsyncPendingPodsScanner(AsyncScanner):
    """First async-native scanner — validates the kubernetes_asyncio stack."""

    meta = _meta("pending")
    is_async = True

    async def async_scan(self, shared_resources=None):
        findings: list[dict] = []
        try:
            from ..async_k8s import get_async_core_client, safe_async

            core = await get_async_core_client()
            pods = await safe_async(core.list_pod_for_all_namespaces(field_selector="status.phase=Pending"))
            if isinstance(pods, ToolError):
                return findings
            for pod in pods.items:
                ns = pod.metadata.namespace
                name = pod.metadata.name
                if _skip_namespace(ns):
                    continue
                created = pod.metadata.creation_timestamp
                if created:
                    age_minutes = (datetime.now(UTC) - created).total_seconds() / 60
                    if age_minutes > 5:
                        reason = ""
                        for cond in pod.status.conditions or []:
                            if cond.type == "PodScheduled" and cond.status == "False":
                                reason = cond.message or cond.reason or "Unschedulable"
                                break
                        findings.append(
                            _make_finding(
                                severity=SEVERITY_WARNING if age_minutes < 30 else SEVERITY_CRITICAL,
                                category="scheduling",
                                title=f"Pod {name} pending for {int(age_minutes)}m",
                                summary=f"Pod has been pending for {int(age_minutes)} minutes. {reason}",
                                resources=[{"kind": "Pod", "name": name, "namespace": ns}],
                            )
                        )
        except Exception as e:
            logger.error("Async pending pod scan failed: %s", e)
        return findings


ALL_SCANNER_INSTANCES: list = [
    FunctionScanner(_meta("crashloop"), scan_crashlooping_pods, accepts_pods=True),
    AsyncPendingPodsScanner(),
    FunctionScanner(_meta("workloads"), scan_failed_deployments),
    FunctionScanner(_meta("nodes"), scan_node_pressure),
    FunctionScanner(_meta("cert_expiry"), scan_expiring_certs),
    FunctionScanner(_meta("alerts"), scan_firing_alerts),
    FunctionScanner(_meta("oom"), scan_oom_killed_pods, accepts_pods=True),
    FunctionScanner(_meta("image_pull"), scan_image_pull_errors, accepts_pods=True),
    FunctionScanner(_meta("operators"), scan_degraded_operators),
    FunctionScanner(_meta("daemonsets"), scan_daemonset_gaps),
    FunctionScanner(_meta("hpa"), scan_hpa_saturation),
]


def get_all_scanner_instances() -> list[FunctionScanner]:
    """Return all scanner instances including audit, SLO, security, and trend scanners."""
    from ..audit_scanner import (
        scan_auth_events,
        scan_config_changes,
        scan_rbac_changes,
        scan_recent_deployments,
        scan_warning_events,
    )
    from .trend_scanners import (
        scan_disk_pressure_forecast,
        scan_error_rate_acceleration,
        scan_hpa_exhaustion_trend,
        scan_memory_pressure_forecast,
    )

    return ALL_SCANNER_INSTANCES + [
        FunctionScanner(_meta("audit_config"), scan_config_changes),
        FunctionScanner(_meta("audit_rbac"), scan_rbac_changes),
        FunctionScanner(_meta("audit_deployment"), scan_recent_deployments),
        FunctionScanner(_meta("audit_events"), scan_warning_events),
        FunctionScanner(_meta("audit_auth"), scan_auth_events),
        FunctionScanner(_meta("slo_burn"), scan_slo_burn_rate),
        FunctionScanner(_meta("security"), scan_security_posture),
        FunctionScanner(_meta("trend_memory"), scan_memory_pressure_forecast),
        FunctionScanner(_meta("trend_disk"), scan_disk_pressure_forecast),
        FunctionScanner(_meta("trend_hpa"), scan_hpa_exhaustion_trend),
        FunctionScanner(_meta("trend_errors"), scan_error_rate_acceleration),
    ]


def build_registry_from_instances() -> dict[str, dict]:
    """Build SCANNER_REGISTRY-format dict from scanner instances."""
    return {
        s.meta.name: {
            "displayName": s.meta.display_name,
            "description": s.meta.description,
            "category": s.meta.category,
            "checks": list(s.meta.checks),
            "auto_fixable": s.meta.auto_fixable,
            "scan_every": s.meta.scan_every,
        }
        for s in get_all_scanner_instances()
    }


def scan_slo_burn_rate() -> list[dict]:
    """Check registered SLOs for error budget depletion."""
    try:
        from ..slo_registry import get_slo_registry

        registry = get_slo_registry()
        if not registry.list_all():
            return []

        statuses = registry.evaluate_with_prometheus()
        findings = []
        for s in statuses:
            if s.alert_level == "ok":
                continue

            severity = "critical" if s.alert_level == "critical" else "warning"
            budget_pct = round(s.error_budget_remaining * 100)
            findings.append(
                _make_finding(
                    severity=severity,
                    category="slo_burn",
                    title=f"SLO {s.alert_level}: {s.definition.service_name} {s.definition.slo_type}",
                    summary=(
                        f"{s.definition.service_name} {s.definition.slo_type} SLO "
                        f"({s.definition.target:.1%} target) has {budget_pct}% error budget remaining. "
                        f"Burn rate: {s.burn_rate:.2%}."
                    ),
                    resources=[{"kind": "Service", "name": s.definition.service_name}],
                    auto_fixable=False,
                    confidence=0.9 if s.alert_level == "critical" else 0.75,
                )
            )

        return findings
    except Exception:
        return []


def scan_security_posture() -> list[dict]:
    """Run comprehensive security posture check and convert to findings format.

    This wrapper calls get_security_summary() and parses its string output
    into structured findings compatible with the monitor system.
    """
    findings: list[dict[str, Any]] = []
    try:
        from ..security_tools import get_security_summary

        summary = get_security_summary()

        # Parse the summary string to extract findings
        # Format: "  [!!!] [CRITICAL] Category: detail"
        for line in summary.split("\n"):
            line = line.strip()
            if not line or not line.startswith("["):
                continue

            # Extract severity
            severity_map = {
                "[!!!]": SEVERITY_CRITICAL,
                "[!!]": SEVERITY_WARNING,
                "[i]": SEVERITY_INFO,
            }
            severity = SEVERITY_INFO
            for marker, sev in severity_map.items():
                if line.startswith(marker):
                    severity = sev
                    break

            # Extract category and detail
            # Format after severity marker: "[SEVERITY] Category: detail"
            parts = line.split("]", 2)  # Split on first 2 "]"
            if len(parts) < 3:
                continue

            remainder = parts[2].strip()
            if ":" not in remainder:
                continue

            category, detail = remainder.split(":", 1)
            category = category.strip()
            detail = detail.strip()

            findings.append(
                _make_finding(
                    severity=severity,
                    category="security",
                    title=f"Security: {category}",
                    summary=detail,
                    resources=[],
                    auto_fixable=False,
                )
            )
    except Exception as e:
        logger.error("Security posture scan failed: %s", e)

    return findings
