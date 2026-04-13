"""Fleet-wide tools — fan-out queries across multiple clusters.

These tools query the same resource across all clusters managed via
ACM (Advanced Cluster Management) or multi-proxy connections. Results
are aggregated with per-cluster status.

The agent uses these when the user mentions "fleet", "all clusters",
"cross-cluster", or "everywhere".
"""

from __future__ import annotations

import json
import logging
from typing import Any

from anthropic import beta_tool

from .errors import ToolError
from .k8s_client import age, get_apps_client, get_core_client, get_custom_client

logger = logging.getLogger("pulse_agent.fleet")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_managed_clusters() -> list[dict]:
    """Discover ACM managed clusters."""
    try:
        cobj = get_custom_client()
        result = cobj.list_cluster_custom_object(
            group="cluster.open-cluster-management.io",
            version="v1",
            plural="managedclusters",
        )
        clusters = []
        for mc in result.get("items", []):
            name = mc["metadata"]["name"]
            if name == "local-cluster":
                continue
            available = False
            for cond in mc.get("status", {}).get("conditions", []):
                if cond.get("type") == "ManagedClusterConditionAvailable":
                    available = cond.get("status") == "True"
            clusters.append({"name": name, "available": available})
        return clusters
    except Exception:
        return []


def _get_proxy_api_client(cluster_name: str):
    """Get an ApiClient proxied through ACM for a managed cluster.

    Copies auth configuration (token, certs) from the default client
    so that requests are properly authenticated.
    """
    from kubernetes import client

    # Get the default config (already loaded by _load_k8s)
    default_config = client.Configuration.get_default_copy()
    # Create a new config with the ACM proxy host, inheriting auth
    proxy_config = client.Configuration()
    proxy_config.host = (
        f"{default_config.host}/apis/cluster.open-cluster-management.io/v1/managedclusters/{cluster_name}/proxy"
    )
    proxy_config.api_key = default_config.api_key
    proxy_config.api_key_prefix = default_config.api_key_prefix
    proxy_config.ssl_ca_cert = default_config.ssl_ca_cert
    proxy_config.cert_file = default_config.cert_file
    proxy_config.key_file = default_config.key_file
    proxy_config.verify_ssl = default_config.verify_ssl
    return client.ApiClient(proxy_config)


def _proxy_core_client(cluster_name: str):
    """Get a CoreV1Api client proxied through ACM for a managed cluster."""
    from kubernetes import client

    return client.CoreV1Api(_get_proxy_api_client(cluster_name))


def _proxy_apps_client(cluster_name: str):
    """Get an AppsV1Api client proxied through ACM for a managed cluster."""
    from kubernetes import client

    return client.AppsV1Api(_get_proxy_api_client(cluster_name))


# ---------------------------------------------------------------------------
# Fleet tools
# ---------------------------------------------------------------------------


@beta_tool
def fleet_list_clusters() -> str:
    """List all managed clusters in the fleet with their availability status."""
    clusters = _get_managed_clusters()
    if not clusters:
        return "No managed clusters found. ACM/MCE may not be installed, or no spoke clusters registered."

    lines = []
    available_count = sum(1 for c in clusters if c["available"])
    for c in clusters:
        status = "Available" if c["available"] else "Unavailable"
        lines.append(f"  {c['name']}: {status}")

    text = f"Fleet: {len(clusters)} managed clusters ({available_count} available)\n" + "\n".join(lines)

    component = {
        "kind": "status_list",
        "items": [
            {
                "name": c["name"],
                "status": "healthy" if c["available"] else "error",
                "detail": "Available" if c["available"] else "Unavailable",
            }
            for c in clusters
        ],
    }
    return (text, component)


