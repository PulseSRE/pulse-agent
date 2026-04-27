"""Skill Loader — loads skill packages from markdown files.

Reads sre_agent/skills/*/skill.md files, parses YAML frontmatter,
builds routing tables, validates dependencies, and supports hot reload.

Tool-category data (TOOL_CATEGORIES, ALWAYS_INCLUDE, get_tool_category, etc.)
moved to tool_categories.py.

Routing logic (classify_query, classify_query_multi, check_handoff, etc.)
moved to skill_router.py.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Re-export routing functions for backward compatibility
from .skill_router import (
    check_handoff,
    classify_query,
    classify_query_multi,
    get_last_routing_decision,
)

# Re-export tool categories for backward compatibility
from .tool_categories import (
    _MCP_NATIVE_OVERLAP,
    _SELF_DESCRIBE_KEYWORDS,
    _SELF_DESCRIBE_TOOLS,
    ALWAYS_INCLUDE,
    MODE_CATEGORIES,
    TOOL_CATEGORIES,
    get_tool_category,
    get_tool_skills,
)

# Declare exports to satisfy F401 (imported but unused) checks
__all__ = [
    "ALWAYS_INCLUDE",
    "MODE_CATEGORIES",
    "TOOL_CATEGORIES",
    "check_handoff",
    "classify_query",
    "classify_query_multi",
    "get_last_routing_decision",
    "get_tool_category",
    "get_tool_skills",
]

logger = logging.getLogger("pulse_agent.skill_loader")

_SKILLS_DIR = Path(__file__).parent / "skills"


# User-created skills go to a writable directory (PVC-backed in containers)
def _get_user_skills_dir() -> Path:
    from .config import get_settings

    return Path(get_settings().server.user_skills_dir)


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
    alert_triggers: list[str] = field(default_factory=list)  # exact alert names that route here
    cluster_components: list[str] = field(default_factory=list)  # K8s component types (etcd, ingress, etc)
    examples: list[dict] = field(default_factory=list)  # few-shot [{scenario, correct, wrong}]
    success_criteria: str = ""  # measurable resolution condition
    risk_level: str = "low"  # low | medium | high — high triggers approval gate
    trigger_patterns: list[str] = field(default_factory=list)  # regex patterns for hard pre-route
    route_priority: int = 50  # lower = checked first in hard pre-route (built-in skills: 10-30)
    conflicts_with: list[str] = field(default_factory=list)  # conflicting skill names
    exclusive: bool = False  # if True, never run a secondary skill alongside this one
    supported_components: list[str] = field(default_factory=list)  # UI component types this skill renders
    generated_by: str = ""
    reviewed: bool = True

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
            "alert_triggers": self.alert_triggers,
            "cluster_components": self.cluster_components,
            "examples": self.examples,
            "success_criteria": self.success_criteria,
            "risk_level": self.risk_level,
            "conflicts_with": self.conflicts_with,
            "exclusive": self.exclusive,
            "supported_components": self.supported_components,
            "generated_by": self.generated_by,
            "reviewed": self.reviewed,
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
        alert_triggers=meta.get("alert_triggers", []),
        cluster_components=meta.get("cluster_components", []),
        examples=meta.get("examples", []),
        success_criteria=meta.get("success_criteria", ""),
        risk_level=meta.get("risk_level", "low"),
        trigger_patterns=meta.get("trigger_patterns", []),
        route_priority=meta.get("route_priority", 50),
        conflicts_with=[c.replace("-", "_") for c in meta.get("conflicts_with", [])],
        exclusive=meta.get("exclusive", False),
        supported_components=meta.get("supported_components", []),
        generated_by=meta.get("generated_by", ""),
        reviewed=meta.get("reviewed", True),
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
    "views",
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

    from .skill_router import reset_hard_pre_route

    reset_hard_pre_route()  # force rebuild of pre-route patterns from updated skill files

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
# Tool Categories — moved to tool_categories.py
# Re-exported here for backward compatibility
# ---------------------------------------------------------------------------

# Tool risk levels — derived from WRITE_TOOLS + known high-risk operations
_TOOL_RISK_LEVELS: dict[str, str] = {}  # populated lazily

MAX_TOOL_BUDGET = 50  # hard cap on tools per agent turn


def get_tool_risk_level(tool_name: str) -> str:
    """Return risk level for a tool: read-only, low, medium, high."""
    if not _TOOL_RISK_LEVELS:
        _populate_risk_levels()
    return _TOOL_RISK_LEVELS.get(tool_name, "read-only")


def _populate_risk_levels() -> None:
    """Classify all tools by risk level."""
    try:
        from .agent import WRITE_TOOLS

        high_risk = {"drain_node", "delete_pod", "rollback_deployment", "cordon_node", "uncordon_node"}
        medium_risk = {"scale_deployment", "restart_deployment", "apply_yaml", "patch_resource"}

        for tool in WRITE_TOOLS:
            if tool in high_risk:
                _TOOL_RISK_LEVELS[tool] = "high"
            elif tool in medium_risk:
                _TOOL_RISK_LEVELS[tool] = "medium"
            else:
                _TOOL_RISK_LEVELS[tool] = "low"
    except Exception:
        logger.debug("Failed to populate tool risk levels", exc_info=True)


def get_tool_avg_latency(tool_name: str) -> int:
    """Get average latency for a tool from usage history (ms)."""
    try:
        from .db import get_database

        db = get_database()
        row = db.fetchone(
            "SELECT AVG(duration_ms) as avg_ms FROM tool_usage "
            "WHERE tool_name = %s AND status = 'success' "
            "AND timestamp > NOW() - INTERVAL '7 days'",
            (tool_name,),
        )
        return int(row["avg_ms"]) if row and row["avg_ms"] else 0
    except Exception:
        return 0


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
        logger.debug("MCP tools unavailable for select_tools: %s", __import__("traceback").format_exc())

    filtered = [t for t in all_tools if t.name in mode_tool_names]

    # Safety: if filtering removed too many, return all
    if len(filtered) < 5:
        logger.warning("Tool selection: mode=%s matched only %d tools, returning all", mode, len(filtered))
        ordered = _reorder_deprioritized(all_tools, deprioritized)
        tool_map = {t.name: t for t in ordered}
        return [t.to_dict() for t in ordered], tool_map, list(tool_map.keys())

    ordered = _reorder_deprioritized(filtered, deprioritized)

    # Enforce tool budget — keep ALWAYS_INCLUDE + self-describe + top tools by relevance
    protected = ALWAYS_INCLUDE | (
        _SELF_DESCRIBE_TOOLS if any(kw in query.lower() for kw in _SELF_DESCRIBE_KEYWORDS) else set()
    )
    if len(ordered) > MAX_TOOL_BUDGET:
        always = [t for t in ordered if t.name in protected]
        rest = [t for t in ordered if t.name not in protected]
        budget_remaining = max(MAX_TOOL_BUDGET - len(always), 0)
        ordered = always + rest[:budget_remaining]
        logger.info(
            "Tool budget enforced: %d → %d tools (budget=%d) for mode=%s",
            len(filtered),
            len(ordered),
            MAX_TOOL_BUDGET,
            mode,
        )
    else:
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

    # Build the set of tools allowed by this skill's categories
    allowed_tool_names: set[str] = set()
    for cat_name in skill.categories:
        cat = TOOL_CATEGORIES.get(cat_name, {})
        allowed_tool_names.update(cat.get("tools", []))
    allowed_tool_names.update(ALWAYS_INCLUDE)

    # plan_builder always gets skill management tools
    if skill.name == "plan_builder":
        allowed_tool_names.update(_SELF_DESCRIBE_TOOLS)

    if not skill.categories:
        # No categories = all tools (like view_designer)
        tool_map = dict(all_tools)
    elif query:
        from .tool_predictor import select_tools_adaptive

        # Restrict predictor to only tools in the skill's categories
        scoped_tools = {n: t for n, t in all_tools.items() if n in allowed_tool_names}
        _defs, tool_map, _offered = select_tools_adaptive(
            query,
            all_tool_map=scoped_tools,
            fallback_categories=skill.categories,
        )
    else:
        tool_map = {n: t for n, t in all_tools.items() if n in allowed_tool_names}

    # Include MCP tools that don't duplicate native tools
    try:
        from .mcp_client import list_mcp_tools

        mcp_added = 0
        for t_info in list_mcp_tools():
            mcp_name = t_info["name"]
            if mcp_name not in _MCP_NATIVE_OVERLAP and mcp_name not in tool_map and mcp_name in all_tools:
                tool_map[mcp_name] = all_tools[mcp_name]
                mcp_added += 1
        if mcp_added:
            logger.debug("Added %d MCP tools for skill '%s'", mcp_added, skill.name)
    except Exception:
        logger.warning(
            "Failed to include MCP tools for skill '%s': %s", skill.name, __import__("traceback").format_exc()
        )

    # Reorder deprioritized tools
    deprioritized = _get_deprioritized_tools()
    if deprioritized:
        ordered = _reorder_deprioritized(list(tool_map.values()), deprioritized)
        tool_map = {t.name: t for t in ordered}

    # Guarantee required tools are present — these bypass TF-IDF prediction.
    # The skill's requires_tools field lists tools that MUST always be available,
    # not just validated at load time. This replaces three separate hard-coded
    # inclusion blocks (write tools, self-describe tools, ALWAYS_INCLUDE).
    for name in skill.requires_tools:
        if name in all_tools and name not in tool_map:
            tool_map[name] = all_tools[name]

    if skill.write_tools:
        # Write-enabled skills get ALL write tools from their categories
        for name in allowed_tool_names & WRITE_TOOL_NAMES:
            if name in all_tools and name not in tool_map:
                tool_map[name] = all_tools[name]
    else:
        # Strip write tools entirely — prevents calling dangerous tools without confirmation.
        tool_map = {n: t for n, t in tool_map.items() if n not in WRITE_TOOL_NAMES}

    tool_defs = [t.to_dict() for t in tool_map.values()]
    write_tools = set(WRITE_TOOL_NAMES) if skill.write_tools else set()

    # Build component hint from skill's context
    component_hint = _build_component_hint(skill, list(tool_map.keys()))

    prompt = skill.system_prompt
    tool_name_set = set(tool_map.keys())
    capabilities_hint = _build_capabilities_hint(tool_name_set)
    if capabilities_hint:
        prompt += f"\n\n{capabilities_hint}"
    mcp_hint = _build_mcp_hint(list(tool_map.keys()))
    if mcp_hint:
        prompt += f"\n\n{mcp_hint}"

    return {
        "system_prompt": prompt,
        "tool_defs": tool_defs,
        "tool_map": tool_map,
        "write_tools": write_tools,
        "component_hint": component_hint,
    }


def _build_capabilities_hint(tool_names: set[str]) -> str:
    """Inject shared capability awareness into all skill prompts."""
    sections: list[str] = []

    # Self-description tools (gap 3)
    self_tools = [
        ("describe_tools", "list all tools you have access to"),
        ("describe_agent", "list all available skills"),
        ("list_ui_components", "list visualization types you can render"),
        ("list_promql_recipes", "list pre-built Prometheus queries"),
        ("list_runbooks", "list available incident playbooks"),
        ("explain_resource", "explain any Kubernetes API resource"),
    ]
    available_self = [(name, desc) for name, desc in self_tools if name in tool_names]
    if available_self:
        lines = [
            "## Answering Capability Questions",
            "When the user asks what you can do, call these tools instead of answering from memory:",
        ]
        for name, desc in available_self:
            lines.append(f"- `{name}()` — {desc}")
        sections.append("\n".join(lines))

    # Memory and learning tools (gap 2)
    memory_tools = []
    if "search_past_incidents" in tool_names:
        memory_tools.append("- `search_past_incidents(query)` — find similar past issues and how they were resolved")
    if "get_learned_runbooks" in tool_names:
        memory_tools.append("- `get_learned_runbooks(query)` — get proven tool sequences from past resolutions")
    if memory_tools:
        sections.append(
            "## Learning From Past Incidents\n"
            "You have incident memory. Use it to recognize patterns and suggest proven fixes:\n"
            + "\n".join(memory_tools)
            + '\nWhen investigating, check memory first — say "I\'ve seen this before" when applicable.'
        )

    # Topology and blast radius tools (gap 4)
    topo_tools = []
    if "get_topology_graph" in tool_names:
        topo_tools.append("- `get_topology_graph(namespace)` — interactive dependency visualization")
    if "get_resource_relationships" in tool_names:
        topo_tools.append("- `get_resource_relationships(namespace, name, kind)` — what depends on this resource")
    if topo_tools:
        sections.append(
            "## Topology & Blast Radius\n"
            "You can analyze resource dependencies and blast radius:\n"
            + "\n".join(topo_tools)
            + "\nUse these when users ask about dependencies, blast radius, or impact analysis."
        )

    # Runbooks (gap 9)
    if "list_runbooks" in tool_names:
        sections.append(
            "## Runbooks\n"
            "You have built-in runbooks for common scenarios (crashlooping pods, node pressure, OOM, etc.). "
            "Call `list_runbooks()` to see available playbooks. Runbooks are also auto-matched to your queries."
        )

    _MAX_HINT_CHARS = 4000
    result = "\n\n".join(sections)
    if len(result) > _MAX_HINT_CHARS:
        truncated = result[:_MAX_HINT_CHARS]
        last_nl = truncated.rfind("\n")
        if last_nl > _MAX_HINT_CHARS // 2:
            truncated = truncated[:last_nl]
        included = truncated.count("\n\n") + 1
        omitted = len(sections) - included
        result = truncated + f"\n... ({omitted} more tool sections omitted)"
    return result


def _build_mcp_hint(tool_names: list[str]) -> str:
    """If MCP tools are available, generate a hint listing them by category."""
    try:
        from .mcp_client import list_mcp_tools

        mcp_tools = [t for t in list_mcp_tools() if t["name"] in set(tool_names)]
        if not mcp_tools:
            return ""

        categories: dict[str, list[str]] = {}
        for t in mcp_tools:
            prefix = t["name"].split("_")[0] if "_" in t["name"] else "other"
            categories.setdefault(prefix, []).append(t["name"])

        lines = [f"You have {len(mcp_tools)} MCP tools available from an OpenShift MCP server:"]
        for cat, tools in sorted(categories.items()):
            lines.append(f"  {cat}: {', '.join(tools)}")
        lines.append(
            "Use these tools when users ask about Helm releases, Tekton pipelines, Service Mesh, KubeVirt, or other MCP-provided capabilities."
        )
        return "\n".join(lines)
    except Exception:
        return ""


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
            logger.debug("Failed to load custom components for skill '%s'", skill.name, exc_info=True)

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
