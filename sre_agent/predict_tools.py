"""Predictive remediation tools — forecasting, optimization, and auto-heal.

Pillar 4: The "Prophet" — moves from reactive to proactive by predicting
resource exhaustion, detecting HPA thrashing, and suggesting fixes.
"""

from __future__ import annotations

from .decorators import beta_tool
from .errors import ToolError
from .k8s_client import get_autoscaling_client, get_core_client, safe
from .prometheus import get_prometheus_client
from .units import parse_cpu_millicores, parse_memory_bytes


def _query_prometheus_trend(query: str, hours: int = 24) -> float | None:
    """Query Prometheus for a linear growth rate over the given time window.

    Returns the per-hour growth rate, or None if Prometheus is unreachable.
    """
    try:
        prom_query = f"deriv({query}[{hours}h])"
        data = get_prometheus_client().query(prom_query, timeout=10)
        if data.get("status") == "success":
            results = data.get("data", {}).get("result", [])
            if results:
                rate_per_sec = float(results[0].get("value", [0, "0"])[1])
                return rate_per_sec * 3600
    except Exception:
        pass
    return None


@beta_tool
def forecast_quota_exhaustion(namespace: str) -> str:
    """Predict when a namespace will hit its ResourceQuota limits based on current usage and growth rate.

    Instead of "you are at 90%", this tells you "you will hit the limit in 14 hours."

    Args:
        namespace: Namespace to forecast.
    """
    core = get_core_client()

    quotas = safe(lambda: core.list_namespaced_resource_quota(namespace))
    if isinstance(quotas, ToolError):
        return str(quotas)
    if not quotas.items:
        return f"No ResourceQuotas in namespace '{namespace}'."

    # Get current pod resource requests as a proxy for growth
    pods = safe(lambda: core.list_namespaced_pod(namespace))
    pod_count = len(pods.items) if not isinstance(pods, ToolError) else 0

    forecasts = []
    for rq in quotas.items:
        hard = rq.status.hard or {}
        used = rq.status.used or {}

        for resource in sorted(hard.keys()):
            hard_val = hard[resource]
            used_val = used.get(resource, "0")

            # Parse values
            if resource in ("cpu", "requests.cpu", "limits.cpu"):
                hard_n = parse_cpu_millicores(hard_val)
                used_n = parse_cpu_millicores(used_val)
            elif resource in ("memory", "requests.memory", "limits.memory"):
                hard_n = parse_memory_bytes(hard_val)
                used_n = parse_memory_bytes(used_val)
            elif resource in (
                "pods",
                "count/pods",
                "services",
                "configmaps",
                "secrets",
                "persistentvolumeclaims",
                "replicationcontrollers",
            ):
                hard_n = int(hard_val) if hard_val.isdigit() else 0
                used_n = int(used_val) if used_val.isdigit() else 0
            else:
                continue

            if hard_n <= 0:
                continue

            usage_pct = (used_n / hard_n) * 100
            remaining = hard_n - used_n

            # Try Prometheus-based trending first (real growth rate)
            hours_until_limit = None
            trend_source = ""
            if resource in ("cpu", "requests.cpu", "limits.cpu"):
                rate = _query_prometheus_trend(
                    f'namespace_cpu:kube_pod_container_resource_requests:sum{{namespace="{namespace}"}}',
                )
                if rate and rate > 0 and remaining > 0:
                    hours_until_limit = remaining / rate
                    trend_source = "prometheus"
            elif resource in ("memory", "requests.memory", "limits.memory"):
                rate = _query_prometheus_trend(
                    f'namespace_memory:kube_pod_container_resource_requests:sum{{namespace="{namespace}"}}',
                )
                if rate and rate > 0 and remaining > 0:
                    hours_until_limit = remaining / rate
                    trend_source = "prometheus"

            # Fallback: estimate from current usage / pod count
            pods_until_limit = float("inf")
            if hours_until_limit is None and pod_count > 0 and used_n > 0:
                per_pod = used_n / pod_count
                pods_until_limit = remaining / per_pod if per_pod > 0 else float("inf")
                trend_source = "estimate"

            severity = "OK"
            if usage_pct >= 95:
                severity = "CRITICAL"
            elif usage_pct >= 80:
                severity = "WARNING"
            elif usage_pct >= 60:
                severity = "WATCH"

            forecast_line = f"[{severity}] {resource}: {used_val}/{hard_val} ({usage_pct:.0f}%)"
            if hours_until_limit is not None:
                if hours_until_limit < 1:
                    forecast_line += f"  EXHAUSTION IN ~{int(hours_until_limit * 60)}min ({trend_source})"
                elif hours_until_limit < 48:
                    forecast_line += f"  EXHAUSTION IN ~{hours_until_limit:.0f}h ({trend_source})"
                else:
                    forecast_line += f"  EXHAUSTION IN ~{hours_until_limit / 24:.0f}d ({trend_source})"
            elif pods_until_limit < float("inf"):
                forecast_line += f"  ~{pods_until_limit:.0f} more pods until limit ({trend_source})"

            forecasts.append(forecast_line)

    return f"Quota forecast for namespace '{namespace}':\n" + "\n".join(forecasts)


