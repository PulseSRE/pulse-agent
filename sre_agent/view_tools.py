"""Tools for creating custom dashboard views from conversation context."""

from __future__ import annotations

import contextvars
import json
import uuid

from .decorators import beta_tool
from .quality_engine import critique_view
from .tool_registry import register_tool

# Structured signal prefix — tools return this instead of magic string markers.
# The API layer checks tool results for this prefix and extracts the JSON signal.
SIGNAL_PREFIX = "__SIGNAL__"


def _signal(signal_type: str, message: str, **kwargs) -> str:
    """Return a structured signal that the API layer can process.

    The returned string contains both a human-readable message (for Claude)
    and a JSON signal (for the API layer) separated by the SIGNAL_PREFIX.
    """
    payload = {"type": signal_type, **kwargs}
    return f"{message}\n{SIGNAL_PREFIX}{json.dumps(payload)}"


# Context variable for current user identity — async-safe and propagates
# across asyncio.to_thread boundaries (unlike threading.local or globals).
_current_user_var: contextvars.ContextVar[str] = contextvars.ContextVar("current_user", default="anonymous")


def set_current_user(owner: str) -> None:
    """Set the current user for view tools (called by API layer per-request)."""
    _current_user_var.set(owner)


def get_current_user() -> str:
    """Get the current user identity."""
    return _current_user_var.get()


@beta_tool
def create_dashboard(title: str, description: str = ""):
    """Create a custom dashboard view. Quality is auto-validated on save — no need to call critique_view.

    Layout is computed automatically based on component types.

    Args:
        title: Name for the dashboard (e.g. "SRE Overview", "Node Health").
        description: Brief description of what the dashboard shows.
    """
    view_id = f"cv-{uuid.uuid4().hex[:12]}"
    kwargs = {"view_id": view_id, "title": title, "description": description}
    return _signal(
        "view_spec",
        f"Created view '{title}' with ID {view_id}. "
        f"The dashboard is now saved and visible to the user. "
        f"Tell the user: 'Here is your dashboard. Would you like any changes?'",
        **kwargs,
    )


