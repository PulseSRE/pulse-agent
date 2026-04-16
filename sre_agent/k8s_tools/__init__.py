"""Kubernetes/OpenShift tools for the SRE agent.

Each tool is decorated with @beta_tool so the Anthropic SDK automatically
generates JSON schemas and the tool runner can execute them.

This package re-exports all tools and constants for backward compatibility.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "ALL_TOOLS",
    "MAX_REPLICAS",
    "MAX_RESULTS",
    "MAX_TAIL_LINES",
    "WRITE_TOOLS",
    "_K8S_NAMESPACE_RE",
    "_K8S_NAME_RE",
    "_KIND_PLURAL_MAP",
    "_SHORT_NAMES",
    "_build_k8s_datasource",
    "_fetch_table_rows",
    "_infer_column_type",
    "_metric_names_cache",
    "_resolve_plural",
    "_resolve_short_name",
    "_validate_k8s_name",
    "_validate_k8s_namespace",
    "apply_yaml",
    "cordon_node",
    "create_live_table",
    "create_network_policy",
    "delete_pod",
    "describe_deployment",
    "describe_node",
    "describe_pod",
    "describe_resource",
    "describe_service",
    "discover_metrics",
    "drain_node",
    "exec_command",
    "get_cluster_operators",
    "get_cluster_version",
    "get_configmap",
    "get_endpoint_slices",
    "get_events",
    "get_firing_alerts",
    "get_node_metrics",
    "get_persistent_volume_claims",
    "get_pod_disruption_budgets",
    "get_pod_logs",
    "get_pod_metrics",
    "get_prometheus_query",
    "get_recent_changes",
    "get_resource_quotas",
    "get_resource_recommendations",
    "get_resource_relationships",
    "get_services",
    "get_tls_certificates",
    "list_cronjobs",
    "list_daemonsets",
    "list_deployments",
    "list_hpas",
    "list_ingresses",
    "list_jobs",
    "list_limit_ranges",
    "list_namespaces",
    "list_nodes",
    "list_operator_subscriptions",
    "list_pods",
    "list_replicasets",
    "list_resources",
    "list_routes",
    "list_statefulsets",
    "record_audit_entry",
    "restart_deployment",
    "rollback_deployment",
    "scale_deployment",
    "search_logs",
    "test_connectivity",
    "top_pods_by_restarts",
    "uncordon_node",
    "verify_query",
    "visualize_nodes",
]

# --- Advanced tools ---
from .advanced import (
    apply_yaml,
    create_network_policy,
    exec_command,
    get_resource_recommendations,
    test_connectivity,
)

# --- Audit tools ---
from .audit import record_audit_entry

# --- Deployment tools ---
from .deployments import (
    describe_deployment,
    list_deployments,
    list_replicasets,
    restart_deployment,
    rollback_deployment,
    scale_deployment,
)

# --- Diagnostic tools ---
from .diagnostics import (
    describe_service,
    get_cluster_operators,
    get_cluster_version,
    get_configmap,
    get_endpoint_slices,
    get_events,
    get_persistent_volume_claims,
    get_pod_disruption_budgets,
    get_recent_changes,
    get_resource_quotas,
    get_services,
    get_tls_certificates,
    list_limit_ranges,
    list_namespaces,
    search_logs,
    top_pods_by_restarts,
)

# --- Generic tools ---
from .generic import (
    _KIND_PLURAL_MAP,
    _SHORT_NAMES,
    _build_k8s_datasource,
    _fetch_table_rows,
    _infer_column_type,
    _resolve_plural,
    _resolve_short_name,
    describe_resource,
    get_resource_relationships,
    list_resources,
)

# --- Live table ---
from .live_table import create_live_table

# --- Monitoring tools ---
from .monitoring import (
    _metric_names_cache,
    discover_metrics,
    get_firing_alerts,
    get_node_metrics,
    get_pod_metrics,
    get_prometheus_query,
    verify_query,
)

# --- Node tools ---
from .nodes import (
    cordon_node,
    describe_node,
    drain_node,
    list_nodes,
    uncordon_node,
    visualize_nodes,
)

# --- Pod tools ---
from .pods import (
    MAX_TAIL_LINES,
    delete_pod,
    describe_pod,
    get_pod_logs,
    list_pods,
)

# --- Validators (used by many modules) ---
from .validators import (
    _K8S_NAME_RE,
    _K8S_NAMESPACE_RE,
    MAX_REPLICAS,
    MAX_RESULTS,
    _validate_k8s_name,
    _validate_k8s_namespace,
)

# --- Workload tools ---
from .workloads import (
    list_cronjobs,
    list_daemonsets,
    list_hpas,
    list_ingresses,
    list_jobs,
    list_operator_subscriptions,
    list_routes,
    list_statefulsets,
)

# --- Constants ---

# Write tools that require user confirmation before execution
WRITE_TOOLS = {
    "scale_deployment",
    "restart_deployment",
    "cordon_node",
    "uncordon_node",
    "delete_pod",
    "apply_yaml",
    "create_network_policy",
    "rollback_deployment",
    "drain_node",
    "exec_command",
    "test_connectivity",
}

ALL_TOOLS: list[Any] = [
    # Universal resource listing + relationships
    get_resource_relationships,
    # Universal resource listing — replaces list_namespaces, list_nodes, list_deployments,
    # list_statefulsets, list_daemonsets, get_services, get_persistent_volume_claims,
    # get_resource_quotas, list_limit_ranges, list_replicasets, get_pod_disruption_budgets.
    # Works for any resource including CRDs.
    list_resources,
    # Generic describe — works for any resource kind
    describe_resource,
    # Specialized tools with unique logic (can't be replaced by list_resources)
    list_pods,  # field_selector, logs link, restart count
    describe_pod,
    get_pod_logs,
    get_events,  # field_selector filtering by kind/name/type
    get_cluster_version,
    get_cluster_operators,  # OpenShift condition->status mapping
    get_configmap,
    get_node_metrics,  # metrics API + unit parsing
    get_pod_metrics,  # metrics API + sort by cpu/memory
    list_jobs,  # show_completed filter, duration
    list_cronjobs,  # schedule, suspended, last run
    list_ingresses,  # rules/paths/backends parsing
    list_routes,  # OpenShift route.openshift.io
    list_hpas,  # current metrics extraction
    list_operator_subscriptions,  # OLM CSV/channel/health
    get_firing_alerts,  # Alertmanager proxy
    discover_metrics,  # Prometheus metric discovery
    verify_query,  # PromQL query validation
    get_prometheus_query,  # PromQL chart generation
    # Diagnostics
    describe_service,
    get_endpoint_slices,
    top_pods_by_restarts,
    get_recent_changes,
    get_tls_certificates,
    search_logs,  # search across pods by label
    get_resource_recommendations,  # right-sizing analysis
    # Write operations
    scale_deployment,
    restart_deployment,
    cordon_node,
    uncordon_node,
    delete_pod,
    rollback_deployment,
    drain_node,
    apply_yaml,
    create_network_policy,
    exec_command,  # run commands in pods
    test_connectivity,  # network connectivity tests
    # Audit
    record_audit_entry,
    # Live table
    create_live_table,
]

# Register all tools in the central registry
from ..tool_registry import register_tool

for _tool in ALL_TOOLS:
    register_tool(_tool, is_write=(_tool.name in WRITE_TOOLS))