@beta_tool
def fleet_list_pods(namespace: str = "default", label_selector: str = "") -> str:
    """List pods across ALL managed clusters in the fleet.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' to list across all namespaces.
        label_selector: Label selector to filter pods, e.g. 'app=nginx'.
    """
    clusters = _get_managed_clusters()
    if not clusters:
        return "No managed clusters found."

    all_rows = []
    lines = []

    # Also query local cluster
    try:
        core = get_core_client()
        kwargs = {}
        if label_selector:
            kwargs["label_selector"] = label_selector
        if namespace.upper() == "ALL":
            result = core.list_pod_for_all_namespaces(**kwargs)
        else:
            result = core.list_namespaced_pod(namespace, **kwargs)

        for pod in result.items[:50]:
            restarts = sum((cs.restart_count for cs in (pod.status.container_statuses or [])), 0)
            all_rows.append(
                {
                    "cluster": "local",
                    "namespace": pod.metadata.namespace,
                    "name": pod.metadata.name,
                    "status": pod.status.phase or "Unknown",
                    "restarts": restarts,
                    "age": age(pod.metadata.creation_timestamp),
                }
            )
        lines.append(f"local: {len(result.items)} pods")
    except Exception as e:
        lines.append(f"local: Error - {e}")

    # Query each managed cluster via ACM proxy
    for cluster in clusters:
        if not cluster["available"]:
            lines.append(f"{cluster['name']}: Unavailable (skipped)")
            continue
        try:
            proxy_core = _proxy_core_client(cluster["name"])
            kwargs = {}
            if label_selector:
                kwargs["label_selector"] = label_selector
            if namespace.upper() == "ALL":
                result = proxy_core.list_pod_for_all_namespaces(**kwargs)
            else:
                result = proxy_core.list_namespaced_pod(namespace, **kwargs)

            for pod in result.items[:50]:
                restarts = sum((cs.restart_count for cs in (pod.status.container_statuses or [])), 0)
                all_rows.append(
                    {
                        "cluster": cluster["name"],
                        "namespace": pod.metadata.namespace,
                        "name": pod.metadata.name,
                        "status": pod.status.phase or "Unknown",
                        "restarts": restarts,
                        "age": age(pod.metadata.creation_timestamp),
                    }
                )
            lines.append(f"{cluster['name']}: {len(result.items)} pods")
        except Exception as e:
            lines.append(f"{cluster['name']}: Error - {e}")

    text = f"Fleet pods in {namespace}:\n" + "\n".join(lines) + f"\n\nTotal: {len(all_rows)} pods across fleet"

    component = (
        {
            "kind": "data_table",
            "title": f"Fleet Pods ({len(all_rows)})",
            "columns": [
                {"id": "cluster", "header": "Cluster"},
                {"id": "namespace", "header": "Namespace"},
                {"id": "name", "header": "Name"},
                {"id": "status", "header": "Status"},
                {"id": "restarts", "header": "Restarts"},
                {"id": "age", "header": "Age"},
            ],
            "rows": all_rows,
        }
        if all_rows
        else None
    )

    return (text, component)


@beta_tool
def fleet_list_deployments(namespace: str = "default") -> str:
    """List deployments across ALL managed clusters in the fleet.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' for all namespaces.
    """
    clusters = _get_managed_clusters()
    if not clusters:
        return "No managed clusters found."

    all_rows = []
    lines = []

    # Local cluster
    try:
        apps = get_apps_client()
        if namespace.upper() == "ALL":
            result = apps.list_deployment_for_all_namespaces()
        else:
            result = apps.list_namespaced_deployment(namespace)

        for dep in result.items[:50]:
            ready = dep.status.ready_replicas or 0
            desired = dep.spec.replicas or 0
            all_rows.append(
                {
                    "cluster": "local",
                    "namespace": dep.metadata.namespace,
                    "name": dep.metadata.name,
                    "ready": f"{ready}/{desired}",
                    "age": age(dep.metadata.creation_timestamp),
                }
            )
        lines.append(f"local: {len(result.items)} deployments")
    except Exception as e:
        lines.append(f"local: Error - {e}")

    for cluster in clusters:
        if not cluster["available"]:
            lines.append(f"{cluster['name']}: Unavailable (skipped)")
            continue
        try:
            proxy_apps = _proxy_apps_client(cluster["name"])
            if namespace.upper() == "ALL":
                result = proxy_apps.list_deployment_for_all_namespaces()
            else:
                result = proxy_apps.list_namespaced_deployment(namespace)

            for dep in result.items[:50]:
                ready = dep.status.ready_replicas or 0
                desired = dep.spec.replicas or 0
                all_rows.append(
                    {
                        "cluster": cluster["name"],
                        "namespace": dep.metadata.namespace,
                        "name": dep.metadata.name,
                        "ready": f"{ready}/{desired}",
                        "age": age(dep.metadata.creation_timestamp),
                    }
                )
            lines.append(f"{cluster['name']}: {len(result.items)} deployments")
        except Exception as e:
            lines.append(f"{cluster['name']}: Error - {e}")

    text = f"Fleet deployments in {namespace}:\n" + "\n".join(lines) + f"\n\nTotal: {len(all_rows)} across fleet"

    component = (
        {
            "kind": "data_table",
            "title": f"Fleet Deployments ({len(all_rows)})",
            "columns": [
                {"id": "cluster", "header": "Cluster"},
                {"id": "namespace", "header": "Namespace"},
                {"id": "name", "header": "Name"},
                {"id": "ready", "header": "Ready"},
                {"id": "age", "header": "Age"},
            ],
            "rows": all_rows,
        }
        if all_rows
        else None
    )

    return (text, component)