@beta_tool
def namespace_summary(namespace: str):
    """Get a high-level summary of a namespace: pod counts by status, deployment health, warning events, and resource usage. Use this as the first tool when the user asks for an overview of a namespace.

    Args:
        namespace: Kubernetes namespace to summarize.
    """
    from .errors import ToolError
    from .k8s_client import get_apps_client, get_core_client, safe

    core = get_core_client()

    # Pod counts
    pods_result = safe(lambda: core.list_namespaced_pod(namespace, limit=500))
    if isinstance(pods_result, ToolError):
        return str(pods_result)

    total_pods = len(pods_result.items)
    running = sum(1 for p in pods_result.items if p.status.phase == "Running")
    failed = sum(1 for p in pods_result.items if p.status.phase == "Failed")
    pending = sum(1 for p in pods_result.items if p.status.phase == "Pending")
    crashloop = sum(
        1
        for p in pods_result.items
        for cs in (p.status.container_statuses or [])
        if cs.state and cs.state.waiting and cs.state.waiting.reason == "CrashLoopBackOff"
    )

    # Deployment counts
    apps = get_apps_client()
    deps_result = safe(lambda: apps.list_namespaced_deployment(namespace, limit=500))
    total_deps = 0
    healthy_deps = 0
    degraded_deps = 0
    if not isinstance(deps_result, ToolError):
        total_deps = len(deps_result.items)
        for dep in deps_result.items:
            ready = dep.status.ready_replicas or 0
            desired = dep.status.replicas or 0
            if ready == desired and desired > 0:
                healthy_deps += 1
            elif ready < desired:
                degraded_deps += 1

    # Additional resource counts
    sts_result = safe(lambda: apps.list_namespaced_stateful_set(namespace, limit=500))
    sts_count = 0 if isinstance(sts_result, ToolError) else len(sts_result.items)

    ds_result = safe(lambda: apps.list_namespaced_daemon_set(namespace, limit=500))
    ds_count = 0 if isinstance(ds_result, ToolError) else len(ds_result.items)

    svc_result = safe(lambda: core.list_namespaced_service(namespace, limit=500))
    svc_count = 0 if isinstance(svc_result, ToolError) else len(svc_result.items)

    cm_result = safe(lambda: core.list_namespaced_config_map(namespace, limit=500))
    cm_count = 0 if isinstance(cm_result, ToolError) else len(cm_result.items)

    secret_result = safe(lambda: core.list_namespaced_secret(namespace, limit=500))
    secret_count = 0 if isinstance(secret_result, ToolError) else len(secret_result.items)

    pvc_result = safe(lambda: core.list_namespaced_persistent_volume_claim(namespace, limit=500))
    pvc_count = 0 if isinstance(pvc_result, ToolError) else len(pvc_result.items)

    # Routes (OpenShift) and Ingresses
    route_count = 0
    try:
        from .k8s_client import get_custom_client

        custom = get_custom_client()
        routes = safe(lambda: custom.list_namespaced_custom_object("route.openshift.io", "v1", namespace, "routes"))
        if not isinstance(routes, ToolError):
            route_count = len(routes.get("items", []))
    except Exception:
        pass

    ingress_count = 0
    try:
        from .k8s_client import get_networking_client

        networking = get_networking_client()
        ingresses = safe(lambda: networking.list_namespaced_ingress(namespace, limit=500))
        if not isinstance(ingresses, ToolError):
            ingress_count = len(ingresses.items)
    except Exception:
        pass

    # Warning events (last hour)
    events_result = safe(lambda: core.list_namespaced_event(namespace, field_selector="type=Warning"))
    warning_count = 0
    if not isinstance(events_result, ToolError):
        warning_count = len(events_result.items)

    # Build text summary
    text = (
        f"Namespace '{namespace}' summary:\n"
        f"  Pods: {total_pods} total — {running} running, {pending} pending, "
        f"{failed} failed, {crashloop} crashlooping\n"
        f"  Deployments: {total_deps} total — {healthy_deps} healthy, "
        f"{degraded_deps} degraded\n"
        f"  StatefulSets: {sts_count} | DaemonSets: {ds_count} | "
        f"Services: {svc_count} | ConfigMaps: {cm_count}\n"
        f"  Routes: {route_count} | Ingresses: {ingress_count} | "
        f"Secrets: {secret_count} | PVCs: {pvc_count}\n"
        f"  Warning events: {warning_count}"
    )

    # Build resource counts component (clickable summary cards)
    resource_counts = {
        "kind": "resource_counts",
        "title": f"{namespace} Resources",
        "namespace": namespace,
        "items": [
            {"resource": "pods", "count": total_pods, "gvr": "v1~pods", "status": "error" if failed > 0 else "healthy"},
            {
                "resource": "deployments",
                "count": total_deps,
                "gvr": "apps~v1~deployments",
                "status": "warning" if degraded_deps > 0 else "healthy",
            },
            {"resource": "statefulsets", "count": sts_count, "gvr": "apps~v1~statefulsets"},
            {"resource": "daemonsets", "count": ds_count, "gvr": "apps~v1~daemonsets"},
            {"resource": "services", "count": svc_count, "gvr": "v1~services"},
            {"resource": "routes", "count": route_count, "gvr": "route.openshift.io~v1~routes"},
            {"resource": "ingresses", "count": ingress_count, "gvr": "networking.k8s.io~v1~ingresses"},
            {"resource": "secrets", "count": secret_count, "gvr": "v1~secrets"},
            {"resource": "pvcs", "count": pvc_count, "gvr": "v1~persistentvolumeclaims"},
            {"resource": "configmaps", "count": cm_count, "gvr": "v1~configmaps"},
            {
                "resource": "events",
                "count": warning_count,
                "gvr": "v1~events",
                "status": "warning" if warning_count > 0 else "healthy",
            },
        ],
    }

    # Build metric cards — only trends that ADD info beyond the resource counts row
    cards = [
        {
            "kind": "metric_card",
            "title": "CPU Usage",
            "query": f'sum(rate(container_cpu_usage_seconds_total{{namespace="{namespace}",container!=""}}[5m]))',
            "color": "#3b82f6",
            "unit": " cores",
        },
        {
            "kind": "metric_card",
            "title": "Memory Usage",
            "query": f'sum(container_memory_working_set_bytes{{namespace="{namespace}",container!=""}}) / 1024 / 1024 / 1024',
            "color": "#8b5cf6",
            "unit": " Gi",
        },
        {
            "kind": "metric_card",
            "title": "Restart Rate",
            "value": str(crashloop),
            "status": "healthy" if crashloop == 0 else "error",
            "description": f"{failed} failed" if failed else "none",
            "query": f'sum(rate(kube_pod_container_status_restarts_total{{namespace="{namespace}"}}[5m]))',
            "color": "#ef4444" if crashloop > 0 else "#10b981",
            "link": f"/r/v1~pods?ns={namespace}",
        },
        {
            "kind": "metric_card",
            "title": "Warning Events",
            "value": str(warning_count),
            "status": "healthy" if warning_count == 0 else "warning",
            "description": "active",
            "query": f"count(kube_event_count{{namespace=\"{namespace}\",type='Warning'}}) or vector(0)",
            "color": "#f59e0b" if warning_count > 0 else "#10b981",
            "link": f"/r/v1~events?ns={namespace}",
        },
    ]
    component = {
        "kind": "grid",
        "title": f"{namespace} Overview",
        "description": f"Key health indicators for the {namespace} namespace",
        "columns": 4,
        "items": [resource_counts] + cards,
    }
    return (text, component)