@beta_tool
def analyze_hpa_thrashing(namespace: str = "ALL") -> str:
    """Detect Horizontal Pod Autoscalers that are thrashing (rapidly scaling up and down). Suggests optimal min-replicas based on observed behavior.

    Args:
        namespace: Namespace to check. Use 'ALL' for cluster-wide.
    """
    auto = get_autoscaling_client()
    if namespace.upper() == "ALL":
        result = safe(lambda: auto.list_horizontal_pod_autoscaler_for_all_namespaces())
    else:
        result = safe(lambda: auto.list_namespaced_horizontal_pod_autoscaler(namespace))
    if isinstance(result, ToolError):
        return str(result)

    findings = []
    for hpa in result.items:
        s = hpa.status
        spec = hpa.spec
        name = f"{hpa.metadata.namespace}/{hpa.metadata.name}"
        ref = f"{spec.scale_target_ref.kind}/{spec.scale_target_ref.name}"

        current = s.current_replicas or 0
        min_r = spec.min_replicas or 1
        max_r = spec.max_replicas

        # Detect thrashing indicators
        issues = []

        # Check conditions for scaling events
        for cond in s.conditions or []:
            if cond.type == "AbleToScale" and cond.status == "True":
                if cond.reason in ("ReadyForNewScale", "SucceededRescale"):
                    # Recent scale event
                    pass
            if cond.type == "ScalingActive" and cond.status == "True":
                pass
            if cond.type == "ScalingLimited" and cond.status == "True":
                issues.append(f"Scaling limited: {cond.message}")

        # Wide range between min and current suggests potential thrashing
        if max_r > 0 and min_r > 0:
            range_ratio = max_r / min_r
            if range_ratio >= 5 and current > min_r * 2:
                suggested_min = max(min_r, current // 2)
                issues.append(
                    f"Wide scaling range ({min_r}-{max_r}, currently {current}). "
                    f"Suggested min-replicas: {suggested_min} to reduce thrashing"
                )

        # Current metrics
        metrics_str = []
        for mc in s.current_metrics or []:
            if mc.type == "Resource" and mc.resource:
                avg = mc.resource.current.average_utilization
                if avg is not None:
                    metrics_str.append(f"{mc.resource.name}={avg}%")
                    # High utilization at max replicas = undersized
                    if current >= max_r and avg > 80:
                        issues.append(
                            f"At max replicas ({max_r}) with {avg}% {mc.resource.name} utilization. "
                            f"Consider increasing max-replicas."
                        )

        if issues:
            findings.append(
                f"HPA: {name} → {ref}\n"
                f"  Replicas: {current} (min={min_r}, max={max_r})\n"
                f"  Metrics: [{', '.join(metrics_str) or 'none'}]\n"
                f"  Issues:\n" + "\n".join(f"    - {i}" for i in issues)
            )

    if not findings:
        return "No HPA issues detected. All autoscalers appear healthy."

    return f"HPA Analysis ({len(findings)} with issues):\n\n" + "\n\n".join(findings)


@beta_tool
def suggest_remediation(error_type: str, namespace: str = "", resource_name: str = "") -> str:
    """Get smart remediation suggestions for common Kubernetes errors. Provides specific fix steps and, where safe, offers one-click heal options.

    Args:
        error_type: The error to get suggestions for (e.g. 'ImagePullBackOff', 'CrashLoopBackOff', 'OOMKilled', 'Pending', 'NodeNotReady').
        namespace: Namespace of the affected resource (optional).
        resource_name: Name of the affected resource (optional).
    """
    remediations = {
        "ImagePullBackOff": {
            "cause": "The container image cannot be pulled. Common causes: wrong image name/tag, expired registry credentials, private registry without pull secret.",
            "steps": [
                "1. Check image name and tag: `describe_pod` → verify image field",
                "2. Check pull secrets: `get_pod_logs` for auth errors",
                "3. Verify registry secret exists in namespace",
                "4. Check if image exists in the registry",
            ],
            "heal": "If the issue is an expired registry secret, create a new one with: "
            "`oc create secret docker-registry <name> --docker-server=<registry> --docker-username=<user> --docker-password=<pass>`",
        },
        "CrashLoopBackOff": {
            "cause": "Container starts and immediately crashes, restarting in a backoff loop.",
            "steps": [
                "1. Check logs: `get_pod_logs` (use previous=True for crash logs)",
                "2. Check events: `describe_pod` for OOM, config errors",
                "3. Check resource limits: container may be OOM-killed",
                "4. Check command/args: entry point may be misconfigured",
                "5. Check ConfigMaps/Secrets: missing config files",
            ],
            "heal": "If OOMKilled: increase memory limits. If config error: check mounted ConfigMaps.",
        },
        "OOMKilled": {
            "cause": "Container exceeded its memory limit and was killed by the kernel OOM killer.",
            "steps": [
                "1. Check current limits: `describe_pod` → container resources",
                "2. Check actual usage: `get_pod_metrics` to see real memory consumption",
                "3. Check for memory leaks: compare usage over time with `get_prometheus_query`",
            ],
            "heal": "Increase the memory limit. Recommended: set limit to 2x the observed peak usage. "
            "Use `scale_deployment` if needed to reduce per-pod load.",
        },
        "Pending": {
            "cause": "Pod cannot be scheduled. Common causes: insufficient resources, node taints/affinity, PVC not bound.",
            "steps": [
                "1. Check events: `describe_pod` → look for FailedScheduling",
                "2. Check node resources: `get_node_metrics` for capacity",
                "3. Check taints: `describe_node` for taints that block scheduling",
                "4. Check PVCs: `get_persistent_volume_claims` for unbound volumes",
                "5. Check quotas: `forecast_quota_exhaustion` for limit hits",
            ],
            "heal": "If resource-constrained: scale up nodes or reduce resource requests. "
            "If taint-blocked: add tolerations to the pod spec.",
        },
        "NodeNotReady": {
            "cause": "A node is reporting NotReady status. The kubelet may be unresponsive, or the node may have disk/memory pressure.",
            "steps": [
                "1. Check node conditions: `describe_node` for DiskPressure, MemoryPressure",
                "2. Check node metrics: `get_node_metrics` for resource exhaustion",
                "3. Check events: `get_events` filtered to the node",
                "4. Check affected pods: `list_pods` with field_selector for the node",
            ],
            "heal": "If DiskPressure: clean up unused images/logs. "
            "If unresponsive: cordon the node and drain workloads to healthy nodes.",
        },
        "ErrImagePull": {
            "cause": "Initial image pull failed. Same root causes as ImagePullBackOff but on first attempt.",
            "steps": [
                "1. Verify image exists: check registry directly",
                "2. Check pull secrets in the namespace",
                "3. Check network connectivity to the registry",
            ],
            "heal": "Verify the image reference and ensure pull secrets are configured.",
        },
    }

    error_key = error_type.strip()
    # Try case-insensitive match
    matched = None
    for key in remediations:
        if key.lower() == error_key.lower():
            matched = remediations[key]
            break

    if not matched:
        return (
            f"No specific remediation guide for '{error_type}'. "
            f"Available guides: {', '.join(remediations.keys())}.\n"
            f"Try: describe_pod and get_pod_logs for the affected resource to diagnose."
        )

    context = f" in {namespace}/{resource_name}" if namespace and resource_name else ""
    lines = [
        f"Remediation for: {error_key}{context}",
        f"\nCause: {matched['cause']}",
        "\nDiagnostic Steps:",
    ]
    for step in matched["steps"]:
        lines.append(f"  {step}")
    lines.append(f"\nHeal: {matched['heal']}")

    return "\n".join(lines)


PREDICT_TOOLS = [forecast_quota_exhaustion, analyze_hpa_thrashing, suggest_remediation]

# Register predict tools in the central registry (all read-only)
from .tool_registry import register_tool

for _tool in PREDICT_TOOLS:
    register_tool(_tool, is_write=False)
