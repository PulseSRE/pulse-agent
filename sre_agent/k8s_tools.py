"""Kubernetes/OpenShift tools for the SRE agent.

Each tool is decorated with @beta_tool so the Anthropic SDK automatically
generates JSON schemas and the tool runner can execute them.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from anthropic import beta_tool
from kubernetes import client
from kubernetes.client.rest import ApiException

# RFC 1123 name validation for K8s resources
_K8S_NAME_RE = re.compile(r'^[a-z0-9]([a-z0-9\-\.]{0,251}[a-z0-9])?$')
_K8S_NAMESPACE_RE = re.compile(r'^[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?$')


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
    "scale_deployment", "restart_deployment", "cordon_node", "uncordon_node",
    "delete_pod", "apply_yaml", "create_network_policy",
    "rollback_deployment", "drain_node",
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
    if isinstance(result, str):
        return result
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
    if isinstance(result, str):
        return result

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
        rows.append({
            "namespace": ns,
            "name": pod.metadata.name,
            "status": pod.status.phase or "Unknown",
            "restarts": restarts,
            "age": age(pod.metadata.creation_timestamp),
            "node": pod.spec.node_name or "",
        })
    total = len(result.items)
    if total > MAX_RESULTS:
        lines.append(f"... and {total - MAX_RESULTS} more pods (truncated)")
    text = "\n".join(lines) or "No pods found."
    component = {
        "kind": "data_table",
        "title": f"Pods ({len(rows)})",
        "columns": [
            {"id": "namespace", "header": "Namespace"},
            {"id": "name", "header": "Name"},
            {"id": "status", "header": "Status"},
            {"id": "restarts", "header": "Restarts"},
            {"id": "age", "header": "Age"},
            {"id": "node", "header": "Node"},
        ],
        "rows": rows,
    } if rows else None
    return (text, component)


@beta_tool
def describe_pod(namespace: str, pod_name: str) -> str:
    """Get detailed information about a specific pod including conditions, containers, and recent events.

    Args:
        namespace: Kubernetes namespace.
        pod_name: Name of the pod.
    """
    core = get_core_client()
    result = safe(lambda: core.read_namespaced_pod(pod_name, namespace))
    if isinstance(result, str):
        return result

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
        info["conditions"].append({
            "type": cond.type,
            "status": cond.status,
            "reason": cond.reason,
            "message": cond.message,
        })

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
        info["containers"].append({
            "name": cs.name,
            "image": cs.image,
            "ready": cs.ready,
            "restarts": cs.restart_count,
            "state": state,
            "reason": reason,
        })

    events = safe(lambda: core.list_namespaced_event(
        namespace,
        field_selector=f"involvedObject.name={pod_name},involvedObject.kind=Pod",
    ))
    if not isinstance(events, str):
        info["recent_events"] = [
            {"type": e.type, "reason": e.reason, "message": e.message, "age": age(e.last_timestamp)}
            for e in sorted(events.items, key=lambda e: e.last_timestamp or datetime.min.replace(tzinfo=timezone.utc), reverse=True)[:10]
        ]

    return json.dumps(info, indent=2, default=str)


@beta_tool
def get_pod_logs(namespace: str, pod_name: str, container: str = "", tail_lines: int = 100, previous: bool = False) -> str:
    """Get logs from a pod container.

    Args:
        namespace: Kubernetes namespace.
        pod_name: Name of the pod.
        container: Container name (required for multi-container pods, optional for single-container).
        tail_lines: Number of recent log lines to retrieve (max 1000).
        previous: If True, get logs from the previous terminated container instance.
    """
    tail_lines = min(max(1, tail_lines), MAX_TAIL_LINES)
    kwargs: dict = {"name": pod_name, "namespace": namespace, "tail_lines": tail_lines, "previous": previous}
    if container:
        kwargs["container"] = container
    result = safe(lambda: get_core_client().read_namespaced_pod_log(**kwargs))
    if isinstance(result, str) and result.startswith("Error"):
        return result
    return result or "(empty logs)"


@beta_tool
def list_nodes() -> str:
    """List all nodes with their status, roles, version, and resource capacity."""
    result = safe(lambda: get_core_client().list_node())
    if isinstance(result, str):
        return result

    lines = []
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
            f"CPU(cap/alloc)={cap.get('cpu','?')}/{alloc.get('cpu','?')}  "
            f"Mem(cap/alloc)={cap.get('memory','?')}/{alloc.get('memory','?')}  "
            f"Version={node.status.node_info.kubelet_version}  "
            f"Age={age(node.metadata.creation_timestamp)}"
        )
    return "\n".join(lines) or "No nodes found."


@beta_tool
def describe_node(node_name: str) -> str:
    """Get detailed information about a node including conditions, taints, and resource usage.

    Args:
        node_name: Name of the node.
    """
    result = safe(lambda: get_core_client().read_node(node_name))
    if isinstance(result, str):
        return result

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
        "taints": [
            {"key": t.key, "value": t.value, "effect": t.effect}
            for t in node.spec.taints or []
        ],
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
def get_events(namespace: str = "default", resource_kind: str = "", resource_name: str = "", event_type: str = "") -> str:
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
    if isinstance(result, str):
        return result

    events = sorted(
        result.items,
        key=lambda e: e.last_timestamp or datetime.min.replace(tzinfo=timezone.utc),
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
        rows.append({
            "age": age(e.last_timestamp) + " ago",
            "type": e.type or "Normal",
            "reason": e.reason or "",
            "resource": f"{e.involved_object.kind}/{e.involved_object.name}",
            "message": (e.message or "")[:120],
        })
    text = "\n".join(lines) or "No events found."
    component = {
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
    } if rows else None
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
    if isinstance(result, str):
        return result

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
        rows.append({
            "namespace": dep.metadata.namespace,
            "name": dep.metadata.name,
            "ready": f"{ready}/{desired}",
            "status": "Healthy" if ready == desired and desired > 0 else ("Degraded" if ready > 0 else "Unavailable"),
            "updated": s.updated_replicas or 0,
            "available": s.available_replicas or 0,
            "age": age(dep.metadata.creation_timestamp),
        })
    text = "\n".join(lines) or "No deployments found."
    component = {
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
    } if rows else None
    return (text, component)


@beta_tool
def describe_deployment(namespace: str, name: str) -> str:
    """Get detailed information about a deployment including strategy, conditions, and pod template.

    Args:
        namespace: Kubernetes namespace.
        name: Name of the deployment.
    """
    result = safe(lambda: get_apps_client().read_namespaced_deployment(name, namespace))
    if isinstance(result, str):
        return result

    dep = result
    containers = []
    for c in dep.spec.template.spec.containers:
        containers.append({
            "name": c.name,
            "image": c.image,
            "resources": {
                "requests": dict(c.resources.requests or {}) if c.resources else {},
                "limits": dict(c.resources.limits or {}) if c.resources else {},
            },
            "ports": [{"port": p.container_port, "protocol": p.protocol} for p in (c.ports or [])],
        })

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
    return json.dumps(info, indent=2, default=str)


@beta_tool
def get_resource_quotas(namespace: str = "default") -> str:
    """Get resource quotas and current usage for a namespace.

    Args:
        namespace: Kubernetes namespace.
    """
    result = safe(lambda: get_core_client().list_namespaced_resource_quota(namespace))
    if isinstance(result, str):
        return result

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
    if isinstance(result, str):
        return result

    lines = []
    for svc in result.items[:MAX_RESULTS]:
        ports = ", ".join(
            f"{p.port}/{p.protocol}" + (f"→{p.target_port}" if p.target_port else "")
            for p in (svc.spec.ports or [])
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
    if isinstance(result, str):
        return result

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
    if isinstance(result, str):
        return result

    info = f"Kubernetes {result.git_version} (Platform: {result.platform})"

    try:
        cv = get_custom_client().get_cluster_custom_object(
            "config.openshift.io", "v1", "clusterversions", "version"
        )
        ocp_version = cv.get("status", {}).get("desired", {}).get("version", "unknown")
        channel = cv.get("spec", {}).get("channel", "unknown")
        conditions = cv.get("status", {}).get("conditions", [])
        cond_summary = ", ".join(
            f"{c['type']}={c['status']}" for c in conditions
        )
        info += f"\nOpenShift {ocp_version} (Channel: {channel})"
        info += f"\nConditions: {cond_summary}"
    except ApiException:
        pass

    return info


@beta_tool
def get_cluster_operators() -> str:
    """List OpenShift ClusterOperators and their status (Available, Progressing, Degraded). Only works on OpenShift clusters."""
    try:
        result = get_custom_client().list_cluster_custom_object(
            "config.openshift.io", "v1", "clusteroperators"
        )
    except ApiException as e:
        return f"Error ({e.status}): {e.reason}. This may not be an OpenShift cluster."

    lines = []
    for co in result.get("items", []):
        name = co["metadata"]["name"]
        conditions = {c["type"]: c["status"] for c in co.get("status", {}).get("conditions", [])}
        lines.append(
            f"{name}  Available={conditions.get('Available','?')}  "
            f"Progressing={conditions.get('Progressing','?')}  "
            f"Degraded={conditions.get('Degraded','?')}"
        )
    return "\n".join(lines) or "No ClusterOperators found."


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
    if err := _validate_k8s_namespace(namespace): return err
    if err := _validate_k8s_name(name): return err
    replicas = min(max(0, replicas), MAX_REPLICAS)
    result = safe(lambda: get_apps_client().patch_namespaced_deployment_scale(
        name, namespace, body={"spec": {"replicas": replicas}}
    ))
    if isinstance(result, str):
        return result
    return f"Scaled {namespace}/{name} to {replicas} replicas."


@beta_tool
def restart_deployment(namespace: str, name: str) -> str:
    """Trigger a rolling restart of a deployment. REQUIRES USER CONFIRMATION.

    Args:
        namespace: Kubernetes namespace.
        name: Name of the deployment to restart.
    """
    if err := _validate_k8s_namespace(namespace): return err
    if err := _validate_k8s_name(name): return err
    now = datetime.now(timezone.utc).isoformat()
    body = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {"kubectl.kubernetes.io/restartedAt": now}
                }
            }
        }
    }
    result = safe(lambda: get_apps_client().patch_namespaced_deployment(name, namespace, body=body))
    if isinstance(result, str):
        return result
    return f"Rolling restart triggered for {namespace}/{name}."


@beta_tool
def cordon_node(node_name: str) -> str:
    """Mark a node as unschedulable (cordon). REQUIRES USER CONFIRMATION.

    Args:
        node_name: Name of the node to cordon.
    """
    if err := _validate_k8s_name(node_name, "node_name"): return err
    result = safe(lambda: get_core_client().patch_node(node_name, body={"spec": {"unschedulable": True}}))
    if isinstance(result, str):
        return result
    return f"Node {node_name} cordoned (marked unschedulable)."


@beta_tool
def uncordon_node(node_name: str) -> str:
    """Mark a node as schedulable (uncordon). REQUIRES USER CONFIRMATION.

    Args:
        node_name: Name of the node to uncordon.
    """
    if err := _validate_k8s_name(node_name, "node_name"): return err
    result = safe(lambda: get_core_client().patch_node(node_name, body={"spec": {"unschedulable": False}}))
    if isinstance(result, str):
        return result
    return f"Node {node_name} uncordoned (marked schedulable)."


@beta_tool
def delete_pod(namespace: str, pod_name: str, grace_period_seconds: int = 30) -> str:
    """Delete a pod (it will be recreated by its controller if one exists). REQUIRES USER CONFIRMATION.

    Args:
        namespace: Kubernetes namespace.
        pod_name: Name of the pod to delete.
        grace_period_seconds: Grace period before force killing (1-300).
    """
    if err := _validate_k8s_namespace(namespace): return err
    if err := _validate_k8s_name(pod_name, "pod_name"): return err
    grace_period_seconds = min(max(1, grace_period_seconds), 300)
    result = safe(lambda: get_core_client().delete_namespaced_pod(
        pod_name, namespace,
        body=client.V1DeleteOptions(grace_period_seconds=grace_period_seconds),
    ))
    if isinstance(result, str):
        return result
    return f"Pod {namespace}/{pod_name} deleted."


@beta_tool
def get_configmap(namespace: str, name: str) -> str:
    """Get the contents of a ConfigMap.

    Args:
        namespace: Kubernetes namespace.
        name: Name of the ConfigMap.
    """
    result = safe(lambda: get_core_client().read_namespaced_config_map(name, namespace))
    if isinstance(result, str):
        return result
    data = result.data or {}
    info = {"name": result.metadata.name, "namespace": result.metadata.namespace, "data": data}
    return json.dumps(info, indent=2, default=str)


# ---------------------------------------------------------------------------
# Metrics API tools (require metrics-server)
# ---------------------------------------------------------------------------


@beta_tool
def get_node_metrics() -> str:
    """Get actual CPU and memory usage for all nodes from the metrics API. Requires metrics-server to be installed."""
    from .units import parse_cpu_millicores, parse_memory_bytes, format_cpu, format_memory

    try:
        result = get_custom_client().list_cluster_custom_object(
            _METRICS_GROUP, _METRICS_VERSION, "nodes"
        )
    except ApiException as e:
        if e.status == 404:
            return "Error: Metrics API not available. Is metrics-server installed?"
        return f"Error ({e.status}): {e.reason}"

    # Get node capacity for utilization %
    nodes_result = safe(lambda: get_core_client().list_node())
    capacity_map = {}
    if not isinstance(nodes_result, str):
        for node in nodes_result.items:
            alloc = node.status.allocatable or {}
            capacity_map[node.metadata.name] = {
                "cpu_m": parse_cpu_millicores(alloc.get("cpu", "0")),
                "mem_bytes": parse_memory_bytes(alloc.get("memory", "0")),
            }

    lines = []
    for item in result.get("items", []):
        name = item["metadata"]["name"]
        usage = item.get("usage", {})
        cpu_m = parse_cpu_millicores(usage.get("cpu", "0"))
        mem_bytes = parse_memory_bytes(usage.get("memory", "0"))

        pct = ""
        if name in capacity_map:
            cap = capacity_map[name]
            cpu_pct = (cpu_m / cap["cpu_m"] * 100) if cap["cpu_m"] > 0 else 0
            mem_pct = (mem_bytes / cap["mem_bytes"] * 100) if cap["mem_bytes"] > 0 else 0
            pct = f"  CPU%={cpu_pct:.0f}%  Mem%={mem_pct:.0f}%"

        lines.append(f"{name}  CPU={format_cpu(cpu_m)}  Memory={format_memory(mem_bytes)}{pct}")

    return "\n".join(lines) or "No node metrics found."


@beta_tool
def get_pod_metrics(namespace: str = "default", sort_by: str = "cpu") -> str:
    """Get actual CPU and memory usage for pods from the metrics API. Requires metrics-server.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' for all namespaces.
        sort_by: Sort results by 'cpu' or 'memory'. Shows top consumers first.
    """
    from .units import parse_cpu_millicores, parse_memory_bytes, format_cpu, format_memory

    try:
        if namespace.upper() == "ALL":
            result = get_custom_client().list_cluster_custom_object(
                _METRICS_GROUP, _METRICS_VERSION, "pods"
            )
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

        pods.append({
            "ns": ns, "name": name,
            "cpu_m": total_cpu_m, "mem_bytes": total_mem_bytes,
            "cpu_str": format_cpu(total_cpu_m), "mem_str": format_memory(total_mem_bytes),
        })

    if sort_by == "memory":
        pods.sort(key=lambda p: p["mem_bytes"], reverse=True)
    else:
        pods.sort(key=lambda p: p["cpu_m"], reverse=True)

    lines = []
    for p in pods[:MAX_RESULTS]:
        lines.append(f"{p['ns']}/{p['name']}  CPU={p['cpu_str']}  Memory={p['mem_str']}")
    total = len(pods)
    if total > MAX_RESULTS:
        lines.append(f"... and {total - MAX_RESULTS} more pods (truncated)")

    return "\n".join(lines) or "No pod metrics found."


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
    if isinstance(result, str):
        return result

    lines = []
    for sts in result.items[:MAX_RESULTS]:
        s = sts.status
        lines.append(
            f"{sts.metadata.namespace}/{sts.metadata.name}  "
            f"Ready={s.ready_replicas or 0}/{s.replicas or 0}  "
            f"Updated={s.updated_replicas or 0}  "
            f"Age={age(sts.metadata.creation_timestamp)}"
        )
    return "\n".join(lines) or "No StatefulSets found."


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
    if isinstance(result, str):
        return result

    lines = []
    for ds in result.items[:MAX_RESULTS]:
        s = ds.status
        lines.append(
            f"{ds.metadata.namespace}/{ds.metadata.name}  "
            f"Desired={s.desired_number_scheduled}  "
            f"Ready={s.number_ready or 0}  "
            f"Available={s.number_available or 0}  "
            f"Misscheduled={s.number_misscheduled or 0}  "
            f"Age={age(ds.metadata.creation_timestamp)}"
        )
    return "\n".join(lines) or "No DaemonSets found."


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
    if isinstance(result, str):
        return result

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
    if isinstance(result, str):
        return result

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
    if isinstance(result, str):
        return result

    lines = []
    for ing in result.items[:MAX_RESULTS]:
        hosts = []
        for rule in ing.spec.rules or []:
            host = rule.host or "*"
            paths = []
            for p in (rule.http.paths if rule.http else []):
                backend = f"{p.backend.service.name}:{p.backend.service.port.number or p.backend.service.port.name}" if p.backend.service else "?"
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
            result = get_custom_client().list_cluster_custom_object(
                "route.openshift.io", "v1", "routes"
            )
        else:
            result = get_custom_client().list_namespaced_custom_object(
                "route.openshift.io", "v1", namespace, "routes"
            )
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
    if isinstance(result, str):
        return result

    lines = []
    for hpa in result.items[:MAX_RESULTS]:
        s = hpa.status
        ref = hpa.spec.scale_target_ref
        target = f"{ref.kind}/{ref.name}"

        metrics_str = []
        for mc in hpa.status.current_metrics or []:
            if mc.type == "Resource" and mc.resource:
                current = mc.resource.current.average_utilization
                metrics_str.append(f"{mc.resource.name}={current}%")

        lines.append(
            f"{hpa.metadata.namespace}/{hpa.metadata.name}  "
            f"Target={target}  "
            f"Replicas={s.current_replicas or 0}/{hpa.spec.min_replicas or 1}-{hpa.spec.max_replicas}  "
            f"Metrics=[{', '.join(metrics_str) or 'none'}]  "
            f"Age={age(hpa.metadata.creation_timestamp)}"
        )
    return "\n".join(lines) or "No HPAs found."


@beta_tool
def list_operator_subscriptions(namespace: str = "ALL") -> str:
    """List OLM Operator Subscriptions showing installed operators, their channels, and install plans. OpenShift only.

    Args:
        namespace: Namespace to check. Use 'ALL' for all namespaces.
    """
    try:
        if namespace.upper() == "ALL":
            result = get_custom_client().list_cluster_custom_object(
                "operators.coreos.com", "v1alpha1", "subscriptions"
            )
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
    import urllib.request
    import urllib.error

    # Try OpenShift alertmanager proxy first
    urls = [
        "https://localhost:9093/api/v2/alerts",
        "http://alertmanager-main.openshift-monitoring.svc:9093/api/v2/alerts",
    ]

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
    for alert in sorted(firing, key=lambda a: a.get("labels", {}).get("severity", ""), reverse=True):
        labels = alert.get("labels", {})
        annotations = alert.get("annotations", {})
        name = labels.get("alertname", "unknown")
        severity = labels.get("severity", "?")
        ns = labels.get("namespace", "cluster-wide")
        summary = annotations.get("summary", annotations.get("message", annotations.get("description", "")))[:200]
        starts = alert.get("startsAt", "?")[:19]

        lines.append(
            f"[{severity.upper()}] {name}  namespace={ns}  since={starts}\n"
            f"  {summary}"
        )

    return f"Firing alerts ({len(firing)}):\n\n" + "\n\n".join(lines)


@beta_tool
def get_prometheus_query(query: str, time_range: str = "") -> str:
    """Execute a PromQL query against Prometheus/Thanos and return the results.

    Args:
        query: PromQL query string, e.g. 'up', 'node_memory_MemAvailable_bytes', 'rate(container_cpu_usage_seconds_total[5m])'.
        time_range: Optional time range for range queries (e.g. '5m', '1h', '24h'). If empty, does an instant query.
    """
    import os
    import urllib.request
    import urllib.error
    import urllib.parse

    # Sanitize query
    if any(c in query for c in [';', '\\', '\n', '\r']):
        return "Error: Invalid characters in query."

    base_url = os.environ.get("THANOS_URL", "")
    if not base_url:
        # Try OpenShift Thanos
        base_url = "https://thanos-querier.openshift-monitoring.svc:9091"

    if time_range:
        # Range query
        endpoint = f"{base_url}/api/v1/query_range"
        params = urllib.parse.urlencode({
            "query": query,
            "start": f"now-{time_range}",
            "end": "now",
            "step": "60",
        })
    else:
        # Instant query
        endpoint = f"{base_url}/api/v1/query"
        params = urllib.parse.urlencode({"query": query})

    # Try service proxy first (in-cluster)
    core = get_core_client()
    try:
        path = f"api/v1/query?{params}" if not time_range else f"api/v1/query_range?{params}"
        result = core.connect_get_namespaced_service_proxy_with_path(
            "thanos-querier:web",
            "openshift-monitoring",
            path=path,
            _preload_content=False,
        )
        data = json.loads(result.data)
    except Exception:
        return f"Cannot reach Prometheus/Thanos. Set THANOS_URL environment variable."

    if data.get("status") != "success":
        return f"Query error: {data.get('error', 'unknown')}"

    result_type = data.get("data", {}).get("resultType", "")
    results = data.get("data", {}).get("result", [])

    if not results:
        return f"Query returned no results for: {query}"

    lines = []
    for r in results[:50]:
        metric = r.get("metric", {})
        label_str = ", ".join(f"{k}={v}" for k, v in metric.items() if k != "__name__")
        name = metric.get("__name__", query)

        if result_type == "vector":
            ts, val = r.get("value", [0, "?"])
            lines.append(f"{name}{{{label_str}}} = {val}")
        elif result_type == "matrix":
            values = r.get("values", [])
            latest = values[-1] if values else [0, "?"]
            lines.append(f"{name}{{{label_str}}} = {latest[1]} (latest of {len(values)} samples)")

    if len(results) > 50:
        lines.append(f"... and {len(results) - 50} more results (truncated)")

    return "\n".join(lines)


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
    if isinstance(result, str):
        return result

    svc = result
    info = {
        "name": svc.metadata.name,
        "namespace": svc.metadata.namespace,
        "type": svc.spec.type,
        "clusterIP": svc.spec.cluster_ip,
        "selector": svc.spec.selector or {},
        "ports": [
            {"name": p.name, "port": p.port, "targetPort": str(p.target_port), "protocol": p.protocol, "nodePort": p.node_port}
            for p in (svc.spec.ports or [])
        ],
        "externalIPs": svc.spec.external_i_ps or [],
        "sessionAffinity": svc.spec.session_affinity,
    }

    # Get endpoints
    ep_result = safe(lambda: core.read_namespaced_endpoints(name, namespace))
    if not isinstance(ep_result, str):
        endpoints = []
        for subset in ep_result.subsets or []:
            addrs = [a.ip + (f" ({a.target_ref.name})" if a.target_ref else "") for a in (subset.addresses or [])]
            not_ready = [a.ip + (f" ({a.target_ref.name})" if a.target_ref else "") for a in (subset.not_ready_addresses or [])]
            ports = [f"{p.port}/{p.protocol}" for p in (subset.ports or [])]
            endpoints.append({"ready": addrs, "notReady": not_ready, "ports": ports})
        info["endpoints"] = endpoints

    # Count matching pods
    if svc.spec.selector:
        label_sel = ",".join(f"{k}={v}" for k, v in svc.spec.selector.items())
        pods = safe(lambda: core.list_namespaced_pod(namespace, label_selector=label_sel))
        if not isinstance(pods, str):
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
            "discovery.k8s.io", "v1", namespace, "endpointslices",
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
    if isinstance(result, str):
        return result

    rsets = result.items
    if deployment_name:
        rsets = [
            rs for rs in rsets
            if any(
                ref.kind == "Deployment" and ref.name == deployment_name
                for ref in (rs.metadata.owner_references or [])
            )
        ]

    if not rsets:
        return f"No ReplicaSets found{' for deployment ' + deployment_name if deployment_name else ''}."

    # Sort by creation (newest first) for rollout history
    rsets.sort(key=lambda rs: rs.metadata.creation_timestamp or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

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
    if isinstance(result, str):
        return result

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
    if isinstance(result, str):
        return result

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
    if isinstance(result, str):
        return result

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
        rows.append({
            "restarts": restarts,
            "namespace": pod.metadata.namespace,
            "name": pod.metadata.name,
            "status": pod.status.phase or "Unknown",
            "age": age(pod.metadata.creation_timestamp),
        })
    text = "\n".join(lines)
    component = {
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
    } if rows else None
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

    cutoff = datetime.now(timezone.utc).replace(microsecond=0)
    cutoff_str = (cutoff - __import__('datetime').timedelta(minutes=minutes)).isoformat() + "Z"

    lines = []

    # Recent events (Warning and Normal)
    if namespace.upper() == "ALL":
        events_result = safe(lambda: core.list_event_for_all_namespaces())
    else:
        events_result = safe(lambda: core.list_namespaced_event(namespace))

    if not isinstance(events_result, str):
        recent_events = [
            e for e in events_result.items
            if e.last_timestamp and e.last_timestamp.replace(tzinfo=timezone.utc) >= cutoff - __import__('datetime').timedelta(minutes=minutes)
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

    if not isinstance(deps_result, str):
        recently_updated = []
        for dep in deps_result.items:
            for cond in dep.status.conditions or []:
                if cond.type == "Progressing" and cond.last_update_time:
                    if cond.last_update_time.replace(tzinfo=timezone.utc) >= cutoff - __import__('datetime').timedelta(minutes=minutes):
                        recently_updated.append(dep)
                        break

        if recently_updated:
            lines.append(f"\nDeployments updated in last {minutes}m ({len(recently_updated)}):")
            for dep in recently_updated[:10]:
                s = dep.status
                lines.append(
                    f"  {dep.metadata.namespace}/{dep.metadata.name}  "
                    f"Ready={s.ready_replicas or 0}/{s.replicas or 0}"
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
        result = safe(lambda: core.list_secret_for_all_namespaces(
            field_selector="type=kubernetes.io/tls", limit=MAX_RESULTS,
        ))
    else:
        result = safe(lambda: core.list_namespaced_secret(
            namespace, field_selector="type=kubernetes.io/tls", limit=MAX_RESULTS,
        ))
    if isinstance(result, str):
        return result

    if not result.items:
        return "No TLS secrets found."

    now = datetime.now(timezone.utc)
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
            certs.append({
                "namespace": secret.metadata.namespace,
                "name": secret.metadata.name,
                "cn": cn,
                "expires": not_after.strftime("%Y-%m-%d"),
                "days_left": days_left,
                "status": status,
            })
        except Exception:
            certs.append({
                "namespace": secret.metadata.namespace,
                "name": secret.metadata.name,
                "cn": "parse-error",
                "expires": "unknown",
                "days_left": -1,
                "status": "UNKNOWN",
            })

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
    if err := _validate_k8s_namespace(namespace): return err
    if err := _validate_k8s_name(name): return err
    # Get current ReplicaSets to find the target revision
    apps = get_apps_client()
    rs_result = safe(lambda: apps.list_namespaced_replica_set(namespace))
    if isinstance(rs_result, str):
        return rs_result

    # Find ReplicaSets owned by this deployment
    owned = [
        rs for rs in rs_result.items
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
            (rs for rs in owned if (rs.metadata.annotations or {}).get("deployment.kubernetes.io/revision") == str(revision)),
            None,
        )
        if not target:
            available = [
                (rs.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "?")
                for rs in owned
            ]
            return f"Revision {revision} not found. Available: {', '.join(available)}"

    # Get the target's pod template and patch the deployment
    target_template = target.spec.template
    target_rev = (target.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "?")
    target_image = target_template.spec.containers[0].image if target_template.spec.containers else "?"

    body = {"spec": {"template": target_template.to_dict()}}
    result = safe(lambda: apps.patch_namespaced_deployment(name, namespace, body))
    if isinstance(result, str):
        return result

    return f"Rolled back {namespace}/{name} to revision {target_rev} (image: {target_image.split('/')[-1]})."


@beta_tool
def drain_node(node_name: str) -> str:
    """Cordon a node and evict all pods (respecting PDBs). REQUIRES USER CONFIRMATION.

    This cordons the node first, then evicts pods one by one. Pods managed by
    DaemonSets are skipped. Pods with PodDisruptionBudgets are respected.

    Args:
        node_name: Name of the node to drain.
    """
    if err := _validate_k8s_name(node_name, "node_name"): return err
    core = get_core_client()

    # Step 1: Cordon
    cordon_result = safe(lambda: core.patch_node(node_name, body={"spec": {"unschedulable": True}}))
    if isinstance(cordon_result, str):
        return f"Failed to cordon: {cordon_result}"

    # Step 2: List pods on the node
    pods_result = safe(lambda: core.list_pod_for_all_namespaces(field_selector=f"spec.nodeName={node_name}"))
    if isinstance(pods_result, str):
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

    if err := _validate_k8s_name(name): return err
    if err := _validate_k8s_namespace(ns): return err

    # Allowlist — only these resource types can be created/modified via apply_yaml.
    # Everything else is blocked to prevent privilege escalation.
    _ALLOWED_KINDS = {
        "Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob",
        "Service", "ConfigMap", "Ingress",
        "NetworkPolicy", "HorizontalPodAutoscaler",
        "LimitRange", "ResourceQuota",
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
        group, version = api_version.split("/", 1)
        base = f"/apis/{api_version}"
    else:
        base = f"/api/{api_version}"

    # Simple kind→plural (covers common cases)
    plural_map = {
        "Deployment": "deployments", "Service": "services", "ConfigMap": "configmaps",
        "Secret": "secrets", "Namespace": "namespaces", "Pod": "pods",
        "ServiceAccount": "serviceaccounts", "Role": "roles", "RoleBinding": "rolebindings",
        "ClusterRole": "clusterroles", "ClusterRoleBinding": "clusterrolebindings",
        "NetworkPolicy": "networkpolicies", "Ingress": "ingresses", "Job": "jobs",
        "CronJob": "cronjobs", "StatefulSet": "statefulsets", "DaemonSet": "daemonsets",
        "PersistentVolumeClaim": "persistentvolumeclaims", "HorizontalPodAutoscaler": "horizontalpodautoscalers",
        "LimitRange": "limitranges", "ResourceQuota": "resourcequotas",
    }
    plural = plural_map.get(kind, kind.lower() + "s")

    # Use server-side apply
    from kubernetes import client as k8s_client
    api = k8s_client.ApiClient()

    dry_run_param = "All" if dry_run else None
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
            query_params=[("fieldManager", "pulse-agent")]
            + ([("dryRun", "All")] if dry_run else []),
            _preload_content=False,
        )
        result = json.loads(resp[0].data)
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
    dry_result = safe(lambda: get_networking_client().create_namespaced_network_policy(
        namespace, body, dry_run="All"
    ))
    if isinstance(dry_result, str):
        return f"Dry-run failed: {dry_result}"

    # Apply for real
    result = safe(lambda: get_networking_client().create_namespaced_network_policy(namespace, body))
    if isinstance(result, str):
        return result
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
    now = datetime.now(timezone.utc)
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


ALL_TOOLS = [
    # Read diagnostics
    list_namespaces,
    list_pods,
    describe_pod,
    get_pod_logs,
    list_nodes,
    describe_node,
    get_events,
    list_deployments,
    describe_deployment,
    get_resource_quotas,
    get_services,
    get_persistent_volume_claims,
    get_cluster_version,
    get_cluster_operators,
    get_configmap,
    get_node_metrics,
    get_pod_metrics,
    # New diagnostics
    list_statefulsets,
    list_daemonsets,
    list_jobs,
    list_cronjobs,
    list_ingresses,
    list_routes,
    list_hpas,
    list_operator_subscriptions,
    get_firing_alerts,
    get_prometheus_query,
    # Sysadmin-requested diagnostics
    describe_service,
    get_endpoint_slices,
    list_replicasets,
    get_pod_disruption_budgets,
    list_limit_ranges,
    top_pods_by_restarts,
    get_recent_changes,
    get_tls_certificates,
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
    # Audit
    record_audit_entry,
]
