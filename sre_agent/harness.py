"""
Claude Harness — optimizations for getting the most out of the agent.

1. Dynamic Tool Selection — categorize tools, only load relevant ones per query
2. Prompt Caching — cache system prompt + runbooks across turns
3. Cluster Context Injection — pre-fetch cluster state into system prompt
4. Structured Output Hints — guide Claude to return component specs
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger("pulse_agent.harness")


# ---------------------------------------------------------------------------
# 1. Dynamic Tool Selection — categorize tools and select by query intent
# ---------------------------------------------------------------------------

TOOL_CATEGORIES = {
    "diagnostics": {
        "keywords": ["health", "check", "status", "diagnose", "what's wrong", "issues", "problems", "failing",
                      "not running", "crash", "error", "oom", "restart", "events", "warning"],
        "tools": [
            "list_pods", "describe_pod", "get_pod_logs", "list_nodes", "describe_node",
            "get_events", "list_deployments", "describe_deployment", "get_cluster_operators",
            "get_cluster_version", "top_pods_by_restarts", "get_recent_changes",
            "get_firing_alerts", "get_node_metrics", "get_pod_metrics",
            "correlate_incident",
        ],
    },
    "workloads": {
        "keywords": ["deploy", "pod", "replica", "stateful", "daemon", "job", "cron",
                      "scale", "rollback", "restart", "workload"],
        "tools": [
            "list_pods", "describe_pod", "get_pod_logs", "list_deployments",
            "describe_deployment", "list_statefulsets", "list_daemonsets",
            "list_jobs", "list_cronjobs", "list_replicasets", "list_hpas",
            "get_pod_disruption_budgets", "scale_deployment", "restart_deployment",
            "rollback_deployment", "delete_pod",
        ],
    },
    "networking": {
        "keywords": ["service", "endpoint", "ingress", "route", "network", "policy",
                      "dns", "connectivity", "port", "traffic"],
        "tools": [
            "get_services", "describe_service", "get_endpoint_slices",
            "list_ingresses", "list_routes", "create_network_policy",
            "scan_network_policies",
        ],
    },
    "security": {
        "keywords": ["security", "audit", "rbac", "scc", "privilege", "secret",
                      "vulnerability", "compliance", "policy", "scan"],
        "tools": [
            "scan_pod_security", "scan_images", "scan_rbac_risks",
            "list_service_account_secrets", "scan_network_policies",
            "scan_sccs", "scan_scc_usage", "scan_secrets", "get_security_summary",
            "get_tls_certificates",
        ],
    },
    "storage": {
        "keywords": ["pvc", "storage", "volume", "persistent", "disk", "capacity"],
        "tools": [
            "get_persistent_volume_claims", "get_resource_quotas",
            "list_limit_ranges",
        ],
    },
    "monitoring": {
        "keywords": ["metric", "prometheus", "alert", "monitor", "cpu", "memory",
                      "usage", "performance", "latency", "grafana"],
        "tools": [
            "get_prometheus_query", "get_firing_alerts", "get_node_metrics",
            "get_pod_metrics", "forecast_quota_exhaustion",
        ],
    },
    "operations": {
        "keywords": ["drain", "cordon", "uncordon", "apply", "yaml", "maintenance",
                      "upgrade", "update", "config"],
        "tools": [
            "cordon_node", "uncordon_node", "drain_node", "apply_yaml",
            "get_configmap", "list_operator_subscriptions",
        ],
    },
    "gitops": {
        "keywords": ["git", "argo", "gitops", "pr", "pull request", "drift", "sync"],
        "tools": [
            "detect_gitops_drift", "propose_git_change",
        ],
    },
    "fleet": {
        "keywords": ["fleet", "all clusters", "cross-cluster", "everywhere", "managed cluster",
                      "multi-cluster", "compare across", "drift across", "acm"],
        "tools": [
            "fleet_list_clusters", "fleet_list_pods", "fleet_list_deployments",
            "fleet_get_alerts", "fleet_compare_resource",
        ],
    },
}

# Tools always included regardless of category
ALWAYS_INCLUDE = {
    "list_namespaces", "get_cluster_version", "record_audit_entry",
    "suggest_remediation",
}


def select_tools(query: str, all_tools: list, all_tool_map: dict) -> tuple[list, dict]:
    """Select relevant tools based on the user's query intent.

    Returns (filtered_tool_defs, filtered_tool_map).
    Falls back to all tools if no category matches.
    """
    query_lower = query.lower()

    # Score each category by keyword matches
    scores: dict[str, int] = {}
    for category, config in TOOL_CATEGORIES.items():
        score = sum(1 for kw in config["keywords"] if kw in query_lower)
        if score > 0:
            scores[category] = score

    if not scores:
        # No match — return all tools (generic query)
        return [t.to_dict() for t in all_tools], {t.name: t for t in all_tools}

    # Take top 2 categories + always-include
    top_categories = sorted(scores.keys(), key=lambda c: scores[c], reverse=True)[:2]
    selected_names = set(ALWAYS_INCLUDE)
    for cat in top_categories:
        selected_names.update(TOOL_CATEGORIES[cat]["tools"])

    # Always include write tools from diagnostics (scale, restart) if workloads matched
    if "diagnostics" in top_categories:
        selected_names.update(TOOL_CATEGORIES["workloads"]["tools"])

    # Filter to only tools that exist in the actual tool list
    available_names = {t.name for t in all_tools}
    selected_names &= available_names

    filtered = [t for t in all_tools if t.name in selected_names]

    logger.info(
        "Tool selection: query=%r categories=%s selected=%d/%d tools",
        query[:50], top_categories, len(filtered), len(all_tools),
    )

    return [t.to_dict() for t in filtered], {t.name: t for t in filtered}


# ---------------------------------------------------------------------------
# 2. Prompt Caching — structure system prompt for cache reuse
# ---------------------------------------------------------------------------

def build_cached_system_prompt(
    base_prompt: str,
    cluster_context: str = "",
) -> list[dict]:
    """Build a system prompt optimized for Anthropic prompt caching.

    Returns a list of content blocks with cache_control on the static parts.
    The base prompt + runbooks are cached (they don't change between turns).
    The cluster context is dynamic and appended without caching.
    """
    blocks = []

    # Static block (cacheable) — system prompt + runbooks
    blocks.append({
        "type": "text",
        "text": base_prompt,
        "cache_control": {"type": "ephemeral"},  # Cached for 5 minutes
    })

    # Dynamic block (not cached) — live cluster context
    if cluster_context:
        blocks.append({
            "type": "text",
            "text": cluster_context,
        })

    return blocks


# ---------------------------------------------------------------------------
# 3. Cluster Context Injection — pre-fetch cluster state
# ---------------------------------------------------------------------------

def gather_cluster_context() -> str:
    """Pre-fetch key cluster state concurrently."""
    from .k8s_client import get_core_client, get_custom_client, safe
    from .errors import ToolError
    import concurrent.futures

    def _fetch_nodes():
        nodes = safe(lambda: get_core_client().list_node())
        if isinstance(nodes, ToolError): return None
        total = len(nodes.items)
        ready = sum(1 for n in nodes.items if any(c.type == "Ready" and c.status == "True" for c in (n.status.conditions or [])))
        roles = {}
        for n in nodes.items:
            for label in (n.metadata.labels or {}):
                if label.startswith("node-role.kubernetes.io/"):
                    role = label.split("/")[-1]
                    roles[role] = roles.get(role, 0) + 1
        role_str = ", ".join(f"{r}={c}" for r, c in sorted(roles.items()))
        return f"Nodes: {ready}/{total} Ready ({role_str})"

    def _fetch_namespaces():
        ns = safe(lambda: get_core_client().list_namespace())
        if isinstance(ns, ToolError): return None
        return f"Namespaces: {len(ns.items)}"

    def _fetch_version():
        try:
            cv = get_custom_client().get_cluster_custom_object("config.openshift.io", "v1", "clusterversions", "version")
            version = cv.get("status", {}).get("desired", {}).get("version", "unknown")
            channel = cv.get("spec", {}).get("channel", "unknown")
            return f"OpenShift: {version} (channel: {channel})"
        except Exception:
            return None

    def _fetch_failing_pods():
        pods = safe(lambda: get_core_client().list_pod_for_all_namespaces(field_selector="status.phase!=Running,status.phase!=Succeeded"))
        if isinstance(pods, ToolError): return None
        failing = [p for p in pods.items if p.status.phase not in ("Running", "Succeeded", "Pending")]
        return f"Failing pods: {len(failing)}" if failing else None

    def _fetch_alerts():
        try:
            core = get_core_client()
            result = core.connect_get_namespaced_service_proxy_with_path(
                "alertmanager-main:web", "openshift-monitoring",
                path="api/v2/alerts", _preload_content=False,
            )
            alerts = json.loads(result.data)
            firing = [a for a in alerts if a.get("status", {}).get("state") == "active"]
            if not firing: return None
            critical = sum(1 for a in firing if a.get("labels", {}).get("severity") == "critical")
            warning = sum(1 for a in firing if a.get("labels", {}).get("severity") == "warning")
            return f"Firing alerts: {len(firing)} ({critical} critical, {warning} warning)"
        except Exception:
            return None

    results = {}
    fetchers = {"nodes": _fetch_nodes, "namespaces": _fetch_namespaces, "version": _fetch_version, "pods": _fetch_failing_pods, "alerts": _fetch_alerts}

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(fn): key for key, fn in fetchers.items()}
        for future in concurrent.futures.as_completed(futures, timeout=10):
            key = futures[future]
            try:
                result = future.result(timeout=5)
                if result:
                    results[key] = result
            except Exception:
                pass

    if not results:
        return ""

    parts = [results[k] for k in ("nodes", "namespaces", "version", "pods", "alerts") if k in results]
    return "\n## Live Cluster State (auto-gathered)\n" + "\n".join(f"- {p}" for p in parts)


# Cached cluster context — refreshed every 60 seconds
_cluster_context_cache: str = ""
_cluster_context_ts: float = 0


def get_cluster_context(max_age: float = 60) -> str:
    """Get cached cluster context, refreshing if stale."""
    import time
    global _cluster_context_cache, _cluster_context_ts
    now = time.time()
    if now - _cluster_context_ts > max_age:
        try:
            _cluster_context_cache = gather_cluster_context()
            _cluster_context_ts = now
        except Exception:
            pass  # Use stale cache on error
    return _cluster_context_cache


# ---------------------------------------------------------------------------
# 4. Structured Output Hints — guide component rendering
# ---------------------------------------------------------------------------

COMPONENT_HINT = """
## UI Component Rendering

When your tool results include structured data (tables, lists, status checks),
the system automatically renders them as interactive UI components in the chat.
You do NOT need to format data as tables in your text — the tools handle rendering.

Focus your text response on:
- Analysis and interpretation of the data
- Root cause identification
- Actionable recommendations
- Risk assessment

Do NOT repeat raw data that the tools already displayed as components.
"""