@beta_tool
def cluster_metrics(category: str = "overview"):
    """Get key cluster metrics as metric cards for dashboard headers.

    Returns different KPI cards based on the category — pick the one that
    matches the dashboard topic, not always the generic overview.

    Args:
        category: One of: 'overview' (nodes, pods, CPU%, memory%),
                  'network' (traffic in/out, errors, dropped packets),
                  'storage' (PVC count, disk usage, PV available),
                  'security' (firing alerts, degraded operators, RBAC risks),
                  'workloads' (deployments, replicas, restarts, HPA),
                  'control_plane' (API latency, etcd leader, scheduler).
    """
    from .errors import ToolError
    from .k8s_client import get_core_client, safe

    # Category-specific metric card definitions
    _CATEGORY_CARDS: dict[str, list[dict]] = {
        "overview": [
            {
                "kind": "metric_card",
                "title": "Nodes Ready",
                "value": "",
                "query": "sum(kube_node_status_condition{condition='Ready',status='true'})",
                "description": "Healthy cluster nodes",
                "color": "#10b981",
                "thresholds": {"warning": 2, "critical": 1},
                "link": "/compute",
            },
            {
                "kind": "metric_card",
                "title": "Pods Running",
                "value": "",
                "query": "count(kube_pod_status_phase{phase='Running'})",
                "description": "Active workload pods",
                "color": "#3b82f6",
                "link": "/workloads",
            },
            {
                "kind": "metric_card",
                "title": "Cluster CPU",
                "value": "",
                "unit": "%",
                "query": "100 - avg(rate(node_cpu_seconds_total{mode='idle'}[5m])) * 100",
                "color": "#3b82f6",
                "thresholds": {"warning": 70, "critical": 90},
            },
            {
                "kind": "metric_card",
                "title": "Cluster Memory",
                "value": "",
                "unit": "%",
                "query": "100 - (sum(node_memory_MemAvailable_bytes) / sum(node_memory_MemTotal_bytes)) * 100",
                "color": "#8b5cf6",
                "thresholds": {"warning": 80, "critical": 95},
            },
        ],
        "network": [
            {
                "kind": "metric_card",
                "title": "Network Receive",
                "value": "",
                "unit": "B/s",
                "query": "sum(rate(container_network_receive_bytes_total[5m]))",
                "description": "Cluster inbound traffic",
                "color": "#3b82f6",
            },
            {
                "kind": "metric_card",
                "title": "Network Transmit",
                "value": "",
                "unit": "B/s",
                "query": "sum(rate(container_network_transmit_bytes_total[5m]))",
                "description": "Cluster outbound traffic",
                "color": "#8b5cf6",
            },
            {
                "kind": "metric_card",
                "title": "Packet Drops",
                "value": "",
                "query": "sum(rate(container_network_receive_packets_dropped_total[5m])) + sum(rate(container_network_transmit_packets_dropped_total[5m]))",
                "description": "Total dropped packets/s",
                "color": "#f59e0b",
                "thresholds": {"warning": 10, "critical": 100},
            },
            {
                "kind": "metric_card",
                "title": "Network Errors",
                "value": "",
                "query": "sum(rate(container_network_receive_errors_total[5m])) + sum(rate(container_network_transmit_errors_total[5m]))",
                "description": "Total network errors/s",
                "color": "#ef4444",
                "thresholds": {"warning": 1, "critical": 10},
            },
        ],
        "storage": [
            {
                "kind": "metric_card",
                "title": "PVCs Bound",
                "value": "",
                "query": "count(kube_persistentvolumeclaim_status_phase{phase='Bound'})",
                "description": "Bound persistent volume claims",
                "color": "#10b981",
            },
            {
                "kind": "metric_card",
                "title": "PVCs Pending",
                "value": "",
                "query": "count(kube_persistentvolumeclaim_status_phase{phase='Pending'}) or vector(0)",
                "description": "Waiting for provisioning",
                "color": "#f59e0b",
                "thresholds": {"warning": 1, "critical": 5},
            },
            {
                "kind": "metric_card",
                "title": "Disk Usage",
                "value": "",
                "unit": "%",
                "query": "100 - (sum(node_filesystem_avail_bytes{device=~'/.*'}) / sum(node_filesystem_size_bytes{device=~'/.*'})) * 100",
                "description": "Cluster filesystem usage",
                "color": "#8b5cf6",
                "thresholds": {"warning": 80, "critical": 90},
            },
            {
                "kind": "metric_card",
                "title": "PVs Available",
                "value": "",
                "query": "count(kube_persistentvolume_status_phase{phase='Available'}) or vector(0)",
                "description": "Unbound persistent volumes",
                "color": "#3b82f6",
            },
        ],
        "security": [
            {
                "kind": "metric_card",
                "title": "Firing Alerts",
                "value": "",
                "query": "count(ALERTS{alertstate='firing'}) or vector(0)",
                "description": "Active alert count",
                "color": "#ef4444",
                "thresholds": {"warning": 1, "critical": 5},
            },
            {
                "kind": "metric_card",
                "title": "Operators Available",
                "value": "",
                "unit": "%",
                "query": "sum(cluster_operator_up == 1) / count(cluster_operator_up) * 100",
                "description": "Cluster operator health",
                "color": "#10b981",
                "thresholds": {"warning": 95, "critical": 90},
            },
            {
                "kind": "metric_card",
                "title": "Targets Down",
                "value": "",
                "unit": "%",
                "query": "100 * (1 - sum(up) / count(up))",
                "description": "Unreachable Prometheus targets",
                "color": "#f59e0b",
                "thresholds": {"warning": 5, "critical": 15},
            },
            {
                "kind": "metric_card",
                "title": "API Error Rate",
                "value": "",
                "unit": "%",
                "query": "sum(rate(apiserver_request_total{code=~'5..'}[5m])) / sum(rate(apiserver_request_total[5m])) * 100",
                "description": "API server 5xx rate",
                "color": "#ef4444",
                "thresholds": {"warning": 1, "critical": 5},
            },
        ],
        "workloads": [
            {
                "kind": "metric_card",
                "title": "Deployments",
                "value": "",
                "query": "count(kube_deployment_status_replicas)",
                "description": "Total deployments",
                "color": "#3b82f6",
            },
            {
                "kind": "metric_card",
                "title": "Unavailable Replicas",
                "value": "",
                "query": "sum(kube_deployment_status_replicas_unavailable) or vector(0)",
                "description": "Replicas not ready",
                "color": "#ef4444",
                "thresholds": {"warning": 1, "critical": 5},
            },
            {
                "kind": "metric_card",
                "title": "Pod Restarts",
                "value": "",
                "query": "sum(increase(kube_pod_container_status_restarts_total[1h]))",
                "description": "Restarts in last hour",
                "color": "#f59e0b",
                "thresholds": {"warning": 10, "critical": 50},
            },
            {
                "kind": "metric_card",
                "title": "HPA Active",
                "value": "",
                "query": "count(kube_horizontalpodautoscaler_status_current_replicas)",
                "description": "Active autoscalers",
                "color": "#8b5cf6",
            },
        ],
        "control_plane": [
            {
                "kind": "metric_card",
                "title": "API Latency p99",
                "value": "",
                "unit": "s",
                "query": "histogram_quantile(0.99, sum(rate(apiserver_request_duration_seconds_bucket{verb!~'WATCH|CONNECT'}[5m])) by (le))",
                "description": "99th percentile latency",
                "color": "#3b82f6",
                "thresholds": {"warning": 1, "critical": 5},
            },
            {
                "kind": "metric_card",
                "title": "etcd Leader",
                "value": "",
                "query": "max(etcd_server_has_leader)",
                "description": "1 = has leader",
                "color": "#10b981",
            },
            {
                "kind": "metric_card",
                "title": "API Request Rate",
                "value": "",
                "unit": "/s",
                "query": "sum(rate(apiserver_request_total[5m]))",
                "description": "Requests per second",
                "color": "#8b5cf6",
            },
            {
                "kind": "metric_card",
                "title": "Scheduler Latency",
                "value": "",
                "unit": "s",
                "query": "histogram_quantile(0.99, sum(rate(scheduler_e2e_scheduling_duration_seconds_bucket[5m])) by (le))",
                "description": "p99 scheduling latency",
                "color": "#f59e0b",
                "thresholds": {"warning": 1, "critical": 10},
            },
        ],
    }

    if category not in _CATEGORY_CARDS:
        category = "overview"

    cards = _CATEGORY_CARDS[category]

    # For overview, enrich with live K8s data
    if category == "overview":
        core = get_core_client()
        nodes_result = safe(lambda: core.list_node())
        node_count = 0
        nodes_ready = 0
        if not isinstance(nodes_result, ToolError):
            node_count = len(nodes_result.items)
            nodes_ready = sum(
                1
                for n in nodes_result.items
                for c in (n.status.conditions or [])
                if c.type == "Ready" and c.status == "True"
            )

        pods_result = safe(lambda: core.list_pod_for_all_namespaces(limit=1000))
        pod_count = 0
        pods_running = 0
        pods_failing = 0
        if not isinstance(pods_result, ToolError):
            pod_count = len(pods_result.items)
            pods_running = sum(1 for p in pods_result.items if p.status.phase == "Running")
            pods_failing = sum(1 for p in pods_result.items if p.status.phase in ("Failed", "Unknown"))

        cards = [
            {
                **cards[0],
                "value": f"{nodes_ready}/{node_count}",
                "status": "healthy" if nodes_ready == node_count else "warning",
            },
            {
                **cards[1],
                "value": str(pods_running),
                "status": "healthy" if pods_failing == 0 else "warning",
                "description": f"{pods_failing} failing" if pods_failing else f"{pod_count} total",
            },
            cards[2],
            cards[3],
        ]

    # Add stat_card summary for overview (big numbers without sparklines)
    if category == "overview":
        try:
            ns_result = safe(lambda: core.list_namespace())
            if not isinstance(ns_result, ToolError):
                active_ns = sum(1 for n in ns_result.items if n.status.phase == "Active")
                cards.append(
                    {
                        "kind": "stat_card",
                        "title": "Namespaces",
                        "value": str(active_ns),
                        "description": "Active namespaces",
                    }
                )
        except Exception:
            pass

    text = f"Cluster metrics ({category}): {len(cards)} KPI cards"
    component = {
        "kind": "grid",
        "title": f"Cluster {category.replace('_', ' ').title()}",
        "columns": min(len(cards), 4),
        "items": cards,
    }
    return (text, component)


