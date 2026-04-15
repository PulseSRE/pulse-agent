"""Tool call evaluation prompts — maps real user queries to expected tool calls.

Each entry is a tuple of (prompt, expected_tools, mode, description).
- prompt: What a real user would type
- expected_tools: Tools that MUST be called (at least one from the list)
- mode: Which agent mode should handle this (sre, security, view_designer, both)
- description: What we're testing

Usage:
    python3 -m pytest tests/test_eval_tool_selection.py -v
"""

from __future__ import annotations

# (prompt, expected_tools, mode, description)
EVAL_PROMPTS: list[tuple[str, list[str], str, str]] = [
    # ─── Diagnostics ──────────────────────────────────────────────────────
    (
        "why are my pods crashing in production",
        ["list_pods", "describe_pod", "get_pod_logs", "get_events"],
        "sre",
        "Crashloop diagnosis should gather pod list, describe, logs, events",
    ),
    (
        "what's wrong with my cluster",
        ["get_cluster_operators", "get_events", "list_pods", "get_firing_alerts"],
        "sre",
        "Cluster health check should check operators, events, pods, alerts",
    ),
    (
        "show me pods with high restart counts",
        ["top_pods_by_restarts"],
        "sre",
        "High restarts has a dedicated tool",
    ),
    (
        "check if there are any OOM killed pods",
        ["list_pods", "describe_pod", "get_events"],
        "sre",
        "OOM investigation needs pod status and events",
    ),
    (
        "why is my deployment not rolling out",
        ["describe_resource", "get_events", "list_pods"],
        "sre",
        "Stuck rollout needs deployment describe, events, pod status",
    ),
    (
        "show me warning events in the default namespace",
        ["get_events"],
        "sre",
        "Direct event query",
    ),
    (
        "what changed in the last hour",
        ["get_recent_changes"],
        "sre",
        "Recent changes has a dedicated tool",
    ),
    (
        "show me the logs for pod nginx-abc in production",
        ["get_pod_logs"],
        "sre",
        "Direct log retrieval",
    ),
    (
        "search logs for error connection refused across all pods",
        ["search_logs"],
        "sre",
        "Cross-pod log search",
    ),
    # ─── Resource Listing ─────────────────────────────────────────────────
    (
        "list all pods in kube-system",
        ["list_pods"],
        "sre",
        "Direct pod listing",
    ),
    (
        "show me all deployments",
        ["list_resources"],
        "sre",
        "Generic resource listing",
    ),
    (
        "list all PVCs in the cluster",
        ["list_resources"],
        "sre",
        "PVC listing via generic list_resources",
    ),
    (
        "show me all ingresses",
        ["list_ingresses"],
        "sre",
        "Ingress listing has dedicated tool",
    ),
    (
        "list routes in production namespace",
        ["list_routes"],
        "sre",
        "Route listing has dedicated tool",
    ),
    (
        "show me HPAs across all namespaces",
        ["list_hpas"],
        "sre",
        "HPA listing",
    ),
    (
        "list all cronjobs",
        ["list_cronjobs"],
        "sre",
        "Cronjob listing",
    ),
    (
        "show me running jobs",
        ["list_jobs"],
        "sre",
        "Job listing",
    ),
    # ─── Node Operations ──────────────────────────────────────────────────
    (
        "show me node status",
        ["list_resources", "get_node_metrics"],
        "sre",
        "Node health check",
    ),
    (
        "which nodes have disk pressure",
        ["list_resources", "get_events"],
        "sre",
        "Node condition check",
    ),
    (
        "drain node worker-2 for maintenance",
        ["drain_node"],
        "sre",
        "Node drain operation",
    ),
    (
        "cordon node worker-1",
        ["cordon_node"],
        "sre",
        "Node cordon operation",
    ),
    (
        "uncordon node worker-1",
        ["uncordon_node"],
        "sre",
        "Node uncordon operation",
    ),
    # ─── Metrics & Monitoring ─────────────────────────────────────────────
    (
        "show me CPU usage across the cluster",
        ["get_prometheus_query"],
        "sre",
        "PromQL query for CPU metrics",
    ),
    (
        "what's the memory usage by namespace",
        ["get_prometheus_query"],
        "sre",
        "PromQL query for memory metrics",
    ),
    (
        "show me pod resource usage in production",
        ["get_pod_metrics"],
        "sre",
        "Pod metrics has dedicated tool",
    ),
    (
        "are there any firing alerts",
        ["get_firing_alerts"],
        "sre",
        "Alert check",
    ),
    (
        "show me node metrics",
        ["get_node_metrics"],
        "sre",
        "Node metrics has dedicated tool",
    ),
    (
        "what prometheus metrics are available for CPU",
        ["discover_metrics"],
        "sre",
        "Metric discovery",
    ),
    (
        "check resource recommendations for production namespace",
        ["get_resource_recommendations"],
        "sre",
        "Right-sizing recommendations",
    ),
    # ─── Write Operations ─────────────────────────────────────────────────
    (
        "scale my-deployment to 5 replicas",
        ["scale_deployment"],
        "sre",
        "Scale operation",
    ),
    (
        "restart the nginx deployment in production",
        ["restart_deployment"],
        "sre",
        "Restart operation",
    ),
    (
        "delete pod nginx-abc-xyz in default namespace",
        ["delete_pod"],
        "sre",
        "Pod deletion",
    ),
    (
        "rollback my-deployment to the previous version",
        ["rollback_deployment"],
        "sre",
        "Deployment rollback",
    ),
    (
        "apply this yaml to create a configmap",
        ["apply_yaml"],
        "sre",
        "YAML apply operation",
    ),
    # ─── Cluster Info ─────────────────────────────────────────────────────
    (
        "what version of OpenShift are we running",
        ["get_cluster_version"],
        "sre",
        "Cluster version check",
    ),
    (
        "show me cluster operator status",
        ["get_cluster_operators"],
        "sre",
        "Operator health check",
    ),
    (
        "list operator subscriptions",
        ["list_operator_subscriptions"],
        "sre",
        "OLM subscription listing",
    ),
    (
        "show me the configmap kube-proxy in kube-system",
        ["get_configmap"],
        "sre",
        "ConfigMap retrieval",
    ),
    (
        "check TLS certificates",
        ["get_tls_certificates"],
        "sre",
        "Certificate check",
    ),
    # ─── Networking ───────────────────────────────────────────────────────
    (
        "describe the kubernetes service in default namespace",
        ["describe_service"],
        "sre",
        "Service description",
    ),
    (
        "show me endpoint slices for my-service",
        ["get_endpoint_slices"],
        "sre",
        "Endpoint slice check",
    ),
    (
        "test connectivity from pod-a to pod-b on port 8080",
        ["test_connectivity"],
        "sre",
        "Network connectivity test",
    ),
    (
        "create a default deny network policy for production",
        ["create_network_policy"],
        "sre",
        "Network policy creation",
    ),
    # ─── Resource Details ─────────────────────────────────────────────────
    (
        "describe pod nginx-abc in production",
        ["describe_pod"],
        "sre",
        "Pod description",
    ),
    (
        "show me resource relationships for deployment nginx",
        ["get_resource_relationships"],
        "sre",
        "Resource relationship tree",
    ),
    (
        "run ls /tmp in pod nginx-abc",
        ["exec_command"],
        "sre",
        "Exec into pod",
    ),
    # ─── Predictive ───────────────────────────────────────────────────────
    (
        "when will we run out of CPU quota in production",
        ["forecast_quota_exhaustion"],
        "sre",
        "Quota forecast",
    ),
    (
        "is my HPA thrashing",
        ["analyze_hpa_thrashing"],
        "sre",
        "HPA analysis",
    ),
    (
        "suggest a fix for this CrashLoopBackOff",
        ["suggest_remediation"],
        "sre",
        "Remediation suggestions",
    ),
    # ─── Incident Correlation ─────────────────────────────────────────────
    (
        "build a timeline of what happened in production in the last hour",
        ["correlate_incident"],
        "sre",
        "Incident timeline correlation",
    ),
    # ─── GitOps ───────────────────────────────────────────────────────────
    (
        "list ArgoCD applications",
        ["get_argo_applications"],
        "sre",
        "Argo app listing",
    ),
    (
        "show me drift from git for the payments app",
        ["detect_gitops_drift"],
        "sre",
        "GitOps drift detection",
    ),
    (
        "create a PR to fix the replica count",
        ["propose_git_change"],
        "sre",
        "Git PR proposal",
    ),
    (
        "show me the ArgoCD app details for payments",
        ["get_argo_app_detail"],
        "sre",
        "Argo app detail view",
    ),
    (
        "what's the source repo for the payments argo app",
        ["get_argo_app_source"],
        "sre",
        "Argo app source info",
    ),
    (
        "show me the sync diff for payments app",
        ["get_argo_sync_diff"],
        "sre",
        "Argo sync diff",
    ),
    (
        "create an ArgoCD application for my-app",
        ["create_argo_application"],
        "sre",
        "Argo app creation",
    ),
    (
        "install the GitOps operator",
        ["install_gitops_operator"],
        "sre",
        "GitOps operator installation",
    ),
    # ─── Fleet ────────────────────────────────────────────────────────────
    (
        "compare pods across all clusters",
        ["fleet_list_pods"],
        "sre",
        "Multi-cluster pod comparison",
    ),
    (
        "list all clusters in the fleet",
        ["fleet_list_clusters"],
        "sre",
        "Fleet cluster listing",
    ),
    (
        "show me alerts across all clusters",
        ["fleet_get_alerts"],
        "sre",
        "Fleet-wide alert check",
    ),
    (
        "compare deployments across clusters",
        ["fleet_list_deployments", "fleet_compare_resource"],
        "sre",
        "Fleet deployment comparison",
    ),
    # ─── Security ─────────────────────────────────────────────────────────
    (
        "scan RBAC for overly permissive roles",
        ["scan_rbac_risks"],
        "security",
        "RBAC risk scan",
    ),
    (
        "check pod security across the cluster",
        ["scan_pod_security"],
        "security",
        "Pod security scan",
    ),
    (
        "audit network policies",
        ["scan_network_policies"],
        "security",
        "Network policy audit",
    ),
    (
        "scan for privileged containers",
        ["scan_scc_usage", "scan_sccs"],
        "security",
        "SCC/privilege scan",
    ),
    (
        "check for exposed secrets",
        ["scan_secrets"],
        "security",
        "Secret exposure scan",
    ),
    (
        "scan container images for vulnerabilities",
        ["scan_images"],
        "security",
        "Image vulnerability scan",
    ),
    (
        "give me a security summary of the cluster",
        ["get_security_summary"],
        "security",
        "Overall security posture",
    ),
    (
        "list service account secrets in production",
        ["list_service_account_secrets"],
        "security",
        "Service account secret listing",
    ),
    # ─── View Designer ────────────────────────────────────────────────────
    (
        "create a dashboard for production namespace",
        ["plan_dashboard", "namespace_summary", "get_prometheus_query", "create_dashboard"],
        "view_designer",
        "Full dashboard creation flow",
    ),
    (
        "build me a cluster overview dashboard",
        ["plan_dashboard", "cluster_metrics", "get_prometheus_query", "create_dashboard"],
        "view_designer",
        "Cluster dashboard creation",
    ),
    (
        "show me my saved dashboards",
        ["list_saved_views"],
        "view_designer",
        "View listing",
    ),
    (
        "add a memory chart to my dashboard",
        ["get_prometheus_query", "add_widget_to_view"],
        "view_designer",
        "Widget addition to existing view",
    ),
    (
        "add a bar chart showing the top namespaces by pod count",
        ["emit_component"],
        "view_designer",
        "Emit bar_list component",
    ),
    (
        "remove the third widget from my dashboard",
        ["update_view_widgets"],
        "view_designer",
        "Widget removal by index",
    ),
    (
        "remove the CPU Usage widget from my network view",
        ["remove_widget_from_view"],
        "view_designer",
        "Widget removal by title",
    ),
    (
        "what metrics are available for network monitoring",
        ["discover_metrics"],
        "view_designer",
        "Metric discovery for dashboard building",
    ),
    (
        "undo the last change to my dashboard",
        ["undo_view_change"],
        "view_designer",
        "View undo operation",
    ),
    (
        "delete my old cluster dashboard",
        ["delete_dashboard"],
        "view_designer",
        "Dashboard deletion",
    ),
    (
        "clone my production dashboard for staging",
        ["clone_dashboard"],
        "view_designer",
        "Dashboard cloning",
    ),
    (
        "optimize the layout of my dashboard and group related widgets",
        ["optimize_view"],
        "view_designer",
        "View layout optimization",
    ),
    (
        "show me cluster KPI metrics",
        ["cluster_metrics"],
        "view_designer",
        "Cluster metric cards",
    ),
    (
        "give me a namespace summary for staging",
        ["namespace_summary"],
        "view_designer",
        "Namespace summary cards",
    ),
    # ─── Cross-Agent Handoff ──────────────────────────────────────────────
    (
        "I found a security issue, hand this off to the security team",
        ["request_security_scan"],
        "sre",
        "SRE to security handoff",
    ),
    (
        "this security finding needs SRE investigation",
        ["request_sre_investigation"],
        "security",
        "Security to SRE handoff",
    ),
    # ─── Audit ────────────────────────────────────────────────────────────
    (
        "log that I restarted the nginx deployment for debugging",
        ["record_audit_entry"],
        "sre",
        "Audit log recording",
    ),
    # Self-description tools
    (
        "what can you do?",
        ["describe_agent"],
        "sre",
        "List agent skills",
    ),
    (
        "what tools do you have?",
        ["describe_tools"],
        "sre",
        "List agent tools",
    ),
    (
        "what UI components can you render?",
        ["list_ui_components"],
        "sre",
        "List UI components",
    ),
    (
        "show me your PromQL recipes",
        ["list_promql_recipes"],
        "sre",
        "List PromQL recipes",
    ),
    (
        "what runbooks do you have?",
        ["list_runbooks"],
        "sre",
        "List runbooks",
    ),
    (
        "explain what fields a Deployment has",
        ["explain_resource"],
        "sre",
        "Explain K8s resource",
    ),
    (
        "what API resources are available?",
        ["list_api_resources"],
        "sre",
        "List K8s API resources",
    ),
    (
        "are there any deprecated APIs?",
        ["list_deprecated_apis"],
        "sre",
        "Check deprecated APIs",
    ),
    (
        "create a skill for database troubleshooting",
        ["create_skill"],
        "sre",
        "Create skill via chat",
    ),
    (
        "edit the capacity_planner skill prompt",
        ["edit_skill"],
        "sre",
        "Edit skill via chat",
    ),
    (
        "delete the test_skill",
        ["delete_skill"],
        "sre",
        "Delete skill via chat",
    ),
    (
        "clone the SRE skill as a new networking skill",
        ["create_skill_from_template"],
        "sre",
        "Clone skill from template",
    ),
]

# Tools that are internal/meta and don't need user-facing eval prompts
EXCLUDED_FROM_EVAL = {
    "set_store",  # Internal state management
    "set_current_user",  # Internal auth
    "get_current_user",  # Internal auth
    "get_cluster_patterns",  # Internal pattern detection
    "verify_query",  # Removed from user-facing tools
    "critique_view",  # Called programmatically after create_dashboard
    "get_view_details",  # Called by agent internally during view editing
    "get_view_versions",  # Called by agent internally for undo
}


def get_all_eval_prompts() -> list[tuple[str, list[str], str, str]]:
    """Static EVAL_PROMPTS + learned prompts from DB (if available)."""
    all_prompts = list(EVAL_PROMPTS)
    try:
        from sre_agent.tool_usage import get_learned_eval_prompts

        learned = get_learned_eval_prompts()
        static_queries = {p[0].lower().strip() for p in EVAL_PROMPTS}
        for prompt in learned:
            if prompt[0].lower().strip() not in static_queries:
                all_prompts.append(prompt)
    except Exception:
        pass  # DB unavailable — use static only
    return all_prompts
