"""Pod-related Kubernetes tools."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from anthropic import beta_tool

from .. import k8s_client as _kc
from ..errors import ToolError
from .validators import MAX_RESULTS, _validate_k8s_name, _validate_k8s_namespace

MAX_TAIL_LINES = 1000


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

    core = _kc.get_core_client()
    if namespace.upper() == "ALL":
        result = _kc.safe(lambda: core.list_pod_for_all_namespaces(limit=MAX_RESULTS, **kwargs))
    else:
        result = _kc.safe(lambda: core.list_namespaced_pod(namespace, limit=MAX_RESULTS, **kwargs))
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
            f"Restarts={restarts}  Age={_kc.age(pod.metadata.creation_timestamp)}"
        )
        rows.append(
            {
                "_gvr": "v1~pods",
                "namespace": ns,
                "name": pod.metadata.name,
                "status": pod.status.phase or "Unknown",
                "restarts": restarts,
                "age": _kc.age(pod.metadata.creation_timestamp),
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
    core = _kc.get_core_client()
    result = _kc.safe(lambda: core.read_namespaced_pod(pod_name, namespace))
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

    events = _kc.safe(
        lambda: core.list_namespaced_event(
            namespace,
            field_selector=f"involvedObject.name={pod_name},involvedObject.kind=Pod",
        )
    )
    if not isinstance(events, ToolError):
        info["recent_events"] = [
            {"type": e.type, "reason": e.reason, "message": e.message, "age": _kc.age(e.last_timestamp)}
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
    result = _kc.safe(lambda: _kc.get_core_client().read_namespaced_pod_log(**kwargs))
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
def delete_pod(namespace: str, pod_name: str, grace_period_seconds: int = 30) -> str:
    """Delete a pod (it will be recreated by its controller if one exists). REQUIRES USER CONFIRMATION.

    Args:
        namespace: Kubernetes namespace.
        pod_name: Name of the pod to delete.
        grace_period_seconds: Grace period before force killing (1-300).
    """
    from kubernetes import client

    if err := _validate_k8s_namespace(namespace):
        return err
    if err := _validate_k8s_name(pod_name, "pod_name"):
        return err
    grace_period_seconds = min(max(1, grace_period_seconds), 300)
    result = _kc.safe(
        lambda: _kc.get_core_client().delete_namespaced_pod(
            pod_name,
            namespace,
            body=client.V1DeleteOptions(grace_period_seconds=grace_period_seconds),
        )
    )
    if isinstance(result, ToolError):
        return str(result)
    return f"Pod {namespace}/{pod_name} deleted."
