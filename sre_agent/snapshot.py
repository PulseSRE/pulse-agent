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
needed to address the object again. Status and server-owned metadata are excluded from the replayed fields. The
original UID is retained separately to refuse rollback onto a recreated object.
Restore tests the current resourceVersion atomically before replacing mutable fields.
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
    annotations = dict(out.get("annotations") or {})
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
        "uid": raw.get("metadata", {}).get("uid"),
    }
    if kind == "ConfigMap":
        snapshot["data"] = raw.get("data", {})
        snapshot["binaryData"] = raw.get("binaryData", {})
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

    from kubernetes.client import ApiClient

    from .k8s_client import get_apps_client, get_core_client

    if not snapshot.get("uid"):
        raise ValueError("Snapshot has no resource UID; identity cannot be verified")
    if kind == "ConfigMap":
        client = get_core_client()
        reader = client.read_namespaced_config_map
        patcher = client.patch_namespaced_config_map
        fields = {"data": snapshot.get("data", {}), "binaryData": snapshot.get("binaryData", {})}
    else:
        client = get_apps_client()
        suffix = {"Deployment": "deployment", "StatefulSet": "stateful_set", "DaemonSet": "daemon_set"}[kind]
        reader = getattr(client, f"read_namespaced_{suffix}")
        patcher = getattr(client, f"patch_namespaced_{suffix}")
        fields = {"spec": snapshot.get("spec", {})}

    serializer = ApiClient()
    current = serializer.sanitize_for_serialization(reader(name, namespace))
    meta = current.get("metadata", {})
    if meta.get("uid") != snapshot["uid"]:
        raise ValueError("Resource was recreated since the snapshot; refusing rollback")
    if not meta.get("resourceVersion"):
        raise ValueError("Resource has no resourceVersion; cannot safely restore")

    # JSON Patch add replaces an existing subtree in its entirety, unlike
    # strategic/merge patches which retain keys and keyed list entries omitted
    # from the old snapshot. Both tests and writes are one atomic API operation.
    body = [
        {"op": "test", "path": "/metadata/uid", "value": snapshot["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]},
    ]
    desired = {f"/{key}": value for key, value in fields.items()}
    for key in ("labels", "annotations"):
        value = dict(snapshot.get("metadata", {}).get(key) or {})
        if key == "annotations":
            last_applied = "kubectl.kubernetes.io/last-applied-configuration"
            if last_applied in (meta.get(key) or {}):
                value[last_applied] = meta[key][last_applied]
        desired[f"/metadata/{key}"] = value
    body.extend({"op": "add", "path": path, "value": value} for path, value in desired.items())
    patcher(name, namespace, body, _content_type="application/json-patch+json")

    restored = serializer.sanitize_for_serialization(reader(name, namespace))
    if restored.get("metadata", {}).get("uid") != snapshot["uid"]:
        raise ValueError("Resource identity changed while verifying rollback")
    for path, value in desired.items():
        actual = restored
        for part in path.strip("/").split("/"):
            actual = actual.get(part, {}) if isinstance(actual, dict) else None
        if (actual or {}) != value:
            raise ValueError(f"Rollback verification failed for {path}")

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