@beta_tool
def list_saved_views():
    """List all custom dashboard views saved by the current user. Returns view titles, descriptions, widget counts, and direct links. Use this when the user asks to see their dashboards, saved views, or custom views."""
    from . import db

    owner = get_current_user()
    views = db.list_views(owner)
    if not views:
        # Fallback: query all views (user identity may differ across sessions/redeploys)
        try:
            _db = db.get_database()
            rows = _db.fetchall(
                "SELECT id, owner, title, description, icon, layout, positions, created_at, updated_at "
                "FROM views ORDER BY updated_at DESC LIMIT 50"
            )
            views = [db._deserialize_view_row(r) for r in rows] if rows else []
        except Exception:
            pass
        if not views:
            return "No saved views found. You can create one by asking me to build a dashboard."

    lines = []
    rows = []
    for v in views:
        widget_count = len(v.get("layout", []))
        lines.append(f"  {v['title']} — {widget_count} widgets — /custom/{v['id']}")
        rows.append(
            {
                "title": v["title"],
                "description": v.get("description", ""),
                "widgets": widget_count,
                "link": f"/custom/{v['id']}",
                "updated": v.get("updated_at", ""),
            }
        )

    text = f"Your saved dashboards ({len(views)}):\n" + "\n".join(lines)
    component = {
        "kind": "data_table",
        "title": f"Your Dashboards ({len(rows)})",
        "columns": [
            {"id": "title", "header": "Title"},
            {"id": "description", "header": "Description"},
            {"id": "widgets", "header": "Widgets"},
            {"id": "link", "header": "Link"},
            {"id": "updated", "header": "Updated"},
        ],
        "rows": rows,
    }
    return (text, component)


