"""Skill-shaped helpers over :mod:`artifact_store`.

Kept as a module so the call sites in self_tools, skill_lifecycle and
skill_scaffolder read in skill terms rather than repeating the artifact kind
and on-disk layout at every one of them.
"""

from __future__ import annotations

from pathlib import Path

from .artifact_store import KIND_SKILL, forget, hydrate, list_artifacts, list_versions, persist

__all__ = ["forget_skill", "hydrate_skills_dir", "list_stored_skills", "persist_skill", "skill_versions"]


def persist_skill(name: str, content: str, *, source: str = "user", created_by: str = "") -> bool:
    """Write a skill through to durable storage, archiving the prior body."""
    dir_name = name.replace("_", "-")
    return persist(
        KIND_SKILL,
        name,
        content,
        rel_path=f"{dir_name}/skill.md",
        source=source,
        created_by=created_by,
    )


def forget_skill(name: str) -> bool:
    """Retire a skill so it is not restored on boot. History is kept."""
    return forget(KIND_SKILL, name)


def list_stored_skills() -> list[dict]:
    return list_artifacts(KIND_SKILL)


def skill_versions(name: str) -> list[dict]:
    """Prior revisions — what the ephemeral .versions/ directory used to hold."""
    return list_versions(KIND_SKILL, name)


def hydrate_skills_dir(skills_dir: Path | None = None) -> int:
    if skills_dir is None:
        from .skill_loader import _get_user_skills_dir

        skills_dir = _get_user_skills_dir()
    return hydrate(KIND_SKILL, Path(skills_dir))
