"""Tool Categories — canonical tool-category data and lookup functions.

Extracted from skill_loader.py to reduce file size and improve modularity.
This module owns the mapping of tools to categories, mode-based category selection,
and tool classification helpers.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Tool Categories
# ---------------------------------------------------------------------------

TOOL_CATEGORIES = {
    "diagnostics": {
        "keywords": [
            "health",
            "check",
            "status",
            "diagnose",
            "what's wrong",
            "what's happening",
            "show me",
            "overview",
            "summary",
            "dashboard",
            "view",
            "issues",
            "problems",
            "failing",
            "not running",
            "crash",
            "error",
            "oom",
            "restart",
            "events",
            "warning",
        ],
        "tools": [
            "list_resources",
            "list_pods",
            "describe_pod",
            "get_pod_logs",
            "list_nodes",
            "get_events",
            "describe_deployment",
            "list_deployments",
            "get_cluster_operators",
            "get_cluster_version",
            "top_pods_by_restarts",
            "get_recent_changes",
            "get_firing_alerts",
            "get_node_metrics",
            "get_pod_metrics",
            "correlate_incident",
            "namespace_summary",
            "cluster_metrics",
            "visualize_nodes",
            "describe_resource",
            "search_logs",
            "get_resource_relationships",
            "create_live_table",
        ],
    },
    "workloads": {
        "keywords": [
            "deploy",
            "pod",
            "replica",
            "stateful",
            "daemon",
            "job",
            "cron",
            "scale",
            "rollback",
            "restart",
            "workload",
        ],
        "tools": [
            "list_resources",
            "list_pods",
            "describe_pod",
            "get_pod_logs",
            "describe_deployment",
            "list_deployments",
            "list_jobs",
            "list_cronjobs",
            "list_hpas",
            "scale_deployment",
            "restart_deployment",
            "rollback_deployment",
            "delete_pod",
        ],
    },
    "networking": {
        "keywords": [
            "service",
            "endpoint",
            "ingress",
            "route",
            "network",
            "policy",
            "dns",
            "connectivity",
            "port",
            "traffic",
        ],
        "tools": [
            "list_resources",
            "describe_service",
            "get_endpoint_slices",
            "list_ingresses",
            "list_routes",
            "create_network_policy",
            "scan_network_policies",
            "test_connectivity",
        ],
    },
    "security": {
        "keywords": [
            "security",
            "audit",
            "rbac",
            "scc",
            "privilege",
            "secret",
            "vulnerability",
            "compliance",
            "policy",
            "scan",
        ],
        "tools": [
            "scan_pod_security",
            "scan_images",
            "scan_rbac_risks",
            "list_service_account_secrets",
            "scan_network_policies",
            "scan_sccs",
            "scan_scc_usage",
            "scan_secrets",
            "get_security_summary",
            "get_tls_certificates",
            "request_security_scan",
        ],
    },
    "storage": {
        "keywords": ["pvc", "storage", "volume", "persistent", "disk", "capacity"],
        "tools": [
            "list_resources",
        ],
    },
    "monitoring": {
        "keywords": [
            "metric",
            "prometheus",
            "alert",
            "monitor",
            "cpu",
            "memory",
            "usage",
            "performance",
            "latency",
            "grafana",
        ],
        "tools": [
            "discover_metrics",
            "get_prometheus_query",
            "get_firing_alerts",
            "get_node_metrics",
            "get_pod_metrics",
            "forecast_quota_exhaustion",
            "get_resource_recommendations",
            "analyze_hpa_thrashing",
        ],
    },
    "operations": {
        "keywords": [
            "drain",
            "cordon",
            "uncordon",
            "apply",
            "yaml",
            "maintenance",
            "upgrade",
            "update",
            "config",
            "task",
            "remind",
            "track",
            "inbox",
        ],
        "tools": [
            "cordon_node",
            "uncordon_node",
            "drain_node",
            "apply_yaml",
            "get_configmap",
            "list_operator_subscriptions",
            "exec_command",
            "create_inbox_task",
        ],
    },
    "gitops": {
        "keywords": ["git", "argo", "gitops", "pr", "pull request", "drift", "sync"],
        "tools": [
            "detect_gitops_drift",
            "propose_git_change",
            "get_argo_applications",
            "get_argo_app_detail",
            "get_argo_app_source",
            "get_argo_sync_diff",
            "install_gitops_operator",
            "create_argo_application",
        ],
    },
    "fleet": {
        "keywords": [
            "fleet",
            "all clusters",
            "cross-cluster",
            "everywhere",
            "managed cluster",
            "multi-cluster",
            "compare across",
            "drift across",
            "acm",
        ],
        "tools": [
            "fleet_list_clusters",
            "fleet_list_pods",
            "fleet_list_deployments",
            "fleet_get_alerts",
            "fleet_compare_resource",
            "fleet_query_metrics",
            "fleet_compare_metrics",
        ],
    },
    "self": {
        "keywords": [
            "skill",
            "capabilities",
            "what can you",
            "help",
            "tools",
            "component",
            "promql",
            "runbook",
            "api resource",
        ],
        "tools": [
            "describe_agent",
            "describe_tools",
            "list_ui_components",
            "list_promql_recipes",
            "list_runbooks",
            "explain_resource",
            "list_api_resources",
            "list_deprecated_apis",
            "create_skill",
            "edit_skill",
            "delete_skill",
            "create_skill_from_template",
        ],
    },
    "views": {
        "keywords": [
            "dashboard",
            "view",
            "widget",
            "chart",
            "table",
            "metric card",
            "layout",
            "topology",
            "dependency graph",
            "live table",
        ],
        "tools": [
            "plan_dashboard",
            "get_topology_graph",
            "create_dashboard",
            "namespace_summary",
            "cluster_metrics",
            "list_saved_views",
            "get_view_details",
            "update_view_widgets",
            "add_widget_to_view",
            "remove_widget_from_view",
            "emit_component",
            "undo_view_change",
            "get_view_versions",
            "delete_dashboard",
            "clone_dashboard",
            "critique_view",
            "optimize_view",
            "create_live_table",
            # Data tools needed for dashboard content
            "get_prometheus_query",
            "discover_metrics",
            "list_pods",
            "list_resources",
            "get_firing_alerts",
            "get_events",
            "get_node_metrics",
            "get_pod_metrics",
            "describe_pod",
        ],
    },
}

# Tools always included regardless of category — these are lightweight and
# broadly useful. Better to include a few extra tools than to miss one the
# user needs.
ALWAYS_INCLUDE = {
    "list_resources",
    "list_pods",
    "get_events",
    "get_firing_alerts",
    "get_cluster_version",
    "namespace_summary",
    "cluster_metrics",
    "visualize_nodes",
    "record_audit_entry",
    "suggest_remediation",
    "request_sre_investigation",
    "request_security_scan",
    "describe_agent",
}

# Self-describe tools — included when user asks about capabilities
_SELF_DESCRIBE_TOOLS = {
    "describe_tools",
    "list_ui_components",
    "list_promql_recipes",
    "list_runbooks",
    "explain_resource",
    "list_api_resources",
    "list_deprecated_apis",
    "create_skill",
    "edit_skill",
    "delete_skill",
    "create_skill_from_template",
}

# Keywords that trigger self-describe tools inclusion
_SELF_DESCRIBE_KEYWORDS = {
    "what can",
    "help",
    "tools",
    "explain",
    "create skill",
    "create a skill",
    "edit skill",
    "skill prompt",
    "delete skill",
    "_skill",
    "clone skill",
    "clone the",
    "from template",
    "recipes",
    "runbooks",
    "components",
    "api resources",
    "deprecated",
    "what do you",
    "capabilities",
}

# MCP tools that duplicate native tools — skip these when building tool_defs.
# Native tools are faster (direct K8s API), have validation, and richer rendering.
# MCP tools only fill gaps (e.g. helm_list, helm_install have no native equivalent).
_MCP_NATIVE_OVERLAP: set[str] = {
    "pods_list",  # native: list_pods
    "pods_list_in_namespace",  # native: list_pods(namespace=...)
    "pods_get",  # native: describe_pod
    "pods_log",  # native: get_pod_logs
    "resources_list",  # native: list_resources
    "resources_get",  # native: describe_resource
    "namespaces_list",  # native: list_resources(kind="Namespace")
    "events_list",  # native: get_events
    "prometheus_query",  # native: get_prometheus_query
    "alertmanager_alerts",  # native: get_firing_alerts
}

# Map orchestrator modes to relevant tool categories
MODE_CATEGORIES: dict[str, list[str] | None] = {
    "sre": ["diagnostics", "workloads", "networking", "storage", "monitoring", "operations", "gitops", "views"],
    "security": ["security", "networking"],
    "view_designer": None,  # all tools — view_designer has its own curated list
    "both": None,  # all categories
}

# Reverse lookup: tool_name -> first matching category
_TOOL_CATEGORY_MAP: dict[str, str] = {}
for _cat_name, _cat_config in TOOL_CATEGORIES.items():
    for _tool_name in _cat_config["tools"]:
        if _tool_name not in _TOOL_CATEGORY_MAP:
            _TOOL_CATEGORY_MAP[_tool_name] = _cat_name


# ---------------------------------------------------------------------------
# Lookup Functions
# ---------------------------------------------------------------------------


def get_tool_category(tool_name: str) -> str | None:
    """Return the primary category for a tool, or None if uncategorized."""
    return _TOOL_CATEGORY_MAP.get(tool_name)


def get_tool_skills(tool_name: str) -> list[str]:
    """Return which skills use this tool based on category overlap.

    Note: This function imports from skill_loader at runtime to avoid
    circular import issues (tool_categories <- skill_loader <- tool_categories).
    """
    # Import here to avoid circular dependency
    from . import skill_loader

    if not skill_loader._skills:
        skill_loader.load_skills()

    tool_cat = _TOOL_CATEGORY_MAP.get(tool_name)
    if not tool_cat:
        return []

    result = []
    for skill_name, skill in skill_loader._skills.items():
        if tool_cat in skill.categories:
            result.append(skill_name)
    return result
