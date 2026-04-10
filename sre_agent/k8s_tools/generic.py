"""Generic resource listing, describing, and relationship tracing tools."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from anthropic import beta_tool
from kubernetes.client.rest import ApiException

from .. import k8s_client as _kc
from ..errors import ToolError
from .validators import MAX_RESULTS, _validate_k8s_name


def _infer_column_type(col_id: str, sample_value=None) -> str:
    """Infer the best column renderer type from the column ID and sample value."""
    if col_id in ("status", "phase", "state"):
        return "status"
    if col_id == "name":
        return "resource_name"
    if col_id == "namespace":
        return "namespace"
    if col_id == "node":
        return "node"
    if col_id == "age":
        return "age"
    if col_id in ("logs", "link"):
        return "link"
    if col_id in ("cpu", "cpu_pct"):
        return "cpu"
    if col_id in ("memory", "mem_pct"):
        return "memory"
    if col_id in ("ready", "replicas", "completions"):
        return "replicas"
    if col_id in ("labels", "annotations"):
        return "labels"
    if col_id in ("severity", "type"):
        return "severity"
    if col_id.endswith("_pct") or col_id == "utilization":
        return "progress"
    if col_id in ("created", "creationTimestamp", "lastSchedule", "lastScheduleTime", "startsAt"):
        return "timestamp"
    if isinstance(sample_value, bool):
        return "boolean"
    if isinstance(sample_value, str) and sample_value.startswith("/"):
        return "link"
    if isinstance(sample_value, str) and len(sample_value) > 18 and "T" in sample_value:
        return "timestamp"
    return "text"


# Short name -> (plural, group) mapping for common resources
_SHORT_NAMES: dict[str, tuple[str, str]] = {
    "po": ("pods", ""),
    "pod": ("pods", ""),
    "svc": ("services", ""),
    "service": ("services", ""),
    "deploy": ("deployments", "apps"),
    "deployment": ("deployments", "apps"),
    "ds": ("daemonsets", "apps"),
    "daemonset": ("daemonsets", "apps"),
    "sts": ("statefulsets", "apps"),
    "statefulset": ("statefulsets", "apps"),
    "rs": ("replicasets", "apps"),
    "replicaset": ("replicasets", "apps"),
    "cm": ("configmaps", ""),
    "configmap": ("configmaps", ""),
    "pvc": ("persistentvolumeclaims", ""),
    "persistentvolumeclaim": ("persistentvolumeclaims", ""),
    "pv": ("persistentvolumes", ""),
    "persistentvolume": ("persistentvolumes", ""),
    "ns": ("namespaces", ""),
    "namespace": ("namespaces", ""),
    "no": ("nodes", ""),
    "node": ("nodes", ""),
    "sa": ("serviceaccounts", ""),
    "serviceaccount": ("serviceaccounts", ""),
    "hpa": ("horizontalpodautoscalers", "autoscaling"),
    "cj": ("cronjobs", "batch"),
    "cronjob": ("cronjobs", "batch"),
    "job": ("jobs", "batch"),
    "ing": ("ingresses", "networking.k8s.io"),
    "ingress": ("ingresses", "networking.k8s.io"),
    "netpol": ("networkpolicies", "networking.k8s.io"),
    "ev": ("events", ""),
    "event": ("events", ""),
    "secret": ("secrets", ""),
    "ep": ("endpoints", ""),
    "quota": ("resourcequotas", ""),
    "limits": ("limitranges", ""),
}


def _resolve_short_name(resource: str, group: str) -> tuple[str, str]:
    """Resolve short names and singular forms to (plural, group)."""
    key = resource.lower().strip()
    if key in _SHORT_NAMES:
        plural, default_group = _SHORT_NAMES[key]
        return plural, group or default_group
    return resource, group


# Kind -> plural mapping for generic resource access
_KIND_PLURAL_MAP: dict[str, str] = {
    "ConfigMap": "configmaps",
    "Service": "services",
    "Pod": "pods",
    "Secret": "secrets",
    "Namespace": "namespaces",
    "Node": "nodes",
    "ServiceAccount": "serviceaccounts",
    "PersistentVolumeClaim": "persistentvolumeclaims",
    "PersistentVolume": "persistentvolumes",
    "Endpoints": "endpoints",
    "Event": "events",
    "ResourceQuota": "resourcequotas",
    "LimitRange": "limitranges",
    "Deployment": "deployments",
    "StatefulSet": "statefulsets",
    "DaemonSet": "daemonsets",
    "ReplicaSet": "replicasets",
    "Job": "jobs",
    "CronJob": "cronjobs",
    "Ingress": "ingresses",
    "NetworkPolicy": "networkpolicies",
    "HorizontalPodAutoscaler": "horizontalpodautoscalers",
    "Role": "roles",
    "RoleBinding": "rolebindings",
    "ClusterRole": "clusterroles",
    "ClusterRoleBinding": "clusterrolebindings",
}


def _resolve_plural(kind: str) -> str:
    """Convert a Kind to its plural resource name."""
    if kind in _KIND_PLURAL_MAP:
        return _KIND_PLURAL_MAP[kind]
    # Fallback: lowercase + 's', handling common suffixes
    lower = kind.lower()
    if lower.endswith("s"):
        return lower + "es"
    if lower.endswith("y"):
        return lower[:-1] + "ies"
    return lower + "s"


@beta_tool
def list_resources(
    resource: str,
    namespace: str = "",
    group: str = "",
    version: str = "v1",
    label_selector: str = "",
    field_selector: str = "",
    sort_by: str = "",
    show_wide: bool = False,
) -> str:
    """List any Kubernetes resource type using the server's printer columns.

    Uses the Kubernetes Table API to get the same columns as 'kubectl get'.
    Works for ANY resource type including CRDs with custom printer columns.
    Accepts short names (po, svc, deploy, cm, ds, sts, hpa, cj, ing, etc.).

    Args:
        resource: Resource type — plural, singular, or short name (e.g. 'pods', 'svc', 'deploy', 'cm').
        namespace: Kubernetes namespace. Use 'ALL' for all namespaces. Leave empty for cluster-scoped.
        group: API group (e.g. 'apps' for deployments). Auto-detected for common types.
        version: API version (default 'v1').
        label_selector: Filter by labels (e.g. 'app=nginx', 'app.kubernetes.io/managed-by=Helm').
        field_selector: Filter by fields (e.g. 'status.phase=Running').
        sort_by: Column name to sort by. Prefix with '-' for descending.
        show_wide: If true, include all columns (like kubectl -o wide).
    """
    import ssl
    import urllib.parse
    import urllib.request

    # Resolve short names (svc->services, deploy->deployments, cm->configmaps, etc.)
    resource, group = _resolve_short_name(resource, group)

    gvr = f"{group}~{version}~{resource}" if group else f"{version}~{resource}"

    # Build the API path
    api_base = f"/apis/{group}/{version}" if group else f"/api/{version}"
    if namespace and namespace.upper() != "ALL":
        path = f"{api_base}/namespaces/{namespace}/{resource}"
    else:
        path = f"{api_base}/{resource}"

    params = {}
    if label_selector:
        params["labelSelector"] = label_selector
    if field_selector:
        params["fieldSelector"] = field_selector
    params["limit"] = str(MAX_RESULTS)

    url = f"https://kubernetes.default.svc{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    # Read SA token for auth
    try:
        with open("/var/run/secrets/kubernetes.io/serviceaccount/token") as f:
            token = f.read().strip()
    except FileNotFoundError:
        return "Error: Not running in-cluster (no service account token)."

    ctx = ssl.create_default_context()
    try:
        ctx.load_verify_locations("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    except Exception as e:
        return f"Error: Cannot load cluster CA certificate ({e}). Refusing to connect without TLS verification."

    # Request as Table format — server returns printer columns + pre-formatted cells
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json;as=Table;g=meta.k8s.io;v=v1",
        },
    )

    try:
        resp = urllib.request.urlopen(req, context=ctx, timeout=15)
        data = json.loads(resp.read())
    except Exception as e:
        return f"Error listing {resource}: {e}"

    col_defs_raw = data.get("columnDefinitions", [])
    table_rows = data.get("rows", [])

    if not table_rows:
        return f"No {resource} found."

    # Map server column names to our renderer types
    _NAME_TYPE_MAP = {
        "Name": "resource_name",
        "Status": "status",
        "Phase": "status",
        "Ready": "replicas",
        "Age": "age",
        "Node": "node",
        "Namespace": "namespace",
        "Suspend": "boolean",
        "Selector": "labels",
        "Node Selector": "labels",
    }

    # Build column definitions — filter by priority unless show_wide
    max_priority = 99 if show_wide else 0
    col_defs = []
    col_indices = []
    for i, col in enumerate(col_defs_raw):
        if col.get("priority", 0) > max_priority:
            continue
        col_id = col["name"].lower().replace(" ", "_").replace("-", "_").replace("(", "").replace(")", "")
        col_type = _NAME_TYPE_MAP.get(col["name"]) or _infer_column_type(col_id) or "text"
        col_defs.append({"id": col_id, "header": col["name"], "type": col_type})
        col_indices.append(i)

    # Detect if resource is cluster-scoped (no namespace on any row)
    is_cluster_scoped = all(
        not (tr.get("object", {}).get("metadata", {}) if isinstance(tr.get("object"), dict) else {}).get("namespace")
        for tr in table_rows[:5]
    )

    # Build rows from Table cells
    rows = []
    for tr in table_rows:
        cells = tr.get("cells", [])
        meta = tr.get("object", {}).get("metadata", {}) if isinstance(tr.get("object"), dict) else {}
        ns = meta.get("namespace", "")
        row: dict = {"_gvr": gvr}
        if ns and not is_cluster_scoped:
            row["namespace"] = ns

        for j, idx in enumerate(col_indices):
            if idx < len(cells):
                val = cells[idx]
                row[col_defs[j]["id"]] = val if val is not None else ""

        # Pass through metadata fields not already in printer columns
        labels = meta.get("labels")
        if labels:
            row["labels"] = labels
        annotations = meta.get("annotations")
        if annotations:
            # Filter out long/noisy annotations
            row["annotations"] = {
                k: v
                for k, v in annotations.items()
                if not k.startswith("kubectl.kubernetes.io/") and not k.startswith("openshift.io/") and len(v) < 200
            } or None
            if not row["annotations"]:
                del row["annotations"]
        owner_refs = meta.get("ownerReferences")
        if owner_refs:
            row["owner"] = "/".join(f"{o['kind']}/{o['name']}" for o in owner_refs)
        row["_uid"] = meta.get("uid", "")

        rows.append(row)

    # Add namespace column only for namespaced resources listed across namespaces
    if (
        not is_cluster_scoped
        and (not namespace or namespace.upper() == "ALL")
        and not any(c["id"] == "namespace" for c in col_defs)
    ):
        col_defs.insert(0, {"id": "namespace", "header": "Namespace", "type": "namespace"})

    # Remove namespace column from cluster-scoped resources
    if is_cluster_scoped:
        col_defs = [c for c in col_defs if c["id"] != "namespace"]

    # Add metadata columns — always present, UI can toggle visibility
    has_labels = any(r.get("labels") for r in rows[:5])
    has_annotations = any(r.get("annotations") for r in rows[:5])
    has_owner = any(r.get("owner") for r in rows[:5])
    if has_labels:
        col_defs.append({"id": "labels", "header": "Labels", "type": "labels"})
    if has_annotations:
        col_defs.append({"id": "annotations", "header": "Annotations", "type": "labels"})
    if has_owner:
        col_defs.append({"id": "owner", "header": "Owner", "type": "text"})

    # Sort if requested
    if sort_by:
        desc = sort_by.startswith("-")
        sort_key = sort_by.lstrip("-").lower().replace(" ", "_").replace("-", "_")
        rows.sort(key=lambda r: str(r.get(sort_key, "")), reverse=desc)

    # Build text summary
    name_col = next((c["id"] for c in col_defs if c.get("type") == "resource_name"), "name")
    text_lines = []
    for r in rows[:20]:
        ns_prefix = f"{r.get('namespace', '')}/" if r.get("namespace") else ""
        text_lines.append(f"{ns_prefix}{r.get(name_col, '?')}")
    text = f"{resource} ({len(rows)}):\n" + "\n".join(text_lines)
    if len(rows) > 20:
        text += f"\n... and {len(rows) - 20} more"

    component = {
        "kind": "data_table",
        "title": f"{resource.replace('_', ' ').title()} ({len(rows)})",
        "description": f"{'Filtered' if label_selector else 'All'} {resource} in {namespace or 'cluster'}",
        "columns": col_defs,
        "rows": rows,
    }
    return (text, component)


@beta_tool
def get_resource_relationships(namespace: str, name: str, kind: str = "Pod") -> str:
    """Trace the ownership chain for a Kubernetes resource — find what controls it and what it controls.

    Shows the full hierarchy: e.g., Pod -> ReplicaSet -> Deployment, or Deployment -> ReplicaSet -> Pods.
    Uses ownerReferences to walk up and label selectors to walk down.

    Args:
        namespace: Kubernetes namespace.
        name: Resource name.
        kind: Resource kind (e.g. 'Pod', 'Deployment', 'StatefulSet'). Default 'Pod'.
    """
    core = _kc.get_core_client()
    apps = _kc.get_apps_client()

    lines = [f"Relationships for {kind}/{name} in {namespace}:"]

    # Walk UP the owner chain
    current_kind = kind
    current_name = name
    owners_chain = []

    for _ in range(5):  # max depth
        try:
            if current_kind == "Pod":
                obj = _kc.safe(lambda: core.read_namespaced_pod(current_name, namespace))
            elif current_kind == "ReplicaSet":
                obj = _kc.safe(lambda: apps.read_namespaced_replica_set(current_name, namespace))
            elif current_kind == "Deployment":
                obj = _kc.safe(lambda: apps.read_namespaced_deployment(current_name, namespace))
            elif current_kind == "StatefulSet":
                obj = _kc.safe(lambda: apps.read_namespaced_stateful_set(current_name, namespace))
            elif current_kind == "DaemonSet":
                obj = _kc.safe(lambda: apps.read_namespaced_daemon_set(current_name, namespace))
            elif current_kind == "Job":
                obj = _kc.safe(lambda: _kc.get_batch_client().read_namespaced_job(current_name, namespace))
            elif current_kind == "CronJob":
                obj = _kc.safe(lambda: _kc.get_batch_client().read_namespaced_cron_job(current_name, namespace))
            else:
                break

            if isinstance(obj, ToolError):
                break

            owner_refs = obj.metadata.owner_references or []
            if not owner_refs:
                break

            owner = owner_refs[0]
            owners_chain.append(f"{current_kind}/{current_name} → owned by → {owner.kind}/{owner.name}")
            current_kind = owner.kind
            current_name = owner.name
        except Exception:
            break

    # Walk DOWN — find resources owned by the target
    children = []
    try:
        all_pods = _kc.safe(lambda: core.list_namespaced_pod(namespace, limit=200))
        if not isinstance(all_pods, ToolError):
            for pod in all_pods.items:
                for ref in pod.metadata.owner_references or []:
                    if ref.name == name and ref.kind == kind:
                        children.append(f"  → Pod/{pod.metadata.name} (status={pod.status.phase})")
                    # Also check if owned by a child ReplicaSet of this Deployment
                    if kind == "Deployment":
                        # Check if this pod's RS is owned by our deployment
                        pass
    except Exception:
        pass

    # Build output
    if owners_chain:
        lines.append("\nOwnership chain (upward):")
        for o in owners_chain:
            lines.append(f"  {o}")
        lines.append(f"  Root: {current_kind}/{current_name}")

    if children:
        lines.append(f"\nOwned resources ({len(children)}):")
        lines.extend(children[:20])

    # Well-known labels
    try:
        if kind == "Pod":
            obj = _kc.safe(lambda: core.read_namespaced_pod(name, namespace))
        elif kind == "Deployment":
            obj = _kc.safe(lambda: apps.read_namespaced_deployment(name, namespace))
        else:
            obj = None

        if obj and not isinstance(obj, ToolError):
            labels = obj.metadata.labels or {}
            important = {
                k: v for k, v in labels.items() if k.startswith("app.kubernetes.io/") or k in ("app", "version")
            }
            if important:
                lines.append("\nWell-known labels:")
                for k, v in sorted(important.items()):
                    lines.append(f"  {k}={v}")
    except Exception:
        pass

    text = "\n".join(lines)

    # Build a visual relationship tree
    _KIND_GVR_MAP = {
        "Pod": "v1~pods",
        "Deployment": "apps~v1~deployments",
        "ReplicaSet": "apps~v1~replicasets",
        "StatefulSet": "apps~v1~statefulsets",
        "DaemonSet": "apps~v1~daemonsets",
        "Job": "batch~v1~jobs",
        "CronJob": "batch~v1~cronjobs",
        "Service": "v1~services",
        "Node": "v1~nodes",
        "ConfigMap": "v1~configmaps",
    }

    tree_nodes = []
    node_ids = set()

    # Add the target resource
    target_id = f"{kind}/{name}"
    tree_nodes.append(
        {
            "id": target_id,
            "label": target_id,
            "kind": kind,
            "name": name,
            "namespace": namespace,
            "status": "healthy",
            "gvr": _KIND_GVR_MAP.get(kind, ""),
            "children": [],
            "detail": "",
        }
    )
    node_ids.add(target_id)

    # Build tree from owners_chain (walk up)
    prev_id = target_id
    for o in owners_chain:
        parts = o.split(" → owned by → ")
        if len(parts) == 2:
            owner_str = parts[1]
            owner_kind, owner_name = owner_str.split("/", 1) if "/" in owner_str else (owner_str, "")
            owner_id = owner_str
            if owner_id not in node_ids:
                tree_nodes.append(
                    {
                        "id": owner_id,
                        "label": owner_id,
                        "kind": owner_kind,
                        "name": owner_name,
                        "namespace": namespace,
                        "status": "healthy",
                        "gvr": _KIND_GVR_MAP.get(owner_kind, ""),
                        "children": [prev_id],
                        "detail": "",
                    }
                )
                node_ids.add(owner_id)
            else:
                # Add child to existing node
                for n in tree_nodes:
                    if n["id"] == owner_id and prev_id not in n["children"]:
                        n["children"].append(prev_id)
            prev_id = owner_id

    root_id = prev_id  # topmost owner

    # Add children (pods owned by target)
    for c in children[:15]:
        parts = c.strip().split("(")
        cname = parts[0].strip().lstrip("→ ").strip()
        status_str = parts[1].rstrip(")").split("=")[1] if len(parts) > 1 else "unknown"
        child_kind = cname.split("/")[0] if "/" in cname else "Pod"
        child_name = cname.split("/")[1] if "/" in cname else cname
        child_id = cname
        if child_id not in node_ids:
            tree_nodes.append(
                {
                    "id": child_id,
                    "label": child_id,
                    "kind": child_kind,
                    "name": child_name,
                    "namespace": namespace,
                    "status": "healthy"
                    if status_str == "Running"
                    else "warning"
                    if status_str == "Pending"
                    else "error",
                    "gvr": _KIND_GVR_MAP.get(child_kind, ""),
                    "children": [],
                    "detail": status_str,
                }
            )
            node_ids.add(child_id)
        # Add to target's children
        for n in tree_nodes:
            if n["id"] == target_id and child_id not in n["children"]:
                n["children"].append(child_id)

    component = (
        {
            "kind": "relationship_tree",
            "title": f"Relationships — {kind}/{name}",
            "description": f"Ownership hierarchy in namespace {namespace}",
            "nodes": tree_nodes,
            "rootId": root_id,
        }
        if tree_nodes
        else None
    )

    return (text, component) if component else text


@beta_tool
def describe_resource(namespace: str, name: str, kind: str, group: str = "", version: str = "v1") -> str:
    """Get detailed information about any Kubernetes resource including metadata, status, conditions, and events. Use this for resources that don't have a specialized describe tool.

    Args:
        namespace: Kubernetes namespace. Use '_' for cluster-scoped resources.
        name: Name of the resource.
        kind: Resource kind (e.g. 'ConfigMap', 'Service', 'StatefulSet').
        group: API group (e.g. 'apps', 'batch'). Empty for core resources.
        version: API version (default 'v1').
    """
    if err := _validate_k8s_name(name):
        return err

    plural = _resolve_plural(kind)

    # Build API URL path
    if group:
        api_base = f"/apis/{group}/{version}"
    else:
        api_base = f"/api/{version}"

    if namespace and namespace != "_":
        path = f"{api_base}/namespaces/{namespace}/{plural}/{name}"
    else:
        path = f"{api_base}/{plural}/{name}"

    # Reuse the existing core client's ApiClient (singleton, already authed)
    api = _kc.get_core_client().api_client

    try:
        resp = api.call_api(
            path,
            "GET",
            auth_settings=["BearerToken"],
            response_type="object",
            _return_http_data_only=True,
        )
        # resp is a tuple (data, status, headers) or just data depending on version
        obj = resp if isinstance(resp, dict) else resp[0] if isinstance(resp, tuple) else resp
    except ApiException as e:
        return f"Error ({e.status}): {e.reason}"
    except Exception as e:
        return f"Error fetching {kind}/{name}: {type(e).__name__}: {e}"

    if not isinstance(obj, dict):
        return f"Unexpected response type: {type(obj).__name__}"

    # Extract key sections for structured display
    metadata = obj.get("metadata", {})
    status = obj.get("status", {})
    labels = metadata.get("labels", {}) or {}
    annotations = metadata.get("annotations", {}) or {}
    owner_refs = metadata.get("ownerReferences", [])

    # Build key-value component
    pairs = [
        {"key": "Name", "value": str(metadata.get("name", ""))},
        {"key": "Namespace", "value": str(metadata.get("namespace", "cluster-scoped"))},
        {"key": "Kind", "value": kind},
        {"key": "Labels", "value": str(len(labels))},
        {"key": "Annotations", "value": str(len(annotations))},
        {
            "key": "Age",
            "value": _kc.age(datetime.fromisoformat(metadata["creationTimestamp"].replace("Z", "+00:00")))
            if metadata.get("creationTimestamp")
            else "unknown",
        },
    ]
    if owner_refs:
        owners = ", ".join(f"{r.get('kind', '?')}/{r.get('name', '?')}" for r in owner_refs)
        pairs.append({"key": "Owned By", "value": owners})

    components: list[dict] = [
        {"kind": "key_value", "title": f"{kind} — {name}", "pairs": pairs},
    ]

    # Labels as badges
    if labels:
        components.append(
            {
                "kind": "badge_list",
                "badges": [{"text": f"{k}={v}", "variant": "info"} for k, v in list(labels.items())[:10]],
            }
        )

    # Status conditions
    conditions = status.get("conditions", [])
    if conditions:
        components.append(
            {
                "kind": "status_list",
                "title": "Conditions",
                "items": [
                    {
                        "name": c.get("type", "?"),
                        "status": "healthy" if c.get("status") == "True" else "error",
                        "detail": c.get("reason") or c.get("message") or "",
                    }
                    for c in conditions
                ],
            }
        )

    # Fetch related events
    if namespace and namespace != "_":
        core = _kc.get_core_client()
        events = _kc.safe(
            lambda: core.list_namespaced_event(
                namespace,
                field_selector=f"involvedObject.name={name},involvedObject.kind={kind}",
            )
        )
        if not isinstance(events, ToolError) and events.items:
            sorted_events = sorted(
                events.items,
                key=lambda e: e.last_timestamp or datetime.min.replace(tzinfo=UTC),
                reverse=True,
            )[:10]
            event_rows = [
                {
                    "age": _kc.age(e.last_timestamp),
                    "type": e.type or "Normal",
                    "reason": e.reason or "",
                    "message": (e.message or "")[:120],
                }
                for e in sorted_events
            ]
            components.append(
                {
                    "kind": "data_table",
                    "title": f"Events ({len(event_rows)})",
                    "columns": [
                        {"id": "age", "header": "Age"},
                        {"id": "type", "header": "Type"},
                        {"id": "reason", "header": "Reason"},
                        {"id": "message", "header": "Message"},
                    ],
                    "rows": event_rows,
                }
            )

    text = json.dumps(obj, indent=2, default=str)

    # Add YAML manifest viewer (spec only, not full object — cleaner for users)
    spec = obj.get("spec")
    if spec:
        import yaml as _yaml

        try:
            yaml_content = _yaml.dump(spec, default_flow_style=False)
        except Exception:
            yaml_content = json.dumps(spec, indent=2, default=str)
        components.append(
            {
                "kind": "yaml_viewer",
                "title": f"{kind} Spec",
                "content": yaml_content,
                "language": "yaml",
            }
        )

    component = {
        "kind": "section",
        "title": f"{kind} Details — {name}",
        "collapsible": False,
        "defaultOpen": True,
        "components": components,
    }
    return (text, component)
