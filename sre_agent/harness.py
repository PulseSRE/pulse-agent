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
import time

logger = logging.getLogger("pulse_agent.harness")

# Cache for deprioritized tools (refreshed every 10 minutes)
_deprioritized_cache: tuple[set[str], float] | None = None
_DEPRIORITIZE_TTL = 600  # 10 minutes


# ---------------------------------------------------------------------------
# 1. Dynamic Tool Selection — categorize tools and select by query intent
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
        "keywords": ["drain", "cordon", "uncordon", "apply", "yaml", "maintenance", "upgrade", "update", "config"],
        "tools": [
            "cordon_node",
            "uncordon_node",
            "drain_node",
            "apply_yaml",
            "get_configmap",
            "list_operator_subscriptions",
            "exec_command",
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
        ],
    },
}

# Tools always included regardless of category — these are lightweight and
# broadly useful. Better to include a few extra tools than to miss one the
# user needs.
ALWAYS_INCLUDE = {
    "list_resources",
    "get_cluster_version",
    "record_audit_entry",
    "suggest_remediation",
    "namespace_summary",
    "cluster_metrics",
    "visualize_nodes",
    "list_pods",
    "get_events",
    "get_firing_alerts",
    "request_sre_investigation",
}


# Reverse lookup: tool_name -> first matching category
_TOOL_CATEGORY_MAP: dict[str, str] = {}
for _cat_name, _cat_config in TOOL_CATEGORIES.items():
    for _tool_name in _cat_config["tools"]:
        if _tool_name not in _TOOL_CATEGORY_MAP:
            _TOOL_CATEGORY_MAP[_tool_name] = _cat_name


def get_tool_category(tool_name: str) -> str | None:
    """Return the primary category for a tool, or None if uncategorized."""
    return _TOOL_CATEGORY_MAP.get(tool_name)


# Map orchestrator modes to relevant tool categories
MODE_CATEGORIES: dict[str, list[str] | None] = {
    "sre": ["diagnostics", "workloads", "networking", "storage", "monitoring", "operations", "gitops"],
    "security": ["security", "networking"],
    "view_designer": None,  # all tools — view_designer has its own curated list
    "both": None,  # all categories
}


def _reorder_deprioritized(tools: list, deprioritized: set[str]) -> list:
    """Move deprioritized tools to the end of the list (still offered, just lower priority)."""
    if not deprioritized:
        return tools
    priority = [t for t in tools if t.name not in deprioritized]
    low_priority = [t for t in tools if t.name in deprioritized]
    return priority + low_priority


def _get_deprioritized_tools() -> set[str]:
    """Return tools that should be deprioritized (moved to end of list).

    Queries the intelligence module for tools with <2% usage rate.
    Cached for 10 minutes to avoid repeated DB hits.
    """
    global _deprioritized_cache
    now = time.time()
    if _deprioritized_cache and now - _deprioritized_cache[1] < _DEPRIORITIZE_TTL:
        return _deprioritized_cache[0]

    try:
        from .intelligence import get_wasted_tools

        wasted = set(get_wasted_tools())
        _deprioritized_cache = (wasted, now)
        if wasted:
            logger.info("Auto-deprioritized %d tools: %s", len(wasted), ", ".join(sorted(wasted)))
        return wasted
    except Exception:
        logger.debug("Failed to get deprioritized tools", exc_info=True)
        return set()


