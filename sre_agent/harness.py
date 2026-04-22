"""
Claude Harness — optimizations for getting the most out of the agent.

1. Dynamic Tool Selection — delegated to skill_loader.py (canonical owner)
2. Prompt Caching — cache system prompt + runbooks across turns
3. Cluster Context Injection — pre-fetch cluster state into system prompt
4. Structured Output Hints — guide Claude to return component specs
"""

from __future__ import annotations

import atexit
import concurrent.futures
import json
import logging

logger = logging.getLogger("pulse_agent.harness")

# Shared pool for cluster context gathering (nodes, namespaces, version, etc.)
_context_pool = concurrent.futures.ThreadPoolExecutor(max_workers=5, thread_name_prefix="ctx")
atexit.register(_context_pool.shutdown, wait=False)

# ---------------------------------------------------------------------------
# 1. Dynamic Tool Selection — delegated to skill_loader.py
# ---------------------------------------------------------------------------

# Re-export from skill_loader for backward compatibility
from .skill_loader import (  # noqa: F401
    ALWAYS_INCLUDE,
    MODE_CATEGORIES,
    TOOL_CATEGORIES,
    select_tools,
)


def get_tool_category(tool_name: str) -> str | None:
    """Return the primary category for a tool, or None if uncategorized."""
    from .skill_loader import get_tool_category as _get

    return _get(tool_name)


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
    from .tool_registry import TOOL_REGISTRY

    all_tools = list(TOOL_REGISTRY.values())
    sre_tool_map = dict(TOOL_REGISTRY)

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
        _, _, offered = select_tools(query, all_tools, sre_tool_map, mode)
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

    # ACM detection — all modes benefit from knowing if fleet tools are available
    def _fetch_acm_info():
        try:
            from .prometheus import get_prometheus_client

            if get_prometheus_client().is_acm_available():
                return "ACM Thanos: Available (use fleet_query_metrics for multi-cluster monitoring)"
        except Exception:
            pass
        return None

    fetchers["acm"] = _fetch_acm_info

    futures = {_context_pool.submit(fn): key for key, fn in fetchers.items()}
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

    parts = [results[k] for k in ("nodes", "namespaces", "version", "pods", "alerts", "acm") if k in results]
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
    "emit_component": ["action_button", "confidence_badge", "resolution_tracker", "blast_radius"],
    "get_topology_graph": ["topology", "blast_radius"],
}


def _select_relevant_schemas_from_registry(tool_names: list[str]) -> str:
    """Select component schemas from the component registry (data-driven alternative).

    Uses get_prompt_hints() from component_registry instead of the hardcoded
    COMPONENT_SCHEMAS dict. Returns formatted text ready for prompt injection.
    """
    from .component_registry import get_prompt_hints

    relevant: set[str] = set()

    for tool in tool_names:
        if tool in _TOOL_COMPONENTS:
            relevant.update(_TOOL_COMPONENTS[tool])

    # Always include data_table (most common)
    relevant.add("data_table")

    return get_prompt_hints(kinds=sorted(relevant)) if relevant else get_prompt_hints()


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
7. `optimize_view(view_id, strategy)` -> reorganize layout:
   - `"group"` — group related widgets into sections (compute, memory, workloads, etc.)
   - `"reflow"` — re-run semantic layout engine (fix whitespace, resize widgets)
   - `"compact"` — remove gaps, pack widgets tightly

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

    Delegates to skill-aware _build_component_hint when a skill is loaded for the mode.
    Falls back to tool-based schema selection for legacy modes.
    """
    # Try skill-aware component hint first (skill controls skip_component_hints)
    try:
        from .skill_loader import get_skill

        skill = get_skill(mode)
        if skill:
            from .skill_loader import _build_component_hint

            return _build_component_hint(skill, tool_names or [])
    except Exception:
        pass

    # Fallback: tool-based schema selection (legacy path)
    import os as _os

    _excluded = {s.strip() for s in _os.environ.get("PULSE_PROMPT_EXCLUDE_SECTIONS", "").split(",") if s.strip()}

    if "component_schemas" in _excluded:
        return ""

    if tool_names:
        hint_text = _select_relevant_schemas_from_registry(tool_names)
    else:
        hint_text = _select_relevant_schemas_from_registry([])
    return "\n## Component Catalog\n\n" + hint_text if hint_text else ""