@beta_tool
def get_view_details(view_id: str):
    """Get details of a saved view including its widget list. Use this before modifying a view to understand its current state.

    Args:
        view_id: The view ID (e.g. 'cv-abc123'). Use list_saved_views first to find IDs.
    """
    from . import db

    owner = get_current_user()
    view = db.get_view(view_id, owner)
    if not view:
        view = db.get_view(view_id)  # Fallback without owner filter
    if not view:
        return f"View '{view_id}' not found."

    widgets = view.get("layout", [])
    lines = [f"View: {view['title']}", f"Description: {view.get('description', '')}", f"Widgets ({len(widgets)}):"]
    for i, w in enumerate(widgets):
        kind = w.get("kind", "unknown")
        title = w.get("title", w.get("kind", "untitled"))
        lines.append(f"  [{i}] {kind}: {title}")

    return "\n".join(lines)


@beta_tool
def add_widget_to_view(view_id: str):
    """Add the most recent component from this conversation to an existing view. Call this AFTER calling a data tool (like get_prometheus_query, list_pods, etc.) that generated a component. The UI will auto-refresh.

    Args:
        view_id: The view ID to add the widget to (e.g. 'cv-abc123').
    """
    return _signal("add_widget", f"Adding latest component to view {view_id}.", view_id=view_id)


