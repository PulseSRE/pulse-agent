"""Skill Loader — loads skill packages from markdown files and routes queries.

Reads sre_agent/skills/*/skill.md files, parses YAML frontmatter,
builds routing tables, validates dependencies, and supports hot reload.

Also owns the canonical tool-category data (TOOL_CATEGORIES, ALWAYS_INCLUDE,
get_tool_category, select_tools) that was previously in harness.py.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger("pulse_agent.skill_loader")

_SKILLS_DIR = Path(__file__).parent / "skills"


# User-created skills go to a writable directory (PVC-backed in containers)
def _get_user_skills_dir() -> Path:
    from .config import get_settings

    return Path(get_settings().user_skills_dir)


@dataclass
class Skill:
    """A loaded skill definition."""

    name: str
    version: int
    description: str
    keywords: list[str]
    categories: list[str]
    write_tools: bool
    priority: int
    system_prompt: str
    requires_tools: list[str] = field(default_factory=list)
    handoff_to: dict[str, list[str]] = field(default_factory=dict)
    configurable: list[dict] = field(default_factory=list)
    eval_scenarios: list[dict] = field(default_factory=list)
    path: Path = field(default=Path("."))
    skip_component_hints: bool = False
    degraded: bool = False
    degraded_reason: str = ""
    display_name: str = ""
    icon: str = ""
    builtin: bool = True

    def to_dict(self) -> dict:
        """Serialize for API responses."""
        return {
            "name": self.name,
            "display_name": self.display_name or _format_display_name(self.name),
            "icon": self.icon or _default_icon(self.name),
            "builtin": self.builtin,
            "version": self.version,
            "description": self.description,
            "keywords": self.keywords,
            "categories": self.categories,
            "write_tools": self.write_tools,
            "priority": self.priority,
            "requires_tools": self.requires_tools,
            "handoff_to": self.handoff_to,
            "configurable": self.configurable,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "prompt_length": len(self.system_prompt),
        }


_BUILTIN_ICONS: dict[str, str] = {
    "sre": "Wrench",
    "security": "Shield",
    "view_designer": "LayoutDashboard",
    "capacity_planner": "TrendingUp",
}


def _default_icon(name: str) -> str:
    return _BUILTIN_ICONS.get(name, "Puzzle")


def _format_display_name(name: str) -> str:
    return name.replace("_", " ").replace("-", " ").title()


# Global skill registry
_skills: dict[str, Skill] = {}
_load_timestamp: float = 0
_keyword_index: list[tuple[str, str, int]] = []  # [(keyword, skill_name, keyword_length)]


def _parse_skill_md(path: Path) -> Skill | None:
    """Parse a skill.md file into a Skill object."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("Failed to read skill file %s: %s", path, e)
        return None

    # Split frontmatter from body
    parts = text.split("---", 2)
    if len(parts) < 3:
        logger.warning("Skill file %s has no YAML frontmatter (missing --- delimiters)", path)
        return None

    try:
        meta = yaml.safe_load(parts[1])
    except yaml.YAMLError as e:
        logger.warning("Invalid YAML frontmatter in %s: %s", path, e)
        return None

    if not isinstance(meta, dict):
        logger.warning("Frontmatter in %s is not a dict", path)
        return None

    name = meta.get("name", "")
    if not name:
        logger.warning("Skill file %s missing 'name' in frontmatter", path)
        return None

    # Parse keywords — support both flat list and comma-separated strings
    raw_keywords = meta.get("keywords", [])
    keywords: list[str] = []
    for entry in raw_keywords:
        if isinstance(entry, str):
            keywords.extend(k.strip() for k in entry.split(",") if k.strip())
        else:
            keywords.append(str(entry))

    body = parts[2].strip()

    # Validate schema
    errors = _validate_schema(meta, path)
    if errors:
        for err in errors:
            logger.warning("Skill '%s' validation: %s", name, err)

    # Determine if this is a built-in skill (from the package dir, not user dir)
    is_builtin = str(_SKILLS_DIR) in str(path)

    return Skill(
        name=name,
        version=meta.get("version", 1),
        description=meta.get("description", ""),
        keywords=keywords,
        categories=meta.get("categories", []),
        write_tools=meta.get("write_tools", False),
        priority=meta.get("priority", 10),
        system_prompt=body,
        requires_tools=meta.get("requires_tools", []),
        handoff_to=meta.get("handoff_to", {}),
        configurable=meta.get("configurable", []),
        skip_component_hints=meta.get("skip_component_hints", False),
        path=path.parent,
        display_name=meta.get("display_name", ""),
        icon=meta.get("icon", ""),
        builtin=is_builtin,
    )


