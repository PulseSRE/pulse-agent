"""A verified trajectory deepens the skill that exists instead of cloning it.

Scaffolding named each new skill after the incident's title words, so two
verified fixes for the same kind of failure produced two sibling skills — the
second one born at version 1, knowing only its own case, and competing with the
first for routing. Nothing ever reached version 2; nothing accumulated.

This module keys learning on the finding category instead. The first verified
trajectory for a category scaffolds a skill stamped with ``incident_type``; every
later one refines that skill in place — a distilled case appended to the body,
keywords and required tools merged, version bumped.

Refinement re-opens the review gate. A person who approved version 3 has not
read version 4, so the skill drops out of automatic routing until someone
re-approves it — and an inbox item says so, because silently unrouting a
working skill would otherwise look like a regression nobody can explain.
"""

from __future__ import annotations

import logging
import re
import time

logger = logging.getLogger("pulse_agent.skill_lifecycle")

# A skill body that grows a case per verified fix would eventually be all
# case history and no procedure. Keep the most recent handful; older cases
# have already been distilled into keywords and required tools.
MAX_VERIFIED_CASES = 5

_CASE_MARKER = "### Verified case"

# Keyword list cap — beyond this the TF-IDF channel gets noise, not signal.
MAX_KEYWORDS = 15
MAX_REQUIRED_TOOLS = 8


def learn_from_verified(candidate) -> str | None:
    """Turn a verified trajectory into skill knowledge — new or deepened.

    Takes a promoted :class:`~sre_agent.trajectory.LearningCandidate`. Returns
    the path of the skill that was created or refined, or None on failure.
    """
    existing = _find_skill_for_category(candidate.category)
    if existing is not None:
        return refine_skill(existing, candidate)
    return _scaffold_new(candidate)


def _find_skill_for_category(category: str):
    """The auto-scaffolded skill previously learned for this finding category."""
    if not category:
        return None
    try:
        from .skill_loader import list_skills

        for skill in list_skills():
            if skill.generated_by == "auto" and skill.incident_type == category:
                return skill
    except Exception:
        logger.debug("Skill lookup for category %s failed", category, exc_info=True)
    return None


def refine_skill(skill, candidate) -> str | None:
    """Fold a newly verified case into an existing scaffolded skill.

    Bumps the version, merges keywords and required tools, appends a distilled
    case section (capped at MAX_VERIFIED_CASES), and re-opens the review gate
    if a person had approved the previous version.
    """
    import yaml

    skill_file = skill.path / "skill.md"
    if not skill_file.exists():
        logger.warning("Cannot refine '%s': skill.md missing on disk", skill.name)
        return None

    try:
        raw = skill_file.read_text(encoding="utf-8")
        parts = raw.split("---")
        if len(parts) < 3:
            logger.warning("Cannot refine '%s': no frontmatter", skill.name)
            return None
        meta = yaml.safe_load(parts[1])
        if not isinstance(meta, dict):
            logger.warning("Cannot refine '%s': frontmatter is not a mapping", skill.name)
            return None
        body = "---".join(parts[2:])
    except Exception:
        logger.warning("Cannot refine '%s': unreadable skill.md", skill.name, exc_info=True)
        return None

    was_reviewed = bool(meta.get("reviewed", False))
    old_version = int(meta.get("version") or 1)
    meta["version"] = old_version + 1
    # The body a person approved is not the body that exists after this write.
    meta["reviewed"] = False

    meta["keywords"] = _merge_keywords(meta.get("keywords") or [], candidate.title)
    meta["requires_tools"] = _merge_tools(meta.get("requires_tools") or [], candidate.tools_called)

    body = _append_case(body, candidate)

    try:
        skill_file.write_text(
            f"---\n{yaml.dump(meta, default_flow_style=False, sort_keys=False)}---{body}",
            encoding="utf-8",
        )
    except Exception:
        logger.warning("Failed to write refined skill '%s'", skill.name, exc_info=True)
        return None

    try:
        from .skill_loader import reload_skills

        reload_skills()
    except Exception:
        logger.debug("Skill reload after refining '%s' failed", skill.name, exc_info=True)

    logger.info(
        "Refined skill '%s' v%d -> v%d with verified case (category=%s)",
        skill.name,
        old_version,
        old_version + 1,
        candidate.category,
    )

    if was_reviewed:
        _request_rereview(skill.name, old_version + 1, candidate)

    return str(skill_file)


def _merge_keywords(existing: list, title: str) -> list:
    from .tool_predictor import extract_tokens

    merged = [str(k) for k in existing]
    for token in extract_tokens(title)[:5]:
        if token not in merged:
            merged.append(token)
    return merged[:MAX_KEYWORDS]


def _merge_tools(existing: list, tools_called: list[str]) -> list:
    merged = [str(t) for t in existing]
    for tool in tools_called:
        if tool not in merged:
            merged.append(tool)
    return merged[:MAX_REQUIRED_TOOLS]


