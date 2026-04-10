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
    """Hot reload all skills."""
    return load_skills()


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

    Returns the highest-scoring skill (by keyword match length sum).
    Falls back to 'sre' if no skill matches.
    """
    if not _skills:
        load_skills()

    q = query.lower()
    scores: dict[str, int] = {}

    for kw, skill_name, kw_len in _keyword_index:
        if kw in q:
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