@beta_tool
def fleet_get_alerts() -> str:
    """Get firing alerts from the local (hub) cluster. Managed cluster alerts require direct Alertmanager access which is not yet supported."""
    clusters = _get_managed_clusters()
    if not clusters:
        return "No managed clusters found."

    all_alerts = []
    lines = []

    # Local cluster alerts
    try:
        from .k8s_tools import get_firing_alerts

        result = get_firing_alerts()
        if isinstance(result, tuple):
            text_result, _ = result
        else:
            text_result = result
        local_count = text_result.count("\n") + 1 if "alert" in text_result.lower() else 0
        lines.append(f"local: {local_count} alerts")
        all_alerts.append({"cluster": "local", "alerts": text_result})
    except Exception as e:
        lines.append(f"local: Error - {e}")

    for cluster in clusters:
        if not cluster["available"]:
            lines.append(f"{cluster['name']}: Unavailable")
            continue
        # Note: Alertmanager access via ACM proxy would need custom routing
        lines.append(f"{cluster['name']}: Alert check requires direct Alertmanager access")

    summary = "Fleet alerts summary:\n" + "\n".join(lines)
    if all_alerts:
        summary += "\n\nLocal cluster alerts:\n" + all_alerts[0]["alerts"]

    return summary


# Map kind -> (client_getter, read_method_name) for fleet_compare_resource
_KIND_READERS: dict[str, tuple[str, str]] = {
    "deployment": ("apps", "read_namespaced_deployment"),
    "statefulset": ("apps", "read_namespaced_stateful_set"),
    "daemonset": ("apps", "read_namespaced_daemon_set"),
    "configmap": ("core", "read_namespaced_config_map"),
    "service": ("core", "read_namespaced_service"),
    "secret": ("core", "read_namespaced_secret"),
    "serviceaccount": ("core", "read_namespaced_service_account"),
}


def _read_resource(kind: str, name: str, namespace: str, core=None, apps=None):
    """Read a resource by kind using the appropriate client."""
    core = core or get_core_client()
    apps = apps or get_apps_client()
    clients = {"core": core, "apps": apps}

    reader = _KIND_READERS.get(kind.lower())
    if reader:
        client_key, method = reader
        return getattr(clients[client_key], method)(name, namespace)
    return f"Unsupported kind for comparison: {kind}. Supported: {', '.join(k.title() for k in _KIND_READERS)}"


