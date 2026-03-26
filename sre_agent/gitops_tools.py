"""ArgoCD integration tools — drift detection, sync status, and live-vs-git diffs.

Pillar 1: The "ArgoCD Shadow" — makes the agent Argo-aware so it can
show provenance, sync state, and drift for every managed resource.
"""

from __future__ import annotations

import json

from anthropic import beta_tool
from kubernetes.client.rest import ApiException

from .k8s_client import get_custom_client, get_core_client, safe

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

    output = f"Diff for '{name}':\n\n" + "\n\n".join(diffs)
    if len(output) > 10000:
        output = output[:10000] + "\n\n... [output truncated — exceeded 10000 chars]"
    return output


@beta_tool
def install_gitops_operator() -> str:
    """Install the OpenShift GitOps operator (ArgoCD) on the cluster. REQUIRES USER CONFIRMATION.

    This creates a Subscription in the openshift-operators namespace to install
    the openshift-gitops-operator from the redhat-operators catalog. Once installed,
    ArgoCD will be available in the openshift-gitops namespace with a default
    ArgoCD instance and AppProject.
    """
    custom = get_custom_client()
    core = get_core_client()

    # Check if already installed
    try:
        subs = custom.list_namespaced_custom_object(
            "operators.coreos.com", "v1alpha1", "openshift-operators", "subscriptions"
        )
        for sub in subs.get("items", []):
            if sub.get("spec", {}).get("name") == "openshift-gitops-operator":
                status = sub.get("status", {})
                csv = status.get("installedCSV", "unknown")
                return f"OpenShift GitOps operator is already installed.\n  Subscription: {sub['metadata']['name']}\n  CSV: {csv}\n  Namespace: openshift-gitops"
    except ApiException:
        pass  # OLM might not be queryable, proceed with install attempt

    # Check if ArgoCD API already exists (operator installed via other means)
    try:
        custom.get_cluster_custom_object("argoproj.io", "v1alpha1", "applications", "")
    except ApiException as e:
        if e.status != 404:
            # API group exists but no resources — ArgoCD is installed
            return "ArgoCD API (argoproj.io) is already available on this cluster. The operator appears to be installed."
    except Exception:
        pass

    # Create the Subscription
    subscription = {
        "apiVersion": "operators.coreos.com/v1alpha1",
        "kind": "Subscription",
        "metadata": {
            "name": "openshift-gitops-operator",
            "namespace": "openshift-operators",
        },
        "spec": {
            "channel": "latest",
            "installPlanApproval": "Automatic",
            "name": "openshift-gitops-operator",
            "source": "redhat-operators",
            "sourceNamespace": "openshift-marketplace",
        },
    }

    try:
        custom.create_namespaced_custom_object(
            "operators.coreos.com", "v1alpha1", "openshift-operators", "subscriptions",
            body=subscription,
        )
    except ApiException as e:
        if e.status == 409:
            return "Subscription already exists. The operator may be installing — check `oc get csv -n openshift-gitops` in a few minutes."
        return f"Error creating Subscription: {e.reason} (HTTP {e.status})"

    return (
        "OpenShift GitOps operator installation started.\n\n"
        "What happens next:\n"
        "1. OLM will download and install the operator (1-3 minutes)\n"
        "2. ArgoCD will be deployed in the openshift-gitops namespace\n"
        "3. A default ArgoCD instance and AppProject will be created\n"
        "4. The ArgoCD console will be available via route in openshift-gitops\n\n"
        "To check progress: `oc get csv -n openshift-gitops`\n"
        "To get the ArgoCD route: `oc get route -n openshift-gitops`\n\n"
        "Once installed, you can create Applications to track your cluster's Git manifests."
    )


@beta_tool
def create_argo_application(
    name: str,
    repo_url: str,
    path: str = "manifests",
    target_namespace: str = "default",
    project: str = "default",
    auto_sync: bool = True,
) -> str:
    """Create an ArgoCD Application to track a Git repository. REQUIRES USER CONFIRMATION.

    Args:
        name: Name for the ArgoCD Application (e.g., 'my-app', 'cluster-config').
        repo_url: Git repository URL (e.g., 'https://github.com/org/repo.git').
        path: Path within the repo containing Kubernetes manifests.
        target_namespace: Target namespace on the cluster for deployed resources.
        project: ArgoCD project (use 'default' unless you have custom projects).
        auto_sync: Enable automatic sync with prune and self-heal.
    """
    custom = get_custom_client()

    # Validate ArgoCD is installed
    try:
        custom.list_cluster_custom_object(_ARGO_GROUP, _ARGO_VERSION, "applications")
    except ApiException as e:
        if e.status == 404:
            return (
                "Error: ArgoCD is not installed on this cluster.\n"
                "Run install_gitops_operator first to install the OpenShift GitOps operator."
            )
        return f"Error checking ArgoCD: {e.reason}"

    # Build the Application spec
    sync_policy = {}
    if auto_sync:
        sync_policy = {
            "automated": {"prune": True, "selfHeal": True},
            "syncOptions": ["CreateNamespace=true"],
        }

    application = {
        "apiVersion": f"{_ARGO_GROUP}/{_ARGO_VERSION}",
        "kind": "Application",
        "metadata": {
            "name": name,
            "namespace": "openshift-gitops",
        },
        "spec": {
            "project": project,
            "source": {
                "repoURL": repo_url,
                "targetRevision": "HEAD",
                "path": path,
            },
            "destination": {
                "server": "https://kubernetes.default.svc",
                "namespace": target_namespace,
            },
            "syncPolicy": sync_policy,
        },
    }

    try:
        custom.create_namespaced_custom_object(
            _ARGO_GROUP, _ARGO_VERSION, "openshift-gitops", "applications",
            body=application,
        )
    except ApiException as e:
        if e.status == 409:
            return f"Application '{name}' already exists in openshift-gitops namespace."
        return f"Error creating Application: {e.reason} (HTTP {e.status})"

    sync_info = "Auto-sync enabled (prune + self-heal)" if auto_sync else "Manual sync — you'll need to trigger syncs manually"

    return (
        f"ArgoCD Application '{name}' created successfully.\n\n"
        f"  Repository: {repo_url}\n"
        f"  Path:       {path}\n"
        f"  Target:     {target_namespace}\n"
        f"  Project:    {project}\n"
        f"  Sync:       {sync_info}\n\n"
        f"ArgoCD will now monitor the Git repo and sync changes to the cluster.\n"
        f"View in Pulse: Navigate to GitOps → Applications"
    )


GITOPS_TOOLS = [
    get_argo_applications,
    get_argo_app_detail,
    detect_gitops_drift,
    get_argo_sync_diff,
    install_gitops_operator,
    create_argo_application,
]