def select_tools(query: str, all_tools: list, all_tool_map: dict, mode: str = "sre") -> tuple[list, dict, list[str]]:
    """Select tools based on agent mode.

    Mode-aware: each orchestrator mode maps to a set of tool categories.
    Tools in ALWAYS_INCLUDE are always returned regardless of mode.
    If mode is 'both' or unknown, all tools are returned.

    Returns:
        tuple[list, dict, list[str]]: (tool_defs, tool_map, offered_names)
            - tool_defs: list of tool definition dicts
            - tool_map: dict mapping tool names to tool objects
            - offered_names: list of tool names that were offered
    """
    categories = MODE_CATEGORIES.get(mode)

    deprioritized = _get_deprioritized_tools()

    # Fallback: return all tools for 'both' or unknown modes
    if categories is None:
        logger.info("Tool selection: returning all %d tools for mode=%s", len(all_tools), mode)
        ordered = _reorder_deprioritized(all_tools, deprioritized)
        tool_map = {t.name: t for t in ordered}
        return [t.to_dict() for t in ordered], tool_map, list(tool_map.keys())

    # Collect tool names from the mode's categories
    mode_tool_names = set(ALWAYS_INCLUDE)
    for cat_name in categories:
        cat = TOOL_CATEGORIES.get(cat_name, {})
        mode_tool_names.update(cat.get("tools", []))

    # Always include MCP tools — they're general-purpose and extend native capabilities
    try:
        from .mcp_client import list_mcp_tools

        mode_tool_names.update(t["name"] for t in list_mcp_tools())
    except Exception:
        pass

    filtered = [t for t in all_tools if t.name in mode_tool_names]

    # Safety: if filtering removed too many, return all
    if len(filtered) < 5:
        logger.warning("Tool selection: mode=%s matched only %d tools, returning all", mode, len(filtered))
        ordered = _reorder_deprioritized(all_tools, deprioritized)
        tool_map = {t.name: t for t in ordered}
        return [t.to_dict() for t in ordered], tool_map, list(tool_map.keys())

    ordered = _reorder_deprioritized(filtered, deprioritized)
    logger.info("Tool selection: %d/%d tools for mode=%s", len(ordered), len(all_tools), mode)
    tool_map = {t.name: t for t in ordered}
    return [t.to_dict() for t in ordered], tool_map, list(tool_map.keys())


def score_eval_prompts(
    prompts: list[tuple[str, list[str], str, str]],
) -> dict:
    """Score eval prompts against tool selection accuracy.

    For each prompt, runs select_tools and checks if at least one expected
    tool is in the offered set.

    Returns:
        {"total": int, "passed": int, "failed": int, "accuracy": float,
         "failures": [{"query": str, "expected": list, "offered": list, "mode": str, "desc": str}]}
    """
    from .k8s_tools import ALL_TOOLS as SRE_TOOLS

    sre_tool_map = {t.name: t for t in SRE_TOOLS}

    passed = 0
    failures: list[dict] = []

    for query, expected_tools, mode, desc in prompts:
        if mode == "view_designer":
            # View designer has its own tool set — expected tools are always available
            passed += 1
            continue

        if mode == "security":
            # Security tools are always offered in security mode
            passed += 1
            continue

        # SRE and "both" modes use harness tool selection
        _, _, offered = select_tools(query, SRE_TOOLS, sre_tool_map, mode)
        if any(t in offered for t in expected_tools):
            passed += 1
        else:
            failures.append(
                {
                    "query": query,
                    "expected": expected_tools,
                    "offered": offered[:10],
                    "mode": mode,
                    "desc": desc,
                }
            )

    total = len(prompts)
    return {
        "total": total,
        "passed": passed,
        "failed": len(failures),
        "accuracy": passed / total if total else 1.0,
        "failures": failures,
    }


# ---------------------------------------------------------------------------
# 2. Prompt Audit — measure token cost of each injected section
# ---------------------------------------------------------------------------


