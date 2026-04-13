"""Deployment-related Kubernetes tools."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from .. import k8s_client as _kc
from ..decorators import beta_tool
from ..errors import ToolError
from .validators import MAX_RESULTS, _validate_k8s_name, _validate_k8s_namespace


@beta_tool
def list_deployments(namespace: str = "default"):
    """List deployments with their replica counts and status.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' for all namespaces.
    """
    apps = _kc.get_apps_client()
    if namespace.upper() == "ALL":
        result = _kc.safe(lambda: apps.list_deployment_for_all_namespaces(limit=MAX_RESULTS))
    else:
        result = _kc.safe(lambda: apps.list_namespaced_deployment(namespace, limit=MAX_RESULTS))
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
            f"Age={_kc.age(dep.metadata.creation_timestamp)}"
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
                "age": _kc.age(dep.metadata.creation_timestamp),
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
def describe_deployment(namespace: str, name: str):
    """Get detailed information about a deployment including strategy, conditions, and pod template.

    Args:
        namespace: Kubernetes namespace.
        name: Name of the deployment.
    """
    result = _kc.safe(lambda: _kc.get_apps_client().read_namespaced_deployment(name, namespace))
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
                {"key": "Age", "value": _kc.age(dep.metadata.creation_timestamp)},
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
def scale_deployment(namespace: str, name: str, replicas: int):
    """Scale a deployment to a specific number of replicas. REQUIRES USER CONFIRMATION.

    Args:
        namespace: Kubernetes namespace.
        name: Name of the deployment to scale.
        replicas: Desired number of replicas (0-100).
    """
    MAX_REPLICAS = 100
    if err := _validate_k8s_namespace(namespace):
        return err
    if err := _validate_k8s_name(name):
        return err
    replicas = min(max(0, replicas), MAX_REPLICAS)
    result = _kc.safe(
        lambda: _kc.get_apps_client().patch_namespaced_deployment_scale(
            name, namespace, body={"spec": {"replicas": replicas}}
        )
    )
    if isinstance(result, ToolError):
        return str(result)
    return f"Scaled {namespace}/{name} to {replicas} replicas."


@beta_tool
def restart_deployment(namespace: str, name: str):
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
    result = _kc.safe(lambda: _kc.get_apps_client().patch_namespaced_deployment(name, namespace, body=body))
    if isinstance(result, ToolError):
        return str(result)
    return f"Rolling restart triggered for {namespace}/{name}."


@beta_tool
def rollback_deployment(namespace: str, name: str, revision: int = 0):
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
    apps = _kc.get_apps_client()
    rs_result = _kc.safe(lambda: apps.list_namespaced_replica_set(namespace))
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
    result = _kc.safe(lambda: apps.patch_namespaced_deployment(name, namespace, body))
    if isinstance(result, ToolError):
        return str(result)

    return f"Rolled back {namespace}/{name} to revision {target_rev} (image: {target_image.split('/')[-1]})."


@beta_tool
def list_replicasets(namespace: str, deployment_name: str = ""):
    """List ReplicaSets, optionally filtered to a specific deployment's rollout history.

    Args:
        namespace: Kubernetes namespace.
        deployment_name: If provided, show only ReplicaSets owned by this deployment (rollout history).
    """
    apps = _kc.get_apps_client()
    result = _kc.safe(lambda: apps.list_namespaced_replica_set(namespace))
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
            f"Age={_kc.age(rs.metadata.creation_timestamp)}"
        )
    return "\n".join(lines)
