"""Durable storage for skills created or refined at runtime.

Skill definitions were only ever written to the filesystem — create_skill and
edit_skill to ``PULSE_AGENT_USER_SKILLS_DIR`` (default ``/tmp/pulse_agent/skills``),
the scaffolder into the installed package directory. Both are the container's
overlay filesystem, so every restart, deploy and reschedule erased them.

That silently broke half the learning flywheel: ``learning_candidates``
(migration 033) made the *input* to learning durable, while the skill it
produces was still thrown away. A skill could be scaffolded from a verified
trajectory, refined to v2 and v3 by later cases, and vanish on the next rollout
with nothing recording it had existed.

The filesystem remains the read path — ``skill_loader`` scans directories and is
untouched. This module is the durable copy beside it:

- :func:`persist_skill` writes through on every create, edit and refinement;
- :func:`hydrate_skills_dir` replays the table into the skills directory at
  startup, before skills are loaded.

Every function fails soft. A database that is unreachable must not stop a skill
being written to disk and used for the life of the pod — losing durability is
bad, refusing to work at all is worse.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("pulse_agent.skill_store")


def _db():
    """Return a Database, or None when persistence is unavailable."""
    try:
        from .db import get_database

        return get_database()
    except Exception:
        logger.debug("Skill store unavailable — skills will not survive a restart", exc_info=True)
        return None


def persist_skill(name: str, content: str, *, source: str = "user", created_by: str = "") -> bool:
    """Upsert one skill's markdown. Returns True when it was stored.

    ``version`` increments on every write rather than being supplied by the
    caller, so a refinement that bumps the frontmatter version and a plain edit
    both leave an accurate count of how many times this skill has changed.
    """
    db = _db()
    if db is None:
        return False
    dir_name = name.replace("_", "-")
    try:
        db.execute(
            "INSERT INTO user_skills (name, dir_name, content, source, created_by) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (name) DO UPDATE SET "
            "  content = EXCLUDED.content, "
            "  dir_name = EXCLUDED.dir_name, "
            "  source = EXCLUDED.source, "
            "  version = user_skills.version + 1, "
            "  updated_at = NOW()",
            (name, dir_name, content, source, created_by),
        )
        return True
    except Exception:
        logger.warning("Failed to persist skill '%s' — it will not survive a restart", name, exc_info=True)
        return False


def forget_skill(name: str) -> bool:
    """Drop a skill from durable storage, so a delete is not undone on boot."""
    db = _db()
    if db is None:
        return False
    try:
        db.execute("DELETE FROM user_skills WHERE name = ?", (name,))
        return True
    except Exception:
        logger.warning("Failed to remove skill '%s' from the store", name, exc_info=True)
        return False


def list_stored_skills() -> list[dict]:
    """Every persisted skill, newest change first."""
    db = _db()
    if db is None:
        return []
    try:
        return db.fetchall(
            "SELECT name, dir_name, content, source, version, created_by, updated_at "
            "FROM user_skills ORDER BY updated_at DESC"
        )
    except Exception:
        logger.warning("Failed to read persisted skills", exc_info=True)
        return []


def hydrate_skills_dir(skills_dir: Path | None = None) -> int:
    """Replay persisted skills onto disk. Returns how many were written.

    Runs at startup, before skills are loaded. Existing files are left alone:
    a skill shipped in the image is the image's to define, and a file already
    present is either that or a write from this same pod — either way the disk
    copy is at least as current as the row.
    """
    if skills_dir is None:
        from .skill_loader import _get_user_skills_dir

        skills_dir = _get_user_skills_dir()

    rows = list_stored_skills()
    if not rows:
        return 0

    written = 0
    for row in rows:
        try:
            target = Path(skills_dir) / row["dir_name"] / "skill.md"
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(row["content"], encoding="utf-8")
            written += 1
        except Exception:
            logger.warning("Failed to restore skill '%s'", row.get("name"), exc_info=True)

    if written:
        logger.info("Restored %d persisted skill(s) into %s", written, skills_dir)
    return written