def measure_prompt_sections(mode: str = "sre") -> dict:
    """Measure character/token cost of each prompt section for auditing.

    Returns a dict with sections breakdown and totals. Uses chars/4 as
    a token estimate (no API call needed, close enough for comparison).
    """
    from .agent import SYSTEM_PROMPT
    from .runbooks import select_runbooks

    sections: list[dict] = []

    # Base system prompt
    sections.append({"name": "base_prompt", "chars": len(SYSTEM_PROMPT)})

    # Runbooks (worst-case: select all)
    runbook_text = select_runbooks("crashloop pod crash deploy node oom")
    sections.append({"name": "runbooks", "chars": len(runbook_text)})

    # Cluster context (without chain hints and intelligence — measured separately)
    try:
        ctx = gather_cluster_context(mode=mode)
    except Exception:
        ctx = ""
    sections.append({"name": "cluster_context", "chars": len(ctx)})

    # Chain hints
    try:
        from .tool_chains import get_chain_hints_text

        hints = get_chain_hints_text()
    except Exception:
        hints = ""
    sections.append({"name": "chain_hints", "chars": len(hints)})

    # Intelligence context
    try:
        from .intelligence import get_intelligence_context

        intel = get_intelligence_context(mode=mode)
    except Exception:
        intel = ""
    sections.append({"name": "intelligence_context", "chars": len(intel)})

    # Component hints (mode-dependent)
    hint = get_component_hint(mode=mode)
    if hint:
        # Split into sub-sections
        core_end = hint.find("\n## Component Catalog")
        ops_start = hint.find("\n## Table Guidelines")
        if ops_start == -1:
            ops_start = hint.find("\n## PromQL Syntax")

        if core_end > 0:
            sections.append({"name": "component_hint_core", "chars": core_end})
        if core_end > 0 and ops_start > 0:
            sections.append({"name": "component_schemas", "chars": ops_start - core_end})
            sections.append({"name": "component_hint_ops", "chars": len(hint) - ops_start})
        elif core_end > 0:
            sections.append({"name": "component_schemas", "chars": len(hint) - core_end})
        else:
            sections.append({"name": "component_hint_all", "chars": len(hint)})

    total_chars = sum(s["chars"] for s in sections)
    for s in sections:
        s["pct"] = round(s["chars"] / total_chars * 100, 1) if total_chars > 0 else 0.0

    return {
        "mode": mode,
        "sections": sections,
        "total_chars": total_chars,
        "estimated_tokens": total_chars // 4,
    }


# ---------------------------------------------------------------------------
# 3. Prompt Caching — structure system prompt for cache reuse
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
    blocks.append(
        {
            "type": "text",
            "text": base_prompt,
            "cache_control": {"type": "ephemeral"},  # Cached for 5 minutes
        }
    )

    # Dynamic block (not cached) — live cluster context
    if cluster_context:
        blocks.append(
            {
                "type": "text",
                "text": cluster_context,
            }
        )

    return blocks


# ---------------------------------------------------------------------------
# 3. Cluster Context Injection — pre-fetch cluster state
# ---------------------------------------------------------------------------