_VALID_CATEGORIES = {
    "diagnostics",
    "workloads",
    "networking",
    "security",
    "storage",
    "monitoring",
    "operations",
    "gitops",
    "fleet",
    "self",
}

_VALID_CONFIGURABLE_TYPES = {"enum", "string", "boolean", "number"}


def _validate_schema(meta: dict, path: Path) -> list[str]:
    """Validate skill frontmatter schema. Returns list of warnings (non-fatal)."""
    errors: list[str] = []

    # Version must be a positive integer
    version = meta.get("version")
    if version is not None and (not isinstance(version, int) or version < 1):
        errors.append(f"version must be a positive integer (got {version!r})")

    # Description should be non-empty
    if not meta.get("description"):
        errors.append("missing 'description' field")

    # Keywords should be non-empty list
    keywords = meta.get("keywords", [])
    if not keywords:
        errors.append("no keywords defined — skill won't be routable")

    # Categories should be valid names
    categories = meta.get("categories", [])
    for cat in categories:
        if cat not in _VALID_CATEGORIES:
            errors.append(f"unknown category '{cat}' — valid: {sorted(_VALID_CATEGORIES)}")

    # Priority should be reasonable
    priority = meta.get("priority", 10)
    if not isinstance(priority, int) or priority < 0 or priority > 100:
        errors.append(f"priority should be 0-100 (got {priority!r})")

    # Handoff targets should be strings mapping to keyword lists
    handoff_to = meta.get("handoff_to", {})
    if handoff_to and not isinstance(handoff_to, dict):
        errors.append("handoff_to must be a dict mapping skill names to keyword lists")
    elif isinstance(handoff_to, dict):
        for target, keywords_list in handoff_to.items():
            if not isinstance(keywords_list, list):
                errors.append(f"handoff_to.{target} must be a list of keywords")

    # Configurable fields must have valid types
    for cfg_field in meta.get("configurable", []):
        if isinstance(cfg_field, dict):
            for field_name, field_def in cfg_field.items():
                if isinstance(field_def, dict):
                    ftype = field_def.get("type", "")
                    if ftype and ftype not in _VALID_CONFIGURABLE_TYPES:
                        errors.append(f"configurable '{field_name}' has invalid type '{ftype}'")
                    if ftype == "enum" and not field_def.get("options"):
                        errors.append(f"configurable '{field_name}' is enum but has no options")
                    if ftype == "number":
                        mn, mx = field_def.get("min"), field_def.get("max")
                        if mn is not None and mx is not None and mn > mx:
                            errors.append(f"configurable '{field_name}' has min > max")

    return errors


def _build_keyword_index(skills: dict[str, Skill]) -> list[tuple[str, str, int]]:
    """Build a keyword → skill index sorted by keyword length (longest first)."""
    index: list[tuple[str, str, int]] = []
    for skill in skills.values():
        for kw in skill.keywords:
            kw_lower = kw.lower().strip()
            if kw_lower:
                index.append((kw_lower, skill.name, len(kw_lower)))
    # Sort by keyword length descending (longer keywords match first)
    index.sort(key=lambda x: -x[2])
    return index


def _validate_skill(skill: Skill) -> None:
    """Check if required tools exist. Mark skill as degraded if not.

    Skips validation if TOOL_REGISTRY hasn't been populated yet (empty at
    import time because tool modules haven't been imported).  Validation
    will run later via ``revalidate_skills()`` or on first actual use in
    ``build_config_from_skill`` / ``classify_query``.
    """
    if not skill.requires_tools:
        return

    from .tool_registry import TOOL_REGISTRY

    # Skip validation if registry hasn't been populated yet
    # (will be validated on first actual use via build_config_from_skill)
    if not TOOL_REGISTRY:
        return

    missing = [t for t in skill.requires_tools if t not in TOOL_REGISTRY]
    if missing:
        skill.degraded = True
        skill.degraded_reason = f"Missing required tools: {', '.join(missing)}"
        logger.warning("Skill '%s' is degraded: %s", skill.name, skill.degraded_reason)


def revalidate_skills() -> None:
    """Re-run validation on all loaded skills.

    Call this after all tool modules have been imported and TOOL_REGISTRY
    is fully populated (e.g., from app.py startup).
    """
    for skill in _skills.values():
        # Reset degraded state before re-validating
        skill.degraded = False
        skill.degraded_reason = ""
        _validate_skill(skill)


