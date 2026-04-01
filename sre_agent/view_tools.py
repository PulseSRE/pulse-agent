"""Tools for creating custom dashboard views from conversation context."""

from __future__ import annotations

import uuid

from anthropic import beta_tool

from .tool_registry import register_tool


@beta_tool
def create_dashboard(title: str, description: str = "") -> str:
    """Create a custom dashboard view that the user can save and access from the sidebar. Use this when the user asks to create a dashboard, custom view, or persistent display of data. The dashboard will contain the component specs from the current conversation.

    Args:
        title: Name for the dashboard (e.g. "SRE Overview", "Node Health").
        description: Brief description of what the dashboard shows.
    """
    view_id = f"cv-{uuid.uuid4().hex[:12]}"
    # Return a marker that the API layer will intercept and convert to a view_spec event
    return f"__VIEW_SPEC__{view_id}|{title}|{description}"


@beta_tool
def namespace_summary(namespace: str) -> str:
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
        f"  Warning events: {warning_count}"
    )

    # Build info_card_grid component
    cards = [
        {"label": "Pods Running", "value": str(running), "sub": f"of {total_pods} total"},
        {"label": "Pods Failing", "value": str(failed + crashloop), "sub": f"{crashloop} crashlooping"},
        {"label": "Deployments", "value": f"{healthy_deps}/{total_deps}", "sub": "healthy"},
        {"label": "Warnings", "value": str(warning_count), "sub": "active events"},
    ]
    component = {
        "kind": "info_card_grid",
        "title": f"Namespace Summary — {namespace}",
        "cards": cards,
    }
    return (text, component)


register_tool(create_dashboard)
register_tool(namespace_summary)
