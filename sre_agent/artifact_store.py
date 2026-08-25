"""Durable storage for everything the agent writes at runtime.

Four kinds of document were written only to the container's overlay filesystem
and erased by every restart, deploy and reschedule:

- **skills** — ``create_skill``/``edit_skill`` to ``PULSE_AGENT_USER_SKILLS_DIR``
  (default ``/tmp/pulse_agent/skills``), the scaffolder and the learning
  flywheel into the installed package directory;
- **plans** — the ``PUT /plan-templates/{type}`` handler rewrites YAML inside
  the installed package;
- **scaffolded evals** — scenarios and replay fixtures written next to the
  bundled ones;
- **version history** — the ``.versions/`` directory beside a skill, which was
  as ephemeral as the skill it was supposed to make recoverable.

They are the same shape (a named text document with a version), so they share
one table rather than four near-identical ones.

The filesystem stays the read path: skill_loader and plan_templates scan
directories and are untouched. This module is the durable copy beside them —
written through on every change, replayed onto disk at startup.

Every function fails soft. A database that is unreachable must not stop a skill
being written and used for the life of the pod: losing durability is bad,
refusing to work at all is worse.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("pulse_agent.artifact_store")

KIND_SKILL = "skill"
KIND_PLAN = "plan"
KIND_EVAL_SCENARIO = "eval_scenario"
KIND_EVAL_FIXTURE = "eval_fixture"


def _db():
    """Return a Database, or None when persistence is unavailable."""
    try:
        from .db import get_database

        return get_database()
    except Exception:
        logger.debug("Artifact store unavailable — runtime writes will not survive a restart", exc_info=True)
        return None


def persist(
    kind: str,
    name: str,
    content: str,
    *,
    rel_path: str,
    source: str = "user",
    created_by: str = "",
) -> bool:
    """Upsert one artifact and append the previous body to its history.

    ``version`` increments on every write rather than being supplied by the
    caller, so the count reflects how many times the document actually changed
    regardless of what any frontmatter claims.
    """
    db = _db()
    if db is None:
        return False
    try:
        # Snapshot what is there now, so an edit stays reversible. Recorded
        # before the upsert because afterwards the old body is gone.
        prior = db.fetchone(
            "SELECT version, content, created_by FROM runtime_artifacts WHERE kind = ? AND name = ?",
            (kind, name),
        )
        if prior:
            db.execute(
                "INSERT INTO runtime_artifact_versions (kind, name, version, content, created_by) "
                "VALUES (?, ?, ?, ?, ?)",
                (kind, name, prior["version"], prior["content"], prior["created_by"]),
            )
        db.execute(
            "INSERT INTO runtime_artifacts (kind, name, rel_path, content, source, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (kind, name) DO UPDATE SET "
            "  content = EXCLUDED.content, "
            "  rel_path = EXCLUDED.rel_path, "
            "  source = EXCLUDED.source, "
            "  version = runtime_artifacts.version + 1, "
            "  updated_at = NOW()",
            (kind, name, rel_path, content, source, created_by),
        )
        return True
    except Exception:
        logger.warning("Failed to persist %s '%s' — it will not survive a restart", kind, name, exc_info=True)
        return False


def forget(kind: str, name: str) -> bool:
    """Retire an artifact, keeping its history so it stays recoverable.

    Only the current row is removed. A retired skill should not reappear on the
    next boot, but it should still be possible to see what it was.
    """
    db = _db()
    if db is None:
        return False
    try:
        db.execute("DELETE FROM runtime_artifacts WHERE kind = ? AND name = ?", (kind, name))
        return True
    except Exception:
        logger.warning("Failed to retire %s '%s'", kind, name, exc_info=True)
        return False


def list_artifacts(kind: str) -> list[dict]:
    """Current state of every artifact of one kind, newest change first."""
    db = _db()
    if db is None:
        return []
    try:
        return db.fetchall(
            "SELECT kind, name, rel_path, content, source, version, created_by, updated_at "
            "FROM runtime_artifacts WHERE kind = ? ORDER BY updated_at DESC",
            (kind,),
        )
    except Exception:
        logger.warning("Failed to read persisted %s artifacts", kind, exc_info=True)
        return []


def list_versions(kind: str, name: str) -> list[dict]:
    """Prior revisions of one artifact, newest first."""
    db = _db()
    if db is None:
        return []
    try:
        return db.fetchall(
            "SELECT version, content, created_by, created_at "
            "FROM runtime_artifact_versions WHERE kind = ? AND name = ? "
            "ORDER BY version DESC",
            (kind, name),
        )
    except Exception:
        logger.warning("Failed to read history for %s '%s'", kind, name, exc_info=True)
        return []


def get_version(kind: str, name: str, version: int) -> dict | None:
    """One prior revision, for previewing or restoring it."""
    db = _db()
    if db is None:
        return None
    try:
        return db.fetchone(
            "SELECT version, content, created_by, created_at "
            "FROM runtime_artifact_versions WHERE kind = ? AND name = ? AND version = ?",
            (kind, name, version),
        )
    except Exception:
        logger.warning("Failed to read version %s of %s '%s'", version, kind, name, exc_info=True)
        return None


def hydrate(kind: str, root: Path) -> int:
    """Replay persisted artifacts of one kind under ``root``. Returns how many were written.

    Runs at startup, before the directory is scanned. Existing files are left
    alone: a document shipped in the image is the image's to define, and a file
    already present is either that or a write from this same pod — either way
    the disk copy is at least as current as the row.
    """
    rows = list_artifacts(kind)
    if not rows:
        return 0

    root = Path(root)
    written = 0
    for row in rows:
        try:
            rel = str(row["rel_path"] or "").strip()
            if not rel:
                continue
            target = (root / rel).resolve()
            # A rel_path from the database must not escape its root.
            if not str(target).startswith(str(root.resolve())):
                logger.warning("Refusing to restore %s '%s' outside its root", kind, row.get("name"))
                continue
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(row["content"], encoding="utf-8")
            written += 1
        except Exception:
            logger.warning("Failed to restore %s '%s'", kind, row.get("name"), exc_info=True)

    if written:
        logger.info("Restored %d persisted %s artifact(s) into %s", written, kind, root)
    return written
