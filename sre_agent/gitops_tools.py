"""ArgoCD integration tools — drift detection, sync status, and live-vs-git diffs.

Pillar 1: The "ArgoCD Shadow" — makes the agent Argo-aware so it can
show provenance, sync state, and drift for every managed resource.
"""

from __future__ import annotations

import json

from anthropic import beta_tool
from kubernetes.client.rest import ApiException

from .k8s_client import get_custom_client, safe

_ARGO_GROUP = "argoproj.io"
_ARGO_VERSION = "v1alpha1"


def check_argo_auto_sync(namespace: str, kind: str = "", name: str = "") -> str | None:
    """Check if a resource is managed by an ArgoCD app with automated sync.

    Returns a warning string if auto-sync is on, None if safe to edit.
    """
    try:
        apps = get_custom_client().list_cluster_custom_object(
            _ARGO_GROUP, _ARGO_VERSION, "applications"
        )
    except ApiException:
        return None  # ArgoCD not installed, safe to proceed

    for app in apps.get("items", []):
        spec = app.get("spec", {})
        dest_ns = spec.get("destination", {}).get("namespace", "")
        sync_policy = spec.get("syncPolicy", {})
        automated = sync_policy.get("automated")

        if not automated:
            continue

        # Check if this app manages the target namespace
        if namespace and dest_ns == namespace:
            app_name = app["metadata"]["name"]
            self_heal = automated.get("selfHeal", False)
            prune = automated.get("prune", False)

            warning = (
                f"WARNING: Namespace '{namespace}' is managed by ArgoCD application "
                f"'{app_name}' with automated sync"
            )
            if self_heal:
                warning += " + selfHeal (changes WILL be reverted)"
            if prune:
                warning += " + prune"
            warning += (
                ". Direct cluster edits will be overwritten on the next sync. "
                "Use propose_git_change to make a permanent change via PR instead."
            )
            return warning

    return None


@beta_tool
def get_argo_applications(namespace: str = "ALL") -> str:
    """List ArgoCD Applications with their sync status, health, and source repo.

    Args:
        namespace: Namespace where ArgoCD Applications live. Use 'ALL' for cluster-wide. Typically 'openshift-gitops' or 'argocd'.
    """
    try:
        if namespace.upper() == "ALL":
            result = get_custom_client().list_cluster_custom_object(
                _ARGO_GROUP, _ARGO_VERSION, "applications"
            )
        else:
            result = get_custom_client().list_namespaced_custom_object(
                _ARGO_GROUP, _ARGO_VERSION, namespace, "applications"
            )
    except ApiException as e:
        if e.status == 404:
            return "ArgoCD not found. The argoproj.io API group is not available on this cluster."
        return f"Error ({e.status}): {e.reason}"

    lines = []
    for app in result.get("items", []):
        meta = app["metadata"]
        spec = app.get("spec", {})
        status = app.get("status", {})

        sync = status.get("sync", {})
        health = status.get("health", {})
        source = spec.get("source", {})

        sync_status = sync.get("status", "Unknown")
        health_status = health.get("status", "Unknown")
        repo = source.get("repoURL", "?")
        path = source.get("path", "")
        revision = sync.get("revision", "?")[:8]
        dest = spec.get("destination", {})
        dest_ns = dest.get("namespace", "?")

        drift = ""
        if sync_status == "OutOfSync":
            drift = " [DRIFT DETECTED]"

        lines.append(
            f"{meta.get('namespace', '?')}/{meta['name']}  "
            f"Sync={sync_status}{drift}  Health={health_status}  "
            f"Repo={repo}  Path={path}  Rev={revision}  "
            f"DestNS={dest_ns}"
        )

    return "\n".join(lines) or "No ArgoCD Applications found."