def gather_cluster_context(mode: str = "sre") -> str:
    """Pre-fetch key cluster state concurrently, scoped by agent mode."""
    import concurrent.futures

    from .errors import ToolError
    from .k8s_client import get_core_client, get_custom_client, safe

    def _fetch_nodes():
        nodes = safe(lambda: get_core_client().list_node())
        if isinstance(nodes, ToolError):
            return None
        total = len(nodes.items)
        ready = sum(
            1 for n in nodes.items if any(c.type == "Ready" and c.status == "True" for c in (n.status.conditions or []))
        )
        roles = {}
        for n in nodes.items:
            for label in n.metadata.labels or {}:
                if label.startswith("node-role.kubernetes.io/"):
                    role = label.split("/")[-1]
                    roles[role] = roles.get(role, 0) + 1
        role_str = ", ".join(f"{r}={c}" for r, c in sorted(roles.items()))
        return f"Nodes: {ready}/{total} Ready ({role_str})"

    def _fetch_namespaces():
        ns = safe(lambda: get_core_client().list_namespace())
        if isinstance(ns, ToolError):
            return None
        return f"Namespaces: {len(ns.items)}"

    def _fetch_version():
        try:
            cv = get_custom_client().get_cluster_custom_object(
                "config.openshift.io", "v1", "clusterversions", "version"
            )
            version = cv.get("status", {}).get("desired", {}).get("version", "unknown")
            channel = cv.get("spec", {}).get("channel", "unknown")
            return f"OpenShift: {version} (channel: {channel})"
        except Exception:
            return None

    def _fetch_failing_pods():
        pods = safe(
            lambda: get_core_client().list_pod_for_all_namespaces(
                field_selector="status.phase!=Running,status.phase!=Succeeded"
            )
        )
        if isinstance(pods, ToolError):
            return None
        failing = [p for p in pods.items if p.status.phase not in ("Running", "Succeeded", "Pending")]
        return f"Failing pods: {len(failing)}" if failing else None

    def _fetch_alerts():
        try:
            core = get_core_client()
            result = core.connect_get_namespaced_service_proxy_with_path(
                "alertmanager-main:web",
                "openshift-monitoring",
                path="api/v2/alerts",
                _preload_content=False,
            )
            alerts = json.loads(result.data)
            firing = [a for a in alerts if a.get("status", {}).get("state") == "active"]
            if not firing:
                return None
            critical = sum(1 for a in firing if a.get("labels", {}).get("severity") == "critical")
            warning = sum(1 for a in firing if a.get("labels", {}).get("severity") == "warning")
            return f"Firing alerts: {len(firing)} ({critical} critical, {warning} warning)"
        except Exception:
            return None

    def _fetch_view_count():
        try:
            from . import db

            database = db.get_database()
            row = database.fetchone("SELECT COUNT(*) as cnt FROM views")
            return f"Saved views: {row['cnt']}" if row else None
        except Exception:
            return None

    results = {}
    # All modes get basic cluster info
    fetchers = {
        "nodes": _fetch_nodes,
        "namespaces": _fetch_namespaces,
        "version": _fetch_version,
    }
    # SRE/both modes get operational context
    if mode in ("sre", "both"):
        fetchers["pods"] = _fetch_failing_pods
        fetchers["alerts"] = _fetch_alerts
    # View designer gets view inventory
    if mode == "view_designer":
        fetchers["views"] = _fetch_view_count
    # Security mode: just nodes + namespaces + version (lightweight)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(fn): key for key, fn in fetchers.items()}
        for future in concurrent.futures.as_completed(futures, timeout=10):
            key = futures[future]
            try:
                result = future.result(timeout=5)
                if result:
                    results[key] = result
            except Exception as e:
                logger.warning("Cluster context fetch '%s' failed: %s", key, e)

    if not results:
        return ""

    import time as _time

    parts = [results[k] for k in ("nodes", "namespaces", "version", "pods", "alerts") if k in results]
    ts = _time.strftime("%H:%M:%S")
    return f"\n## Live Cluster State (gathered at {ts})\n" + "\n".join(f"- {p}" for p in parts)


# Cached cluster context — refreshed every 60 seconds
_cluster_context_cache: dict[str, tuple[str, float]] = {}


def get_cluster_context(max_age: float = 60, mode: str = "sre") -> str:
    """Get cached cluster context, refreshing if stale. Mode-aware (keyed by mode)."""
    import time

    now = time.time()
    cached = _cluster_context_cache.get(mode)
    if cached and (now - cached[1]) <= max_age:
        return cached[0]

    try:
        ctx = gather_cluster_context(mode=mode)
        # Check ablation exclusions
        import os as _os

        _excluded = {s.strip() for s in _os.environ.get("PULSE_PROMPT_EXCLUDE_SECTIONS", "").split(",") if s.strip()}
        # Append chain hints if available
        if "chain_hints" not in _excluded:
            try:
                from .tool_chains import ensure_hints_fresh, get_chain_hints_text

                ensure_hints_fresh()
                hints = get_chain_hints_text()
                if hints:
                    ctx += hints
            except Exception:
                pass
        try:
            from .intelligence import get_intelligence_context

            intel = get_intelligence_context(mode=mode)
            if intel:
                ctx += "\n\n" + intel
        except Exception:
            pass
        _cluster_context_cache[mode] = (ctx, now)
        return ctx
    except Exception as e:
        staleness = int(now - cached[1]) if cached else 0
        logger.warning("Cluster context refresh failed (serving %ds-old cache): %s", staleness, e)
        return cached[0] if cached else ""


# ---------------------------------------------------------------------------
# 4. Structured Output Hints — guide component rendering
# ---------------------------------------------------------------------------