def _append_case(body: str, candidate) -> str:
    """Append a distilled case, keeping only the most recent MAX_VERIFIED_CASES."""
    date = time.strftime("%Y-%m-%d")
    tools = " → ".join(f"`{t}`" for t in candidate.tools_called[:10]) or "(none recorded)"
    case = (
        f"\n\n{_CASE_MARKER} — {date}\n"
        f"- Incident: {candidate.title}\n"
        f"- Root cause: {candidate.root_cause}\n"
        f"- Tools: {tools}\n"
        f"- Confidence at diagnosis: {candidate.confidence:.0%} (fix verified on a later scan)\n"
    )

    marker_re = re.compile(rf"\n{re.escape(_CASE_MARKER)}")
    pieces = marker_re.split(body)
    base, cases = pieces[0], pieces[1:]
    kept = cases[-(MAX_VERIFIED_CASES - 1) :] if MAX_VERIFIED_CASES > 1 else []
    rebuilt = base.rstrip("\n")
    for c in kept:
        rebuilt += f"\n{_CASE_MARKER}{c.rstrip()}" + "\n"
    return rebuilt + case


def _request_rereview(skill_name: str, version: int, candidate) -> None:
    """Surface the re-opened review gate instead of silently unrouting the skill."""
    try:
        from .inbox import upsert_inbox_item

        upsert_inbox_item(
            {
                "item_type": "task",
                "title": f"Skill '{skill_name}' learned a new case — re-review to restore routing",
                "summary": (
                    f"A verified fix for '{candidate.title}' was folded into skill "
                    f"'{skill_name}' (now v{version}). Because its content changed, the "
                    "skill is out of automatic routing until a person re-approves it: "
                    f"POST /admin/skills/{skill_name}/approve, or the Toolbox UI."
                ),
                "severity": "low",
                "confidence": float(candidate.confidence),
                "noise_score": 0,
                "namespace": None,
                "resources": [],
                "correlation_key": f"skill-rereview:{skill_name}",
                "created_by": "system:skill-lifecycle",
                # No "generator" key: the generator cycle auto-resolves any item
                # carrying one that a generator did not re-emit this cycle, and
                # this item is created here, not by a generator — it must stay
                # until a person acts on it.
                "metadata": {"source": "skill_lifecycle", "skill": skill_name, "version": version},
            }
        )
    except Exception:
        logger.debug("Failed to create re-review inbox item for '%s'", skill_name, exc_info=True)


def note_recurrence(category: str, detail: str) -> None:
    """The lesson learned for this category just failed in the field — say so.

    A skill scaffolded or refined from a verified fix inherits that verdict's
    time horizon: when the same condition recurs after verification, the case
    the skill was deepened on is dubious. The skill is not silently quarantined
    — a recurrence is one data point, and pulling a working skill over it is
    the operator's call — but the operator cannot make that call unseen, so an
    inbox item lays out the evidence and the quarantine endpoint.
    """
    skill = _find_skill_for_category(category)
    if skill is None:
        return
    try:
        from .inbox import upsert_inbox_item

        upsert_inbox_item(
            {
                "item_type": "task",
                "title": f"Skill '{skill.name}' learned from a fix that did not hold — review it",
                "summary": (
                    f"{detail} Skill '{skill.name}' was created or refined from that "
                    "verified fix, so its most recent case may teach the wrong lesson. "
                    f"Review the skill, and quarantine it if the lesson is wrong: "
                    f"POST /admin/skills/{skill.name}/quarantine, or the Toolbox UI."
                ),
                "severity": "low",
                "confidence": 0.7,
                "noise_score": 0,
                "namespace": None,
                "resources": [],
                "correlation_key": f"skill-recurrence:{skill.name}",
                "created_by": "system:skill-lifecycle",
                # No "generator" key — same reasoning as _request_rereview: the
                # generator cycle must not auto-resolve an item it did not emit.
                "metadata": {"source": "skill_lifecycle", "skill": skill.name, "category": category},
            }
        )
    except Exception:
        logger.debug("Failed to create recurrence inbox item for '%s'", skill.name, exc_info=True)


def _scaffold_new(candidate) -> str | None:
    """First verified trajectory for this category: create the skill.

    This is the creation path that used to live in the verification pipeline,
    now stamping ``incident_type`` so the next verified case refines instead of
    cloning.
    """
    try:
        from .eval_scaffolder import scaffold_eval_from_investigation
        from .skill_scaffolder import (
            save_scaffolded_skill,
            scaffold_plan_template,
            scaffold_skill_from_resolution,
        )

        skill_content = scaffold_skill_from_resolution(
            query=candidate.title,
            tools_called=candidate.tools_called,
            investigation_summary=candidate.summary,
            root_cause=candidate.root_cause,
            confidence=candidate.confidence,
            incident_type=candidate.category or "",
        )
        tokens = (candidate.title or "unknown").lower().split()[:3]
        skill_name = "-".join(t for t in tokens if t.isalnum())[:40] or "auto-skill"
        path = save_scaffolded_skill(skill_content, skill_name)
        scaffold_plan_template(
            skill_name=skill_name,
            plan_phases=["triage", "diagnose", "remediate", "verify"],
            incident_type=candidate.category or "unknown",
            confidence=candidate.confidence,
        )
        logger.info("Scaffolded skill '%s' from a VERIFIED trajectory", skill_name)

        try:
            scaffold_eval_from_investigation(
                skill_name=skill_name,
                finding={"category": candidate.category, "title": candidate.title},
                investigation_result={
                    "summary": candidate.summary,
                    "suspected_cause": candidate.root_cause,
                    "confidence": candidate.confidence,
                },
            )
        except Exception:
            logger.debug("Eval scaffolding from verified trajectory failed", exc_info=True)

        return path
    except Exception:
        logger.debug("Scaffolding from verified trajectory failed", exc_info=True)
        return None
