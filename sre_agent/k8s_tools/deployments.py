"""Deployment-related Kubernetes tools."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

logger = logging.getLogger("pulse_agent.k8s_tools")

from .. import k8s_client as _kc
from ..decorators import beta_tool
from ..errors import ToolError
from .generic import _build_k8s_datasource
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
            "datasources": [_build_k8s_datasource("deployments", namespace, group="apps")],
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
        logger.debug("Failed to serialize pod template spec for deployment %s", name, exc_info=True)

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
    # Rejected rather than clamped: a confirmed -1 used to scale to 0 (an
    # outage nobody asked for) and 500 silently became 100.
    if not 0 <= replicas <= MAX_REPLICAS:
        return f"Error: replicas must be between 0 and {MAX_REPLICAS}, got {replicas}. Nothing was changed."
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


# ---------------------------------------------------------------------------
# Rollout history (Deployment ReplicaSets, StatefulSet/DaemonSet ControllerRevisions)
# ---------------------------------------------------------------------------

_ROLLOUT_KINDS = {
    "deployment": ("Deployment", "apps~v1~deployments"),
    "statefulset": ("StatefulSet", "apps~v1~statefulsets"),
    "daemonset": ("DaemonSet", "apps~v1~daemonsets"),
}


def _iso(ts) -> str | None:
    if ts is None:
        return None
    if isinstance(ts, str):
        return ts
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.isoformat().replace("+00:00", "Z")


def _owned_by(obj, uid: str | None, kind: str, name: str) -> bool:
    for ref in obj.metadata.owner_references or []:
        if ref.kind == kind and (ref.uid == uid if uid else ref.name == name):
            return True
    return False


def _template_images(template: dict | None) -> list[str]:
    spec = ((template or {}).get("spec")) or {}
    return [c.get("image", "") for c in (spec.get("containers") or []) if isinstance(c, dict)]


def _cr_template(data) -> dict:
    """The pod template stored in a ControllerRevision's data (a dict, or a JSON string)."""
    if isinstance(data, str | bytes):
        try:
            data = json.loads(data)
        except ValueError:
            return {}
    if not isinstance(data, dict):
        return {}
    return ((data.get("spec") or {}).get("template")) or {}


def _conditions(status) -> list[dict]:
    return [
        {"type": c.type, "status": c.status, "reason": c.reason or "", "message": c.message or ""}
        for c in (getattr(status, "conditions", None) or [])
    ]