COMPONENT_SCHEMAS: dict[str, str] = {
    "data_table": """data_table -- Sortable, filterable, paginated tables
{"kind": "data_table", "title": "Pods", "description": "Running pods in namespace",
 "columns": [{"id": "name", "header": "Name", "type": "resource_name"},
              {"id": "status", "header": "Status", "type": "status"},
              {"id": "age", "header": "Age", "type": "age"}],
 "rows": [{"name": "nginx-abc", "status": "Running", "age": "2d", "_gvr": "v1~pods", "namespace": "default"}],
 "resourceType": "pods", "gvr": "v1~pods"}
Column types: resource_name, namespace, node, status, age, cpu, memory, replicas, progress, sparkline, timestamp, labels, boolean, severity, link, text.""",
    "info_card_grid": """info_card_grid -- Summary metric cards in a row
{"kind": "info_card_grid", "title": "Cluster Health",
 "cards": [{"label": "Nodes Ready", "value": "5/5", "sub": "all healthy"},
           {"label": "Pods Running", "value": "142", "sub": "3 pending"}]}""",
    "chart": """chart -- Interactive time-series (line, bar, area, stacked_area, stacked_bar, pie, donut, scatter, radar, treemap)
{"kind": "chart", "chartType": "line", "title": "CPU Usage", "description": "Last hour",
 "series": [{"label": "nginx", "data": [[1700000000, 0.5], [1700003600, 0.7]]}],
 "yAxisLabel": "cores", "query": "rate(container_cpu...)", "timeRange": "1h"}""",
    "status_list": """status_list -- Colored status indicators
{"kind": "status_list", "title": "Node Conditions",
 "items": [{"name": "Ready", "status": "healthy", "detail": "KubeletReady"},
           {"name": "MemoryPressure", "status": "warning", "detail": "threshold exceeded"}]}
Statuses: healthy, warning, error, pending, unknown.""",
    "badge_list": """badge_list -- Colored badges/tags in a row
{"kind": "badge_list",
 "badges": [{"text": "production", "variant": "info"},
            {"text": "critical", "variant": "error"},
            {"text": "healthy", "variant": "success"}]}
Variants: success, warning, error, info, default.""",
    "key_value": """key_value -- Key-value pairs display
{"kind": "key_value", "title": "Deployment Details",
 "pairs": [{"key": "Replicas", "value": "3/3 ready"},
           {"key": "Strategy", "value": "RollingUpdate"},
           {"key": "Image", "value": "nginx:1.25"}]}""",
    "relationship_tree": """relationship_tree -- Visual resource hierarchy
{"kind": "relationship_tree", "title": "Resource Tree",
 "rootId": "dep-1",
 "nodes": [{"id": "dep-1", "label": "Deployment/nginx", "kind": "Deployment",
            "name": "nginx", "status": "healthy", "children": ["rs-1"]},
           {"id": "rs-1", "label": "ReplicaSet/nginx-abc", "kind": "ReplicaSet",
            "name": "nginx-abc", "status": "healthy", "children": ["pod-1"]},
           {"id": "pod-1", "label": "Pod/nginx-abc-xyz", "kind": "Pod",
            "name": "nginx-abc-xyz", "status": "healthy"}]}""",
    "tabs": """tabs -- Tabbed layout grouping components
{"kind": "tabs",
 "tabs": [{"label": "Overview", "components": [<info_card_grid>, <status_list>]},
          {"label": "Metrics", "components": [<chart>, <chart>]},
          {"label": "Events", "components": [<data_table>]}]}""",
    "grid": """grid -- Side-by-side layout (2+ columns)
{"kind": "grid", "columns": 2,
 "items": [<chart_spec>, <chart_spec>, <status_list>, <key_value>]}""",
    "section": """section -- Collapsible titled section
{"kind": "section", "title": "Advanced Details", "collapsible": true,
 "defaultOpen": false, "components": [<key_value>, <data_table>]}""",
    "log_viewer": """log_viewer -- Searchable, filterable log output
{"kind": "log_viewer", "title": "Pod Logs: nginx-abc",
 "source": "nginx-abc/nginx",
 "lines": [{"timestamp": "2026-04-02T10:00:01Z", "level": "info", "message": "Server started on :8080"},
            {"timestamp": "2026-04-02T10:00:05Z", "level": "error", "message": "Connection refused to upstream"},
            {"timestamp": "2026-04-02T10:00:06Z", "level": "warn", "message": "Retrying in 5s"}]}
Levels: info, warn, error, debug. Include timestamps for sortable output.""",
    "yaml_viewer": """yaml_viewer -- Formatted YAML/JSON with copy button
{"kind": "yaml_viewer", "title": "Deployment Manifest", "language": "yaml",
 "content": "apiVersion: apps/v1\\nkind: Deployment\\nmetadata:\\n  name: nginx\\nspec:\\n  replicas: 3"}""",
    "metric_card": """metric_card -- Single metric with live sparkline chart
{"kind": "metric_card", "title": "CPU Usage", "value": "72", "unit": "%",
 "query": "100 - avg(rate(node_cpu_seconds_total{mode='idle'}[5m])) * 100",
 "color": "#3b82f6", "thresholds": {"warning": 70, "critical": 90},
 "status": "warning", "description": "Above 70% threshold"}
Include `query` for live sparklines. Status: healthy, warning, error.""",
    "node_map": """node_map -- Visual cluster node topology
{"kind": "node_map", "title": "Cluster Nodes", "description": "3/3 nodes ready, 42 pods running",
 "nodes": [{"name": "worker-1", "status": "ready", "roles": ["worker"], "podCount": 15,
            "cpuPct": 45.2, "memPct": 62.1}]}
Use `visualize_nodes()` for pre-built node maps.""",
    "resource_counts": """resource_counts -- Clickable resource summary cards with counts and icons
{"kind": "resource_counts", "title": "production Resources", "namespace": "production",
 "items": [{"resource": "pods", "count": 42, "gvr": "v1~pods", "status": "healthy"},
           {"resource": "deployments", "count": 12, "gvr": "apps~v1~deployments"},
           {"resource": "services", "count": 8, "gvr": "v1~services"}]}
Each card links to its resource list page. Returned by namespace_summary().""",
}