def load_skills(skills_dir: Path | None = None) -> dict[str, Skill]:
    """Load all skill packages from built-in and user-created directories."""
    global _skills, _load_timestamp, _keyword_index, _selector
    _selector = None  # force re-initialization when skills change

    loaded: dict[str, Skill] = {}

    # Scan both built-in and user-created skill directories
    dirs_to_scan = [skills_dir or _SKILLS_DIR, _get_user_skills_dir()]

    for directory in dirs_to_scan:
        if not directory.exists():
            continue

        for skill_dir in sorted(directory.iterdir()):
            skill_file = None
            if skill_dir.is_dir():
                skill_file = skill_dir / "skill.md"
            elif skill_dir.is_file() and skill_dir.suffix == ".md":
                skill_file = skill_dir

            if skill_file and skill_file.exists():
                skill = _parse_skill_md(skill_file)
                if skill:
                    _validate_skill(skill)
                    loaded[skill.name] = skill
                    logger.info(
                        "Loaded skill: %s v%d (%d keywords, %d categories%s)",
                        skill.name,
                        skill.version,
                        len(skill.keywords),
                        len(skill.categories),
                        ", DEGRADED" if skill.degraded else "",
                    )

    _skills = loaded
    _keyword_index = _build_keyword_index(loaded)
    _load_timestamp = time.time()

    logger.info("Loaded %d skills: %s", len(loaded), ", ".join(sorted(loaded.keys())))
    return loaded


def reload_skills() -> dict[str, Skill]:
    """Hot reload all skills. Logs added/removed/changed skills."""
    old_names = set(_skills.keys())
    old_versions = {n: s.version for n, s in _skills.items()}
    new_skills = load_skills()
    new_names = set(new_skills.keys())

    added = new_names - old_names
    removed = old_names - new_names
    changed = [n for n in old_names & new_names if old_versions.get(n) != new_skills[n].version]

    if added:
        logger.info("Skills added: %s", ", ".join(sorted(added)))
    if removed:
        logger.info("Skills removed: %s", ", ".join(sorted(removed)))
    if changed:
        logger.info("Skills updated: %s", ", ".join(sorted(changed)))
    if not added and not removed and not changed:
        logger.info("No skill changes detected")

    return new_skills


def list_skills() -> list[Skill]:
    """Return all loaded skills."""
    if not _skills:
        load_skills()
    return list(_skills.values())


def get_skill(name: str) -> Skill | None:
    """Get a skill by name."""
    if not _skills:
        load_skills()
    return _skills.get(name)


_LLM_CLASSIFY_THRESHOLD = 4  # minimum keyword score to skip LLM fallback
_llm_cache: dict[str, tuple[str, float]] = {}  # query_hash → (skill_name, timestamp)
_LLM_CACHE_TTL = 300  # 5 minutes
_LLM_CACHE_MAX = 100

# Last routing decision — stores confidence info for analytics/debugging
_last_routing_decision: dict = {}


def get_last_routing_decision() -> dict | None:
    """Return the last routing decision, or None if no routing has occurred."""
    return dict(_last_routing_decision) if _last_routing_decision else None


_selector = None  # SkillSelector | None — lazy-initialized


def _get_selector():
    """Lazy-initialize the SkillSelector singleton."""
    global _selector
    if _selector is None:
        if not _skills:
            load_skills()
        from .skill_selector import SkillSelector

        _selector = SkillSelector(_skills, keyword_index=_keyword_index)
    return _selector


def classify_query(query: str, *, context: dict | None = None) -> Skill:
    """Route a query to the best matching skill.

    ORCA multi-signal routing: keyword + component tags + historical channels
    with weighted score fusion and dynamic thresholds.
    """
    if not _skills:
        load_skills()

    # Apply typo correction
    try:
        from .orchestrator import fix_typos

        q = fix_typos(query)
    except ImportError:
        q = query

    selector = _get_selector()
    result = selector.select(q, context=context)

    best_skill = _skills.get(result.skill_name)

    # If ORCA didn't find a high-confidence match, try LLM fallback
    if result.source == "fallback" and not best_skill:
        llm_result = _llm_classify(query)
        if llm_result:
            best_skill = llm_result
            result.skill_name = llm_result.name
            result.source = "llm_fallback"

    if not best_skill:
        best_skill = _skills.get("sre") or next(iter(_skills.values()))
        result.skill_name = best_skill.name

    # Pre-route handoff
    handoff_target = check_handoff(best_skill, query)
    if handoff_target:
        logger.info(
            "classify_query: pre-route handoff %s → %s for '%s'",
            best_skill.name,
            handoff_target.name,
            query[:60],
        )
        best_skill = handoff_target
        result.skill_name = handoff_target.name

    # Update _last_routing_decision for backward compat
    _last_routing_decision.clear()
    _last_routing_decision.update(
        {
            "skill_name": result.skill_name,
            "keyword_score": int(result.fused_scores.get(result.skill_name, 0) * 10),
            "used_llm_fallback": result.source == "llm_fallback",
            "competing_scores": {k: int(v * 10) for k, v in result.fused_scores.items()},
        }
    )

    logger.debug(
        "classify_query: '%s' → %s (source=%s, score=%.3f, threshold=%.2f, %dms)",
        query[:60],
        result.skill_name,
        result.source,
        result.fused_scores.get(result.skill_name, 0),
        result.threshold_used,
        result.selection_ms,
    )

    return best_skill


