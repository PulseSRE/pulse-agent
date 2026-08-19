"""Tools for diagnosing and clearing deletions that never completed.

``scan_stuck_deletions`` finds these; these two tools are what an operator (or
the agent, with confirmation) uses to resolve one.

The split is deliberate. ``diagnose_stuck_deletion`` is a read and runs freely,
because working out *what* is blocking a deletion is tedious, mechanical, and
completely safe to automate. ``remove_finalizer`` is a write behind the
confirmation gate, because a finalizer is a controller's promise to clean
something up before the object disappears — cloud load balancers, volumes, DNS
records, external database entries. Forcing one off does not perform that
cleanup; it silently abandons it. That is why this tool refuses more cases than
``kubectl patch`` would.
"""

from __future__ import annotations

import json

from kubernetes.client.rest import ApiException

from .. import k8s_client as _kc
from ..decorators import beta_tool
from ..errors import ToolError, classify_api_error
from .generic import _resolve_plural
from .validators import _validate_k8s_name, _validate_k8s_namespace

# Finalizers the Kubernetes control plane owns. Forcing these off does not
# resolve anything — the controller behind them is still running and will
# either re-add them or leave real resources orphaned.
_CONTROL_PLANE_FINALIZERS = {
    "kubernetes.io/pvc-protection",
    "kubernetes.io/pv-protection",
    "kubernetes.io/pod-protection",
}


def _resource_path(kind: str, name: str, namespace: str, group: str, version: str) -> str:
    plural = _resolve_plural(kind)
    api_base = f"/apis/{group}/{version}" if group else f"/api/{version}"
    if namespace and namespace != "_":
        return f"{api_base}/namespaces/{namespace}/{plural}/{name}"
    return f"{api_base}/{plural}/{name}"


def _get_object(path: str) -> dict | str:
    try:
        obj = _kc.get_raw_json(path, "stuck_deletion")
    except Exception as e:
        return f"Error fetching {path}: {type(e).__name__}: {e}"
    if isinstance(obj, ToolError):
        return str(obj)
    if not isinstance(obj, dict):
        return f"Unexpected response type: {type(obj).__name__}"
    return obj


def _namespace_content_remaining(obj: dict) -> str:
    """The API server's own account of what is still inside a namespace."""
    for cond in obj.get("status", {}).get("conditions", []) or []:
        if cond.get("type") == "NamespaceContentRemaining" and cond.get("status") == "True":
            return (cond.get("message") or "").strip()
    return ""


@beta_tool
def diagnose_stuck_deletion(kind: str, name: str, namespace: str = "_", group: str = "", version: str = "v1"):
    """Explain why a resource's deletion has not completed: finalizers, owners, and remaining content.

    Args:
        kind: Resource kind (e.g. 'Namespace', 'Pod', 'CustomResourceDefinition').
        name: Name of the resource.
        namespace: Kubernetes namespace. Use '_' for cluster-scoped resources.
        group: API group (e.g. 'apiextensions.k8s.io'). Empty for core resources.
        version: API version (default 'v1').
    """
    if err := _validate_k8s_name(name):
        return err
    if namespace and namespace != "_":
        if err := _validate_k8s_namespace(namespace):
            return err

    obj = _get_object(_resource_path(kind, name, namespace, group, version))
    if isinstance(obj, str):
        return obj

    meta = obj.get("metadata", {})
    deletion_timestamp = meta.get("deletionTimestamp")
    if not deletion_timestamp:
        return f"{kind}/{name} is not being deleted — no deletionTimestamp is set. Nothing is stuck."

    lines = [f"{kind}/{name} has been pending deletion since {deletion_timestamp}."]

    metadata_finalizers = list(meta.get("finalizers") or [])
    if metadata_finalizers:
        lines.append("")
        lines.append("metadata.finalizers still present (each is a controller that has not finished):")
        for f in metadata_finalizers:
            note = " — control-plane owned, do not force" if f in _CONTROL_PLANE_FINALIZERS else ""
            lines.append(f"  - {f}{note}")

    spec_finalizers = list(obj.get("spec", {}).get("finalizers") or [])
    if spec_finalizers:
        lines.append("")
        lines.append(f"spec.finalizers: {', '.join(spec_finalizers)}")

    remaining = _namespace_content_remaining(obj)
    if remaining:
        lines.append("")
        lines.append(f"Content still inside the namespace: {remaining}")
        lines.append("Deleting these, or fixing the controller that owns them, is the real fix.")

    owners = meta.get("ownerReferences", []) or []
    if owners:
        lines.append("")
        lines.append("Owner references:")
        for o in owners:
            lines.append(f"  - {o.get('kind', '?')}/{o.get('name', '?')}")

    if not metadata_finalizers and not spec_finalizers:
        lines.append("")
        lines.append(
            "No finalizers remain. The object is waiting on the API server's garbage "
            "collector or, for a Pod, on the kubelet confirming termination — check "
            "the node's health rather than the object."
        )

    return "\n".join(lines)


