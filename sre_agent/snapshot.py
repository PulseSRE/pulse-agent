"""Capture a restorable copy of a resource before changing it.

Pulse recorded a sentence before every auto-fix:

    before = f"Deployment {name} in {ns}: image={bad_image}, revision={rev}"

That is a description, not a snapshot. You cannot restore from it. Rollback was
additionally limited to the three restart tools and gated on an action reaching
`completed`, which on a trust-2 cluster never happens — so in practice a fix that
made things worse had no mechanical undo, only prose for an operator to act on.

This is the piece Hermes calls a checkpoint: it snapshots the working directory
before touching files so a bad change can be reversed. The cluster equivalent is
the resource's own spec, captured immediately before the write.

What is captured is deliberately narrow — the mutable spec and the metadata
needed to address the object again. Status, resourceVersion, uid and
managedFields are stripped: they are server-owned, meaningless to replay, and
`resourceVersion` in particular would make the restore fail with a conflict.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("pulse_agent.snapshot")

# Server-owned fields. Replaying these either fails outright (resourceVersion
# conflicts) or asserts something untrue about the object's history.
_STRIP_METADATA = (
    "resourceVersion",
    "uid",
    "creationTimestamp",
    "generation",
    "managedFields",
    "selfLink",
    "ownerReferences",
    "finalizers",
)

SUPPORTED_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "ConfigMap")


def _clean_metadata(meta: dict) -> dict:
    out = {k: v for k, v in (meta or {}).items() if k not in _STRIP_METADATA}
    annotations = out.get("annotations") or {}
    # kubectl's last-applied blob is a whole second copy of the object and would
    # double the snapshot for no benefit.
    annotations.pop("kubectl.kubernetes.io/last-applied-configuration", None)
    if annotations:
        out["annotations"] = annotations
    else:
        out.pop("annotations", None)
    return out


def capture(kind: str, name: str, namespace: str) -> dict[str, Any] | None:
    """Snapshot a resource so the change about to be made can be undone.

    Returns None rather than raising: a fix must not be blocked because the
    snapshot failed, but the caller can see it has no undo and say so.
    """
    if kind not in SUPPORTED_KINDS:
        logger.debug("No snapshot support for kind %s", kind)
        return None

    try:
        from .k8s_client import get_apps_client, get_core_client

        if kind == "ConfigMap":
            obj = get_core_client().read_namespaced_config_map(name, namespace)
        else:
            apps = get_apps_client()
            reader = {
                "Deployment": apps.read_namespaced_deployment,
                "StatefulSet": apps.read_namespaced_stateful_set,
                "DaemonSet": apps.read_namespaced_daemon_set,
            }[kind]
            obj = reader(name, namespace)
    except Exception:
        logger.warning("Could not snapshot %s %s/%s", kind, namespace, name, exc_info=True)
        return None

    try:
        from kubernetes.client import ApiClient

        raw = ApiClient().sanitize_for_serialization(obj)
    except Exception:
        logger.warning("Could not serialise snapshot of %s %s/%s", kind, namespace, name, exc_info=True)
        return None

    snapshot = {
        "kind": kind,
        "name": name,
        "namespace": namespace,
        "metadata": _clean_metadata(raw.get("metadata", {})),
    }
    if kind == "ConfigMap":
        snapshot["data"] = raw.get("data", {})
    else:
        snapshot["spec"] = raw.get("spec", {})
    return snapshot


def describe(snapshot: dict[str, Any] | None) -> str:
    """One line naming what a snapshot would restore, for logs and the UI."""
    if not snapshot:
        return "no snapshot — this change cannot be undone automatically"
    return f"{snapshot['kind']} {snapshot['namespace']}/{snapshot['name']} captured before change"


def restore(snapshot: dict[str, Any]) -> str:
    """Put a resource back the way the snapshot found it.

    Raises on failure. A rollback that fails quietly is worse than one that never
    existed, because the operator believes the change was undone.
    """
    if not snapshot:
        raise ValueError("No snapshot to restore from")

    kind = snapshot.get("kind")
    name = snapshot.get("name")
    namespace = snapshot.get("namespace")
    if kind not in SUPPORTED_KINDS or not name or not namespace:
        raise ValueError(f"Snapshot is not restorable: kind={kind!r} name={name!r} namespace={namespace!r}")

    from .k8s_client import get_apps_client, get_core_client

    if kind == "ConfigMap":
        body = {"metadata": snapshot.get("metadata", {}), "data": snapshot.get("data", {})}
        get_core_client().patch_namespaced_config_map(name, namespace, body)
    else:
        body = {"metadata": snapshot.get("metadata", {}), "spec": snapshot.get("spec", {})}
        apps = get_apps_client()
        patcher = {
            "Deployment": apps.patch_namespaced_deployment,
            "StatefulSet": apps.patch_namespaced_stateful_set,
            "DaemonSet": apps.patch_namespaced_daemon_set,
        }[kind]
        patcher(name, namespace, body)

    return f"Restored {kind} {namespace}/{name} from snapshot"


def to_json(snapshot: dict[str, Any] | None) -> str:
    if not snapshot:
        return ""
    try:
        return json.dumps(snapshot)
    except (TypeError, ValueError):
        logger.warning("Snapshot is not JSON-serialisable; storing nothing", exc_info=True)
        return ""


def from_json(blob: str | None) -> dict[str, Any] | None:
    if not blob:
        return None
    try:
        data = json.loads(blob)
        return data if isinstance(data, dict) else None
    except (TypeError, ValueError):
        return None