def _llm_classify(query: str) -> Skill | None:
    """Use a lightweight LLM call to classify ambiguous queries.

    Caches results (FIFO, 100 entries, 5min TTL) to avoid repeat API calls.
    Returns None on any error (caller falls back to keyword/default).
    """
    import hashlib

    query_hash = hashlib.md5(query.lower().strip().encode()).hexdigest()[:16]

    # Check cache
    cached = _llm_cache.get(query_hash)
    if cached:
        name, ts = cached
        if time.time() - ts < _LLM_CACHE_TTL:
            skill = _skills.get(name)
            if skill:
                logger.debug("LLM classify cache hit: '%s' → %s", query[:50], name)
                return skill

    try:
        from .agent import create_client

        client = create_client()

        skill_options = "\n".join(f"- {s.name}: {s.description}" for s in _skills.values())
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=20,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Classify this user query into exactly one skill.\n\n"
                        f"Available skills:\n{skill_options}\n\n"
                        f"Query: {query}\n\n"
                        f"Reply with ONLY the skill name, nothing else."
                    ),
                }
            ],
        )

        name = response.content[0].text.strip().lower().replace(" ", "_")
        skill = _skills.get(name)
        if skill:
            # Cache the result
            _llm_cache[query_hash] = (name, time.time())
            # Evict oldest entries if cache is full
            while len(_llm_cache) > _LLM_CACHE_MAX:
                oldest_key = next(iter(_llm_cache))
                del _llm_cache[oldest_key]
            logger.info("LLM classify: '%s' → %s", query[:50], name)
            return skill

        logger.debug("LLM classify returned unknown skill: '%s'", name)
        return None
    except Exception as e:
        logger.debug("LLM classify failed: %s", e)
        return None


def check_handoff(current_skill: Skill, query: str) -> Skill | None:
    """Check if the query should trigger a handoff to another skill.

    Returns the target skill if a handoff keyword matches, else None.
    """
    if not current_skill.handoff_to:
        return None

    q = query.lower()
    for target_name, keywords in current_skill.handoff_to.items():
        for kw in keywords:
            if kw.lower() in q:
                target = get_skill(target_name)
                if target:
                    logger.info(
                        "Handoff: %s → %s (triggered by '%s')",
                        current_skill.name,
                        target_name,
                        kw,
                    )
                    return target

    return None


def get_mode_categories() -> dict[str, list[str] | None]:
    """Build MODE_CATEGORIES dynamically from loaded skills (replaces harness.py hardcoding)."""
    if not _skills:
        load_skills()

    result: dict[str, list[str] | None] = {}
    for skill in _skills.values():
        result[skill.name] = skill.categories if skill.categories else None

    # 'both' mode returns all tools
    result["both"] = None
    return result


# ---------------------------------------------------------------------------
# Tool Categories — canonical tool-selection data (moved from harness.py)
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
            "list_my_skills",
            "list_my_tools",
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
    "list_my_skills",
}

