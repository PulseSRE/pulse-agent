"""Kubernetes/OpenShift tools for the SRE agent.

Each tool is decorated with @beta_tool so the Anthropic SDK automatically
generates JSON schemas and the tool runner can execute them.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

from anthropic import beta_tool
from kubernetes import client
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream as k8s_stream

# RFC 1123 name validation for K8s resources
_K8S_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9\-\.]{0,251}[a-z0-9])?$")
_K8S_NAMESPACE_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?$")


def _validate_k8s_name(value: str, field: str = "name") -> str | None:
    """Validate a K8s resource name. Returns error message or None if valid."""
    if not value:
        return f"Error: {field} is required."
    if len(value) > 253:
        return f"Error: {field} too long (max 253 chars)."
    if not _K8S_NAME_RE.match(value):
        return f"Error: {field} '{value}' is not a valid Kubernetes name (RFC 1123)."
    return None


def _validate_k8s_namespace(value: str) -> str | None:
    """Validate a K8s namespace name. Returns error message or None if valid."""
    if not value:
        return None  # namespace is often optional
    if len(value) > 63:
        return "Error: namespace too long (max 63 chars)."
    if not _K8S_NAMESPACE_RE.match(value):
        return f"Error: namespace '{value}' is not a valid Kubernetes namespace name."
    return None


from .errors import ToolError
from .k8s_client import (
    age,
    get_apps_client,
    get_autoscaling_client,
    get_batch_client,
    get_core_client,
    get_custom_client,
    get_networking_client,
    get_version_client,
    safe,
)

# Metrics API uses the CustomObjectsApi to query metrics.k8s.io
_METRICS_GROUP = "metrics.k8s.io"
_METRICS_VERSION = "v1beta1"

# Write tools that require user confirmation before execution
WRITE_TOOLS = {
    "scale_deployment",
    "restart_deployment",
    "cordon_node",
    "uncordon_node",
    "delete_pod",
    "apply_yaml",
    "create_network_policy",
    "rollback_deployment",
    "drain_node",
    "exec_command",
    "test_connectivity",
}

MAX_TAIL_LINES = 1000
MAX_REPLICAS = 100
MAX_RESULTS = 200


# ---------------------------------------------------------------------------
# Diagnostic tools (read-only)
# ---------------------------------------------------------------------------


@beta_tool
def list_namespaces() -> str:
    """List all namespaces in the cluster with their status."""
    result = safe(lambda: get_core_client().list_namespace(limit=MAX_RESULTS))
    if isinstance(result, ToolError):
        return str(result)
    lines = []
    for ns in result.items:
        lines.append(f"{ns.metadata.name}  Status={ns.status.phase}  Age={age(ns.metadata.creation_timestamp)}")
    return "\n".join(lines) or "No namespaces found."


@beta_tool
def list_pods(namespace: str = "default", label_selector: str = "", field_selector: str = "") -> str:
    """List pods in a namespace with their status, restarts, and age.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' to list across all namespaces.
        label_selector: Label selector to filter pods, e.g. 'app=nginx'.
        field_selector: Field selector, e.g. 'status.phase=Failed'.
    """
    kwargs = {}
    if label_selector:
        kwargs["label_selector"] = label_selector
    if field_selector:
        kwargs["field_selector"] = field_selector

    core = get_core_client()
    if namespace.upper() == "ALL":
        result = safe(lambda: core.list_pod_for_all_namespaces(limit=MAX_RESULTS, **kwargs))
    else:
        result = safe(lambda: core.list_namespaced_pod(namespace, limit=MAX_RESULTS, **kwargs))
    if isinstance(result, ToolError):
        return str(result)

    lines = []
    rows = []
    for pod in result.items[:MAX_RESULTS]:
        restarts = sum(
            (cs.restart_count for cs in (pod.status.container_statuses or [])),
            0,
        )
        ns = pod.metadata.namespace
        lines.append(
            f"{ns}/{pod.metadata.name}  Status={pod.status.phase}  "
            f"Restarts={restarts}  Age={age(pod.metadata.creation_timestamp)}"
        )
        rows.append(
            {
                "_gvr": "v1~pods",
                "namespace": ns,
                "name": pod.metadata.name,
                "status": pod.status.phase or "Unknown",
                "restarts": restarts,
                "age": age(pod.metadata.creation_timestamp),
                "node": pod.spec.node_name or "",
                "logs": f"/logs/{ns}/{pod.metadata.name}",
            }
        )
    total = len(result.items)
    if total > MAX_RESULTS:
        lines.append(f"... and {total - MAX_RESULTS} more pods (truncated)")
    text = "\n".join(lines) or "No pods found."
    component = (
        {
            "kind": "data_table",
            "title": f"Pods ({len(rows)})",
            "columns": [
                {"id": "namespace", "header": "Namespace"},
                {"id": "name", "header": "Name"},
                {"id": "status", "header": "Status"},
                {"id": "restarts", "header": "Restarts"},
                {"id": "age", "header": "Age"},
                {"id": "node", "header": "Node"},
                {"id": "logs", "header": "Logs"},
            ],
            "rows": rows,
        }
        if rows
        else None
    )
    return (text, component)


@beta_tool
def describe_pod(namespace: str, pod_name: str) -> str:
    """Get detailed information about a specific pod including conditions, containers, and recent events.

    Args:
        namespace: Kubernetes namespace.
        pod_name: Name of the pod.
    """
    if err := _validate_k8s_namespace(namespace):
        return err
    if err := _validate_k8s_name(pod_name, "pod_name"):
        return err
    core = get_core_client()
    result = safe(lambda: core.read_namespaced_pod(pod_name, namespace))
    if isinstance(result, ToolError):
        return str(result)

    pod = result
    info = {
        "name": pod.metadata.name,
        "namespace": pod.metadata.namespace,
        "node": pod.spec.node_name,
        "status": pod.status.phase,
        "ip": pod.status.pod_ip,
        "qos_class": pod.status.qos_class,
        "labels": pod.metadata.labels or {},
        "conditions": [],
        "containers": [],
    }

    for cond in pod.status.conditions or []:
        info["conditions"].append(
            {
                "type": cond.type,
                "status": cond.status,
                "reason": cond.reason,
                "message": cond.message,
            }
        )

    for cs in pod.status.container_statuses or []:
        state = "unknown"
        reason = ""
        if cs.state.running:
            state = "running"
        elif cs.state.waiting:
            state = "waiting"
            reason = cs.state.waiting.reason or ""
        elif cs.state.terminated:
            state = "terminated"
            reason = cs.state.terminated.reason or ""
        info["containers"].append(
            {
                "name": cs.name,
                "image": cs.image,
                "ready": cs.ready,
                "restarts": cs.restart_count,
                "state": state,
                "reason": reason,
            }
        )

    events = safe(
        lambda: core.list_namespaced_event(
            namespace,
            field_selector=f"involvedObject.name={pod_name},involvedObject.kind=Pod",
        )
    )
    if not isinstance(events, ToolError):
        info["recent_events"] = [
            {"type": e.type, "reason": e.reason, "message": e.message, "age": age(e.last_timestamp)}
            for e in sorted(
                events.items, key=lambda e: e.last_timestamp or datetime.min.replace(tzinfo=UTC), reverse=True
            )[:10]
        ]

    text = json.dumps(info, indent=2, default=str)

    # Build structured components for rich UI rendering
    details_component = {
        "kind": "key_value",
        "title": f"Pod — {pod_name}",
        "pairs": [
            {"key": "Namespace", "value": str(info["namespace"])},
            {"key": "Node", "value": str(info.get("node", ""))},
            {"key": "Status", "value": str(info["status"])},
            {"key": "IP", "value": str(info.get("ip", ""))},
            {"key": "QoS Class", "value": str(info.get("qos_class", ""))},
        ],
    }

    components: list[dict] = [details_component]

    # Labels as badges
    labels = info.get("labels") or {}
    if labels:
        components.append(
            {
                "kind": "badge_list",
                "badges": [{"text": f"{k}={v}", "variant": "info"} for k, v in list(labels.items())[:10]],
            }
        )

    # Conditions as status list
    if info["conditions"]:
        components.append(
            {
                "kind": "status_list",
                "title": "Conditions",
                "items": [
                    {
                        "name": c["type"],
                        "status": "healthy" if c["status"] == "True" else "error",
                        "detail": c.get("reason") or c.get("message") or "",
                    }
                    for c in info["conditions"]
                ],
            }
        )

    # Containers as table
    if info["containers"]:
        components.append(
            {
                "kind": "data_table",
                "title": "Containers",
                "columns": [
                    {"id": "name", "header": "Name"},
                    {"id": "image", "header": "Image"},
                    {"id": "state", "header": "State", "type": "status"},
                    {"id": "ready", "header": "Ready", "type": "boolean"},
                    {"id": "restarts", "header": "Restarts"},
                    {"id": "reason", "header": "Reason"},
                ],
                "rows": info["containers"],
            }
        )

    # Wrap in a section
    component = {
        "kind": "section",
        "title": f"Pod Details — {pod_name}",
        "collapsible": False,
        "defaultOpen": True,
        "components": components,
    }

    return (text, component)


@beta_tool
def get_pod_logs(
    namespace: str, pod_name: str, container: str = "", tail_lines: int = 100, previous: bool = False
) -> str:
    """Get logs from a pod container.

    Args:
        namespace: Kubernetes namespace.
        pod_name: Name of the pod.
        container: Container name (required for multi-container pods, optional for single-container).
        tail_lines: Number of recent log lines to retrieve (max 1000).
        previous: If True, get logs from the previous terminated container instance.
    """
    if err := _validate_k8s_namespace(namespace):
        return err
    if err := _validate_k8s_name(pod_name, "pod_name"):
        return err
    tail_lines = min(max(1, tail_lines), MAX_TAIL_LINES)
    kwargs: dict = {"name": pod_name, "namespace": namespace, "tail_lines": tail_lines, "previous": previous}
    if container:
        kwargs["container"] = container
    result = safe(lambda: get_core_client().read_namespaced_pod_log(**kwargs))
    if isinstance(result, ToolError):
        return str(result)
    log_text = result or ""
    if not log_text:
        return "(empty logs)"

    # Parse log lines into structured log_viewer component
    lines = []
    for raw_line in log_text.strip().split("\n"):
        if not raw_line:
            continue
        entry: dict = {"message": raw_line}
        # Try to extract timestamp (common K8s log format: 2026-01-01T00:00:00.000Z ...)
        if len(raw_line) > 24 and raw_line[4] == "-" and "T" in raw_line[:20]:
            ts_end = raw_line.find(" ", 20)
            if ts_end > 0:
                entry["timestamp"] = raw_line[:ts_end]
                entry["message"] = raw_line[ts_end + 1 :]
        # Detect log level
        msg_lower = entry["message"].lower()
        if any(w in msg_lower for w in ("error", "fatal", "panic", "exception")):
            entry["level"] = "error"
        elif any(w in msg_lower for w in ("warn", "warning")):
            entry["level"] = "warn"
        elif any(w in msg_lower for w in ("debug", "trace")):
            entry["level"] = "debug"
        else:
            entry["level"] = "info"
        lines.append(entry)

    source = f"{pod_name}/{container}" if container else pod_name
    component = {
        "kind": "log_viewer",
        "title": f"Logs: {source}",
        "source": source,
        "lines": lines[-500:],  # Cap at 500 lines
    }
    return (log_text[:2000] if len(log_text) > 2000 else log_text, component)


@beta_tool
def list_nodes() -> str:
    """List all nodes with their status, roles, version, and resource capacity."""
    result = safe(lambda: get_core_client().list_node())
    if isinstance(result, ToolError):
        return str(result)

    lines = []
    rows = []
    for node in result.items:
        roles = [
            label.split("/")[-1]
            for label in (node.metadata.labels or {})
            if label.startswith("node-role.kubernetes.io/")
        ] or ["<none>"]

        conditions = {c.type: c.status for c in node.status.conditions or []}
        ready = conditions.get("Ready", "Unknown")

        cap = node.status.capacity or {}
        alloc = node.status.allocatable or {}
        lines.append(
            f"{node.metadata.name}  Roles={','.join(roles)}  Ready={ready}  "
            f"CPU(cap/alloc)={cap.get('cpu', '?')}/{alloc.get('cpu', '?')}  "
            f"Mem(cap/alloc)={cap.get('memory', '?')}/{alloc.get('memory', '?')}  "
            f"Version={node.status.node_info.kubelet_version}  "
            f"Age={age(node.metadata.creation_timestamp)}"
        )
        rows.append(
            {
                "_gvr": "v1~nodes",
                "name": node.metadata.name,
                "roles": ",".join(roles),
                "status": "Ready" if ready == "True" else "NotReady",
                "cpu": f"{cap.get('cpu', '?')}/{alloc.get('cpu', '?')}",
                "memory": f"{cap.get('memory', '?')}/{alloc.get('memory', '?')}",
                "version": node.status.node_info.kubelet_version,
                "age": age(node.metadata.creation_timestamp),
            }
        )
    text = "\n".join(lines) or "No nodes found."
    component = (
        {
            "kind": "data_table",
            "title": f"Nodes ({len(rows)})",
            "columns": [
                {"id": "name", "header": "Name"},
                {"id": "roles", "header": "Roles"},
                {"id": "status", "header": "Status"},
                {"id": "cpu", "header": "CPU (cap/alloc)"},
                {"id": "memory", "header": "Memory (cap/alloc)"},
                {"id": "version", "header": "Version"},
                {"id": "age", "header": "Age"},
            ],
            "rows": rows,
        }
        if rows
        else None
    )
    return (text, component)


@beta_tool
def visualize_nodes(label_selector: str = "", show_pods: bool = True) -> str:
    """Visualize cluster nodes as an interactive hex map showing health status,
    CPU/memory usage, and pod distribution. Each node renders as a clickable
    hexagon with status coloring and resource gauges. Use this for cluster
    overviews and node health dashboards.

    Args:
        label_selector: Filter nodes by labels (e.g., 'node-role.kubernetes.io/worker=').
        show_pods: Include per-node pod details for clickable pod dots (default: true).
    """
    kwargs = {}
    if label_selector:
        kwargs["label_selector"] = label_selector

    result = safe(lambda: get_core_client().list_node(**kwargs))
    if isinstance(result, ToolError):
        return str(result)

    node_specs = []
    for node in result.items:
        conditions = {c.type: c.status for c in node.status.conditions or []}
        ready = conditions.get("Ready", "Unknown") == "True"
        labels = node.metadata.labels or {}
        roles = [label.split("/")[-1] for label in labels if label.startswith("node-role.kubernetes.io/")] or ["worker"]

        alloc = node.status.allocatable or {}
        cap = node.status.capacity or {}

        pressure_conditions = [
            c.type for c in (node.status.conditions or []) if c.type.endswith("Pressure") and c.status == "True"
        ]

        status = "not-ready"
        if ready and not node.spec.unschedulable:
            status = "pressure" if pressure_conditions else "ready"
        elif node.spec.unschedulable:
            status = "cordoned"

        node_specs.append(
            {
                "name": node.metadata.name,
                "status": status,
                "roles": roles,
                "podCount": 0,
                "podCap": int(alloc.get("pods", cap.get("pods", "110"))),
                "age": age(node.metadata.creation_timestamp),
                "instanceType": labels.get("node.kubernetes.io/instance-type", ""),
                "conditions": pressure_conditions,
            }
        )

    # Get pod counts per node
    pods_by_node: dict[str, list[dict]] = {}
    if show_pods:
        pods_result = safe(lambda: get_core_client().list_pod_for_all_namespaces(limit=1000))
        if not isinstance(pods_result, ToolError):
            for pod in pods_result.items:
                node_name = pod.spec.node_name
                if not node_name:
                    continue
                if node_name not in pods_by_node:
                    pods_by_node[node_name] = []
                cs = (pod.status.container_statuses or [None])[0]
                wait_reason = cs.state.waiting.reason if cs and cs.state and cs.state.waiting else None
                pods_by_node[node_name].append(
                    {
                        "name": pod.metadata.name,
                        "namespace": pod.metadata.namespace or "",
                        "status": wait_reason or pod.status.phase or "Unknown",
                        "restarts": cs.restart_count if cs else 0,
                    }
                )

    for ns in node_specs:
        ns["podCount"] = len(pods_by_node.get(ns["name"], []))

    # Get CPU/memory metrics
    try:
        from kubernetes import client as k8s_client

        from .units import parse_cpu_millicores, parse_memory_bytes

        custom = k8s_client.CustomObjectsApi()
        metrics = custom.list_cluster_custom_object("metrics.k8s.io", "v1beta1", "nodes")
        for m in metrics.get("items", []):
            name = m["metadata"]["name"]
            ns_spec = next((n for n in node_specs if n["name"] == name), None)
            if ns_spec:
                cpu_usage = parse_cpu_millicores(m["usage"]["cpu"])
                mem_usage = parse_memory_bytes(m["usage"]["memory"])
                alloc_node = next((n for n in result.items if n.metadata.name == name), None)
                if alloc_node:
                    node_alloc = alloc_node.status.allocatable or {}
                    cpu_alloc = parse_cpu_millicores(node_alloc.get("cpu", "1"))
                    mem_alloc = parse_memory_bytes(node_alloc.get("memory", "1Gi"))
                    ns_spec["cpuPct"] = round(cpu_usage / cpu_alloc * 100, 1) if cpu_alloc else None
                    ns_spec["memPct"] = round(mem_usage / mem_alloc * 100, 1) if mem_alloc else None
    except Exception:
        pass

    ready_count = sum(1 for n in node_specs if n["status"] == "ready")
    total_pods = sum(n["podCount"] for n in node_specs)
    text = f"Cluster: {ready_count}/{len(node_specs)} nodes ready, {total_pods} pods running"

    component = {
        "kind": "node_map",
        "title": "Cluster Nodes",
        "description": text,
        "nodes": node_specs,
    }
    if show_pods:
        component["pods"] = pods_by_node

    return (text, component)


@beta_tool
def describe_node(node_name: str) -> str:
    """Get detailed information about a node including conditions, taints, and resource usage.

    Args:
        node_name: Name of the node.
    """
    if err := _validate_k8s_name(node_name, "node_name"):
        return err
    result = safe(lambda: get_core_client().read_node(node_name))
    if isinstance(result, ToolError):
        return str(result)

    node = result
    info = {
        "name": node.metadata.name,
        "labels": node.metadata.labels or {},
        "annotations_count": len(node.metadata.annotations or {}),
        "creation": str(node.metadata.creation_timestamp),
        "conditions": [
            {"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
            for c in node.status.conditions or []
        ],
        "taints": [{"key": t.key, "value": t.value, "effect": t.effect} for t in node.spec.taints or []],
        "capacity": dict(node.status.capacity or {}),
        "allocatable": dict(node.status.allocatable or {}),
        "node_info": {
            "os": node.status.node_info.operating_system,
            "arch": node.status.node_info.architecture,
            "kernel": node.status.node_info.kernel_version,
            "container_runtime": node.status.node_info.container_runtime_version,
            "kubelet": node.status.node_info.kubelet_version,
        },
        "unschedulable": node.spec.unschedulable or False,
    }
    return json.dumps(info, indent=2, default=str)


@beta_tool
def get_events(
    namespace: str = "default", resource_kind: str = "", resource_name: str = "", event_type: str = ""
) -> str:
    """Get cluster events, optionally filtered by resource.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' for cluster-wide events.
        resource_kind: Filter by resource kind (e.g. 'Pod', 'Node', 'Deployment').
        resource_name: Filter by resource name.
        event_type: Filter by event type: 'Normal' or 'Warning'.
    """
    field_parts = []
    if resource_kind:
        field_parts.append(f"involvedObject.kind={resource_kind}")
    if resource_name:
        field_parts.append(f"involvedObject.name={resource_name}")
    if event_type:
        field_parts.append(f"type={event_type}")
    field_selector = ",".join(field_parts)

    kwargs = {}
    if field_selector:
        kwargs["field_selector"] = field_selector

    core = get_core_client()
    if namespace.upper() == "ALL":
        result = safe(lambda: core.list_event_for_all_namespaces(**kwargs))
    else:
        result = safe(lambda: core.list_namespaced_event(namespace, **kwargs))
    if isinstance(result, ToolError):
        return str(result)

    events = sorted(
        result.items,
        key=lambda e: e.last_timestamp or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )[:50]

    lines = []
    rows = []
    for e in events:
        lines.append(
            f"{age(e.last_timestamp)} ago  {e.type}  {e.reason}  "
            f"{e.involved_object.kind}/{e.involved_object.name}  "
            f"{e.message}"
        )
        rows.append(
            {
                "age": age(e.last_timestamp) + " ago",
                "type": e.type or "Normal",
                "reason": e.reason or "",
                "resource": f"{e.involved_object.kind}/{e.involved_object.name}",
                "message": (e.message or "")[:120],
            }
        )
    text = "\n".join(lines) or "No events found."
    component = (
        {
            "kind": "data_table",
            "title": f"Events ({len(rows)})",
            "columns": [
                {"id": "age", "header": "Age"},
                {"id": "type", "header": "Type"},
                {"id": "reason", "header": "Reason"},
                {"id": "resource", "header": "Resource"},
                {"id": "message", "header": "Message"},
            ],
            "rows": rows,
        }
        if rows
        else None
    )
    return (text, component)


@beta_tool
def list_deployments(namespace: str = "default") -> str:
    """List deployments with their replica counts and status.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' for all namespaces.
    """
    apps = get_apps_client()
    if namespace.upper() == "ALL":
        result = safe(lambda: apps.list_deployment_for_all_namespaces(limit=MAX_RESULTS))
    else:
        result = safe(lambda: apps.list_namespaced_deployment(namespace, limit=MAX_RESULTS))
    if isinstance(result, ToolError):
        return str(result)

    lines = []
    rows = []
    for dep in result.items[:MAX_RESULTS]:
        s = dep.status
        ready = s.ready_replicas or 0
        desired = s.replicas or 0
        lines.append(
            f"{dep.metadata.namespace}/{dep.metadata.name}  "
            f"Ready={ready}/{desired}  "
            f"Updated={s.updated_replicas or 0}  "
            f"Available={s.available_replicas or 0}  "
            f"Age={age(dep.metadata.creation_timestamp)}"
        )
        rows.append(
            {
                "_gvr": "apps~v1~deployments",
                "namespace": dep.metadata.namespace,
                "name": dep.metadata.name,
                "ready": f"{ready}/{desired}",
                "status": "Healthy"
                if ready == desired and desired > 0
                else ("Degraded" if ready > 0 else "Unavailable"),
                "updated": s.updated_replicas or 0,
                "available": s.available_replicas or 0,
                "age": age(dep.metadata.creation_timestamp),
            }
        )
    text = "\n".join(lines) or "No deployments found."
    component = (
        {
            "kind": "data_table",
            "title": f"Deployments ({len(rows)})",
            "columns": [
                {"id": "namespace", "header": "Namespace"},
                {"id": "name", "header": "Name"},
                {"id": "ready", "header": "Ready"},
                {"id": "status", "header": "Status"},
                {"id": "updated", "header": "Updated"},
                {"id": "age", "header": "Age"},
            ],
            "rows": rows,
        }
        if rows
        else None
    )
    return (text, component)


@beta_tool
def describe_deployment(namespace: str, name: str) -> str:
    """Get detailed information about a deployment including strategy, conditions, and pod template.

    Args:
        namespace: Kubernetes namespace.
        name: Name of the deployment.
    """
    result = safe(lambda: get_apps_client().read_namespaced_deployment(name, namespace))
    if isinstance(result, ToolError):
        return str(result)

    dep = result
    containers = []
    for c in dep.spec.template.spec.containers:
        containers.append(
            {
                "name": c.name,
                "image": c.image,
                "resources": {
                    "requests": dict(c.resources.requests or {}) if c.resources else {},
                    "limits": dict(c.resources.limits or {}) if c.resources else {},
                },
                "ports": [{"port": p.container_port, "protocol": p.protocol} for p in (c.ports or [])],
            }
        )

    s = dep.status
    ready = s.ready_replicas or 0
    desired = s.replicas or 0

    info = {
        "name": dep.metadata.name,
        "namespace": dep.metadata.namespace,
        "replicas": dep.spec.replicas,
        "strategy": dep.spec.strategy.type if dep.spec.strategy else "unknown",
        "selector": dep.spec.selector.match_labels,
        "labels": dep.metadata.labels or {},
        "conditions": [
            {"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
            for c in dep.status.conditions or []
        ],
        "containers": containers,
    }
    text = json.dumps(info, indent=2, default=str)

    components: list[dict] = [
        {
            "kind": "key_value",
            "title": f"Deployment — {name}",
            "pairs": [
                {"key": "Namespace", "value": str(dep.metadata.namespace)},
                {"key": "Replicas", "value": f"{ready}/{desired} ready"},
                {"key": "Strategy", "value": info["strategy"]},
                {
                    "key": "Selector",
                    "value": ", ".join(f"{k}={v}" for k, v in (dep.spec.selector.match_labels or {}).items()),
                },
                {"key": "Age", "value": age(dep.metadata.creation_timestamp)},
            ],
        },
    ]

    labels = info.get("labels") or {}
    if labels:
        components.append(
            {
                "kind": "badge_list",
                "badges": [{"text": f"{k}={v}", "variant": "info"} for k, v in list(labels.items())[:10]],
            }
        )

    if info["conditions"]:
        components.append(
            {
                "kind": "status_list",
                "title": "Conditions",
                "items": [
                    {
                        "name": c["type"],
                        "status": "healthy" if c["status"] == "True" else "warning",
                        "detail": c.get("reason") or c.get("message") or "",
                    }
                    for c in info["conditions"]
                ],
            }
        )

    if containers:
        components.append(
            {
                "kind": "data_table",
                "title": "Containers",
                "columns": [
                    {"id": "name", "header": "Name"},
                    {"id": "image", "header": "Image"},
                ],
                "rows": [{"name": c["name"], "image": c["image"]} for c in containers],
            }
        )

    # Pod template spec as YAML
    try:
        import yaml as _yaml

        template_spec = dep.spec.template.spec.to_dict() if hasattr(dep.spec.template.spec, "to_dict") else {}
        if template_spec:
            components.append(
                {
                    "kind": "yaml_viewer",
                    "title": "Pod Template Spec",
                    "content": _yaml.dump(template_spec, default_flow_style=False),
                    "language": "yaml",
                }
            )
    except Exception:
        pass

    component = {
        "kind": "section",
        "title": f"Deployment Details — {name}",
        "collapsible": False,
        "defaultOpen": True,
        "components": components,
    }
    return (text, component)


@beta_tool
def get_resource_quotas(namespace: str = "default") -> str:
    """Get resource quotas and current usage for a namespace.

    Args:
        namespace: Kubernetes namespace.
    """
    result = safe(lambda: get_core_client().list_namespaced_resource_quota(namespace))
    if isinstance(result, ToolError):
        return str(result)

    if not result.items:
        return f"No resource quotas defined in namespace '{namespace}'."

    lines = []
    for rq in result.items:
        lines.append(f"Quota: {rq.metadata.name}")
        hard = rq.status.hard or {}
        used = rq.status.used or {}
        for resource in sorted(hard.keys()):
            lines.append(f"  {resource}: {used.get(resource, '0')} / {hard[resource]}")
    return "\n".join(lines)


@beta_tool
def get_services(namespace: str = "default") -> str:
    """List services in a namespace with their type, cluster IP, and ports.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' for all namespaces.
    """
    core = get_core_client()
    if namespace.upper() == "ALL":
        result = safe(lambda: core.list_service_for_all_namespaces())
    else:
        result = safe(lambda: core.list_namespaced_service(namespace))
    if isinstance(result, ToolError):
        return str(result)

    lines = []
    for svc in result.items[:MAX_RESULTS]:
        ports = ", ".join(
            f"{p.port}/{p.protocol}" + (f"→{p.target_port}" if p.target_port else "") for p in (svc.spec.ports or [])
        )
        lines.append(
            f"{svc.metadata.namespace}/{svc.metadata.name}  "
            f"Type={svc.spec.type}  ClusterIP={svc.spec.cluster_ip}  Ports=[{ports}]"
        )
    return "\n".join(lines) or "No services found."


@beta_tool
def get_persistent_volume_claims(namespace: str = "default") -> str:
    """List PersistentVolumeClaims with their status, capacity, and storage class.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' for all namespaces.
    """
    core = get_core_client()
    if namespace.upper() == "ALL":
        result = safe(lambda: core.list_persistent_volume_claim_for_all_namespaces())
    else:
        result = safe(lambda: core.list_namespaced_persistent_volume_claim(namespace))
    if isinstance(result, ToolError):
        return str(result)

    lines = []
    for pvc in result.items[:MAX_RESULTS]:
        cap = (pvc.status.capacity or {}).get("storage", "?")
        lines.append(
            f"{pvc.metadata.namespace}/{pvc.metadata.name}  "
            f"Status={pvc.status.phase}  Capacity={cap}  "
            f"StorageClass={pvc.spec.storage_class_name}  "
            f"Age={age(pvc.metadata.creation_timestamp)}"
        )
    return "\n".join(lines) or "No PVCs found."


@beta_tool
def get_cluster_version() -> str:
    """Get the Kubernetes/OpenShift cluster version information."""
    result = safe(lambda: get_version_client().get_code())
    if isinstance(result, ToolError):
        return str(result)

    info = f"Kubernetes {result.git_version} (Platform: {result.platform})"

    try:
        cv = get_custom_client().get_cluster_custom_object("config.openshift.io", "v1", "clusterversions", "version")
        ocp_version = cv.get("status", {}).get("desired", {}).get("version", "unknown")
        channel = cv.get("spec", {}).get("channel", "unknown")
        conditions = cv.get("status", {}).get("conditions", [])
        cond_summary = ", ".join(f"{c['type']}={c['status']}" for c in conditions)
        info += f"\nOpenShift {ocp_version} (Channel: {channel})"
        info += f"\nConditions: {cond_summary}"
    except ApiException:
        pass

    return info


@beta_tool
def get_cluster_operators() -> str:
    """List OpenShift ClusterOperators and their status (Available, Progressing, Degraded). Only works on OpenShift clusters."""
    try:
        result = get_custom_client().list_cluster_custom_object("config.openshift.io", "v1", "clusteroperators")
    except ApiException as e:
        return f"Error ({e.status}): {e.reason}. This may not be an OpenShift cluster."

    lines = []
    items = []
    for co in result.get("items", []):
        name = co["metadata"]["name"]
        conditions = {c["type"]: c["status"] for c in co.get("status", {}).get("conditions", [])}
        available = conditions.get("Available", "?")
        degraded = conditions.get("Degraded", "?")
        lines.append(
            f"{name}  Available={available}  Progressing={conditions.get('Progressing', '?')}  Degraded={degraded}"
        )
        status = "error" if degraded == "True" else ("healthy" if available == "True" else "warning")
        items.append({"name": name, "status": status, "detail": f"Available={available}"})
    text = "\n".join(lines) or "No ClusterOperators found."
    component = {"kind": "status_list", "title": f"Cluster Operators ({len(items)})", "items": items} if items else None
    return (text, component)


# ---------------------------------------------------------------------------
# Action tools (write operations — require user confirmation)
# ---------------------------------------------------------------------------


@beta_tool
def scale_deployment(namespace: str, name: str, replicas: int) -> str:
    """Scale a deployment to a specific number of replicas. REQUIRES USER CONFIRMATION.

    Args:
        namespace: Kubernetes namespace.
        name: Name of the deployment to scale.
        replicas: Desired number of replicas (0-100).
    """
    if err := _validate_k8s_namespace(namespace):
        return err
    if err := _validate_k8s_name(name):
        return err
    replicas = min(max(0, replicas), MAX_REPLICAS)
    result = safe(
        lambda: get_apps_client().patch_namespaced_deployment_scale(
            name, namespace, body={"spec": {"replicas": replicas}}
        )
    )
    if isinstance(result, ToolError):
        return str(result)
    return f"Scaled {namespace}/{name} to {replicas} replicas."


@beta_tool
def restart_deployment(namespace: str, name: str) -> str:
    """Trigger a rolling restart of a deployment. REQUIRES USER CONFIRMATION.

    Args:
        namespace: Kubernetes namespace.
        name: Name of the deployment to restart.
    """
    if err := _validate_k8s_namespace(namespace):
        return err
    if err := _validate_k8s_name(name):
        return err
    now = datetime.now(UTC).isoformat()
    body = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": now}}}}}
    result = safe(lambda: get_apps_client().patch_namespaced_deployment(name, namespace, body=body))
    if isinstance(result, ToolError):
        return str(result)
    return f"Rolling restart triggered for {namespace}/{name}."


@beta_tool
def cordon_node(node_name: str) -> str:
    """Mark a node as unschedulable (cordon). REQUIRES USER CONFIRMATION.

    Args:
        node_name: Name of the node to cordon.
    """
    if err := _validate_k8s_name(node_name, "node_name"):
        return err
    result = safe(lambda: get_core_client().patch_node(node_name, body={"spec": {"unschedulable": True}}))
    if isinstance(result, ToolError):
        return str(result)
    return f"Node {node_name} cordoned (marked unschedulable)."


@beta_tool
def uncordon_node(node_name: str) -> str:
    """Mark a node as schedulable (uncordon). REQUIRES USER CONFIRMATION.

    Args:
        node_name: Name of the node to uncordon.
    """
    if err := _validate_k8s_name(node_name, "node_name"):
        return err
    result = safe(lambda: get_core_client().patch_node(node_name, body={"spec": {"unschedulable": False}}))
    if isinstance(result, ToolError):
        return str(result)
    return f"Node {node_name} uncordoned (marked schedulable)."


@beta_tool
def delete_pod(namespace: str, pod_name: str, grace_period_seconds: int = 30) -> str:
    """Delete a pod (it will be recreated by its controller if one exists). REQUIRES USER CONFIRMATION.

    Args:
        namespace: Kubernetes namespace.
        pod_name: Name of the pod to delete.
        grace_period_seconds: Grace period before force killing (1-300).
    """
    if err := _validate_k8s_namespace(namespace):
        return err
    if err := _validate_k8s_name(pod_name, "pod_name"):
        return err
    grace_period_seconds = min(max(1, grace_period_seconds), 300)
    result = safe(
        lambda: get_core_client().delete_namespaced_pod(
            pod_name,
            namespace,
            body=client.V1DeleteOptions(grace_period_seconds=grace_period_seconds),
        )
    )
    if isinstance(result, ToolError):
        return str(result)
    return f"Pod {namespace}/{pod_name} deleted."


@beta_tool
def get_configmap(namespace: str, name: str) -> str:
    """Get the contents of a ConfigMap.

    Args:
        namespace: Kubernetes namespace.
        name: Name of the ConfigMap.
    """
    result = safe(lambda: get_core_client().read_namespaced_config_map(name, namespace))
    if isinstance(result, ToolError):
        return str(result)
    data = result.data or {}
    info = {"name": result.metadata.name, "namespace": result.metadata.namespace, "data": data}
    text = json.dumps(info, indent=2, default=str)

    # Render each data key as a yaml_viewer component
    if len(data) == 1:
        key, val = next(iter(data.items()))
        lang = "json" if val.strip().startswith("{") or val.strip().startswith("[") else "yaml"
        component = {"kind": "yaml_viewer", "title": f"ConfigMap: {name}/{key}", "content": val, "language": lang}
        return (text, component)

    # Multiple keys → key_value summary
    component = {
        "kind": "key_value",
        "title": f"ConfigMap: {name}",
        "pairs": [{"key": k, "value": v[:100] + ("..." if len(v) > 100 else "")} for k, v in data.items()],
    }
    return (text, component)


# ---------------------------------------------------------------------------
# Metrics API tools (require metrics-server)
# ---------------------------------------------------------------------------


@beta_tool
def get_node_metrics() -> str:
    """Get actual CPU and memory usage for all nodes from the metrics API. Requires metrics-server to be installed."""
    from .units import format_cpu, format_memory, parse_cpu_millicores, parse_memory_bytes

    try:
        result = get_custom_client().list_cluster_custom_object(_METRICS_GROUP, _METRICS_VERSION, "nodes")
    except ApiException as e:
        if e.status == 404:
            return "Error: Metrics API not available. Is metrics-server installed?"
        return f"Error ({e.status}): {e.reason}"

    # Get node capacity for utilization %
    nodes_result = safe(lambda: get_core_client().list_node())
    capacity_map = {}
    if not isinstance(nodes_result, ToolError):
        for node in nodes_result.items:
            alloc = node.status.allocatable or {}
            capacity_map[node.metadata.name] = {
                "cpu_m": parse_cpu_millicores(alloc.get("cpu", "0")),
                "mem_bytes": parse_memory_bytes(alloc.get("memory", "0")),
            }

    lines = []
    rows = []
    for item in result.get("items", []):
        name = item["metadata"]["name"]
        usage = item.get("usage", {})
        cpu_m = parse_cpu_millicores(usage.get("cpu", "0"))
        mem_bytes = parse_memory_bytes(usage.get("memory", "0"))

        cpu_pct_val = 0
        mem_pct_val = 0
        pct = ""
        if name in capacity_map:
            cap = capacity_map[name]
            cpu_pct_val = (cpu_m / cap["cpu_m"] * 100) if cap["cpu_m"] > 0 else 0
            mem_pct_val = (mem_bytes / cap["mem_bytes"] * 100) if cap["mem_bytes"] > 0 else 0
            pct = f"  CPU%={cpu_pct_val:.0f}%  Mem%={mem_pct_val:.0f}%"

        lines.append(f"{name}  CPU={format_cpu(cpu_m)}  Memory={format_memory(mem_bytes)}{pct}")
        rows.append(
            {
                "name": name,
                "cpu": format_cpu(cpu_m),
                "memory": format_memory(mem_bytes),
                "cpu_pct": f"{cpu_pct_val:.0f}%",
                "mem_pct": f"{mem_pct_val:.0f}%",
            }
        )

    text = "\n".join(lines) or "No node metrics found."
    component = (
        {
            "kind": "data_table",
            "title": f"Node Metrics ({len(rows)})",
            "columns": [
                {"id": "name", "header": "Node"},
                {"id": "cpu", "header": "CPU Usage"},
                {"id": "cpu_pct", "header": "CPU %"},
                {"id": "memory", "header": "Memory Usage"},
                {"id": "mem_pct", "header": "Memory %"},
            ],
            "rows": rows,
        }
        if rows
        else None
    )
    return (text, component)


@beta_tool
def get_pod_metrics(namespace: str = "default", sort_by: str = "cpu") -> str:
    """Get actual CPU and memory usage for pods from the metrics API. Requires metrics-server.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' for all namespaces.
        sort_by: Sort results by 'cpu' or 'memory'. Shows top consumers first.
    """
    from .units import format_cpu, format_memory, parse_cpu_millicores, parse_memory_bytes

    try:
        if namespace.upper() == "ALL":
            result = get_custom_client().list_cluster_custom_object(_METRICS_GROUP, _METRICS_VERSION, "pods")
        else:
            result = get_custom_client().list_namespaced_custom_object(
                _METRICS_GROUP, _METRICS_VERSION, namespace, "pods"
            )
    except ApiException as e:
        if e.status == 404:
            return "Error: Metrics API not available. Is metrics-server installed?"
        return f"Error ({e.status}): {e.reason}"

    pods = []
    for item in result.get("items", []):
        ns = item["metadata"]["namespace"]
        name = item["metadata"]["name"]
        total_cpu_m = 0
        total_mem_bytes = 0
        for container in item.get("containers", []):
            usage = container.get("usage", {})
            total_cpu_m += parse_cpu_millicores(usage.get("cpu", "0"))
            total_mem_bytes += parse_memory_bytes(usage.get("memory", "0"))

        pods.append(
            {
                "ns": ns,
                "name": name,
                "cpu_m": total_cpu_m,
                "mem_bytes": total_mem_bytes,
                "cpu_str": format_cpu(total_cpu_m),
                "mem_str": format_memory(total_mem_bytes),
            }
        )

    if sort_by == "memory":
        pods.sort(key=lambda p: p["mem_bytes"], reverse=True)
    else:
        pods.sort(key=lambda p: p["cpu_m"], reverse=True)

    lines = []
    rows = []
    for p in pods[:MAX_RESULTS]:
        lines.append(f"{p['ns']}/{p['name']}  CPU={p['cpu_str']}  Memory={p['mem_str']}")
        rows.append(
            {
                "namespace": p["ns"],
                "name": p["name"],
                "cpu": p["cpu_str"],
                "memory": p["mem_str"],
            }
        )
    total = len(pods)
    if total > MAX_RESULTS:
        lines.append(f"... and {total - MAX_RESULTS} more pods (truncated)")

    text = "\n".join(lines) or "No pod metrics found."
    sort_label = "CPU" if sort_by != "memory" else "Memory"
    component = (
        {
            "kind": "data_table",
            "title": f"Pod Metrics — Top by {sort_label} ({len(rows)})",
            "columns": [
                {"id": "namespace", "header": "Namespace"},
                {"id": "name", "header": "Pod"},
                {"id": "cpu", "header": "CPU"},
                {"id": "memory", "header": "Memory"},
            ],
            "rows": rows,
        }
        if rows
        else None
    )
    return (text, component)


# ---------------------------------------------------------------------------
# Additional diagnostic tools
# ---------------------------------------------------------------------------


@beta_tool
def list_statefulsets(namespace: str = "default") -> str:
    """List StatefulSets with their replica counts and status.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' for all namespaces.
    """
    apps = get_apps_client()
    if namespace.upper() == "ALL":
        result = safe(lambda: apps.list_stateful_set_for_all_namespaces())
    else:
        result = safe(lambda: apps.list_namespaced_stateful_set(namespace))
    if isinstance(result, ToolError):
        return str(result)

    lines = []
    rows = []
    for sts in result.items[:MAX_RESULTS]:
        s = sts.status
        ready = s.ready_replicas or 0
        desired = s.replicas or 0
        lines.append(
            f"{sts.metadata.namespace}/{sts.metadata.name}  "
            f"Ready={ready}/{desired}  "
            f"Updated={s.updated_replicas or 0}  "
            f"Age={age(sts.metadata.creation_timestamp)}"
        )
        rows.append(
            {
                "_gvr": "apps~v1~statefulsets",
                "namespace": sts.metadata.namespace,
                "name": sts.metadata.name,
                "ready": f"{ready}/{desired}",
                "status": "Healthy"
                if ready == desired and desired > 0
                else ("Degraded" if ready > 0 else "Unavailable"),
                "updated": s.updated_replicas or 0,
                "age": age(sts.metadata.creation_timestamp),
            }
        )
    text = "\n".join(lines) or "No StatefulSets found."
    component = (
        {
            "kind": "data_table",
            "title": f"StatefulSets ({len(rows)})",
            "columns": [
                {"id": "namespace", "header": "Namespace"},
                {"id": "name", "header": "Name"},
                {"id": "ready", "header": "Ready"},
                {"id": "status", "header": "Status"},
                {"id": "updated", "header": "Updated"},
                {"id": "age", "header": "Age"},
            ],
            "rows": rows,
        }
        if rows
        else None
    )
    return (text, component)


@beta_tool
def list_daemonsets(namespace: str = "default") -> str:
    """List DaemonSets with their status and node counts.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' for all namespaces.
    """
    apps = get_apps_client()
    if namespace.upper() == "ALL":
        result = safe(lambda: apps.list_daemon_set_for_all_namespaces())
    else:
        result = safe(lambda: apps.list_namespaced_daemon_set(namespace))
    if isinstance(result, ToolError):
        return str(result)

    lines = []
    rows = []
    for ds in result.items[:MAX_RESULTS]:
        s = ds.status
        desired = s.desired_number_scheduled
        ready = s.number_ready or 0
        lines.append(
            f"{ds.metadata.namespace}/{ds.metadata.name}  "
            f"Desired={desired}  "
            f"Ready={ready}  "
            f"Available={s.number_available or 0}  "
            f"Misscheduled={s.number_misscheduled or 0}  "
            f"Age={age(ds.metadata.creation_timestamp)}"
        )
        rows.append(
            {
                "_gvr": "apps~v1~daemonsets",
                "namespace": ds.metadata.namespace,
                "name": ds.metadata.name,
                "desired": desired,
                "ready": ready,
                "available": s.number_available or 0,
                "status": "Healthy" if ready == desired else "Degraded",
                "age": age(ds.metadata.creation_timestamp),
            }
        )
    text = "\n".join(lines) or "No DaemonSets found."
    component = (
        {
            "kind": "data_table",
            "title": f"DaemonSets ({len(rows)})",
            "columns": [
                {"id": "namespace", "header": "Namespace"},
                {"id": "name", "header": "Name"},
                {"id": "desired", "header": "Desired"},
                {"id": "ready", "header": "Ready"},
                {"id": "available", "header": "Available"},
                {"id": "status", "header": "Status"},
                {"id": "age", "header": "Age"},
            ],
            "rows": rows,
        }
        if rows
        else None
    )
    return (text, component)


@beta_tool
def list_jobs(namespace: str = "default", show_completed: bool = False) -> str:
    """List Jobs with their status, completions, and duration.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' for all namespaces.
        show_completed: If False (default), only show active/failed jobs.
    """
    batch = get_batch_client()
    if namespace.upper() == "ALL":
        result = safe(lambda: batch.list_job_for_all_namespaces())
    else:
        result = safe(lambda: batch.list_namespaced_job(namespace))
    if isinstance(result, ToolError):
        return str(result)

    lines = []
    for job in result.items[:MAX_RESULTS]:
        s = job.status
        succeeded = s.succeeded or 0
        failed = s.failed or 0
        active = s.active or 0
        completions = job.spec.completions or 1

        if not show_completed and succeeded >= completions and failed == 0 and active == 0:
            continue

        duration = ""
        if s.start_time and s.completion_time:
            delta = s.completion_time - s.start_time
            duration = f"  Duration={int(delta.total_seconds())}s"

        status = "Running" if active > 0 else ("Complete" if succeeded >= completions else "Failed")
        lines.append(
            f"{job.metadata.namespace}/{job.metadata.name}  "
            f"Status={status}  "
            f"Completions={succeeded}/{completions}  "
            f"Failed={failed}  Active={active}"
            f"{duration}  Age={age(job.metadata.creation_timestamp)}"
        )
    return "\n".join(lines) or "No matching Jobs found."


@beta_tool
def list_cronjobs(namespace: str = "default") -> str:
    """List CronJobs with their schedule, last run, and active jobs.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' for all namespaces.
    """
    batch = get_batch_client()
    if namespace.upper() == "ALL":
        result = safe(lambda: batch.list_cron_job_for_all_namespaces())
    else:
        result = safe(lambda: batch.list_namespaced_cron_job(namespace))
    if isinstance(result, ToolError):
        return str(result)

    lines = []
    for cj in result.items[:MAX_RESULTS]:
        last_schedule = age(cj.status.last_schedule_time) + " ago" if cj.status.last_schedule_time else "never"
        active = len(cj.status.active or [])
        suspended = "SUSPENDED" if cj.spec.suspend else "Active"
        lines.append(
            f"{cj.metadata.namespace}/{cj.metadata.name}  "
            f"Schedule={cj.spec.schedule}  {suspended}  "
            f"LastRun={last_schedule}  ActiveJobs={active}  "
            f"Age={age(cj.metadata.creation_timestamp)}"
        )
    return "\n".join(lines) or "No CronJobs found."


@beta_tool
def list_ingresses(namespace: str = "default") -> str:
    """List Ingresses with their hosts, paths, and backends.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' for all namespaces.
    """
    net = get_networking_client()
    if namespace.upper() == "ALL":
        result = safe(lambda: net.list_ingress_for_all_namespaces())
    else:
        result = safe(lambda: net.list_namespaced_ingress(namespace))
    if isinstance(result, ToolError):
        return str(result)

    lines = []
    for ing in result.items[:MAX_RESULTS]:
        hosts = []
        for rule in ing.spec.rules or []:
            host = rule.host or "*"
            paths = []
            for p in rule.http.paths if rule.http else []:
                backend = (
                    f"{p.backend.service.name}:{p.backend.service.port.number or p.backend.service.port.name}"
                    if p.backend.service
                    else "?"
                )
                paths.append(f"{p.path or '/'}→{backend}")
            hosts.append(f"{host} [{', '.join(paths)}]")

        tls = "TLS" if ing.spec.tls else "HTTP"
        class_name = ing.spec.ingress_class_name or "default"
        lines.append(
            f"{ing.metadata.namespace}/{ing.metadata.name}  "
            f"Class={class_name}  {tls}  "
            f"Hosts: {'; '.join(hosts)}  "
            f"Age={age(ing.metadata.creation_timestamp)}"
        )
    return "\n".join(lines) or "No Ingresses found."


@beta_tool
def list_routes(namespace: str = "default") -> str:
    """List OpenShift Routes with their hosts, paths, TLS, and target services. OpenShift only.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' for all namespaces.
    """
    try:
        if namespace.upper() == "ALL":
            result = get_custom_client().list_cluster_custom_object("route.openshift.io", "v1", "routes")
        else:
            result = get_custom_client().list_namespaced_custom_object("route.openshift.io", "v1", namespace, "routes")
    except ApiException as e:
        return f"Error ({e.status}): {e.reason}. Is this an OpenShift cluster?"

    lines = []
    for route in result.get("items", [])[:MAX_RESULTS]:
        meta = route["metadata"]
        spec = route.get("spec", {})
        status = route.get("status", {})

        host = spec.get("host", "?")
        path = spec.get("path", "/")
        svc = spec.get("to", {}).get("name", "?")
        port = spec.get("port", {}).get("targetPort", "?")
        tls = "TLS" if spec.get("tls") else "HTTP"
        termination = spec.get("tls", {}).get("termination", "") if spec.get("tls") else ""

        admitted = "Unknown"
        for ingress in status.get("ingress", []):
            for cond in ingress.get("conditions", []):
                if cond.get("type") == "Admitted":
                    admitted = "Admitted" if cond.get("status") == "True" else "NotAdmitted"

        lines.append(
            f"{meta.get('namespace', '?')}/{meta['name']}  "
            f"{tls}{('/' + termination) if termination else ''}  "
            f"Host={host}{path}  Service={svc}:{port}  "
            f"Status={admitted}"
        )
    return "\n".join(lines) or "No Routes found."


@beta_tool
def list_hpas(namespace: str = "default") -> str:
    """List Horizontal Pod Autoscalers with their current/target metrics and replica counts.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' for all namespaces.
    """
    auto = get_autoscaling_client()
    if namespace.upper() == "ALL":
        result = safe(lambda: auto.list_horizontal_pod_autoscaler_for_all_namespaces())
    else:
        result = safe(lambda: auto.list_namespaced_horizontal_pod_autoscaler(namespace))
    if isinstance(result, ToolError):
        return str(result)

    lines = []
    rows = []
    for hpa in result.items[:MAX_RESULTS]:
        s = hpa.status
        ref = hpa.spec.scale_target_ref
        target = f"{ref.kind}/{ref.name}"

        metrics_str = []
        for mc in hpa.status.current_metrics or []:
            if mc.type == "Resource" and mc.resource:
                current = mc.resource.current.average_utilization
                metrics_str.append(f"{mc.resource.name}={current}%")

        replicas_str = f"{s.current_replicas or 0}/{hpa.spec.min_replicas or 1}-{hpa.spec.max_replicas}"
        metrics_display = ", ".join(metrics_str) or "none"
        lines.append(
            f"{hpa.metadata.namespace}/{hpa.metadata.name}  "
            f"Target={target}  "
            f"Replicas={replicas_str}  "
            f"Metrics=[{metrics_display}]  "
            f"Age={age(hpa.metadata.creation_timestamp)}"
        )
        rows.append(
            {
                "_gvr": "autoscaling~v2~horizontalpodautoscalers",
                "namespace": hpa.metadata.namespace,
                "name": hpa.metadata.name,
                "target": target,
                "replicas": replicas_str,
                "metrics": metrics_display,
                "age": age(hpa.metadata.creation_timestamp),
            }
        )
    text = "\n".join(lines) or "No HPAs found."
    component = (
        {
            "kind": "data_table",
            "title": f"HPAs ({len(rows)})",
            "columns": [
                {"id": "namespace", "header": "Namespace"},
                {"id": "name", "header": "Name"},
                {"id": "target", "header": "Target"},
                {"id": "replicas", "header": "Replicas (cur/min-max)"},
                {"id": "metrics", "header": "Metrics"},
                {"id": "age", "header": "Age"},
            ],
            "rows": rows,
        }
        if rows
        else None
    )
    return (text, component)


@beta_tool
def list_operator_subscriptions(namespace: str = "ALL") -> str:
    """List OLM Operator Subscriptions showing installed operators, their channels, and install plans. OpenShift only.

    Args:
        namespace: Namespace to check. Use 'ALL' for all namespaces.
    """
    try:
        if namespace.upper() == "ALL":
            result = get_custom_client().list_cluster_custom_object("operators.coreos.com", "v1alpha1", "subscriptions")
        else:
            result = get_custom_client().list_namespaced_custom_object(
                "operators.coreos.com", "v1alpha1", namespace, "subscriptions"
            )
    except ApiException as e:
        return f"Error ({e.status}): {e.reason}. OLM may not be installed."

    lines = []
    for sub in result.get("items", [])[:MAX_RESULTS]:
        meta = sub["metadata"]
        spec = sub.get("spec", {})
        status = sub.get("status", {})

        pkg = spec.get("name", "?")
        channel = spec.get("channel", "?")
        source = spec.get("source", "?")
        csv = status.get("installedCSV", "not installed")
        state = status.get("state", "Unknown")

        conditions = status.get("conditions", [])
        health = "OK"
        for c in conditions:
            if c.get("type") == "CatalogSourcesUnhealthy" and c.get("status") == "True":
                health = "CatalogUnhealthy"

        lines.append(
            f"{meta.get('namespace', '?')}/{meta['name']}  "
            f"Package={pkg}  Channel={channel}  Source={source}  "
            f"CSV={csv}  State={state}  Health={health}"
        )
    return "\n".join(lines) or "No Operator Subscriptions found."


@beta_tool
def get_firing_alerts() -> str:
    """Get all currently firing alerts from Alertmanager. Returns alert name, severity, namespace, summary, and duration."""

    # Try OpenShift alertmanager proxy first
    # Alertmanager URLs to try
    # "https://localhost:9093/api/v2/alerts"
    # "http://alertmanager-main.openshift-monitoring.svc:9093/api/v2/alerts"

    core = get_core_client()
    # Try to use the service proxy
    try:
        result = core.connect_get_namespaced_service_proxy_with_path(
            "alertmanager-main:web",
            "openshift-monitoring",
            path="api/v2/alerts",
            _preload_content=False,
        )
        data = json.loads(result.data)
    except Exception:
        # Fallback: try via custom API
        try:
            result = get_custom_client().get_cluster_custom_object(
                "monitoring.coreos.com", "v1", "alertmanagers", "main"
            )
            return "Alertmanager found but cannot query alerts via this method. Configure ALERTMANAGER_URL."
        except Exception:
            return "Cannot reach Alertmanager. It may not be installed or accessible."

    if not isinstance(data, list):
        return "Unexpected response format from Alertmanager."

    firing = [a for a in data if a.get("status", {}).get("state") == "active"]
    if not firing:
        return "No alerts currently firing."

    lines = []
    items = []
    _severity_to_status = {"critical": "error", "warning": "warning", "info": "info"}
    for alert in sorted(firing, key=lambda a: a.get("labels", {}).get("severity", ""), reverse=True):
        labels = alert.get("labels", {})
        annotations = alert.get("annotations", {})
        name = labels.get("alertname", "unknown")
        severity = labels.get("severity", "?")
        ns = labels.get("namespace", "cluster-wide")
        summary = annotations.get("summary", annotations.get("message", annotations.get("description", "")))[:200]
        starts = alert.get("startsAt", "?")[:19]

        lines.append(f"[{severity.upper()}] {name}  namespace={ns}  since={starts}\n  {summary}")
        items.append(
            {
                "name": f"[{severity.upper()}] {name}",
                "status": _severity_to_status.get(severity.lower(), "warning"),
                "detail": f"{ns} — {summary[:100]}" if summary else ns,
            }
        )

    text = f"Firing alerts ({len(firing)}):\n\n" + "\n\n".join(lines)
    component = (
        {
            "kind": "status_list",
            "title": f"Firing Alerts ({len(items)})",
            "items": items,
        }
        if items
        else None
    )
    return (text, component)


# In-memory cache for metric names (TTL 5 minutes)
_metric_names_cache: dict = {"data": None, "ts": 0}

_CATEGORY_PREFIXES: dict[str, list[str]] = {
    "cpu": ["container_cpu_", "node_cpu_", "process_cpu_", "pod:container_cpu_"],
    "memory": ["container_memory_", "node_memory_", "machine_memory_"],
    "network": ["container_network_", "node_network_"],
    "storage": ["node_filesystem_", "kubelet_volume_", "container_fs_"],
    "pods": ["kube_pod_", "kube_running_pod_", "kubelet_running_"],
    "nodes": ["kube_node_", "machine_", "node_"],
    "api_server": ["apiserver_"],
    "etcd": ["etcd_"],
    "alerts": ["ALERTS"],
}


@beta_tool
def discover_metrics(category: str = "all") -> str:
    """Discover available Prometheus metrics on this cluster. Call this BEFORE
    writing PromQL queries to know which metrics actually exist.

    Args:
        category: One of: 'cpu', 'memory', 'network', 'storage', 'pods',
                  'nodes', 'api_server', 'etcd', 'alerts', 'all'.
    """
    import os
    import ssl
    import time as _time
    import urllib.request

    from .promql_recipes import RECIPES, get_recipe

    valid_cats = set(_CATEGORY_PREFIXES.keys()) | {"all"}
    if category not in valid_cats:
        return f"Invalid category '{category}'. Available categories: {', '.join(sorted(valid_cats))}"

    now = _time.time()
    if _metric_names_cache["data"] is not None and now - _metric_names_cache["ts"] < 300:
        all_metrics = _metric_names_cache["data"]
    else:
        base_url = os.environ.get(
            "THANOS_URL",
            "https://thanos-querier.openshift-monitoring.svc:9091",
        )
        url = f"{base_url}/api/v1/label/__name__/values"

        try:
            token = ""
            try:
                with open("/var/run/secrets/kubernetes.io/serviceaccount/token") as f:
                    token = f.read().strip()
            except FileNotFoundError:
                pass

            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            headers = {}
            if token:
                headers["Authorization"] = f"Bearer {token}"

            req = urllib.request.Request(url, headers=headers)
            resp = urllib.request.urlopen(req, context=ctx, timeout=15)
            data = json.loads(resp.read())

            if data.get("status") != "success":
                return f"Prometheus error: {data.get('error', 'unknown')}"

            all_metrics = sorted(data.get("data", []))
            _metric_names_cache["data"] = all_metrics
            _metric_names_cache["ts"] = now

        except Exception as e:
            lines = [f"Cannot reach Prometheus ({e}). Using hardcoded recipes:"]
            cat_recipes = RECIPES.get(category, []) if category != "all" else [r for rs in RECIPES.values() for r in rs]
            for r in cat_recipes[:15]:
                lines.append(f"  {r.metric}")
                lines.append(f"    Recipe: {r.query}")
                lines.append(f'    Chart: {r.chart_type} | Title: "{r.name}"')
            return "\n".join(lines)

    if category == "all":
        filtered = all_metrics
    else:
        prefixes = _CATEGORY_PREFIXES[category]
        filtered = [m for m in all_metrics if any(m.startswith(p) or m.startswith(p.rstrip("_")) for p in prefixes)]

    if not filtered:
        return f"No metrics found for category '{category}' (0 of {len(all_metrics)} total metrics matched)."

    lines = [f"Available {category} metrics ({len(filtered)} found):"]
    for metric_name in filtered[:30]:
        recipe = get_recipe(metric_name)
        lines.append(f"  {metric_name}")
        if recipe:
            lines.append(f"    Recipe: {recipe.query}")
            lines.append(f'    Chart: {recipe.chart_type} | Title: "{recipe.name}"')

    if len(filtered) > 30:
        lines.append(f"  ... and {len(filtered) - 30} more")

    return "\n".join(lines)


@beta_tool
def verify_query(query: str) -> str:
    """Test a PromQL query against Prometheus to verify it returns data.
    Call this BEFORE using a query in a dashboard to ensure it works.

    Args:
        query: PromQL query to test.
    """
    import os
    import ssl
    import urllib.parse
    import urllib.request

    if not query or not query.strip():
        return "Error: query is empty."

    if any(c in query for c in [";", "\\", "\n", "\r"]):
        return "Error: Invalid characters in query."

    base_url = os.environ.get(
        "THANOS_URL",
        "https://thanos-querier.openshift-monitoring.svc:9091",
    )
    params = urllib.parse.urlencode({"query": query})
    url = f"{base_url}/api/v1/query?{params}"

    try:
        token = ""
        try:
            with open("/var/run/secrets/kubernetes.io/serviceaccount/token") as f:
                token = f.read().strip()
        except FileNotFoundError:
            pass

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        req = urllib.request.Request(url, headers=headers)
        resp = urllib.request.urlopen(req, context=ctx, timeout=15)
        data = json.loads(resp.read())
    except Exception as e:
        return f"FAIL_UNREACHABLE: Cannot reach Prometheus at {base_url}: {e}"

    if data.get("status") != "success":
        error_msg = data.get("error", "unknown error")
        try:
            from .promql_recipes import record_query_result

            record_query_result(query, success=False, series_count=0)
        except Exception:
            pass
        return f"FAIL_SYNTAX: {error_msg}"

    results = data.get("data", {}).get("result", [])

    if not results:
        try:
            from .promql_recipes import record_query_result

            record_query_result(query, success=False, series_count=0)
        except Exception:
            pass
        return "FAIL_NO_DATA: Query returned 0 results. Metric may not exist or labels may be wrong."

    sample = results[0]
    metric_name = sample.get("metric", {}).get("__name__", "")
    value = sample.get("value", [None, ""])[1] if sample.get("value") else ""
    sample_info = f"{metric_name}={value}" if metric_name else f"value={value}"

    try:
        from .promql_recipes import record_query_result

        record_query_result(query, success=True, series_count=len(results))
    except Exception:
        pass

    return f"PASS: Query returns data ({len(results)} series, sample: {sample_info})"


@beta_tool
def get_prometheus_query(query: str, time_range: str = "1h") -> str:
    """Execute a PromQL query against Prometheus/Thanos and return the results as an interactive chart.

    Args:
        query: PromQL query string, e.g. 'up', 'node_memory_MemAvailable_bytes', 'rate(container_cpu_usage_seconds_total[5m])'.
        time_range: Time range for the query (e.g. '5m', '1h', '24h'). Defaults to '1h'. Use '' for instant queries (returns a table instead of chart).
    """
    import os
    import urllib.error
    import urllib.parse
    import urllib.request

    # Sanitize query
    if any(c in query for c in [";", "\\", "\n", "\r"]):
        return "Error: Invalid characters in query."

    # Default to range query — instant queries produce tables, not charts
    if not time_range:
        time_range = "1h"

    base_url = os.environ.get("THANOS_URL", "")
    if not base_url:
        # Try OpenShift Thanos
        base_url = "https://thanos-querier.openshift-monitoring.svc:9091"

    if time_range:
        # Range query — convert relative time to unix timestamps
        import time as _time

        _UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        try:
            unit = time_range[-1]
            amount = int(time_range[:-1])
            seconds = amount * _UNITS.get(unit, 3600)
        except (ValueError, IndexError):
            seconds = 3600  # default 1h

        now = int(_time.time())
        # Auto-adjust step based on range to keep ~60-120 data points
        step = max(60, seconds // 120)
        params = urllib.parse.urlencode(
            {
                "query": query,
                "start": str(now - seconds),
                "end": str(now),
                "step": str(step),
            }
        )
    else:
        # Instant query
        params = urllib.parse.urlencode({"query": query})

    # Connect to Thanos/Prometheus using direct HTTPS with SA bearer token
    import ssl

    endpoint = f"api/v1/query?{params}" if not time_range else f"api/v1/query_range?{params}"
    thanos_url = f"{base_url}/{endpoint}"

    try:
        # Read service account token for in-cluster auth
        token = ""
        try:
            with open("/var/run/secrets/kubernetes.io/serviceaccount/token") as f:
                token = f.read().strip()
        except FileNotFoundError:
            pass

        # Skip TLS verification for in-cluster Thanos (uses service-serving CA
        # which differs from the SA CA cert). This is safe — it's internal traffic.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        req = urllib.request.Request(thanos_url, headers=headers)
        resp = urllib.request.urlopen(req, context=ctx, timeout=15)
        data = json.loads(resp.read())
    except Exception as e:
        return f"Cannot reach Prometheus/Thanos at {base_url}: {e}"

    if data.get("status") != "success":
        try:
            from .promql_recipes import record_query_result

            record_query_result(query, success=False, series_count=0)
        except Exception:
            pass
        return f"Query error: {data.get('error', 'unknown')}"

    result_type = data.get("data", {}).get("resultType", "")
    results = data.get("data", {}).get("result", [])

    if not results:
        try:
            from .promql_recipes import record_query_result

            record_query_result(query, success=False, series_count=0)
        except Exception:
            pass
        # Suggest verified recipe alternatives
        try:
            from .promql_recipes import _detect_category, get_recipes_for_category

            cat = _detect_category(query)
            if cat:
                alternatives = get_recipes_for_category(cat)[:3]
                if alternatives:
                    alt_text = "\n".join(f"  - {r.name}: {r.query}" for r in alternatives)
                    return (
                        f"Query returned no results for: {query}\n\n"
                        f"Try these verified alternatives for '{cat}':\n{alt_text}"
                    )
        except Exception:
            pass
        return f"Query returned no results for: {query}"

    # Default color palette for chart series
    _CHART_COLORS = ["#60a5fa", "#34d399", "#fbbf24", "#f87171", "#a78bfa", "#38bdf8", "#fb923c", "#e879f9"]

    # Generate a human-readable title from PromQL
    def _title_from_query(q: str) -> str:
        q = q.strip()
        # Extract the metric name and grouping
        import re as _re

        # Common patterns → friendly names
        if "cpu_usage_seconds_total" in q:
            group = _re.search(r"by\s*\(([^)]+)\)", q)
            by = group.group(1) if group else ""
            ns = _re.search(r'namespace="([^"]+)"', q)
            prefix = f"{ns.group(1)} — " if ns else ""
            return f"{prefix}CPU Usage" + (f" by {by}" if by else "")
        if "memory" in q.lower():
            group = _re.search(r"by\s*\(([^)]+)\)", q)
            by = group.group(1) if group else ""
            return "Memory Usage" + (f" by {by}" if by else "")
        if "node_cpu_seconds_total" in q:
            return "Node CPU Utilization"
        if "node_memory" in q:
            return "Node Memory Pressure"
        if "network_receive" in q:
            group = _re.search(r"by\s*\(([^)]+)\)", q)
            by = group.group(1) if group else ""
            return "Network Receive" + (f" by {by}" if by else "")
        if "network_transmit" in q:
            group = _re.search(r"by\s*\(([^)]+)\)", q)
            by = group.group(1) if group else ""
            return "Network Transmit" + (f" by {by}" if by else "")
        if "restart" in q.lower():
            return "Pod Restarts"
        if "ALERTS" in q:
            return "Firing Alerts"
        if "kube_event" in q:
            return "Warning Events"
        if "filesystem" in q or "volume_stats" in q:
            return "Disk Usage"
        if "predict_linear" in q:
            return "Capacity Projection"
        # Fallback: use the metric name, cleaned up
        metric = _re.search(r"([a-z_]+)\{", q) or _re.search(r"([a-z_]+)\(", q) or _re.search(r"^([a-z_]+)", q)
        return metric.group(1).replace("_", " ").title() if metric else q[:60]

    def _desc_from_query(q: str, tr: str, count: int) -> str:
        """Generate a useful description explaining why this data matters."""
        if "cpu_usage_seconds_total" in q:
            return "Identifies which workloads consume the most CPU — helps optimize resource requests and spot runaway processes"
        if "memory" in q.lower():
            return (
                "Shows actual memory consumption — useful for right-sizing resource limits and detecting memory leaks"
            )
        if "node_cpu_seconds_total" in q:
            return "Tracks node-level CPU saturation — high utilization may require scaling or workload rebalancing"
        if "node_memory" in q.lower():
            return "Monitors available memory per node — low availability risks OOM kills and pod evictions"
        if "up" in q and "up{" not in q:
            return "Service availability — 1 means up, 0 means the target is down or unreachable"
        if "network_receive" in q:
            return "Inbound network traffic — spikes may indicate unexpected load or DDoS"
        if "network_transmit" in q:
            return "Outbound network traffic — useful for identifying high-bandwidth workloads"
        if "restart" in q.lower():
            return "Container restarts over time — sustained restarts indicate crashlooping or resource issues"
        if "ALERTS" in q:
            return "Firing alert count over time — rising trend indicates degrading cluster health"
        if "filesystem" in q or "volume_stats" in q:
            return "Storage utilization — watch for volumes approaching capacity"
        if "predict_linear" in q:
            return "Linear projection based on recent trends — shows estimated future values"
        return f"{'Time series' if tr else 'Snapshot'} with {count} {'series' if tr else 'results'}"

    def _pick_chart_type(q: str, chart_series: list, raw_results: list) -> str:
        """Pick the best chart type based on query pattern and data shape."""
        q_lower = q.lower()
        num_series = len(chart_series)

        # Stacked area: "sum by" queries showing namespace/pod breakdown
        if "sum by" in q_lower and num_series >= 3:
            if "cpu" in q_lower or "memory" in q_lower or "network" in q_lower:
                return "stacked_area"

        # Bar chart: topk queries, comparison across items
        if "topk" in q_lower or num_series >= 5:
            # Check if values are relatively flat (comparison, not trend)
            if chart_series:
                data = chart_series[0].get("data", [])
                if len(data) <= 5:
                    return "bar"

        # Area chart: single series utilization/percentage metrics
        if num_series == 1:
            if any(w in q_lower for w in ("percent", "ratio", "utilization", "usage", "100 -")):
                return "area"

        # Stacked bar: count/sum by category (e.g., pod status, alert severity)
        if "count" in q_lower and "by" in q_lower and num_series <= 5:
            return "stacked_bar"

        # Default: line chart for time-series trends
        return "line"

    lines = []
    if result_type == "matrix":
        # Range query → build a ChartSpec
        series = []
        for i, r in enumerate(results[:10]):
            metric = r.get("metric", {})
            label_parts = [f"{v}" for k, v in metric.items() if k != "__name__"]
            label = ", ".join(label_parts) or metric.get("__name__", f"series-{i}")
            values = r.get("values", [])
            import math

            data = [[int(float(ts) * 1000), float(val)] for ts, val in values if not math.isnan(float(val))]
            latest = values[-1][1] if values else "?"
            lines.append(f"{label} = {latest} (latest of {len(values)} samples)")
            series.append({"label": label[:60], "data": data, "color": _CHART_COLORS[i % len(_CHART_COLORS)]})

        if len(results) > 10:
            lines.append(f"... and {len(results) - 10} more series (truncated to top 10 for chart)")

        text = "\n".join(lines)

        # Pick chart type based on data shape and query pattern
        chart_type = _pick_chart_type(query, series, results)

        component = {
            "kind": "chart",
            "chartType": chart_type,
            "title": _title_from_query(query),
            "description": _desc_from_query(query, time_range, len(series)),
            "series": series,
            "yAxisLabel": "",
            "height": 300,
            "query": query,
            "timeRange": time_range,
        }
        try:
            from .promql_recipes import record_query_result

            record_query_result(query, success=True, series_count=len(series))
        except Exception:
            pass
        return (text, component)

    else:
        # Instant query (vector) → build a DataTableSpec
        # Detect label keys from first result to build dynamic columns
        rows = []
        label_keys = []
        if results:
            first_metric = results[0].get("metric", {})
            label_keys = [k for k in first_metric if k != "__name__"]

        for r in results[:50]:
            metric = r.get("metric", {})
            label_str = ", ".join(f"{k}={v}" for k, v in metric.items() if k != "__name__")
            name = metric.get("__name__", "")
            _ts, val = r.get("value", [0, "?"])
            lines.append(f"{name}{{{label_str}}} = {val}" if label_str else f"{name} = {val}")

            row: dict = {}
            if label_keys:
                for k in label_keys:
                    row[k] = metric.get(k, "")
            else:
                row["metric"] = name or query
            row["value"] = str(val)
            rows.append(row)

        if len(results) > 50:
            lines.append(f"... and {len(results) - 50} more results (truncated)")

        text = "\n".join(lines)

        # Build columns from label keys
        if label_keys:
            columns = [{"id": k, "header": k.replace("_", " ").title()} for k in label_keys]
        else:
            columns = [{"id": "metric", "header": "Metric"}]
        columns.append({"id": "value", "header": "Value"})

        component = (
            {
                "kind": "data_table",
                "title": _title_from_query(query),
                "description": _desc_from_query(query, "", len(rows)),
                "columns": columns,
                "rows": rows,
                "query": query,
            }
            if rows
            else None
        )
        try:
            from .promql_recipes import record_query_result

            record_query_result(query, success=True, series_count=len(rows))
        except Exception:
            pass
        return (text, component)


# ---------------------------------------------------------------------------
# Additional diagnostic tools (requested by sysadmin review)
# ---------------------------------------------------------------------------


@beta_tool
def describe_service(namespace: str, name: str) -> str:
    """Get detailed information about a service including endpoints, ports, selector, and target pods.

    Args:
        namespace: Kubernetes namespace.
        name: Name of the service.
    """
    core = get_core_client()
    result = safe(lambda: core.read_namespaced_service(name, namespace))
    if isinstance(result, ToolError):
        return str(result)

    svc = result
    info = {
        "name": svc.metadata.name,
        "namespace": svc.metadata.namespace,
        "type": svc.spec.type,
        "clusterIP": svc.spec.cluster_ip,
        "selector": svc.spec.selector or {},
        "ports": [
            {
                "name": p.name,
                "port": p.port,
                "targetPort": str(p.target_port),
                "protocol": p.protocol,
                "nodePort": p.node_port,
            }
            for p in (svc.spec.ports or [])
        ],
        "externalIPs": svc.spec.external_i_ps or [],
        "sessionAffinity": svc.spec.session_affinity,
    }

    # Get endpoints
    ep_result = safe(lambda: core.read_namespaced_endpoints(name, namespace))
    if not isinstance(ep_result, ToolError):
        endpoints = []
        for subset in ep_result.subsets or []:
            addrs = [a.ip + (f" ({a.target_ref.name})" if a.target_ref else "") for a in (subset.addresses or [])]
            not_ready = [
                a.ip + (f" ({a.target_ref.name})" if a.target_ref else "") for a in (subset.not_ready_addresses or [])
            ]
            ports = [f"{p.port}/{p.protocol}" for p in (subset.ports or [])]
            endpoints.append({"ready": addrs, "notReady": not_ready, "ports": ports})
        info["endpoints"] = endpoints

    # Count matching pods
    if svc.spec.selector:
        label_sel = ",".join(f"{k}={v}" for k, v in svc.spec.selector.items())
        pods = safe(lambda: core.list_namespaced_pod(namespace, label_selector=label_sel))
        if not isinstance(pods, ToolError):
            info["matchingPods"] = len(pods.items)
            info["readyPods"] = sum(1 for p in pods.items if p.status.phase == "Running")

    return json.dumps(info, indent=2, default=str)


@beta_tool
def get_endpoint_slices(namespace: str, service_name: str) -> str:
    """Get EndpointSlices for a service showing which pods are backing it and their readiness.

    Args:
        namespace: Kubernetes namespace.
        service_name: Name of the service to inspect.
    """
    try:
        result = get_custom_client().list_namespaced_custom_object(
            "discovery.k8s.io",
            "v1",
            namespace,
            "endpointslices",
            label_selector=f"kubernetes.io/service-name={service_name}",
        )
    except ApiException as e:
        return f"Error ({e.status}): {e.reason}"

    slices = result.get("items", [])
    if not slices:
        return f"No EndpointSlices found for service '{service_name}' in namespace '{namespace}'."

    lines = []
    for es in slices:
        name = es["metadata"]["name"]
        addr_type = es.get("addressType", "?")
        ports = ", ".join(f"{p.get('name', '?')}:{p['port']}/{p.get('protocol', 'TCP')}" for p in es.get("ports", []))
        lines.append(f"EndpointSlice: {name}  Type={addr_type}  Ports=[{ports}]")

        for ep in es.get("endpoints", []):
            ready = ep.get("conditions", {}).get("ready", False)
            addresses = ", ".join(ep.get("addresses", []))
            target = ep.get("targetRef", {})
            pod_name = target.get("name", "?") if target else "?"
            status = "Ready" if ready else "NotReady"
            lines.append(f"  {status}  {addresses}  Pod={pod_name}")

    return "\n".join(lines)


@beta_tool
def list_replicasets(namespace: str, deployment_name: str = "") -> str:
    """List ReplicaSets, optionally filtered to a specific deployment's rollout history.

    Args:
        namespace: Kubernetes namespace.
        deployment_name: If provided, show only ReplicaSets owned by this deployment (rollout history).
    """
    apps = get_apps_client()
    result = safe(lambda: apps.list_namespaced_replica_set(namespace))
    if isinstance(result, ToolError):
        return str(result)

    rsets = result.items
    if deployment_name:
        rsets = [
            rs
            for rs in rsets
            if any(
                ref.kind == "Deployment" and ref.name == deployment_name for ref in (rs.metadata.owner_references or [])
            )
        ]

    if not rsets:
        return f"No ReplicaSets found{' for deployment ' + deployment_name if deployment_name else ''}."

    # Sort by creation (newest first) for rollout history
    rsets.sort(key=lambda rs: rs.metadata.creation_timestamp or datetime.min.replace(tzinfo=UTC), reverse=True)

    lines = []
    for rs in rsets[:20]:
        s = rs.status
        revision = (rs.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "?")
        image = rs.spec.template.spec.containers[0].image if rs.spec.template.spec.containers else "?"
        lines.append(
            f"Revision={revision}  {rs.metadata.name}  "
            f"Replicas={s.ready_replicas or 0}/{s.replicas or 0}  "
            f"Image={image.split('/')[-1]}  "
            f"Age={age(rs.metadata.creation_timestamp)}"
        )
    return "\n".join(lines)


@beta_tool
def get_pod_disruption_budgets(namespace: str = "ALL") -> str:
    """List PodDisruptionBudgets showing min available, max unavailable, disruptions allowed, and current healthy pods.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' for all namespaces.
    """
    try:
        from kubernetes.client import PolicyV1Api

        policy = PolicyV1Api()
    except ImportError:
        return "Error: PolicyV1Api not available in this kubernetes client version."

    from .k8s_client import _load_k8s

    _load_k8s()

    if namespace.upper() == "ALL":
        result = safe(lambda: policy.list_pod_disruption_budget_for_all_namespaces())
    else:
        result = safe(lambda: policy.list_namespaced_pod_disruption_budget(namespace))
    if isinstance(result, ToolError):
        return str(result)

    if not result.items:
        return "No PodDisruptionBudgets found."

    lines = []
    for pdb in result.items:
        s = pdb.status
        spec = pdb.spec
        min_avail = spec.min_available if spec.min_available is not None else "N/A"
        max_unavail = spec.max_unavailable if spec.max_unavailable is not None else "N/A"
        selector = spec.selector.match_labels if spec.selector and spec.selector.match_labels else {}

        lines.append(
            f"{pdb.metadata.namespace}/{pdb.metadata.name}  "
            f"MinAvailable={min_avail}  MaxUnavailable={max_unavail}  "
            f"Allowed={s.disruptions_allowed or 0}  "
            f"Current={s.current_healthy or 0}/{s.expected_pods or 0}  "
            f"Selector={selector}"
        )
    return "\n".join(lines)


@beta_tool
def list_limit_ranges(namespace: str = "default") -> str:
    """List LimitRanges in a namespace showing default requests/limits for containers.

    Args:
        namespace: Kubernetes namespace.
    """
    result = safe(lambda: get_core_client().list_namespaced_limit_range(namespace))
    if isinstance(result, ToolError):
        return str(result)

    if not result.items:
        return f"No LimitRanges defined in namespace '{namespace}'."

    lines = []
    for lr in result.items:
        lines.append(f"LimitRange: {lr.metadata.name}")
        for limit in lr.spec.limits or []:
            lines.append(f"  Type={limit.type}")
            if limit.default:
                lines.append(f"    Default limits: {dict(limit.default)}")
            if limit.default_request:
                lines.append(f"    Default requests: {dict(limit.default_request)}")
            if limit.max:
                lines.append(f"    Max: {dict(limit.max)}")
            if limit.min:
                lines.append(f"    Min: {dict(limit.min)}")
    return "\n".join(lines)


@beta_tool
def top_pods_by_restarts(namespace: str = "ALL", limit: int = 20) -> str:
    """Show pods sorted by restart count (highest first). The fastest way to find troubled workloads.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' for all namespaces.
        limit: Maximum number of pods to return (default 20).
    """
    core = get_core_client()
    if namespace.upper() == "ALL":
        result = safe(lambda: core.list_pod_for_all_namespaces())
    else:
        result = safe(lambda: core.list_namespaced_pod(namespace))
    if isinstance(result, ToolError):
        return str(result)

    pods_with_restarts = []
    for pod in result.items:
        restarts = sum(
            (cs.restart_count for cs in (pod.status.container_statuses or [])),
            0,
        )
        if restarts > 0:
            pods_with_restarts.append((restarts, pod))

    pods_with_restarts.sort(key=lambda x: x[0], reverse=True)

    if not pods_with_restarts:
        return "No pods with restarts found."

    lines = []
    rows = []
    for restarts, pod in pods_with_restarts[:limit]:
        lines.append(
            f"Restarts={restarts}  {pod.metadata.namespace}/{pod.metadata.name}  "
            f"Status={pod.status.phase}  Age={age(pod.metadata.creation_timestamp)}"
        )
        rows.append(
            {
                "restarts": restarts,
                "namespace": pod.metadata.namespace,
                "name": pod.metadata.name,
                "status": pod.status.phase or "Unknown",
                "age": age(pod.metadata.creation_timestamp),
            }
        )
    text = "\n".join(lines)
    component = (
        {
            "kind": "data_table",
            "title": f"Top Pods by Restarts ({len(rows)})",
            "columns": [
                {"id": "restarts", "header": "Restarts"},
                {"id": "namespace", "header": "Namespace"},
                {"id": "name", "header": "Name"},
                {"id": "status", "header": "Status"},
                {"id": "age", "header": "Age"},
            ],
            "rows": rows,
        }
        if rows
        else None
    )
    return (text, component)


@beta_tool
def get_recent_changes(namespace: str = "ALL", minutes: int = 60) -> str:
    """Show recent cluster changes: new/modified resources, deployments, scaling events, and config changes from the last N minutes.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' for cluster-wide.
        minutes: Look back period in minutes (default 60, max 1440).
    """
    minutes = min(max(1, minutes), 1440)
    core = get_core_client()
    apps = get_apps_client()

    cutoff = datetime.now(UTC).replace(microsecond=0)
    # cutoff_str used for event time comparison below
    _ = (cutoff - __import__("datetime").timedelta(minutes=minutes)).isoformat() + "Z"

    lines = []

    # Recent events (Warning and Normal)
    if namespace.upper() == "ALL":
        events_result = safe(lambda: core.list_event_for_all_namespaces())
    else:
        events_result = safe(lambda: core.list_namespaced_event(namespace))

    if not isinstance(events_result, ToolError):
        recent_events = [
            e
            for e in events_result.items
            if e.last_timestamp
            and e.last_timestamp.replace(tzinfo=UTC) >= cutoff - __import__("datetime").timedelta(minutes=minutes)
        ]
        # Group by reason
        reasons: dict[str, int] = {}
        for e in recent_events:
            reasons[e.reason or "Unknown"] = reasons.get(e.reason or "Unknown", 0) + 1

        if reasons:
            lines.append(f"Events in last {minutes}m ({len(recent_events)} total):")
            for reason, count in sorted(reasons.items(), key=lambda x: -x[1])[:15]:
                lines.append(f"  {reason}: {count}")

        # Highlight warning events
        warnings = [e for e in recent_events if e.type == "Warning"]
        if warnings:
            lines.append(f"\nWarning events ({len(warnings)}):")
            for e in warnings[:10]:
                lines.append(
                    f"  {age(e.last_timestamp)} ago  {e.reason}  "
                    f"{e.involved_object.kind}/{e.involved_object.name}  {e.message}"
                )

    # Recent deployments that changed
    if namespace.upper() == "ALL":
        deps_result = safe(lambda: apps.list_deployment_for_all_namespaces())
    else:
        deps_result = safe(lambda: apps.list_namespaced_deployment(namespace))

    if not isinstance(deps_result, ToolError):
        recently_updated = []
        for dep in deps_result.items:
            for cond in dep.status.conditions or []:
                if cond.type == "Progressing" and cond.last_update_time:
                    if cond.last_update_time.replace(tzinfo=UTC) >= cutoff - __import__("datetime").timedelta(
                        minutes=minutes
                    ):
                        recently_updated.append(dep)
                        break

        if recently_updated:
            lines.append(f"\nDeployments updated in last {minutes}m ({len(recently_updated)}):")
            for dep in recently_updated[:10]:
                s = dep.status
                lines.append(
                    f"  {dep.metadata.namespace}/{dep.metadata.name}  Ready={s.ready_replicas or 0}/{s.replicas or 0}"
                )

    if not lines:
        return f"No significant changes in the last {minutes} minutes."

    return "\n".join(lines)


@beta_tool
def get_tls_certificates(namespace: str = "ALL") -> str:
    """List TLS secrets and their certificate expiry dates. Helps identify certificates approaching expiry.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' for all namespaces.
    """
    import base64

    from cryptography import x509

    core = get_core_client()
    if namespace.upper() == "ALL":
        result = safe(
            lambda: core.list_secret_for_all_namespaces(
                field_selector="type=kubernetes.io/tls",
                limit=MAX_RESULTS,
            )
        )
    else:
        result = safe(
            lambda: core.list_namespaced_secret(
                namespace,
                field_selector="type=kubernetes.io/tls",
                limit=MAX_RESULTS,
            )
        )
    if isinstance(result, ToolError):
        return str(result)

    if not result.items:
        return "No TLS secrets found."

    now = datetime.now(UTC)
    certs = []

    for secret in result.items:
        cert_data = (secret.data or {}).get("tls.crt", "")
        if not cert_data:
            continue

        try:
            pem_bytes = base64.b64decode(cert_data)
            cert = x509.load_pem_x509_certificate(pem_bytes)
            not_after = cert.not_valid_after_utc
            days_left = (not_after - now).days

            # Extract CN from subject
            cn = "unknown"
            try:
                cn_attrs = cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)
                if cn_attrs:
                    cn = cn_attrs[0].value
            except Exception:
                pass

            status = "OK" if days_left > 30 else "EXPIRING" if days_left > 0 else "EXPIRED"
            certs.append(
                {
                    "namespace": secret.metadata.namespace,
                    "name": secret.metadata.name,
                    "cn": cn,
                    "expires": not_after.strftime("%Y-%m-%d"),
                    "days_left": days_left,
                    "status": status,
                }
            )
        except Exception:
            certs.append(
                {
                    "namespace": secret.metadata.namespace,
                    "name": secret.metadata.name,
                    "cn": "parse-error",
                    "expires": "unknown",
                    "days_left": -1,
                    "status": "UNKNOWN",
                }
            )

    # Sort by days_left (most urgent first)
    certs.sort(key=lambda c: c["days_left"])

    lines = [f"TLS Certificates ({len(certs)}):"]
    lines.append(f"  {'NAMESPACE':<20} {'NAME':<30} {'CN':<25} {'EXPIRES':<12} {'DAYS':>5}  STATUS")
    for c in certs:
        lines.append(
            f"  {c['namespace']:<20} {c['name']:<30} {c['cn'][:24]:<25} "
            f"{c['expires']:<12} {c['days_left']:>5}  {c['status']}"
        )

    expiring = [c for c in certs if c["status"] in ("EXPIRING", "EXPIRED")]
    if expiring:
        lines.append(f"\n⚠️  {len(expiring)} certificate(s) need attention!")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Write tools — operations, apply YAML, network policies
# ---------------------------------------------------------------------------


@beta_tool
def rollback_deployment(namespace: str, name: str, revision: int = 0) -> str:
    """Rollback a deployment to a previous revision. If revision is 0, rolls back to the previous revision. REQUIRES USER CONFIRMATION.

    Args:
        namespace: Kubernetes namespace.
        name: Name of the deployment to rollback.
        revision: Target revision number (0 = previous revision).
    """
    if err := _validate_k8s_namespace(namespace):
        return err
    if err := _validate_k8s_name(name):
        return err
    # Get current ReplicaSets to find the target revision
    apps = get_apps_client()
    rs_result = safe(lambda: apps.list_namespaced_replica_set(namespace))
    if isinstance(rs_result, ToolError):
        return str(rs_result)

    # Find ReplicaSets owned by this deployment
    owned = [
        rs
        for rs in rs_result.items
        if any(ref.kind == "Deployment" and ref.name == name for ref in (rs.metadata.owner_references or []))
    ]

    if not owned:
        return f"No ReplicaSets found for deployment {namespace}/{name}."

    # Sort by revision
    owned.sort(
        key=lambda rs: int((rs.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "0")),
        reverse=True,
    )

    if revision == 0:
        # Rollback to previous (second-newest)
        if len(owned) < 2:
            return f"No previous revision available for {namespace}/{name}."
        target = owned[1]
    else:
        target = next(
            (
                rs
                for rs in owned
                if (rs.metadata.annotations or {}).get("deployment.kubernetes.io/revision") == str(revision)
            ),
            None,
        )
        if not target:
            available = [(rs.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "?") for rs in owned]
            return f"Revision {revision} not found. Available: {', '.join(available)}"

    # Get the target's pod template and patch the deployment
    target_template = target.spec.template
    target_rev = (target.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "?")
    target_image = target_template.spec.containers[0].image if target_template.spec.containers else "?"

    body = {"spec": {"template": target_template.to_dict()}}
    result = safe(lambda: apps.patch_namespaced_deployment(name, namespace, body))
    if isinstance(result, ToolError):
        return str(result)

    return f"Rolled back {namespace}/{name} to revision {target_rev} (image: {target_image.split('/')[-1]})."


@beta_tool
def drain_node(node_name: str) -> str:
    """Cordon a node and evict all pods (respecting PDBs). REQUIRES USER CONFIRMATION.

    This cordons the node first, then evicts pods one by one. Pods managed by
    DaemonSets are skipped. Pods with PodDisruptionBudgets are respected.

    Args:
        node_name: Name of the node to drain.
    """
    if err := _validate_k8s_name(node_name, "node_name"):
        return err
    core = get_core_client()

    # Step 1: Cordon
    cordon_result = safe(lambda: core.patch_node(node_name, body={"spec": {"unschedulable": True}}))
    if isinstance(cordon_result, ToolError):
        return f"Failed to cordon: {cordon_result}"

    # Step 2: List pods on the node
    pods_result = safe(lambda: core.list_pod_for_all_namespaces(field_selector=f"spec.nodeName={node_name}"))
    if isinstance(pods_result, ToolError):
        return f"Cordoned but failed to list pods: {pods_result}"

    evicted = 0
    skipped = 0
    failed = 0

    for pod in pods_result.items:
        ns = pod.metadata.namespace
        name = pod.metadata.name

        # Skip DaemonSet pods
        if any(ref.kind == "DaemonSet" for ref in (pod.metadata.owner_references or [])):
            skipped += 1
            continue

        # Skip mirror pods (static pods)
        if (pod.metadata.annotations or {}).get("kubernetes.io/config.mirror"):
            skipped += 1
            continue

        # Evict the pod
        eviction = client.V1Eviction(
            metadata=client.V1ObjectMeta(name=name, namespace=ns),
            delete_options=client.V1DeleteOptions(grace_period_seconds=30),
        )
        try:
            core.create_namespaced_pod_eviction(name, ns, eviction)
            evicted += 1
        except ApiException as e:
            if e.status == 429:
                failed += 1  # PDB would be violated
            else:
                failed += 1

    return (
        f"Node {node_name} drained. "
        f"Cordoned=true, Evicted={evicted}, Skipped={skipped} (DaemonSet/mirror), "
        f"Failed={failed} (PDB violations or errors)."
    )


# ---------------------------------------------------------------------------
# Write tools — apply YAML and create network policies
# ---------------------------------------------------------------------------


@beta_tool
def apply_yaml(yaml_content: str, namespace: str = "", dry_run: bool = True) -> str:
    """Apply a YAML manifest to the cluster. Runs server-side dry-run first by default. REQUIRES USER CONFIRMATION.

    Args:
        yaml_content: The YAML content to apply (single resource only).
        namespace: Override namespace (optional, uses the one in the YAML if not specified).
        dry_run: If True (default), only validate — don't actually apply. Set to False to apply for real.
    """
    import yaml as yaml_lib

    try:
        resource = yaml_lib.safe_load(yaml_content)
    except Exception as e:
        return f"Error parsing YAML: {e}"

    if not isinstance(resource, dict) or "apiVersion" not in resource or "kind" not in resource:
        return "Error: YAML must contain a single Kubernetes resource with apiVersion and kind."

    api_version = resource.get("apiVersion", "")
    kind = resource.get("kind", "")
    metadata = resource.get("metadata", {})
    name = metadata.get("name", "")
    ns = namespace or metadata.get("namespace", "default")

    if not name:
        return "Error: Resource must have metadata.name."

    if err := _validate_k8s_name(name):
        return err
    if err := _validate_k8s_namespace(ns):
        return err

    # Allowlist — only these resource types can be created/modified via apply_yaml.
    # Everything else is blocked to prevent privilege escalation.
    _ALLOWED_KINDS = {
        "Deployment",
        "StatefulSet",
        "DaemonSet",
        "Job",
        "CronJob",
        "Service",
        "ConfigMap",
        "Ingress",
        "NetworkPolicy",
        "HorizontalPodAutoscaler",
        "LimitRange",
        "ResourceQuota",
        "PersistentVolumeClaim",
    }
    if kind not in _ALLOWED_KINDS:
        return (
            f"Error: Creating/modifying {kind} resources is not allowed via apply_yaml. "
            f"Allowed kinds: {', '.join(sorted(_ALLOWED_KINDS))}"
        )

    # Check ArgoCD auto-sync — warn if changes will be reverted
    from .gitops_tools import check_argo_auto_sync

    argo_warning = check_argo_auto_sync(ns, kind, name)
    if argo_warning and not dry_run:
        return argo_warning

    # Build API path
    if "/" in api_version:
        _group, _version = api_version.split("/", 1)
        base = f"/apis/{api_version}"
    else:
        base = f"/api/{api_version}"

    # Simple kind→plural (covers common cases)
    plural_map = {
        "Deployment": "deployments",
        "Service": "services",
        "ConfigMap": "configmaps",
        "Secret": "secrets",
        "Namespace": "namespaces",
        "Pod": "pods",
        "ServiceAccount": "serviceaccounts",
        "Role": "roles",
        "RoleBinding": "rolebindings",
        "ClusterRole": "clusterroles",
        "ClusterRoleBinding": "clusterrolebindings",
        "NetworkPolicy": "networkpolicies",
        "Ingress": "ingresses",
        "Job": "jobs",
        "CronJob": "cronjobs",
        "StatefulSet": "statefulsets",
        "DaemonSet": "daemonsets",
        "PersistentVolumeClaim": "persistentvolumeclaims",
        "HorizontalPodAutoscaler": "horizontalpodautoscalers",
        "LimitRange": "limitranges",
        "ResourceQuota": "resourcequotas",
    }
    plural = plural_map.get(kind, kind.lower() + "s")

    # Use server-side apply
    from kubernetes import client as k8s_client

    api = k8s_client.ApiClient()

    try:
        # Try server-side apply (PATCH with application/apply-patch+yaml)
        path = f"{base}/namespaces/{ns}/{plural}/{name}" if ns and kind != "Namespace" else f"{base}/{plural}/{name}"
        resp = api.call_api(
            path,
            "PATCH",
            body=json.dumps(resource),
            header_params={
                "Content-Type": "application/apply-patch+yaml",
                "Accept": "application/json",
            },
            query_params=[("fieldManager", "pulse-agent")] + ([("dryRun", "All")] if dry_run else []),
            _preload_content=False,
        )
        json.loads(resp[0].data)  # validate response is valid JSON
        action = "Dry-run validated" if dry_run else "Applied"
        return f"{action} {kind}/{name} in namespace {ns} successfully."
    except ApiException as e:
        return f"Error ({e.status}): {e.reason}\n{e.body}"
    except Exception as e:
        return f"Error applying YAML: {type(e).__name__}: {e}"


@beta_tool
def create_network_policy(
    namespace: str,
    name: str = "default-deny-ingress",
    policy_type: str = "deny-all-ingress",
) -> str:
    """Create a network policy in a namespace. REQUIRES USER CONFIRMATION.

    Args:
        namespace: Target namespace for the network policy.
        name: Name of the NetworkPolicy resource.
        policy_type: Policy template: 'deny-all-ingress' (default), 'deny-all-egress', or 'deny-all'.
    """
    if policy_type == "deny-all-ingress":
        spec = {"podSelector": {}, "policyTypes": ["Ingress"]}
    elif policy_type == "deny-all-egress":
        spec = {"podSelector": {}, "policyTypes": ["Egress"]}
    elif policy_type == "deny-all":
        spec = {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}
    else:
        return f"Unknown policy type: {policy_type}. Use 'deny-all-ingress', 'deny-all-egress', or 'deny-all'."

    body = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": name, "namespace": namespace},
        "spec": spec,
    }

    # Dry-run first to validate
    dry_result = safe(lambda: get_networking_client().create_namespaced_network_policy(namespace, body, dry_run="All"))
    if isinstance(dry_result, ToolError):
        return f"Dry-run failed: {dry_result}"

    # Apply for real
    result = safe(lambda: get_networking_client().create_namespaced_network_policy(namespace, body))
    if isinstance(result, ToolError):
        return str(result)
    return f"NetworkPolicy '{name}' created in namespace '{namespace}' (type={policy_type})."


# ---------------------------------------------------------------------------
# Audit trail — write actions to cluster ConfigMap
# ---------------------------------------------------------------------------


@beta_tool
def record_audit_entry(action: str, details: str, namespace: str = "pulse-agent") -> str:
    """Record an agent action to a ConfigMap in the cluster for team visibility.

    Args:
        action: Short action name (e.g. 'scale_deployment', 'security_scan').
        details: Description of what was done and the outcome.
        namespace: Namespace for the audit ConfigMap (default: pulse-agent).
    """
    now = datetime.now(UTC)
    entry_key = f"{now.strftime('%Y%m%d-%H%M%S-%f')}-{action}"
    # Truncate details to prevent exceeding ConfigMap 1MB limit
    truncated = details[:1000] if len(details) > 1000 else details
    entry_value = f"{now.isoformat()} | {action} | {truncated}"

    core = get_core_client()

    # Ensure namespace exists
    try:
        core.read_namespace(namespace)
    except ApiException as e:
        if e.status == 404:
            return f"Namespace '{namespace}' does not exist. Create it first."
        return f"Error checking namespace: {e.reason}"

    cm_name = "pulse-agent-audit"

    # Retry loop for 409 Conflict (optimistic concurrency)
    for attempt in range(3):
        try:
            cm = core.read_namespaced_config_map(cm_name, namespace)
            data = cm.data or {}
            # Keep last 100 entries
            if len(data) >= 100:
                oldest = sorted(data.keys())[0]
                del data[oldest]
            data[entry_key] = entry_value
            cm.data = data
            core.replace_namespaced_config_map(cm_name, namespace, cm)
            return f"Audit entry recorded: {entry_key}"
        except ApiException as e:
            if e.status == 404:
                # Create the ConfigMap
                body = client.V1ConfigMap(
                    metadata=client.V1ObjectMeta(name=cm_name, namespace=namespace),
                    data={entry_key: entry_value},
                )
                safe(lambda: core.create_namespaced_config_map(namespace, body))
                return f"Audit entry recorded: {entry_key}"
            elif e.status == 409 and attempt < 2:
                continue  # Retry on conflict
            else:
                return f"Error writing audit: {e.reason}"

    return f"Audit entry recorded: {entry_key}"


def _infer_column_type(col_id: str, sample_value=None) -> str:
    """Infer the best column renderer type from the column ID and sample value."""
    if col_id in ("status", "phase", "state"):
        return "status"
    if col_id == "name":
        return "resource_name"
    if col_id == "namespace":
        return "namespace"
    if col_id == "node":
        return "node"
    if col_id == "age":
        return "age"
    if col_id in ("logs", "link"):
        return "link"
    if col_id in ("cpu", "cpu_pct"):
        return "cpu"
    if col_id in ("memory", "mem_pct"):
        return "memory"
    if col_id in ("ready", "replicas", "completions"):
        return "replicas"
    if col_id in ("labels", "annotations"):
        return "labels"
    if col_id in ("severity", "type"):
        return "severity"
    if col_id.endswith("_pct") or col_id == "utilization":
        return "progress"
    if col_id in ("created", "creationTimestamp", "lastSchedule", "lastScheduleTime", "startsAt"):
        return "timestamp"
    if isinstance(sample_value, bool):
        return "boolean"
    if isinstance(sample_value, str) and sample_value.startswith("/"):
        return "link"
    if isinstance(sample_value, str) and len(sample_value) > 18 and "T" in sample_value:
        return "timestamp"
    return "text"


# Short name → (plural, group) mapping for common resources
_SHORT_NAMES: dict[str, tuple[str, str]] = {
    "po": ("pods", ""),
    "pod": ("pods", ""),
    "svc": ("services", ""),
    "service": ("services", ""),
    "deploy": ("deployments", "apps"),
    "deployment": ("deployments", "apps"),
    "ds": ("daemonsets", "apps"),
    "daemonset": ("daemonsets", "apps"),
    "sts": ("statefulsets", "apps"),
    "statefulset": ("statefulsets", "apps"),
    "rs": ("replicasets", "apps"),
    "replicaset": ("replicasets", "apps"),
    "cm": ("configmaps", ""),
    "configmap": ("configmaps", ""),
    "pvc": ("persistentvolumeclaims", ""),
    "persistentvolumeclaim": ("persistentvolumeclaims", ""),
    "pv": ("persistentvolumes", ""),
    "persistentvolume": ("persistentvolumes", ""),
    "ns": ("namespaces", ""),
    "namespace": ("namespaces", ""),
    "no": ("nodes", ""),
    "node": ("nodes", ""),
    "sa": ("serviceaccounts", ""),
    "serviceaccount": ("serviceaccounts", ""),
    "hpa": ("horizontalpodautoscalers", "autoscaling"),
    "cj": ("cronjobs", "batch"),
    "cronjob": ("cronjobs", "batch"),
    "job": ("jobs", "batch"),
    "ing": ("ingresses", "networking.k8s.io"),
    "ingress": ("ingresses", "networking.k8s.io"),
    "netpol": ("networkpolicies", "networking.k8s.io"),
    "ev": ("events", ""),
    "event": ("events", ""),
    "secret": ("secrets", ""),
    "ep": ("endpoints", ""),
    "quota": ("resourcequotas", ""),
    "limits": ("limitranges", ""),
}


def _resolve_short_name(resource: str, group: str) -> tuple[str, str]:
    """Resolve short names and singular forms to (plural, group)."""
    key = resource.lower().strip()
    if key in _SHORT_NAMES:
        plural, default_group = _SHORT_NAMES[key]
        return plural, group or default_group
    return resource, group


@beta_tool
def list_resources(
    resource: str,
    namespace: str = "",
    group: str = "",
    version: str = "v1",
    label_selector: str = "",
    field_selector: str = "",
    sort_by: str = "",
    show_wide: bool = False,
) -> str:
    """List any Kubernetes resource type using the server's printer columns.

    Uses the Kubernetes Table API to get the same columns as 'kubectl get'.
    Works for ANY resource type including CRDs with custom printer columns.
    Accepts short names (po, svc, deploy, cm, ds, sts, hpa, cj, ing, etc.).

    Args:
        resource: Resource type — plural, singular, or short name (e.g. 'pods', 'svc', 'deploy', 'cm').
        namespace: Kubernetes namespace. Use 'ALL' for all namespaces. Leave empty for cluster-scoped.
        group: API group (e.g. 'apps' for deployments). Auto-detected for common types.
        version: API version (default 'v1').
        label_selector: Filter by labels (e.g. 'app=nginx', 'app.kubernetes.io/managed-by=Helm').
        field_selector: Filter by fields (e.g. 'status.phase=Running').
        sort_by: Column name to sort by. Prefix with '-' for descending.
        show_wide: If true, include all columns (like kubectl -o wide).
    """
    import ssl
    import urllib.parse
    import urllib.request

    # Resolve short names (svc→services, deploy→deployments, cm→configmaps, etc.)
    resource, group = _resolve_short_name(resource, group)

    gvr = f"{group}~{version}~{resource}" if group else f"{version}~{resource}"

    # Build the API path
    api_base = f"/apis/{group}/{version}" if group else f"/api/{version}"
    if namespace and namespace.upper() != "ALL":
        path = f"{api_base}/namespaces/{namespace}/{resource}"
    else:
        path = f"{api_base}/{resource}"

    params = {}
    if label_selector:
        params["labelSelector"] = label_selector
    if field_selector:
        params["fieldSelector"] = field_selector
    params["limit"] = str(MAX_RESULTS)

    url = f"https://kubernetes.default.svc{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    # Read SA token for auth
    try:
        with open("/var/run/secrets/kubernetes.io/serviceaccount/token") as f:
            token = f.read().strip()
    except FileNotFoundError:
        return "Error: Not running in-cluster (no service account token)."

    ctx = ssl.create_default_context()
    try:
        ctx.load_verify_locations("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    except Exception as e:
        return f"Error: Cannot load cluster CA certificate ({e}). Refusing to connect without TLS verification."

    # Request as Table format — server returns printer columns + pre-formatted cells
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json;as=Table;g=meta.k8s.io;v=v1",
        },
    )

    try:
        resp = urllib.request.urlopen(req, context=ctx, timeout=15)
        data = json.loads(resp.read())
    except Exception as e:
        return f"Error listing {resource}: {e}"

    col_defs_raw = data.get("columnDefinitions", [])
    table_rows = data.get("rows", [])

    if not table_rows:
        return f"No {resource} found."

    # Map server column names to our renderer types
    _NAME_TYPE_MAP = {
        "Name": "resource_name",
        "Status": "status",
        "Phase": "status",
        "Ready": "replicas",
        "Age": "age",
        "Node": "node",
        "Namespace": "namespace",
        "Suspend": "boolean",
        "Selector": "labels",
        "Node Selector": "labels",
    }

    # Build column definitions — filter by priority unless show_wide
    max_priority = 99 if show_wide else 0
    col_defs = []
    col_indices = []
    for i, col in enumerate(col_defs_raw):
        if col.get("priority", 0) > max_priority:
            continue
        col_id = col["name"].lower().replace(" ", "_").replace("-", "_").replace("(", "").replace(")", "")
        col_type = _NAME_TYPE_MAP.get(col["name"]) or _infer_column_type(col_id) or "text"
        col_defs.append({"id": col_id, "header": col["name"], "type": col_type})
        col_indices.append(i)

    # Detect if resource is cluster-scoped (no namespace on any row)
    is_cluster_scoped = all(
        not (tr.get("object", {}).get("metadata", {}) if isinstance(tr.get("object"), dict) else {}).get("namespace")
        for tr in table_rows[:5]
    )

    # Build rows from Table cells
    rows = []
    for tr in table_rows:
        cells = tr.get("cells", [])
        meta = tr.get("object", {}).get("metadata", {}) if isinstance(tr.get("object"), dict) else {}
        ns = meta.get("namespace", "")
        row: dict = {"_gvr": gvr}
        if ns and not is_cluster_scoped:
            row["namespace"] = ns

        for j, idx in enumerate(col_indices):
            if idx < len(cells):
                val = cells[idx]
                row[col_defs[j]["id"]] = val if val is not None else ""

        rows.append(row)

    # Add namespace column only for namespaced resources listed across namespaces
    if (
        not is_cluster_scoped
        and (not namespace or namespace.upper() == "ALL")
        and not any(c["id"] == "namespace" for c in col_defs)
    ):
        col_defs.insert(0, {"id": "namespace", "header": "Namespace", "type": "namespace"})

    # Remove namespace column from cluster-scoped resources
    if is_cluster_scoped:
        col_defs = [c for c in col_defs if c["id"] != "namespace"]

    # Sort if requested
    if sort_by:
        desc = sort_by.startswith("-")
        sort_key = sort_by.lstrip("-").lower().replace(" ", "_").replace("-", "_")
        rows.sort(key=lambda r: str(r.get(sort_key, "")), reverse=desc)

    # Build text summary
    name_col = next((c["id"] for c in col_defs if c.get("type") == "resource_name"), "name")
    text_lines = []
    for r in rows[:20]:
        ns_prefix = f"{r.get('namespace', '')}/" if r.get("namespace") else ""
        text_lines.append(f"{ns_prefix}{r.get(name_col, '?')}")
    text = f"{resource} ({len(rows)}):\n" + "\n".join(text_lines)
    if len(rows) > 20:
        text += f"\n... and {len(rows) - 20} more"

    component = {
        "kind": "data_table",
        "title": f"{resource.replace('_', ' ').title()} ({len(rows)})",
        "description": f"{'Filtered' if label_selector else 'All'} {resource} in {namespace or 'cluster'}",
        "columns": col_defs,
        "rows": rows,
    }
    return (text, component)


@beta_tool
def get_resource_relationships(namespace: str, name: str, kind: str = "Pod") -> str:
    """Trace the ownership chain for a Kubernetes resource — find what controls it and what it controls.

    Shows the full hierarchy: e.g., Pod → ReplicaSet → Deployment, or Deployment → ReplicaSet → Pods.
    Uses ownerReferences to walk up and label selectors to walk down.

    Args:
        namespace: Kubernetes namespace.
        name: Resource name.
        kind: Resource kind (e.g. 'Pod', 'Deployment', 'StatefulSet'). Default 'Pod'.
    """
    core = get_core_client()
    apps = get_apps_client()

    lines = [f"Relationships for {kind}/{name} in {namespace}:"]

    # Walk UP the owner chain
    current_kind = kind
    current_name = name
    owners_chain = []

    for _ in range(5):  # max depth
        try:
            if current_kind == "Pod":
                obj = safe(lambda: core.read_namespaced_pod(current_name, namespace))
            elif current_kind == "ReplicaSet":
                obj = safe(lambda: apps.read_namespaced_replica_set(current_name, namespace))
            elif current_kind == "Deployment":
                obj = safe(lambda: apps.read_namespaced_deployment(current_name, namespace))
            elif current_kind == "StatefulSet":
                obj = safe(lambda: apps.read_namespaced_stateful_set(current_name, namespace))
            elif current_kind == "DaemonSet":
                obj = safe(lambda: apps.read_namespaced_daemon_set(current_name, namespace))
            elif current_kind == "Job":
                obj = safe(lambda: get_batch_client().read_namespaced_job(current_name, namespace))
            elif current_kind == "CronJob":
                obj = safe(lambda: get_batch_client().read_namespaced_cron_job(current_name, namespace))
            else:
                break

            if isinstance(obj, ToolError):
                break

            owner_refs = obj.metadata.owner_references or []
            if not owner_refs:
                break

            owner = owner_refs[0]
            owners_chain.append(f"{current_kind}/{current_name} → owned by → {owner.kind}/{owner.name}")
            current_kind = owner.kind
            current_name = owner.name
        except Exception:
            break

    # Walk DOWN — find resources owned by the target
    children = []
    try:
        all_pods = safe(lambda: core.list_namespaced_pod(namespace, limit=200))
        if not isinstance(all_pods, ToolError):
            for pod in all_pods.items:
                for ref in pod.metadata.owner_references or []:
                    if ref.name == name and ref.kind == kind:
                        children.append(f"  → Pod/{pod.metadata.name} (status={pod.status.phase})")
                    # Also check if owned by a child ReplicaSet of this Deployment
                    if kind == "Deployment":
                        # Check if this pod's RS is owned by our deployment
                        pass
    except Exception:
        pass

    # Build output
    if owners_chain:
        lines.append("\nOwnership chain (upward):")
        for o in owners_chain:
            lines.append(f"  {o}")
        lines.append(f"  Root: {current_kind}/{current_name}")

    if children:
        lines.append(f"\nOwned resources ({len(children)}):")
        lines.extend(children[:20])

    # Well-known labels
    try:
        if kind == "Pod":
            obj = safe(lambda: core.read_namespaced_pod(name, namespace))
        elif kind == "Deployment":
            obj = safe(lambda: apps.read_namespaced_deployment(name, namespace))
        else:
            obj = None

        if obj and not isinstance(obj, ToolError):
            labels = obj.metadata.labels or {}
            important = {
                k: v for k, v in labels.items() if k.startswith("app.kubernetes.io/") or k in ("app", "version")
            }
            if important:
                lines.append("\nWell-known labels:")
                for k, v in sorted(important.items()):
                    lines.append(f"  {k}={v}")
    except Exception:
        pass

    text = "\n".join(lines)

    # Build a visual relationship tree
    _KIND_GVR_MAP = {
        "Pod": "v1~pods",
        "Deployment": "apps~v1~deployments",
        "ReplicaSet": "apps~v1~replicasets",
        "StatefulSet": "apps~v1~statefulsets",
        "DaemonSet": "apps~v1~daemonsets",
        "Job": "batch~v1~jobs",
        "CronJob": "batch~v1~cronjobs",
        "Service": "v1~services",
        "Node": "v1~nodes",
        "ConfigMap": "v1~configmaps",
    }

    tree_nodes = []
    node_ids = set()

    # Add the target resource
    target_id = f"{kind}/{name}"
    tree_nodes.append(
        {
            "id": target_id,
            "label": target_id,
            "kind": kind,
            "name": name,
            "namespace": namespace,
            "status": "healthy",
            "gvr": _KIND_GVR_MAP.get(kind, ""),
            "children": [],
            "detail": "",
        }
    )
    node_ids.add(target_id)

    # Build tree from owners_chain (walk up)
    prev_id = target_id
    for o in owners_chain:
        parts = o.split(" → owned by → ")
        if len(parts) == 2:
            owner_str = parts[1]
            owner_kind, owner_name = owner_str.split("/", 1) if "/" in owner_str else (owner_str, "")
            owner_id = owner_str
            if owner_id not in node_ids:
                tree_nodes.append(
                    {
                        "id": owner_id,
                        "label": owner_id,
                        "kind": owner_kind,
                        "name": owner_name,
                        "namespace": namespace,
                        "status": "healthy",
                        "gvr": _KIND_GVR_MAP.get(owner_kind, ""),
                        "children": [prev_id],
                        "detail": "",
                    }
                )
                node_ids.add(owner_id)
            else:
                # Add child to existing node
                for n in tree_nodes:
                    if n["id"] == owner_id and prev_id not in n["children"]:
                        n["children"].append(prev_id)
            prev_id = owner_id

    root_id = prev_id  # topmost owner

    # Add children (pods owned by target)
    for c in children[:15]:
        parts = c.strip().split("(")
        cname = parts[0].strip().lstrip("→ ").strip()
        status_str = parts[1].rstrip(")").split("=")[1] if len(parts) > 1 else "unknown"
        child_kind = cname.split("/")[0] if "/" in cname else "Pod"
        child_name = cname.split("/")[1] if "/" in cname else cname
        child_id = cname
        if child_id not in node_ids:
            tree_nodes.append(
                {
                    "id": child_id,
                    "label": child_id,
                    "kind": child_kind,
                    "name": child_name,
                    "namespace": namespace,
                    "status": "healthy"
                    if status_str == "Running"
                    else "warning"
                    if status_str == "Pending"
                    else "error",
                    "gvr": _KIND_GVR_MAP.get(child_kind, ""),
                    "children": [],
                    "detail": status_str,
                }
            )
            node_ids.add(child_id)
        # Add to target's children
        for n in tree_nodes:
            if n["id"] == target_id and child_id not in n["children"]:
                n["children"].append(child_id)

    component = (
        {
            "kind": "relationship_tree",
            "title": f"Relationships — {kind}/{name}",
            "description": f"Ownership hierarchy in namespace {namespace}",
            "nodes": tree_nodes,
            "rootId": root_id,
        }
        if tree_nodes
        else None
    )

    return (text, component) if component else text


# ---------------------------------------------------------------------------
# Kind → plural mapping for generic resource access
# ---------------------------------------------------------------------------

_KIND_PLURAL_MAP: dict[str, str] = {
    "ConfigMap": "configmaps",
    "Service": "services",
    "Pod": "pods",
    "Secret": "secrets",
    "Namespace": "namespaces",
    "Node": "nodes",
    "ServiceAccount": "serviceaccounts",
    "PersistentVolumeClaim": "persistentvolumeclaims",
    "PersistentVolume": "persistentvolumes",
    "Endpoints": "endpoints",
    "Event": "events",
    "ResourceQuota": "resourcequotas",
    "LimitRange": "limitranges",
    "Deployment": "deployments",
    "StatefulSet": "statefulsets",
    "DaemonSet": "daemonsets",
    "ReplicaSet": "replicasets",
    "Job": "jobs",
    "CronJob": "cronjobs",
    "Ingress": "ingresses",
    "NetworkPolicy": "networkpolicies",
    "HorizontalPodAutoscaler": "horizontalpodautoscalers",
    "Role": "roles",
    "RoleBinding": "rolebindings",
    "ClusterRole": "clusterroles",
    "ClusterRoleBinding": "clusterrolebindings",
}


def _resolve_plural(kind: str) -> str:
    """Convert a Kind to its plural resource name."""
    if kind in _KIND_PLURAL_MAP:
        return _KIND_PLURAL_MAP[kind]
    # Fallback: lowercase + 's', handling common suffixes
    lower = kind.lower()
    if lower.endswith("s"):
        return lower + "es"
    if lower.endswith("y"):
        return lower[:-1] + "ies"
    return lower + "s"


# ---------------------------------------------------------------------------
# Generic describe_resource — works for any K8s resource
# ---------------------------------------------------------------------------


@beta_tool
def describe_resource(namespace: str, name: str, kind: str, group: str = "", version: str = "v1") -> str:
    """Get detailed information about any Kubernetes resource including metadata, status, conditions, and events. Use this for resources that don't have a specialized describe tool.

    Args:
        namespace: Kubernetes namespace. Use '_' for cluster-scoped resources.
        name: Name of the resource.
        kind: Resource kind (e.g. 'ConfigMap', 'Service', 'StatefulSet').
        group: API group (e.g. 'apps', 'batch'). Empty for core resources.
        version: API version (default 'v1').
    """
    if err := _validate_k8s_name(name):
        return err

    plural = _resolve_plural(kind)

    # Build API URL path
    if group:
        api_base = f"/apis/{group}/{version}"
    else:
        api_base = f"/api/{version}"

    if namespace and namespace != "_":
        path = f"{api_base}/namespaces/{namespace}/{plural}/{name}"
    else:
        path = f"{api_base}/{plural}/{name}"

    # Reuse the existing core client's ApiClient (singleton, already authed)
    api = get_core_client().api_client

    try:
        resp = api.call_api(
            path,
            "GET",
            auth_settings=["BearerToken"],
            response_type="object",
            _return_http_data_only=True,
        )
        # resp is a tuple (data, status, headers) or just data depending on version
        obj = resp if isinstance(resp, dict) else resp[0] if isinstance(resp, tuple) else resp
    except ApiException as e:
        return f"Error ({e.status}): {e.reason}"
    except Exception as e:
        return f"Error fetching {kind}/{name}: {type(e).__name__}: {e}"

    if not isinstance(obj, dict):
        return f"Unexpected response type: {type(obj).__name__}"

    # Extract key sections for structured display
    metadata = obj.get("metadata", {})
    status = obj.get("status", {})
    labels = metadata.get("labels", {}) or {}
    annotations = metadata.get("annotations", {}) or {}
    owner_refs = metadata.get("ownerReferences", [])

    # Build key-value component
    pairs = [
        {"key": "Name", "value": str(metadata.get("name", ""))},
        {"key": "Namespace", "value": str(metadata.get("namespace", "cluster-scoped"))},
        {"key": "Kind", "value": kind},
        {"key": "Labels", "value": str(len(labels))},
        {"key": "Annotations", "value": str(len(annotations))},
        {
            "key": "Age",
            "value": age(datetime.fromisoformat(metadata["creationTimestamp"].replace("Z", "+00:00")))
            if metadata.get("creationTimestamp")
            else "unknown",
        },
    ]
    if owner_refs:
        owners = ", ".join(f"{r.get('kind', '?')}/{r.get('name', '?')}" for r in owner_refs)
        pairs.append({"key": "Owned By", "value": owners})

    components: list[dict] = [
        {"kind": "key_value", "title": f"{kind} — {name}", "pairs": pairs},
    ]

    # Labels as badges
    if labels:
        components.append(
            {
                "kind": "badge_list",
                "badges": [{"text": f"{k}={v}", "variant": "info"} for k, v in list(labels.items())[:10]],
            }
        )

    # Status conditions
    conditions = status.get("conditions", [])
    if conditions:
        components.append(
            {
                "kind": "status_list",
                "title": "Conditions",
                "items": [
                    {
                        "name": c.get("type", "?"),
                        "status": "healthy" if c.get("status") == "True" else "error",
                        "detail": c.get("reason") or c.get("message") or "",
                    }
                    for c in conditions
                ],
            }
        )

    # Fetch related events
    if namespace and namespace != "_":
        core = get_core_client()
        events = safe(
            lambda: core.list_namespaced_event(
                namespace,
                field_selector=f"involvedObject.name={name},involvedObject.kind={kind}",
            )
        )
        if not isinstance(events, ToolError) and events.items:
            sorted_events = sorted(
                events.items,
                key=lambda e: e.last_timestamp or datetime.min.replace(tzinfo=UTC),
                reverse=True,
            )[:10]
            event_rows = [
                {
                    "age": age(e.last_timestamp),
                    "type": e.type or "Normal",
                    "reason": e.reason or "",
                    "message": (e.message or "")[:120],
                }
                for e in sorted_events
            ]
            components.append(
                {
                    "kind": "data_table",
                    "title": f"Events ({len(event_rows)})",
                    "columns": [
                        {"id": "age", "header": "Age"},
                        {"id": "type", "header": "Type"},
                        {"id": "reason", "header": "Reason"},
                        {"id": "message", "header": "Message"},
                    ],
                    "rows": event_rows,
                }
            )

    text = json.dumps(obj, indent=2, default=str)

    # Add YAML manifest viewer (spec only, not full object — cleaner for users)
    spec = obj.get("spec")
    if spec:
        import yaml as _yaml

        try:
            yaml_content = _yaml.dump(spec, default_flow_style=False)
        except Exception:
            yaml_content = json.dumps(spec, indent=2, default=str)
        components.append(
            {
                "kind": "yaml_viewer",
                "title": f"{kind} Spec",
                "content": yaml_content,
                "language": "yaml",
            }
        )

    component = {
        "kind": "section",
        "title": f"{kind} Details — {name}",
        "collapsible": False,
        "defaultOpen": True,
        "components": components,
    }
    return (text, component)


# ---------------------------------------------------------------------------
# exec_command — run a command inside a pod container
# ---------------------------------------------------------------------------

# Characters that indicate shell metacharacters (security risk)
_DANGEROUS_CHARS = set(";|&$><`")

MAX_EXEC_OUTPUT = 10 * 1024  # 10KB cap


@beta_tool
def exec_command(namespace: str, pod_name: str, command: str, container: str = "") -> str:
    """Execute a command inside a running pod container. Use this for debugging, checking environment variables, testing connectivity, or inspecting files.

    Args:
        namespace: Kubernetes namespace.
        pod_name: Name of the pod.
        command: Command to run (e.g. 'env', 'cat /etc/config/app.yaml', 'whoami'). Shell metacharacters are not allowed.
        container: Container name. Optional if pod has only one container.
    """
    if err := _validate_k8s_namespace(namespace):
        return err
    if err := _validate_k8s_name(pod_name, "pod_name"):
        return err
    if not command or not command.strip():
        return "Error: command is required."

    # Reject shell metacharacters
    if any(c in command for c in _DANGEROUS_CHARS):
        return "Error: Shell metacharacters (;|&$><`) are not allowed in commands for security reasons."

    cmd_parts = command.split()
    core = get_core_client()

    kwargs: dict = {
        "name": pod_name,
        "namespace": namespace,
        "command": cmd_parts,
        "stderr": True,
        "stdout": True,
        "stdin": False,
        "tty": False,
    }
    if container:
        kwargs["container"] = container

    try:
        output = k8s_stream(core.connect_get_namespaced_pod_exec, **kwargs)
    except ApiException as e:
        return f"Error ({e.status}): {e.reason}"
    except Exception as e:
        return f"Error executing command: {type(e).__name__}: {e}"

    if not output:
        return "(no output)"

    if len(output) > MAX_EXEC_OUTPUT:
        output = output[:MAX_EXEC_OUTPUT] + f"\n\n... (truncated, {len(output)} total bytes)"

    return output


# ---------------------------------------------------------------------------
# search_logs — search logs across pods matching a label selector
# ---------------------------------------------------------------------------


@beta_tool
def search_logs(namespace: str, label_selector: str, pattern: str, tail_lines: int = 100, container: str = "") -> str:
    """Search logs across multiple pods matching a label selector. Returns matching lines with pod name prefix.

    Args:
        namespace: Kubernetes namespace.
        label_selector: Label selector (e.g. 'app=nginx').
        pattern: Text pattern to search for in logs (case-insensitive).
        tail_lines: Number of recent lines to search per pod (default 100, max 500).
        container: Container name. Optional.
    """
    if err := _validate_k8s_namespace(namespace):
        return err
    if not label_selector:
        return "Error: label_selector is required."
    if not pattern:
        return "Error: pattern is required."

    tail_lines = min(max(1, tail_lines), 500)
    core = get_core_client()

    # List pods matching the label selector
    pods_result = safe(lambda: core.list_namespaced_pod(namespace, label_selector=label_selector))
    if isinstance(pods_result, ToolError):
        return str(pods_result)

    if not pods_result.items:
        return f"No pods found matching label selector '{label_selector}' in namespace '{namespace}'."

    pattern_lower = pattern.lower()
    pods_to_search = pods_result.items[:20]  # Cap at 20 pods
    pods_searched = len(pods_to_search)

    def _fetch_pod_logs(pod):
        """Fetch and filter logs for a single pod."""
        pod_name = pod.metadata.name
        kwargs: dict = {"name": pod_name, "namespace": namespace, "tail_lines": tail_lines}
        if container:
            kwargs["container"] = container

        logs = safe(lambda: core.read_namespaced_pod_log(**kwargs))
        if isinstance(logs, ToolError):
            return [f"[{pod_name}] Error reading logs: {logs}"], False

        if not logs:
            return [], False

        pod_matches = []
        for line in logs.split("\n"):
            if pattern_lower in line.lower():
                pod_matches.append(f"[{pod_name}] {line}")

        return pod_matches[:50], bool(pod_matches)  # Cap per pod

    from concurrent.futures import ThreadPoolExecutor

    matches: list[str] = []
    pods_with_matches = 0
    with ThreadPoolExecutor(max_workers=5) as executor:
        results = executor.map(_fetch_pod_logs, pods_to_search)
        for pod_matches, had_matches in results:
            matches.extend(pod_matches)
            if had_matches:
                pods_with_matches += 1

    if not matches:
        return f"No matches for '{pattern}' in logs of {pods_searched} pods matching '{label_selector}'."

    header = f"Found {len(matches)} matching lines across {pods_with_matches}/{pods_searched} pods:"
    text = header + "\n\n" + "\n".join(matches[:200])

    # Build log_viewer component
    log_lines = []
    for line in matches[:200]:
        source = ""
        msg = line
        if line.startswith("[") and "] " in line:
            bracket_end = line.index("] ")
            source = line[1:bracket_end]
            msg = line[bracket_end + 2 :]
        level = (
            "error"
            if any(w in msg.lower() for w in ("error", "fatal", "panic"))
            else "warn"
            if any(w in msg.lower() for w in ("warn", "warning"))
            else "info"
        )
        log_lines.append({"message": msg, "source": source, "level": level})

    component = {
        "kind": "log_viewer",
        "title": f"Log Search: '{pattern}' ({len(matches)} matches)",
        "source": label_selector,
        "lines": log_lines,
    }
    return (text, component)


# ---------------------------------------------------------------------------
# test_connectivity — test network connectivity from a pod
# ---------------------------------------------------------------------------


@beta_tool
def test_connectivity(source_namespace: str, source_pod: str, target_host: str, target_port: int) -> str:
    """Test network connectivity from a pod to a target host and port. Useful for debugging service discovery, network policies, and DNS issues.

    Args:
        source_namespace: Namespace of the source pod.
        source_pod: Name of the source pod.
        target_host: Target hostname or IP (e.g. 'my-service.default.svc', '10.0.0.1').
        target_port: Target port number.
    """
    if err := _validate_k8s_namespace(source_namespace):
        return err
    if err := _validate_k8s_name(source_pod, "source_pod"):
        return err
    if not target_host:
        return "Error: target_host is required."
    # Sanitize target_host — only allow alphanumeric, dots, dashes, colons (IPv6)
    if not re.match(r"^[a-zA-Z0-9.\-:]+$", target_host):
        return "Error: target_host contains invalid characters."
    if not (1 <= target_port <= 65535):
        return f"Error: target_port must be between 1 and 65535, got {target_port}."

    core = get_core_client()

    # Try multiple connectivity check methods (not all containers have all tools)
    methods = [
        # nc (netcat) — most common
        ["nc", "-zv", "-w", "5", target_host, str(target_port)],
        # bash built-in /dev/tcp (works on most containers with bash)
        ["timeout", "5", "bash", "-c", f"echo > /dev/tcp/{target_host}/{target_port}"],
        # wget — available in many alpine-based images
        ["wget", "--spider", "--timeout=5", f"http://{target_host}:{target_port}/", "-O", "/dev/null"],
    ]

    import time as _time

    for cmd in methods:
        start = _time.monotonic()
        try:
            output = k8s_stream(
                core.connect_get_namespaced_pod_exec,
                name=source_pod,
                namespace=source_namespace,
                command=cmd,
                stderr=True,
                stdout=True,
                stdin=False,
                tty=False,
            )
            elapsed_ms = int((_time.monotonic() - start) * 1000)
            # If we got here without exception, connection likely succeeded
            return (
                f"Connection to {target_host}:{target_port} succeeded.\n"
                f"Latency: {elapsed_ms}ms\n"
                f"Method: {cmd[0]}\n"
                f"Output: {(output or '').strip()[:500]}"
            )
        except ApiException as e:
            if e.status == 404:
                return f"Error: Pod '{source_pod}' not found in namespace '{source_namespace}'."
            # Command failed — try next method
            elapsed_ms = int((_time.monotonic() - start) * 1000)
            if cmd == methods[-1]:
                # Last method — report failure
                return (
                    f"Connection to {target_host}:{target_port} FAILED.\n"
                    f"Latency: {elapsed_ms}ms\n"
                    f"All connectivity methods failed. The target may be unreachable, "
                    f"blocked by a NetworkPolicy, or the container lacks network tools.\n"
                    f"Last error: {e.reason}"
                )
            continue
        except Exception as e:
            elapsed_ms = int((_time.monotonic() - start) * 1000)
            if cmd == methods[-1]:
                return (
                    f"Connection to {target_host}:{target_port} FAILED.\n"
                    f"Latency: {elapsed_ms}ms\n"
                    f"Error: {type(e).__name__}: {e}"
                )
            continue

    return f"Connection to {target_host}:{target_port} FAILED. No connectivity tools available in the container."


# ---------------------------------------------------------------------------
# get_resource_recommendations — right-sizing analysis
# ---------------------------------------------------------------------------


@beta_tool
def get_resource_recommendations(namespace: str, time_range: str = "24h") -> str:
    """Analyze resource usage vs requests/limits and recommend right-sizing. Shows over-provisioned and under-provisioned workloads.

    Args:
        namespace: Kubernetes namespace.
        time_range: Time window for usage analysis (default '24h').
    """
    import os
    import ssl
    import urllib.parse
    import urllib.request

    if err := _validate_k8s_namespace(namespace):
        return err

    base_url = os.environ.get("THANOS_URL", "")
    if not base_url:
        base_url = "https://thanos-querier.openshift-monitoring.svc:9091"

    # Build Prometheus queries for CPU and memory P95
    cpu_query = (
        f"quantile_over_time(0.95, rate(container_cpu_usage_seconds_total"
        f'{{namespace="{namespace}",container!="",container!="POD"}}[5m])[{time_range}:])'
    )
    mem_query = (
        f"quantile_over_time(0.95, container_memory_working_set_bytes"
        f'{{namespace="{namespace}",container!="",container!="POD"}}[{time_range}:])'
    )

    # Also get current requests/limits from kube_state_metrics
    cpu_req_query = f'kube_pod_container_resource_requests{{namespace="{namespace}",resource="cpu"}}'
    mem_req_query = f'kube_pod_container_resource_requests{{namespace="{namespace}",resource="memory"}}'

    # Read SA token
    try:
        with open("/var/run/secrets/kubernetes.io/serviceaccount/token") as f:
            token = f.read().strip()
    except FileNotFoundError:
        token = ""

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    def _instant_query(query: str) -> list[dict]:
        params = urllib.parse.urlencode({"query": query})
        url = f"{base_url}/api/v1/query?{params}"
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, headers=headers)
        try:
            resp = urllib.request.urlopen(req, context=ctx, timeout=15)
            data = json.loads(resp.read())
            if data.get("status") == "success":
                return data.get("data", {}).get("result", [])
        except Exception:
            pass
        return []

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=4) as executor:
        cpu_usage_f = executor.submit(_instant_query, cpu_query)
        mem_usage_f = executor.submit(_instant_query, mem_query)
        cpu_requests_f = executor.submit(_instant_query, cpu_req_query)
        mem_requests_f = executor.submit(_instant_query, mem_req_query)

    cpu_usage = cpu_usage_f.result()
    mem_usage = mem_usage_f.result()
    cpu_requests = cpu_requests_f.result()
    mem_requests = mem_requests_f.result()

    if not cpu_usage and not mem_usage and not cpu_requests:
        return (
            f"No resource metrics available for namespace '{namespace}'. "
            "Ensure Prometheus/Thanos and kube-state-metrics are running."
        )

    # Index requests by pod+container
    def _key(r: dict) -> str:
        m = r.get("metric", {})
        return f"{m.get('pod', '')}:{m.get('container', '')}"

    cpu_req_map = {_key(r): float(r["value"][1]) for r in cpu_requests if r.get("value")}
    mem_req_map = {_key(r): float(r["value"][1]) for r in mem_requests if r.get("value")}
    cpu_use_map = {_key(r): float(r["value"][1]) for r in cpu_usage if r.get("value")}
    mem_use_map = {_key(r): float(r["value"][1]) for r in mem_usage if r.get("value")}

    # Merge all keys
    all_keys = set(cpu_req_map) | set(mem_req_map) | set(cpu_use_map) | set(mem_use_map)

    rows = []
    for key in sorted(all_keys):
        pod, container = key.split(":", 1) if ":" in key else (key, "")
        if not pod or not container:
            continue

        cpu_req = cpu_req_map.get(key, 0)
        cpu_p95 = cpu_use_map.get(key, 0)
        mem_req = mem_req_map.get(key, 0)
        mem_p95 = mem_use_map.get(key, 0)

        # Recommend: 20% headroom above P95, rounded to nearest 50m/50Mi
        cpu_rec = max(0.05, round((cpu_p95 * 1.2) * 20) / 20)  # Round to nearest 50m
        mem_rec = max(64 * 1024 * 1024, int((mem_p95 * 1.2) / (50 * 1024 * 1024)) * 50 * 1024 * 1024)  # Round 50Mi

        def _fmt_cpu(cores: float) -> str:
            if cores < 1:
                return f"{int(cores * 1000)}m"
            return f"{cores:.2f}"

        def _fmt_mem(b: float) -> str:
            mi = b / (1024 * 1024)
            if mi < 1024:
                return f"{int(mi)}Mi"
            return f"{mi / 1024:.1f}Gi"

        rows.append(
            {
                "pod": pod,
                "container": container,
                "cpu_request": _fmt_cpu(cpu_req),
                "cpu_p95": _fmt_cpu(cpu_p95),
                "cpu_recommendation": _fmt_cpu(cpu_rec),
                "mem_request": _fmt_mem(mem_req),
                "mem_p95": _fmt_mem(mem_p95),
                "mem_recommendation": _fmt_mem(mem_rec),
            }
        )

    if not rows:
        return f"No workload resource data found for namespace '{namespace}'."

    # Text summary
    lines = [f"Resource recommendations for namespace '{namespace}' (P95 over {time_range}):"]
    for r in rows[:30]:
        lines.append(
            f"  {r['pod']}/{r['container']}: "
            f"CPU {r['cpu_request']}→{r['cpu_recommendation']} (P95={r['cpu_p95']})  "
            f"Mem {r['mem_request']}→{r['mem_recommendation']} (P95={r['mem_p95']})"
        )
    text = "\n".join(lines)

    component = {
        "kind": "data_table",
        "title": f"Resource Recommendations — {namespace}",
        "description": f"Right-sizing based on P95 usage over {time_range} with 20% headroom",
        "columns": [
            {"id": "pod", "header": "Pod"},
            {"id": "container", "header": "Container"},
            {"id": "cpu_request", "header": "CPU Request"},
            {"id": "cpu_p95", "header": "CPU P95"},
            {"id": "cpu_recommendation", "header": "CPU Rec."},
            {"id": "mem_request", "header": "Mem Request"},
            {"id": "mem_p95", "header": "Mem P95"},
            {"id": "mem_recommendation", "header": "Mem Rec."},
        ],
        "rows": rows[:50],
    }
    return (text, component)


ALL_TOOLS = [
    # Universal resource listing + relationships
    get_resource_relationships,
    # Universal resource listing — replaces list_namespaces, list_nodes, list_deployments,
    # list_statefulsets, list_daemonsets, get_services, get_persistent_volume_claims,
    # get_resource_quotas, list_limit_ranges, list_replicasets, get_pod_disruption_budgets.
    # Works for any resource including CRDs.
    list_resources,
    # Generic describe — works for any resource kind
    describe_resource,
    # Specialized tools with unique logic (can't be replaced by list_resources)
    list_pods,  # field_selector, logs link, restart count
    describe_pod,
    get_pod_logs,
    get_events,  # field_selector filtering by kind/name/type
    get_cluster_version,
    get_cluster_operators,  # OpenShift condition→status mapping
    get_configmap,
    get_node_metrics,  # metrics API + unit parsing
    get_pod_metrics,  # metrics API + sort by cpu/memory
    list_jobs,  # show_completed filter, duration
    list_cronjobs,  # schedule, suspended, last run
    list_ingresses,  # rules/paths/backends parsing
    list_routes,  # OpenShift route.openshift.io
    list_hpas,  # current metrics extraction
    list_operator_subscriptions,  # OLM CSV/channel/health
    get_firing_alerts,  # Alertmanager proxy
    discover_metrics,  # Prometheus metric discovery
    verify_query,  # PromQL query validation
    get_prometheus_query,  # PromQL chart generation
    # Diagnostics
    describe_service,
    get_endpoint_slices,
    top_pods_by_restarts,
    get_recent_changes,
    get_tls_certificates,
    search_logs,  # search across pods by label
    get_resource_recommendations,  # right-sizing analysis
    # Write operations
    scale_deployment,
    restart_deployment,
    cordon_node,
    uncordon_node,
    delete_pod,
    rollback_deployment,
    drain_node,
    apply_yaml,
    create_network_policy,
    exec_command,  # run commands in pods
    test_connectivity,  # network connectivity tests
    # Audit
    record_audit_entry,
]

# Register all tools in the central registry
from .tool_registry import register_tool

for _tool in ALL_TOOLS:
    register_tool(_tool, is_write=(_tool.name in WRITE_TOOLS))