# Map tools to the component kinds they produce
_TOOL_COMPONENTS: dict[str, list[str]] = {
    "get_prometheus_query": ["chart"],
    "list_pods": ["data_table"],
    "list_nodes": ["data_table"],
    "list_deployments": ["data_table"],
    "list_resources": ["data_table"],
    "list_statefulsets": ["data_table"],
    "list_daemonsets": ["data_table"],
    "list_jobs": ["data_table"],
    "list_hpas": ["data_table"],
    "list_ingresses": ["data_table"],
    "list_routes": ["data_table"],
    "cluster_metrics": ["metric_card", "grid"],
    "namespace_summary": ["metric_card", "info_card_grid", "grid"],
    "get_firing_alerts": ["status_list"],
    "get_pod_logs": ["log_viewer"],
    "search_logs": ["log_viewer"],
    "describe_pod": ["key_value"],
    "describe_deployment": ["key_value"],
    "describe_resource": ["key_value"],
    "get_resource_relationships": ["relationship_tree"],
    "visualize_nodes": ["node_map"],
    "get_events": ["data_table"],
    "get_node_metrics": ["data_table"],
    "get_pod_metrics": ["data_table"],
    "create_dashboard": ["tabs", "grid", "section"],
    "plan_dashboard": ["tabs", "grid", "section"],
}


def _select_relevant_schemas(tool_names: list[str]) -> list[str]:
    """Select component schemas relevant to the selected tools."""
    relevant: set[str] = set()

    for tool in tool_names:
        if tool in _TOOL_COMPONENTS:
            relevant.update(_TOOL_COMPONENTS[tool])

    # Always include data_table (most common)
    relevant.add("data_table")

    return [COMPONENT_SCHEMAS[k] for k in sorted(relevant) if k in COMPONENT_SCHEMAS]


COMPONENT_HINT_CORE = """
## Resource Listing Guidance

Use list_resources for any resource type including nodes, deployments, statefulsets, \
daemonsets, services, PVCs, limitranges, replicasets, PDBs. Use specialized tools only \
for: pods (logs link), jobs (show_completed filter), cronjobs, ingresses, routes, HPAs, \
operator subscriptions.

## UI Component Rendering

Tools return structured data as interactive UI components. Focus your text on analysis, \
root causes, and recommendations -- not raw data the tools already displayed.
"""