_SELF_DESCRIBE_TOOLS = {
    "list_my_tools",
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

_SELF_DESCRIBE_KEYWORDS = {
    "what can",
    "help",
    "tools",
    "explain",
    "create skill",
    "edit skill",
    "delete skill",
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


# Cache for deprioritized tools (refreshed every 10 minutes)
_deprioritized_cache: tuple[set[str], float] | None = None
_DEPRIORITIZE_TTL = 600  # 10 minutes


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

    # Include self-describe tools when query asks about capabilities
    if any(kw in query.lower() for kw in _SELF_DESCRIBE_KEYWORDS):
        mode_tool_names.update(_SELF_DESCRIBE_TOOLS)

    # Include MCP tools that don't duplicate native tools
    try:
        from .mcp_client import list_mcp_tools

        for t in list_mcp_tools():
            if t["name"] not in _MCP_NATIVE_OVERLAP:
                mode_tool_names.update([t["name"]])
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


def build_config_from_skill(skill: Skill, query: str = "") -> dict:
    """Build agent config from a skill — same format as orchestrator.build_orchestrated_config().

    Returns dict with: system_prompt, tool_defs, tool_map, write_tools.
    Tools are selected from the full tool registry based on the skill's categories.
    Multiple skills share the same tools and MCP connections.
    """
    from .tool_registry import TOOL_REGISTRY, WRITE_TOOL_NAMES

    # Full registry includes ALL tools: SRE, security, view designer, MCP, etc.
    # If registry is populated, use it. Otherwise fall back to legacy module-level maps.
    if TOOL_REGISTRY:
        all_tools = dict(TOOL_REGISTRY)
    else:
        # Fallback: merge tool maps from all agent modules
        from .agent import TOOL_MAP as SRE_MAP
        from .security_agent import TOOL_MAP as SEC_MAP
        from .view_designer import TOOL_MAP as VD_MAP

        all_tools = {**SRE_MAP, **SEC_MAP, **VD_MAP}

    if not skill.categories:
        # No categories = all tools (like view_designer)
        tool_map = dict(all_tools)
    elif query:
        from .tool_predictor import select_tools_adaptive

        _defs, tool_map, _offered = select_tools_adaptive(
            query,
            all_tool_map=all_tools,
            fallback_categories=skill.categories,
        )
    else:
        # Collect tools from the skill's categories + ALWAYS_INCLUDE
        tool_names = set(ALWAYS_INCLUDE)
        for cat_name in skill.categories:
            cat = TOOL_CATEGORIES.get(cat_name, {})
            tool_names.update(cat.get("tools", []))

        tool_map = {n: t for n, t in all_tools.items() if n in tool_names}

    # Include MCP tools that don't duplicate native tools
    try:
        from .mcp_client import list_mcp_tools

        for t_info in list_mcp_tools():
            mcp_name = t_info["name"]
            if mcp_name not in _MCP_NATIVE_OVERLAP and mcp_name not in tool_map and mcp_name in all_tools:
                tool_map[mcp_name] = all_tools[mcp_name]
    except Exception:
        pass

    # Reorder deprioritized tools
    deprioritized = _get_deprioritized_tools()
    if deprioritized:
        ordered = _reorder_deprioritized(list(tool_map.values()), deprioritized)
        tool_map = {t.name: t for t in ordered}

    tool_defs = [t.to_dict() for t in tool_map.values()]
    write_tools = set(WRITE_TOOL_NAMES) if skill.write_tools else set()

    # Build component hint from skill's context
    component_hint = _build_component_hint(skill, list(tool_map.keys()))

    return {
        "system_prompt": skill.system_prompt,
        "tool_defs": tool_defs,
        "tool_map": tool_map,
        "write_tools": write_tools,
        "component_hint": component_hint,
    }


def _build_component_hint(skill: Skill, tool_names: list[str]) -> str:
    """Build component rendering guidance tailored to a skill.

    - view_designer/security: empty (view_designer has its own guide, security doesn't render)
    - Other skills: relevant component schemas based on offered tools
    - Skills with components.yaml: include custom component definitions
    """
    if skill.skip_component_hints:
        return ""

    from .component_registry import get_prompt_hints
    from .harness import _TOOL_COMPONENTS

    # Select schemas relevant to the skill's tools
    relevant: set[str] = {"data_table"}  # always include
    for tool in tool_names:
        if tool in _TOOL_COMPONENTS:
            relevant.update(_TOOL_COMPONENTS[tool])

    hint_text = get_prompt_hints(kinds=sorted(relevant))
    hint = "\n## Component Catalog\n\n" + hint_text if hint_text else ""

    # Append custom components from skill's components.yaml
    components_file = skill.path / "components.yaml"
    if components_file.exists():
        try:
            import yaml

            data = yaml.safe_load(components_file.read_text(encoding="utf-8"))
            custom = data.get("components", {})
            if custom:
                hint += "\n\n## Custom Components\n"
                for name, spec in custom.items():
                    desc = spec.get("description", "")
                    hint += f"\n{name} — {desc}"
        except Exception:
            pass

    return hint


def load_skill_evals(skill_name: str) -> list[dict]:
    """Load eval scenarios from a skill's evals.yaml."""
    skill = get_skill(skill_name)
    if not skill:
        return []

    evals_file = skill.path / "evals.yaml"
    if not evals_file.exists():
        return []

    try:
        data = yaml.safe_load(evals_file.read_text(encoding="utf-8"))
        return data.get("scenarios", [])
    except Exception as e:
        logger.warning("Failed to load evals for skill '%s': %s", skill_name, e)
        return []