@beta_tool
def get_rollout_history(kind: str, namespace: str, name: str):
    """Get the revision history of a Deployment, StatefulSet or DaemonSet, with images per revision.

    Returns each revision (ReplicaSets for a Deployment, ControllerRevisions for a
    StatefulSet/DaemonSet) with its container images, change-cause, ready/desired
    replicas, creation time and which one is current, plus in-flight rollout
    status. Renders a rollout_history timeline. Read-only; use
    rollback_deployment to roll a Deployment back.

    Args:
        kind: Deployment, StatefulSet or DaemonSet.
        namespace: Kubernetes namespace.
        name: Workload name.
    """
    spec_kind = _ROLLOUT_KINDS.get((kind or "").lower())
    if not spec_kind:
        return "Error: kind must be one of Deployment, StatefulSet, DaemonSet."
    err = _validate_k8s_namespace(namespace) or _validate_k8s_name(name)
    if err:
        return err
    kind_name, gvr = spec_kind
    apps = _kc.get_apps_client()

    if kind_name == "Deployment":
        wl = _kc.safe(lambda: apps.read_namespaced_deployment(name, namespace))
    elif kind_name == "StatefulSet":
        wl = _kc.safe(lambda: apps.read_namespaced_stateful_set(name, namespace))
    else:
        wl = _kc.safe(lambda: apps.read_namespaced_daemon_set(name, namespace))
    if isinstance(wl, ToolError):
        return str(wl)
    uid = wl.metadata.uid
    st = wl.status

    revisions: list[dict] = []
    if kind_name == "Deployment":
        result = _kc.safe(lambda: apps.list_namespaced_replica_set(namespace))
        if isinstance(result, ToolError):
            return str(result)
        current_rev = (wl.metadata.annotations or {}).get("deployment.kubernetes.io/revision")
        for rs in result.items:
            if not _owned_by(rs, uid, "Deployment", name):
                continue
            ann = rs.metadata.annotations or {}
            rev = ann.get("deployment.kubernetes.io/revision")
            containers = rs.spec.template.spec.containers if rs.spec and rs.spec.template else []
            revisions.append(
                {
                    "revision": int(rev) if rev and rev.isdigit() else 0,
                    "name": rs.metadata.name,
                    "createdAt": _iso(rs.metadata.creation_timestamp),
                    "images": [c.image for c in containers or []],
                    "changeCause": ann.get("kubernetes.io/change-cause", ""),
                    "replicas": {"ready": rs.status.ready_replicas or 0, "desired": rs.spec.replicas or 0},
                    "current": rev is not None and rev == current_rev,
                    "source": "replicaset",
                }
            )
        strategy = wl.spec.strategy.type if wl.spec and wl.spec.strategy else "RollingUpdate"
        in_flight = {
            "strategy": strategy,
            "updated": st.updated_replicas or 0,
            "ready": st.ready_replicas or 0,
            "available": st.available_replicas or 0,
            "desired": wl.spec.replicas if wl.spec and wl.spec.replicas is not None else 1,
            "conditions": _conditions(st),
        }
    else:
        result = _kc.safe(lambda: apps.list_namespaced_controller_revision(namespace))
        if isinstance(result, ToolError):
            return str(result)
        owned = [cr for cr in result.items if _owned_by(cr, uid, kind_name, name)]
        if kind_name == "StatefulSet":
            current_name = st.update_revision or ""
            ready = st.ready_replicas or 0
            desired = wl.spec.replicas if wl.spec and wl.spec.replicas is not None else 1
            strategy = wl.spec.update_strategy.type if wl.spec and wl.spec.update_strategy else "RollingUpdate"
            in_flight = {
                "strategy": strategy,
                "updated": st.updated_replicas or 0,
                "ready": ready,
                "available": st.available_replicas or 0,
                "desired": desired,
                "conditions": _conditions(st),
            }
        else:
            top = max((cr.revision or 0 for cr in owned), default=0)
            current_name = next((cr.metadata.name for cr in owned if (cr.revision or 0) == top), "")
            ready = st.number_ready or 0
            desired = st.desired_number_scheduled or 0
            strategy = wl.spec.update_strategy.type if wl.spec and wl.spec.update_strategy else "RollingUpdate"
            in_flight = {
                "strategy": strategy,
                "updated": st.updated_number_scheduled or 0,
                "ready": ready,
                "available": st.number_available or 0,
                "desired": desired,
                "conditions": _conditions(st),
            }
        for cr in owned:
            ann = cr.metadata.annotations or {}
            is_current = cr.metadata.name == current_name
            revisions.append(
                {
                    "revision": int(cr.revision or 0),
                    "name": cr.metadata.name,
                    "createdAt": _iso(cr.metadata.creation_timestamp),
                    "images": _template_images(_cr_template(cr.data)),
                    "changeCause": ann.get("kubernetes.io/change-cause", ""),
                    # Replica counts belong to the workload, not a ControllerRevision.
                    "replicas": {"ready": ready, "desired": desired} if is_current else None,
                    "current": is_current,
                    "source": "controllerrevision",
                }
            )

    revisions.sort(key=lambda r: r["revision"])
    if not revisions:
        return f"No revisions found for {kind_name} {namespace}/{name}."

    lines = [f"Rollout history for {kind_name} {namespace}/{name} ({len(revisions)} revisions):"]
    prev_images: list[str] | None = None
    for r in revisions:
        changed = "" if prev_images is None or prev_images == r["images"] else "  (image changed)"
        cause = f"  cause: {r['changeCause']}" if r["changeCause"] else ""
        mark = "  <- current" if r["current"] else ""
        lines.append(f"  rev {r['revision']}  {r['name']}  {', '.join(r['images'])}{changed}{cause}{mark}")
        prev_images = r["images"]
    f = in_flight
    lines.append(
        f"Rollout: {f['updated']}/{f['desired']} updated, {f['ready']} ready, {f['available']} available ({f['strategy']})"
    )
    component = {
        "kind": "rollout_history",
        "title": f"Rollout history — {name}",
        "workload": {"kind": kind_name, "namespace": namespace, "name": name, "gvr": gvr},
        "revisions": revisions,
        "inFlight": in_flight,
    }
    return ("\n".join(lines), component)