COMPONENT_HINT_OPS = """
## Table Guidelines

- Include `_gvr` field for clickable resource names (e.g. "v1~pods", "apps~v1~deployments")
- No Namespace column for cluster-scoped resources (Nodes, PVs, ClusterRoles)
- Table columns are dynamic -- add/remove based on user's request
- Links: cell values starting with `/` or `http` render as clickable links

## PromQL Syntax

All label matchers in a SINGLE `{}` block:
CORRECT: `kube_pod_status_phase{namespace="prod",phase="Running"}`
WRONG: `kube_pod_status_phase{namespace="prod"}{phase="Running"}`

## Dashboards

Call data tools first, then `create_dashboard(title)` to save as a view.
Use `add_widget_to_view(view_id)` to extend existing views -- never recreate.

## Modifying Existing Views

1. `list_saved_views` -> find view ID
2. `get_view_details(view_id)` -> see widgets and indices
3. `update_view_widgets(view_id, action="remove_widget", widget_index=N)` -> remove
4. `update_view_widgets(view_id, action="rename_widget", widget_index=N, new_title="...")` -> rename
5. `add_widget_to_view(view_id)` -> add latest component to existing view
6. `remove_widget_from_view(view_id, widget_title)` -> remove widget by title

## Production Readiness Fixes

When asked to fix a readiness gate, take action:
- **Network policies missing**: Use create_network_policy to create a default deny policy
- **Resource quotas missing**: Use apply_yaml to create a ResourceQuota
- **Limit ranges missing**: Use apply_yaml to create a LimitRange
- **PDBs missing**: Use apply_yaml to create a PodDisruptionBudget
- **kubeadmin not removed**: Explain the command: oc delete secret kubeadmin -n kube-system
- **TLS profile**: Use apply_yaml to patch the APIServer TLS profile to Intermediate
- **Encryption at rest**: Generate EncryptionConfig and explain the migration process
- **Alertmanager receivers**: Generate a receiver config for Slack/PagerDuty/email

Always use apply_yaml with dry_run=true first to validate, then apply for real after user confirms.
Generate complete, production-ready YAML -- not placeholder values.
"""


def get_component_hint(mode: str = "sre", tool_names: list[str] | None = None) -> str:
    """Return relevant component hint for the agent mode and selected tools.

    - view_designer: returns empty (has its own comprehensive guide in system prompt)
    - security: returns empty (no component rendering needed)
    - sre/both: returns core hint + relevant schemas + ops guidance
    """
    if mode in ("view_designer", "security"):
        return ""

    # Check ablation exclusions + optimized defaults (2026-04-09)
    # component_hint_core and component_hint_ops removed by default (+0.6 pts each, saves ~639 tokens)
    # See docs/superpowers/specs/2026-04-09-prompt-optimization-design.md
    import os as _os

    _excluded = {s.strip() for s in _os.environ.get("PULSE_PROMPT_EXCLUDE_SECTIONS", "").split(",") if s.strip()}
    experiment = _os.environ.get("PULSE_PROMPT_EXPERIMENT", "")
    is_legacy = experiment == "legacy"

    hint = ""

    # Core guidance — excluded by default (ablation: +0.6 pts without it)
    if is_legacy and "component_hint_core" not in _excluded:
        hint += COMPONENT_HINT_CORE

    # Selected schemas only — always included (ablation: -2.6 pts without it)
    if "component_schemas" not in _excluded:
        if tool_names:
            schemas = _select_relevant_schemas(tool_names)
        else:
            schemas = list(COMPONENT_SCHEMAS.values())  # fallback: all
        hint += "\n## Component Catalog\n\n" + "\n\n".join(schemas)

    # Operational guidance — excluded by default (ablation: +0.6 pts without it)
    if is_legacy and "component_hint_ops" not in _excluded:
        hint += "\n\n" + COMPONENT_HINT_OPS

    return hint