@beta_tool
def fleet_compare_resource(kind: str, name: str, namespace: str = "default") -> str:
    """Compare a specific resource across all managed clusters to detect configuration drift.

    Args:
        kind: Resource kind (Deployment, StatefulSet, DaemonSet, ConfigMap, Service, Secret, ServiceAccount)
        name: Resource name.
        namespace: Kubernetes namespace.
    """
    if kind.lower() not in _KIND_READERS:
        return f"Unsupported kind: {kind}. Supported: {', '.join(k.title() for k in _KIND_READERS)}"

    clusters = _get_managed_clusters()
    if not clusters:
        return "No managed clusters found."

    resources: dict[str, dict] = {}

    # Fetch from local cluster
    try:
        result = _read_resource(kind, name, namespace)

        if hasattr(result, "to_dict"):
            resources["local"] = result.to_dict()
        else:
            resources["local"] = result
    except Exception as e:
        resources["local"] = {"error": str(e)}

    # Fetch from managed clusters
    for cluster in clusters:
        if not cluster["available"]:
            resources[cluster["name"]] = {"error": "Cluster unavailable"}
            continue
        try:
            proxy_core = _proxy_core_client(cluster["name"])
            proxy_apps = _proxy_apps_client(cluster["name"])
            result = _read_resource(kind, name, namespace, core=proxy_core, apps=proxy_apps)

            if isinstance(result, ToolError):
                resources[cluster["name"]] = {"error": str(result)}
            elif hasattr(result, "to_dict"):
                resources[cluster["name"]] = result.to_dict()
            else:
                resources[cluster["name"]] = result
        except Exception as e:
            resources[cluster["name"]] = {"error": str(e)}

    # Compare key fields
    diffs = []
    ignore_prefixes = {
        "metadata.uid",
        "metadata.resource_version",
        "metadata.creation_timestamp",
        "metadata.managed_fields",
        "metadata.generation",
        "metadata.self_link",
        "status",
    }

    def flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
        items: dict[str, Any] = {}
        if isinstance(obj, dict):
            for k, v in obj.items():
                new_key = f"{prefix}.{k}" if prefix else k
                if any(new_key.startswith(p) for p in ignore_prefixes):
                    continue
                if isinstance(v, (dict, list)):
                    items.update(flatten(v, new_key))
                else:
                    items[new_key] = v
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                items.update(flatten(v, f"{prefix}[{i}]"))
        else:
            items[prefix] = obj
        return items

    flat_resources = {}
    for cluster_name, resource in resources.items():
        if "error" not in resource:
            flat_resources[cluster_name] = flatten(resource)

    if len(flat_resources) < 2:
        return f"Need at least 2 clusters with the resource to compare. Got: {list(flat_resources.keys())}"

    all_fields = set()
    for flat in flat_resources.values():
        all_fields.update(flat.keys())

    for field in sorted(all_fields):
        values = {}
        for cluster_name, flat in flat_resources.items():
            values[cluster_name] = flat.get(field, "<missing>")

        unique_values = set(json.dumps(v, default=str) for v in values.values())
        if len(unique_values) > 1:
            diffs.append({"field": field, "values": {k: str(v)[:80] for k, v in values.items()}})

    if not diffs:
        return f"{kind}/{name} is identical across {list(flat_resources.keys())}. No drift detected."

    lines = [f"Drift detected in {kind}/{name} across {len(flat_resources)} clusters:"]
    for d in diffs[:30]:
        lines.append(f"\n  {d['field']}:")
        for cluster_name, value in d["values"].items():
            lines.append(f"    {cluster_name}: {value}")

    if len(diffs) > 30:
        lines.append(f"\n  ... and {len(diffs) - 30} more drifted fields")

    text = "\n".join(lines)

    component = {
        "kind": "data_table",
        "title": f"Configuration Drift: {kind}/{name} ({len(diffs)} differences)",
        "columns": [
            {"id": "field", "header": "Field"},
            *[{"id": cn, "header": cn} for cn in flat_resources],
        ],
        "rows": [{"field": d["field"], **d["values"]} for d in diffs[:50]],
    }

    return (text, component)


# All fleet tools
FLEET_TOOLS = [
    fleet_list_clusters,
    fleet_list_pods,
    fleet_list_deployments,
    fleet_get_alerts,
    fleet_compare_resource,
]

# Register fleet tools in the central registry (all read-only)
from .tool_registry import register_tool

for _tool in FLEET_TOOLS:
    register_tool(_tool, is_write=False)