@beta_tool
def remove_finalizer(kind: str, name: str, finalizer: str, namespace: str = "_", group: str = "", version: str = "v1"):
    """Remove ONE named finalizer from a resource already pending deletion. REQUIRES USER CONFIRMATION.

    Destructive and not reversible: the finalizer exists so a controller can
    clean up external state (cloud load balancers, volumes, DNS records) before
    the object disappears. Removing it skips that cleanup and leaks whatever it
    was protecting. Prefer fixing the controller. Run diagnose_stuck_deletion
    first.

    Args:
        kind: Resource kind (e.g. 'Namespace', 'Pod', 'CustomResourceDefinition').
        name: Name of the resource.
        finalizer: The exact finalizer string to remove. One only — no wildcards.
        namespace: Kubernetes namespace. Use '_' for cluster-scoped resources.
        group: API group. Empty for core resources.
        version: API version (default 'v1').
    """
    if err := _validate_k8s_name(name):
        return err
    if namespace and namespace != "_":
        if err := _validate_k8s_namespace(namespace):
            return err
    if not finalizer or not finalizer.strip():
        return "A finalizer name is required. Run diagnose_stuck_deletion to see which ones are present."
    finalizer = finalizer.strip()

    path = _resource_path(kind, name, namespace, group, version)
    obj = _get_object(path)
    if isinstance(obj, str):
        return obj

    meta = obj.get("metadata", {})
    if not meta.get("deletionTimestamp"):
        return (
            f"Refusing: {kind}/{name} is not being deleted. Removing a finalizer from a live "
            f"object breaks the controller's cleanup contract for whenever it is deleted later. "
            f"Delete the object first if that is what you want."
        )

    if finalizer in _CONTROL_PLANE_FINALIZERS:
        return (
            f"Refusing: '{finalizer}' is owned by the Kubernetes control plane. It clears itself "
            f"once whatever is still using the resource stops using it — for pvc-protection, that "
            f"means a pod is still mounting the claim. Find and remove that consumer instead."
        )

    metadata_finalizers = list(meta.get("finalizers") or [])
    spec_finalizers = list(obj.get("spec", {}).get("finalizers") or [])

    if finalizer in metadata_finalizers:
        remaining = [f for f in metadata_finalizers if f != finalizer]
        api = _kc.get_core_client().api_client
        try:
            api.call_api(
                path,
                "PATCH",
                body=json.dumps({"metadata": {"finalizers": remaining}}),
                header_params={
                    "Content-Type": "application/merge-patch+json",
                    "Accept": "application/json",
                },
                auth_settings=["BearerToken"],
                _preload_content=False,
            )
        except ApiException as e:
            return str(classify_api_error(e, "remove_finalizer"))
        except Exception as e:
            return f"Error removing finalizer: {type(e).__name__}: {e}"
        left = ", ".join(remaining) if remaining else "none"
        return f"Removed finalizer '{finalizer}' from {kind}/{name}. Remaining finalizers: {left}."

    if finalizer in spec_finalizers:
        # Namespaces keep 'kubernetes' in spec.finalizers and it can only be
        # cleared through the /finalize subresource. This is the classic
        # orphan-everything footgun, so gate it on the API server's own
        # statement that the namespace is actually empty.
        remaining_content = _namespace_content_remaining(obj)
        if remaining_content:
            return (
                f"Refusing: {kind}/{name} still contains resources — {remaining_content}. Clearing "
                f"spec.finalizers now would delete the namespace record while leaving those objects "
                f"orphaned and unreachable. Remove the remaining content first, then retry."
            )
        remaining = [f for f in spec_finalizers if f != finalizer]
        api = _kc.get_core_client().api_client
        body = dict(obj)
        body["spec"] = {**obj.get("spec", {}), "finalizers": remaining}
        try:
            api.call_api(
                f"{path}/finalize",
                "PUT",
                body=json.dumps(body),
                header_params={"Content-Type": "application/json", "Accept": "application/json"},
                auth_settings=["BearerToken"],
                _preload_content=False,
            )
        except ApiException as e:
            return str(classify_api_error(e, "remove_finalizer"))
        except Exception as e:
            return f"Error finalizing {kind}/{name}: {type(e).__name__}: {e}"
        return f"Cleared spec.finalizer '{finalizer}' on {kind}/{name}; deletion can now complete."

    present = ", ".join(metadata_finalizers + spec_finalizers) or "none"
    return f"Finalizer '{finalizer}' is not present on {kind}/{name}. Present finalizers: {present}."
