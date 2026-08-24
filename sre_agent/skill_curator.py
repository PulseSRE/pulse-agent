"""Skill portfolio curation — the forgetting half of learning.

A self-improving system that only accumulates degrades into a self-cluttering
one: scaffolded skills that never route still cost selector attention, prompt
tokens, and reviewer trust. This is Hermes's curator idea run through Pulse's
gates — the curator only ever *proposes* (inbox items a person acts on) and
the archive action it points at is recoverable by design.

Invariants, in order of importance:
  - Only agent-created skills (``generated_by: auto``) are ever curated.
    Built-in and human-authored skills are out of scope entirely.
  - Nothing is deleted. Archiving moves the skill directory to
    ``.archive/`` next to where it lived; the loader skips dot-dirs, so an
    archived skill stops loading but ``restore_skill`` brings it back intact.
  - Pinned skills (``pinned: true`` frontmatter) are exempt from every
    proposal — pinning is the human "leave this alone".
  - The curator proposes, a person disposes: archiving happens only through
    the admin endpoint, never automatically.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("pulse_agent.curator")

# An agent-created skill that hasn't routed in this long — and is old enough
# to have had the chance — becomes an archive proposal.
STALE_AFTER_DAYS = 30

# Keyword overlap (Jaccard) above which two agent-created skills look like the
# same lesson learned twice, worth consolidating into one.
CONSOLIDATION_OVERLAP = 0.5

ARCHIVE_DIR_NAME = ".archive"


def _agent_created_skills() -> list:
    from .skill_loader import list_skills

    return [s for s in list_skills() if s.generated_by == "auto"]


def _recent_invocations(days: int) -> dict[str, int]:
    """skill_name -> invocation count over the window; {} when no database."""
    try:
        from .repositories import get_skill_analytics_repo

        rows = get_skill_analytics_repo().fetch_skill_stats(days)
        return {str(r["skill_name"]): int(r["invocations"]) for r in rows}
    except Exception:
        logger.debug("Skill usage unavailable; curator skips staleness proposals", exc_info=True)
        return {}


def _skill_age_days(skill) -> float:
    try:
        return (time.time() - (skill.path / "skill.md").stat().st_mtime) / 86400
    except OSError:
        return 0.0


def find_stale_skills(days: int = STALE_AFTER_DAYS) -> list[dict[str, Any]]:
    """Agent-created skills with zero routes in the window, old enough to matter.

    Gated on affirmative usage data: when analytics are unreachable this
    returns nothing, because "no recorded invocations" must never be
    manufactured from a failed query.
    """
    usage = _recent_invocations(days)
    if not usage:
        return []

    stale: list[dict[str, Any]] = []
    for skill in _agent_created_skills():
        if skill.pinned:
            continue
        if usage.get(skill.name, 0) > 0:
            continue
        age = _skill_age_days(skill)
        if age < days:
            continue  # too young to have earned traffic
        stale.append(
            {
                "name": skill.name,
                "age_days": round(age),
                "reviewed": skill.reviewed,
                "quarantined": skill.quarantined,
                "incident_type": skill.incident_type,
            }
        )
    return stale


def find_overlapping_skills() -> list[dict[str, Any]]:
    """Pairs of agent-created skills whose keywords say they're the same lesson."""
    skills = [s for s in _agent_created_skills() if not s.pinned]
    pairs: list[dict[str, Any]] = []
    for i, a in enumerate(skills):
        set_a = {k.lower() for k in a.keywords}
        if not set_a:
            continue
        for b in skills[i + 1 :]:
            set_b = {k.lower() for k in b.keywords}
            if not set_b:
                continue
            union = set_a | set_b
            overlap = len(set_a & set_b) / len(union)
            if overlap >= CONSOLIDATION_OVERLAP:
                pairs.append(
                    {
                        "names": [a.name, b.name],
                        "overlap": round(overlap, 2),
                        "shared_keywords": sorted(set_a & set_b)[:8],
                    }
                )
    return pairs


def archive_skill(name: str) -> Path:
    """Move an agent-created skill's directory into .archive/ and reload.

    Raises ValueError for anything that isn't an archivable agent-created
    skill directory — the endpoint surfaces that as a 4xx.
    """
    from .skill_loader import get_skill, reload_skills

    skill = get_skill(name)
    if not skill:
        raise ValueError(f"Skill '{name}' not found")
    if skill.generated_by != "auto":
        raise ValueError(f"Skill '{name}' is not agent-created; only agent-created skills are curated")
    if skill.pinned:
        raise ValueError(f"Skill '{name}' is pinned; unpin it first if you really mean to archive it")

    skill_dir = skill.path
    if not (skill_dir / "skill.md").exists():
        raise ValueError(f"Skill '{name}' has no skill.md directory to archive")

    archive_root = skill_dir.parent / ARCHIVE_DIR_NAME
    archive_root.mkdir(parents=True, exist_ok=True)
    dest = archive_root / skill_dir.name
    if dest.exists():
        # A previous archive of the same name: keep both, never overwrite
        dest = archive_root / f"{skill_dir.name}-{int(time.time())}"
    shutil.move(str(skill_dir), str(dest))
    reload_skills()
    logger.warning("Skill '%s' archived to %s", name, dest)
    return dest


def restore_skill(name: str) -> Path:
    """Move an archived skill back into rotation and reload."""
    from .skill_loader import reload_skills

    for archive_root in _archive_roots():
        src = archive_root / name
        if src.exists() and (src / "skill.md").exists():
            dest = archive_root.parent / name
            if dest.exists():
                raise ValueError(f"A live skill named '{name}' already exists; cannot restore over it")
            shutil.move(str(src), str(dest))
            reload_skills()
            logger.warning("Skill '%s' restored from archive", name)
            return dest
    raise ValueError(f"No archived skill named '{name}'")


def list_archived() -> list[dict[str, Any]]:
    """Archived skills across both skill roots — name and when it was archived."""
    out: list[dict[str, Any]] = []
    for archive_root in _archive_roots():
        if not archive_root.exists():
            continue
        for entry in sorted(archive_root.iterdir()):
            if entry.is_dir() and (entry / "skill.md").exists():
                out.append(
                    {
                        "name": entry.name,
                        "archived_at": int(entry.stat().st_mtime * 1000),
                    }
                )
    return out


def _archive_roots() -> list[Path]:
    from .skill_loader import _SKILLS_DIR, _get_user_skills_dir

    return [_get_user_skills_dir() / ARCHIVE_DIR_NAME, _SKILLS_DIR / ARCHIVE_DIR_NAME]
