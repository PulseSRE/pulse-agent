"""Node-related Kubernetes tools."""

from __future__ import annotations

import json

from anthropic import beta_tool

from .. import k8s_client as _kc
from ..errors import ToolError
from .validators import _validate_k8s_name


@beta_tool
def list_nodes() -> str:
    """List all nodes with their status, roles, version, and resource capacity."""
    result = _kc.safe(lambda: _kc.get_core_client().list_node())
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
            f"Age={_kc.age(node.metadata.creation_timestamp)}"
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
                "age": _kc.age(node.metadata.creation_timestamp),
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

    result = _kc.safe(lambda: _kc.get_core_client().list_node(**kwargs))
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
                "age": _kc.age(node.metadata.creation_timestamp),
                "instanceType": labels.get("node.kubernetes.io/instance-type", ""),
                "conditions": pressure_conditions,
            }
        )

    # Get pod counts per node
    pods_by_node: dict[str, list[dict]] = {}
    if show_pods:
        pods_result = _kc.safe(lambda: _kc.get_core_client().list_pod_for_all_namespaces(limit=1000))
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

        from ..units import parse_cpu_millicores, parse_memory_bytes

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
    result = _kc.safe(lambda: _kc.get_core_client().read_node(node_name))
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
def cordon_node(node_name: str) -> str:
    """Mark a node as unschedulable (cordon). REQUIRES USER CONFIRMATION.

    Args:
        node_name: Name of the node to cordon.
    """
    if err := _validate_k8s_name(node_name, "node_name"):
        return err
    result = _kc.safe(lambda: _kc.get_core_client().patch_node(node_name, body={"spec": {"unschedulable": True}}))
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
    result = _kc.safe(lambda: _kc.get_core_client().patch_node(node_name, body={"spec": {"unschedulable": False}}))
    if isinstance(result, ToolError):
        return str(result)
    return f"Node {node_name} uncordoned (marked schedulable)."


@beta_tool
def drain_node(node_name: str) -> str:
    """Cordon a node and evict all pods (respecting PDBs). REQUIRES USER CONFIRMATION.

    This cordons the node first, then evicts pods one by one. Pods managed by
    DaemonSets are skipped. Pods with PodDisruptionBudgets are respected.

    Args:
        node_name: Name of the node to drain.
    """
    from kubernetes import client
    from kubernetes.client.rest import ApiException

    if err := _validate_k8s_name(node_name, "node_name"):
        return err
    core = _kc.get_core_client()

    # Step 1: Cordon
    cordon_result = _kc.safe(lambda: core.patch_node(node_name, body={"spec": {"unschedulable": True}}))
    if isinstance(cordon_result, ToolError):
        return f"Failed to cordon: {cordon_result}"

    # Step 2: List pods on the node
    pods_result = _kc.safe(lambda: core.list_pod_for_all_namespaces(field_selector=f"spec.nodeName={node_name}"))
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