@beta_tool
def emit_component(kind: str, spec_json: str):
    """Emit a custom component for the current dashboard. Use for bar_list, progress_list, stat_card, or any component type.

    The component is added to the session and will be included when create_dashboard is called.

    Args:
        kind: Component kind (e.g. 'bar_list', 'progress_list', 'stat_card', 'status_list').
        spec_json: JSON string with the component spec. Must include all required fields for the kind.
            Example bar_list: {"title": "Top Pods", "items": [{"label": "nginx", "value": 42}]}
            Example progress_list: {"title": "Node CPU", "items": [{"label": "node-1", "value": 70, "max": 100, "unit": "%"}]}
            Example stat_card: {"title": "Error Rate", "value": "2.3", "unit": "%", "trend": "down", "trendValue": "12%"}
    """
    import json as _json

    try:
        spec = _json.loads(spec_json)
    except _json.JSONDecodeError as e:
        return f"Invalid JSON: {e}"

    spec["kind"] = kind

    from .quality_engine import VALID_KINDS, QualityResult, _validate_component

    if kind not in VALID_KINDS:
        return f"Invalid kind '{kind}'. Valid: {', '.join(sorted(VALID_KINDS))}"

    # Schema-only validation (no dashboard structure rules like "must have chart + table")
    result = QualityResult()
    _validate_component(spec, result)
    if result.errors:
        return f"Invalid {kind} spec: {'; '.join(result.errors)}"

    text = f"Emitted {kind} component"
    if spec.get("title"):
        text += f": {spec['title']}"

    return (text, spec)


@beta_tool
def delete_dashboard(view_id: str):
    """Delete a saved dashboard view permanently. This cannot be undone.

    Args:
        view_id: The view ID to delete (e.g. 'cv-abc123'). Use list_saved_views to find IDs.
    """
    from . import db

    owner = get_current_user()
    # Try with current user first, then fallback to view's actual owner
    success = db.delete_view(view_id, owner)
    if not success:
        view = db.get_view(view_id)
        if view:
            success = db.delete_view(view_id, view.get("owner", owner))
    if not success:
        return f"View '{view_id}' not found or you don't have permission to delete it."
    return _signal("view_deleted", f"Deleted dashboard {view_id}.", view_id=view_id)


@beta_tool
def clone_dashboard(view_id: str, new_title: str = ""):
    """Clone an existing dashboard to create a copy you can modify independently.

    Args:
        view_id: The view ID to clone (e.g. 'cv-abc123'). Use list_saved_views to find IDs.
        new_title: Optional new title for the cloned view. If empty, appends '(copy)' to original title.
    """
    from . import db

    owner = get_current_user()
    new_id = db.clone_view(view_id, owner)
    if not new_id:
        return f"View '{view_id}' not found or you don't have permission to clone it."

    # Rename if new_title provided
    if new_title:
        db.update_view(new_id, owner, title=new_title)

    return _signal(
        "view_cloned",
        f"Cloned dashboard {view_id} → {new_id}." + (f" Renamed to '{new_title}'." if new_title else ""),
        view_id=new_id,
        source_view_id=view_id,
    )


register_tool(delete_dashboard)
register_tool(clone_dashboard)
register_tool(create_dashboard)
register_tool(namespace_summary)
register_tool(cluster_metrics)
register_tool(list_saved_views)
register_tool(get_view_details)
register_tool(add_widget_to_view)
register_tool(emit_component)

# Exported list for view_designer agent
# (critique_view already imported at top)

# Import mutation tools module to register tools
from .view_mutations import (
    get_view_versions,
    optimize_view,
    remove_widget_from_view,
    undo_view_change,
    update_view_widgets,
)
from .view_planner import plan_dashboard

register_tool(critique_view)
register_tool(plan_dashboard)


VALID_TOPOLOGY_KINDS = frozenset(
    {
        "Node",
        "Pod",
        "Deployment",
        "ReplicaSet",
        "StatefulSet",
        "DaemonSet",
        "Job",
        "CronJob",
        "Service",
        "Ingress",
        "Route",
        "ConfigMap",
        "Secret",
        "PVC",
        "ServiceAccount",
        "NetworkPolicy",
        "HelmRelease",
        "HPA",
    }
)

VALID_TOPOLOGY_RELATIONSHIPS = frozenset(
    {
        "owns",
        "selects",
        "mounts",
        "references",
        "uses",
        "schedules",
        "routes_to",
        "applies_to",
        "scales",
        "manages",
    }
)

VALID_LAYOUT_HINTS = frozenset({"top-down", "left-to-right", "grouped"})

_MAX_GROUP_SIZE = 20


