"""Skill Loader — loads skill packages from markdown files and routes queries.

Reads sre_agent/skills/*/skill.md files, parses YAML frontmatter,
builds routing tables, validates dependencies, and supports hot reload.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger("pulse_agent.skill_loader")

_SKILLS_DIR = Path(__file__).parent / "skills"


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
    degraded: bool = False
    degraded_reason: str = ""

    def to_dict(self) -> dict:
        """Serialize for API responses."""
        return {
            "name": self.name,
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
        path=path.parent,
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
    """Check if required tools exist. Mark skill as degraded if not."""
    if not skill.requires_tools:
        return

    from .tool_registry import TOOL_REGISTRY

    missing = [t for t in skill.requires_tools if t not in TOOL_REGISTRY]
    if missing:
        skill.degraded = True
        skill.degraded_reason = f"Missing required tools: {', '.join(missing)}"
        logger.warning("Skill '%s' is degraded: %s", skill.name, skill.degraded_reason)


def load_skills(skills_dir: Path | None = None) -> dict[str, Skill]:
    """Load all skill packages from the skills directory."""
    global _skills, _load_timestamp, _keyword_index

    directory = skills_dir or _SKILLS_DIR
    if not directory.exists():
        logger.info("Skills directory not found: %s", directory)
        return {}

    loaded: dict[str, Skill] = {}

    for skill_dir in sorted(directory.iterdir()):
        skill_file = None
        if skill_dir.is_dir():
            skill_file = skill_dir / "skill.md"
        elif skill_dir.is_file() and skill_dir.suffix == ".md":
            # Support flat files too (skills/sre.md)
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


def classify_query(query: str) -> Skill:
    """Route a query to the best matching skill based on keyword scoring.

    Applies typo correction before matching. Uses word-boundary matching
    for short keywords (< 4 chars) to avoid false positives like "pod" in "tripod".
    Falls back to 'sre' if no skill matches.
    """
    if not _skills:
        load_skills()

    # Apply typo correction if available
    try:
        from .orchestrator import fix_typos

        q = fix_typos(query).lower()
    except ImportError:
        q = query.lower()

    scores: dict[str, int] = {}

    for kw, skill_name, kw_len in _keyword_index:
        if kw_len < 4:
            # Short keywords: word boundary match to avoid "pod" matching "tripod"
            import re

            if re.search(r"\b" + re.escape(kw) + r"\b", q):
                scores[skill_name] = scores.get(skill_name, 0) + kw_len
        elif kw in q:
            scores[skill_name] = scores.get(skill_name, 0) + kw_len

    if not scores:
        # Default to SRE (highest priority general-purpose skill)
        return _skills.get("sre") or next(iter(_skills.values()))

    # Apply priority weighting
    best_name = max(scores, key=lambda n: (scores[n], _skills[n].priority))
    return _skills[best_name]


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


def build_config_from_skill(skill: Skill) -> dict:
    """Build agent config from a skill — same format as orchestrator.build_orchestrated_config().

    Returns dict with: system_prompt, tool_defs, tool_map, write_tools.
    Tools are selected from the full tool registry based on the skill's categories.
    Multiple skills share the same tools and MCP connections.
    """
    from .harness import ALWAYS_INCLUDE, TOOL_CATEGORIES
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
        tool_map = all_tools
        tool_defs = [t.to_dict() for t in tool_map.values()]
    else:
        # Collect tools from the skill's categories + ALWAYS_INCLUDE
        tool_names = set(ALWAYS_INCLUDE)
        for cat_name in skill.categories:
            cat = TOOL_CATEGORIES.get(cat_name, {})
            tool_names.update(cat.get("tools", []))

        tool_map = {n: t for n, t in all_tools.items() if n in tool_names}
        tool_defs = [t.to_dict() for t in tool_map.values()]

    write_tools = set(WRITE_TOOL_NAMES) if skill.write_tools else set()

    return {
        "system_prompt": skill.system_prompt,
        "tool_defs": tool_defs,
        "tool_map": tool_map,
        "write_tools": write_tools,
    }


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
