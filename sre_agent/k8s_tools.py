"""Kubernetes/OpenShift tools for the SRE agent.

Each tool is decorated with @beta_tool so the Anthropic SDK automatically
generates JSON schemas and the tool runner can execute them.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from anthropic import beta_tool
from kubernetes import client
from kubernetes.client.rest import ApiException

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
    result = safe(lambda: get_core_client().list_namespace())
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
        result = safe(lambda: core.list_pod_for_all_namespaces(**kwargs))
    else:
        result = safe(lambda: core.list_namespaced_pod(namespace, **kwargs))
    if isinstance(result, str):
        return result

    lines = []
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
    total = len(result.items)
    if total > MAX_RESULTS:
        lines.append(f"... and {total - MAX_RESULTS} more pods (truncated)")
    return "\n".join(lines) or "No pods found."


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
    for e in events:
        lines.append(
            f"{age(e.last_timestamp)} ago  {e.type}  {e.reason}  "
            f"{e.involved_object.kind}/{e.involved_object.name}  "
            f"{e.message}"
        )
    return "\n".join(lines) or "No events found."


@beta_tool
def list_deployments(namespace: str = "default") -> str:
    """List deployments with their replica counts and status.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' for all namespaces.
    """
    apps = get_apps_client()
    if namespace.upper() == "ALL":
        result = safe(lambda: apps.list_deployment_for_all_namespaces())
    else:
        result = safe(lambda: apps.list_namespaced_deployment(namespace))
    if isinstance(result, str):
        return result

    lines = []
    for dep in result.items[:MAX_RESULTS]:
        s = dep.status
        lines.append(
            f"{dep.metadata.namespace}/{dep.metadata.name}  "
            f"Ready={s.ready_replicas or 0}/{s.replicas or 0}  "
            f"Updated={s.updated_replicas or 0}  "
            f"Available={s.available_replicas or 0}  "
            f"Age={age(dep.metadata.creation_timestamp)}"
        )
    return "\n".join(lines) or "No deployments found."


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
    # Write operations
    scale_deployment,
    restart_deployment,
    cordon_node,
    uncordon_node,
    delete_pod,
    apply_yaml,
    create_network_policy,
    # Audit
    record_audit_entry,
]