def get_topology_graph(
    namespace: str = "",
    kinds: str = "",
    relationships: str = "",
    layout_hint: str = "",
    include_metrics: bool = False,
    group_by: str = "",
):
    """Build an interactive dependency topology graph showing resource relationships, health status, and risk levels.

    Returns a visual network graph filtered by resource kinds and relationships.
    Each node shows health status (healthy/warning/error).

    Perspective reference — use these parameter patterns:
    - Hardware/capacity: kinds="Node,Pod" relationships="schedules" layout_hint="grouped" include_metrics=true group_by="node"
    - App structure: kinds="Deployment,ReplicaSet,Pod,ConfigMap,Secret,PVC,ServiceAccount" relationships="owns,references,mounts,uses" layout_hint="top-down"
    - Network flow: kinds="Route,Ingress,Service,Pod,NetworkPolicy" relationships="routes_to,selects,applies_to" layout_hint="left-to-right"
    - Tenant usage: kinds="Namespace,Pod,Node" relationships="schedules" layout_hint="grouped" include_metrics=true group_by="namespace"
    - Helm releases: kinds="HelmRelease,Deployment,StatefulSet,Service,ConfigMap,Secret" relationships="manages,owns" layout_hint="grouped"

    Args:
        namespace: Kubernetes namespace to graph. Leave empty for all namespaces.
        kinds: Comma-separated resource types to include (e.g. "Node,Pod,Service"). Empty = all types.
        relationships: Comma-separated relationship types to include (e.g. "owns,selects"). Empty = auto-infer from kinds.
        layout_hint: Layout strategy: "top-down", "left-to-right", or "grouped". Empty = "top-down".
        include_metrics: Fetch CPU/memory metrics from metrics-server for Node/Pod resources.
        group_by: Group nodes by "namespace" or a label key (e.g. "team"). Requires layout_hint="grouped".
    """
    kind_set: set[str] | None = None
    if kinds:
        kind_set = {k.strip() for k in kinds.split(",") if k.strip()}
        invalid = kind_set - VALID_TOPOLOGY_KINDS
        if invalid:
            return (
                f"Invalid kinds: {', '.join(sorted(invalid))}. Valid kinds: {', '.join(sorted(VALID_TOPOLOGY_KINDS))}"
            )

    rel_set: set[str] | None = None
    if relationships:
        rel_set = {r.strip() for r in relationships.split(",") if r.strip()}
        invalid = rel_set - VALID_TOPOLOGY_RELATIONSHIPS
        if invalid:
            return f"Invalid relationships: {', '.join(sorted(invalid))}. Valid relationships: {', '.join(sorted(VALID_TOPOLOGY_RELATIONSHIPS))}"

    if layout_hint and layout_hint not in VALID_LAYOUT_HINTS:
        return f"Invalid layout hint: {layout_hint}. Valid layout hints: {', '.join(sorted(VALID_LAYOUT_HINTS))}"

    from .dependency_graph import get_dependency_graph

    graph = get_dependency_graph()
    nodes: list[dict] = []
    edges: list[dict] = []

    finding_status: dict[str, str] = {}
    try:
        from .db import get_database

        db = get_database()
        rows = db.fetchall("SELECT severity, resources FROM findings WHERE resolved = 0")
        for f in rows or []:
            sev = f.get("severity", "")
            for res_str in (f.get("resources") or "").split(","):
                res_str = res_str.strip()
                if res_str:
                    finding_status[res_str] = "error" if sev in ("critical", "warning") else "warning"
    except Exception:
        pass

    # Cluster-scoped kinds (namespace="") should pass namespace filter when explicitly requested
    cluster_scoped = {"Node", "HPA"}
    for key, node in graph.get_nodes().items():
        if namespace and node.namespace != namespace:
            if not (kind_set and node.kind in kind_set and node.kind in cluster_scoped):
                continue
        if kind_set and node.kind not in kind_set:
            continue
        resource_key = f"{node.kind}:{node.namespace}:{node.name}"
        status = finding_status.get(resource_key, "healthy")
        node_dict: dict = {
            "id": key,
            "kind": node.kind,
            "name": node.name,
            "namespace": node.namespace,
            "status": status,
        }
        if group_by:
            if group_by == "namespace":
                node_dict["group"] = node.namespace or "cluster-scoped"
            elif group_by == "node":
                if node.kind == "Node":
                    node_dict["group"] = node.name
                else:
                    parent_node = None
                    for edge in graph.get_edges():
                        if edge.target == key and edge.relationship == "schedules":
                            src = graph.get_node(edge.source)
                            if src and src.kind == "Node":
                                parent_node = src.name
                                break
                    node_dict["group"] = parent_node or "unscheduled"
            else:
                node_dict["group"] = node.labels.get(group_by, "unlabeled")
        nodes.append(node_dict)

    # Cap group sizes
    if group_by:
        groups: dict[str, list[dict]] = {}
        for n in nodes:
            g = n.get("group", "")
            if g not in groups:
                groups[g] = []
            groups[g].append(n)
        capped: list[dict] = []
        for g, members in groups.items():
            if len(members) <= _MAX_GROUP_SIZE:
                capped.extend(members)
            else:
                capped.extend(members[:_MAX_GROUP_SIZE])
                overflow = len(members) - _MAX_GROUP_SIZE
                capped.append(
                    {
                        "id": f"_summary/{g}",
                        "kind": "Summary",
                        "name": f"+ {overflow} more",
                        "namespace": members[0]["namespace"],
                        "status": "healthy",
                        "group": g,
                    }
                )
        nodes = capped

    # Metrics enrichment
    if include_metrics:
        from .dependency_graph import _fetch_metrics

        node_met, pod_met = _fetch_metrics(namespace)
        for n in nodes:
            if n["kind"] == "Node":
                m = node_met.get(n["name"])
                if m:
                    cpu_pct = round(m["cpu_usage_m"] * 100 / m["cpu_capacity_m"]) if m["cpu_capacity_m"] else 0
                    mem_pct = round(m["memory_usage_b"] * 100 / m["memory_capacity_b"]) if m["memory_capacity_b"] else 0
                    n["metrics"] = {
                        "cpu_usage": m["cpu_usage"],
                        "cpu_capacity": m["cpu_capacity"],
                        "cpu_percent": cpu_pct,
                        "memory_usage": m["memory_usage"],
                        "memory_capacity": m["memory_capacity"],
                        "memory_percent": mem_pct,
                    }
            elif n["kind"] == "Pod":
                key = f"{n['namespace']}/{n['name']}"
                m = pod_met.get(key)
                if m:
                    n["metrics"] = {
                        "cpu_usage": m["cpu_usage"],
                        "memory_usage": m["memory_usage"],
                        "cpu_percent": 0,
                        "memory_percent": 0,
                    }

    node_ids = {n["id"] for n in nodes}
    node_kinds = {n["kind"] for n in nodes}

    for edge in graph.get_edges():
        if edge.source not in node_ids or edge.target not in node_ids:
            continue
        if rel_set and edge.relationship not in rel_set:
            continue
        if kind_set and not rel_set:
            src_node = graph.get_node(edge.source)
            tgt_node = graph.get_node(edge.target)
            if src_node and tgt_node:
                if src_node.kind not in node_kinds or tgt_node.kind not in node_kinds:
                    continue
        edges.append(
            {
                "source": edge.source,
                "target": edge.target,
                "relationship": edge.relationship,
            }
        )

    if kind_set and rel_set and not edges and nodes:
        return (
            f"No edges possible: relationship types {', '.join(sorted(rel_set))} do not connect "
            f"the given kinds {', '.join(sorted(kind_set))}. Try removing the relationships filter "
            f"or adding the missing kinds."
        )

    if not nodes:
        ns_label = f" in {namespace}" if namespace else ""
        return f"No topology data available{ns_label}. The dependency graph is built during monitor scans."

    ns_label = f" — {namespace}" if namespace else ""
    kind_counts: dict[str, int] = {}
    for n in nodes:
        kind_counts[n["kind"]] = kind_counts.get(n["kind"], 0) + 1
    summary_parts = [f"{c} {k}s" for k, c in sorted(kind_counts.items(), key=lambda x: -x[1])]

    text = (
        f"Topology graph{ns_label}: {len(nodes)} resources, {len(edges)} relationships. "
        f"Resources: {', '.join(summary_parts)}."
    )

    component: dict = {
        "kind": "topology",
        "title": f"Topology{ns_label}",
        "description": f"{len(nodes)} resources, {len(edges)} relationships",
        "layout_hint": layout_hint or "top-down",
        "include_metrics": include_metrics,
        "group_by": group_by,
        "nodes": nodes,
        "edges": edges,
    }

    return (text, component)


get_topology_graph_raw = get_topology_graph
get_topology_graph = beta_tool(get_topology_graph)

register_tool(get_topology_graph)

VIEW_TOOLS = [
    create_dashboard,
    delete_dashboard,
    clone_dashboard,
    namespace_summary,
    cluster_metrics,
    list_saved_views,
    get_view_details,
    update_view_widgets,
    add_widget_to_view,
    remove_widget_from_view,
    emit_component,
    undo_view_change,
    get_view_versions,
    critique_view,
    plan_dashboard,
    optimize_view,
    get_topology_graph,
]