@beta_tool
def get_argo_app_detail(name: str, namespace: str = "openshift-gitops") -> str:
    """Get detailed information about a specific ArgoCD Application including sync status, conditions, resources, and source.

    Args:
        name: Name of the ArgoCD Application.
        namespace: Namespace of the Application resource.
    """
    try:
        app = get_custom_client().get_namespaced_custom_object(
            _ARGO_GROUP, _ARGO_VERSION, namespace, "applications", name
        )
    except ApiException as e:
        return f"Error ({e.status}): {e.reason}"

    spec = app.get("spec", {})
    status = app.get("status", {})
    sync = status.get("sync", {})
    health = status.get("health", {})
    source = spec.get("source", {})
    operation = status.get("operationState", {})

    info = {
        "name": app["metadata"]["name"],
        "namespace": app["metadata"]["namespace"],
        "sync_status": sync.get("status", "Unknown"),
        "health_status": health.get("status", "Unknown"),
        "source": {
            "repo": source.get("repoURL", ""),
            "path": source.get("path", ""),
            "target_revision": source.get("targetRevision", "HEAD"),
        },
        "destination": {
            "server": spec.get("destination", {}).get("server", ""),
            "namespace": spec.get("destination", {}).get("namespace", ""),
        },
        "revision": sync.get("revision", ""),
        "conditions": [
            {"type": c.get("type"), "message": c.get("message", "")}
            for c in status.get("conditions", [])
        ],
        "last_sync": {
            "phase": operation.get("phase", ""),
            "message": operation.get("message", ""),
            "started": operation.get("startedAt", ""),
            "finished": operation.get("finishedAt", ""),
        },
    }

    # List managed resources with their sync/health status
    resources = []
    for r in status.get("resources", [])[:50]:
        res_sync = r.get("status", "Unknown")
        res_health = r.get("health", {}).get("status", "Unknown")
        drift_marker = " [DRIFT]" if res_sync == "OutOfSync" else ""
        resources.append({
            "kind": r.get("kind"),
            "name": r.get("name"),
            "namespace": r.get("namespace", ""),
            "sync": res_sync + drift_marker,
            "health": res_health,
        })
    info["resources"] = resources

    return json.dumps(info, indent=2, default=str)


@beta_tool
def detect_gitops_drift(namespace: str = "ALL") -> str:
    """Find all ArgoCD-managed resources that have drifted from their Git source (OutOfSync). Shows what changed and where.

    Args:
        namespace: Namespace to check. Use 'ALL' for cluster-wide.
    """
    try:
        if namespace.upper() == "ALL":
            result = get_custom_client().list_cluster_custom_object(
                _ARGO_GROUP, _ARGO_VERSION, "applications"
            )
        else:
            result = get_custom_client().list_namespaced_custom_object(
                _ARGO_GROUP, _ARGO_VERSION, namespace, "applications"
            )
    except ApiException as e:
        if e.status == 404:
            return "ArgoCD not installed on this cluster."
        return f"Error ({e.status}): {e.reason}"

    drifted = []
    for app in result.get("items", []):
        meta = app["metadata"]
        status = app.get("status", {})
        sync = status.get("sync", {})

        if sync.get("status") != "OutOfSync":
            continue

        app_name = f"{meta.get('namespace', '?')}/{meta['name']}"
        source = app.get("spec", {}).get("source", {})

        # Find which resources are out of sync
        out_of_sync_resources = []
        for r in status.get("resources", []):
            if r.get("status") == "OutOfSync":
                out_of_sync_resources.append(
                    f"    {r.get('kind')}/{r.get('name')} in {r.get('namespace', 'cluster')}"
                )

        drifted.append(
            f"APP: {app_name}\n"
            f"  Repo: {source.get('repoURL', '?')}\n"
            f"  Path: {source.get('path', '?')}\n"
            f"  Drifted resources ({len(out_of_sync_resources)}):\n"
            + "\n".join(out_of_sync_resources[:20])
        )

    if not drifted:
        return "No drift detected. All ArgoCD applications are in sync with Git."

    return f"DRIFT DETECTED in {len(drifted)} application(s):\n\n" + "\n\n".join(drifted)


@beta_tool
def get_argo_sync_diff(name: str, namespace: str = "openshift-gitops") -> str:
    """Get the live-vs-desired diff for an ArgoCD Application. Shows what changed between the cluster state and the Git source.

    Args:
        name: Name of the ArgoCD Application.
        namespace: Namespace of the Application resource.
    """
    try:
        app = get_custom_client().get_namespaced_custom_object(
            _ARGO_GROUP, _ARGO_VERSION, namespace, "applications", name
        )
    except ApiException as e:
        return f"Error ({e.status}): {e.reason}"

    status = app.get("status", {})
    sync = status.get("sync", {})

    if sync.get("status") == "Synced":
        return f"Application '{name}' is in sync. No diff available."

    # ArgoCD stores the comparison result in status.resources
    diffs = []
    for r in status.get("resources", []):
        if r.get("status") != "OutOfSync":
            continue

        kind = r.get("kind", "?")
        rname = r.get("name", "?")
        rns = r.get("namespace", "")
        diff_text = r.get("diff", "")

        if diff_text:
            diffs.append(f"--- {kind}/{rname} (namespace: {rns})\n{diff_text}")
        else:
            diffs.append(
                f"--- {kind}/{rname} (namespace: {rns})\n"
                f"  Status: OutOfSync (diff details require ArgoCD server API)"
            )

    if not diffs:
        return f"Application '{name}' is OutOfSync but no resource-level diffs are available via the CRD. Use 'argocd app diff {name}' for full diff."

    return f"Diff for '{name}':\n\n" + "\n\n".join(diffs)


GITOPS_TOOLS = [
    get_argo_applications,
    get_argo_app_detail,
    detect_gitops_drift,
    get_argo_sync_diff,
]
