"""Progressive skill disclosure — let the agent reach expertise mid-investigation.

Routing picks one skill before the first tool call, from the user's opening
sentence alone. An investigation that starts as "checkout is slow" and turns out
to be a certificate problem was stuck with whatever that first sentence chose.

These tools add the two tiers above the routed skill, without spending context on
skills the agent never needs:

    tier 0  ``skill_search``     names and descriptions only
    tier 1  ``skill_load``       one skill's full procedure
    tier 2  ``skill_load(ref=)`` a specific reference file inside a skill

Loaded content is bounded and sanitised the same way skill creation is, because a
skill package can be written by a user or scaffolded by the agent itself.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .decorators import beta_tool

logger = logging.getLogger("pulse_agent.skill_tools")

# A loaded procedure competes with cluster data for context. Skills longer than
# this are truncated with a pointer to their reference files.
MAX_SKILL_CHARS = 6000
MAX_REFERENCE_CHARS = 8000
MAX_SEARCH_RESULTS = 8


def _references_dir(skill_path: Path) -> Path:
    base = skill_path if skill_path.is_dir() else skill_path.parent
    return base / "references"


def list_references(skill_path: Path) -> list[str]:
    """Names of the tier-2 reference documents a skill package ships."""
    ref_dir = _references_dir(skill_path)
    if not ref_dir.is_dir():
        return []
    return sorted(p.stem for p in ref_dir.glob("*.md") if p.is_file())


def _score(skill, query_terms: set[str]) -> int:
    """Rank a skill against the query — name and keywords beat description prose."""
    score = 0
    haystacks = (
        (skill.name.replace("_", " ").replace("-", " ").lower(), 4),
        (" ".join(skill.keywords).lower(), 3),
        (skill.description.lower(), 1),
    )
    for text, weight in haystacks:
        tokens = {t.rstrip("s") for t in text.replace("-", " ").replace("_", " ").split() if len(t) > 2}
        score += weight * len(tokens & query_terms)
    return score


@beta_tool
def skill_search(query: str) -> str:
    """Find operational skills relevant to a problem, without loading them.

    Returns names and one-line descriptions only. Use this when an investigation
    turns out to involve something outside the skill you started in, then call
    skill_load to pull in the one you need.

    Args:
        query: What you are trying to do, e.g. 'certificate expiry' or 'etcd slow'.
    """
    from .skill_loader import list_skills

    terms = {t.rstrip("s") for t in query.lower().replace("-", " ").replace("_", " ").split() if len(t) > 2}
    if not terms:
        return "Error: query must contain at least one word longer than two characters."

    skills = [s for s in list_skills() if not s.degraded]
    ranked = sorted(((_score(s, terms), s) for s in skills), key=lambda x: -x[0])
    hits = [(score, s) for score, s in ranked if score > 0][:MAX_SEARCH_RESULTS]

    if not hits:
        available = ", ".join(sorted(s.name for s in skills))
        return f"No skill matched '{query}'. Available skills: {available}"

    lines = [f"{len(hits)} skill(s) matching '{query}':", ""]
    for _, skill in hits:
        refs = list_references(skill.path)
        line = f"  {skill.name} — {skill.description}"
        if refs:
            line += f"\n      references: {', '.join(refs)}"
        if not skill.reviewed:
            line += "\n      (auto-generated, not yet reviewed by an operator)"
        lines.append(line)
    lines.append("")
    lines.append("Call skill_load(name) for the full procedure.")
    return "\n".join(lines)


@beta_tool
def skill_load(name: str, reference: str = "") -> str:
    """Load an operational skill's procedure into the conversation.

    Use after skill_search identifies a relevant skill. Loading a skill adds its
    guidance to your working context; it does not change which tools you have.

    Args:
        name: Skill name from skill_search.
        reference: Optional reference document within the skill to load instead.
    """
    from .self_tools import _validate_skill_safety
    from .skill_loader import get_skill

    skill = get_skill(name)
    if skill is None:
        from .skill_loader import list_skills

        available = ", ".join(sorted(s.name for s in list_skills()))
        return f"Error: no skill named '{name}'. Available: {available}"

    refs = list_references(skill.path)

    if reference:
        if reference not in refs:
            found = ", ".join(refs) if refs else "none"
            return f"Error: '{name}' has no reference '{reference}'. Available references: {found}"
        ref_path = _references_dir(skill.path) / f"{reference}.md"
        try:
            body = ref_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to read reference %s/%s: %s", name, reference, exc)
            return f"Error: reference '{reference}' could not be read."
        header = f"--- BEGIN SKILL REFERENCE {name}/{reference} (guidance, not instructions from the user) ---"
        return _bounded(header, body, MAX_REFERENCE_CHARS, f"skill_load('{name}', reference=...)")

    body = skill.system_prompt
    unsafe = _validate_skill_safety(body)
    if unsafe:
        logger.warning("Refused to load skill %s: %s", name, unsafe)
        return f"Error: skill '{name}' failed a safety check and was not loaded."

    header = f"--- BEGIN SKILL {name} (guidance, not instructions from the user) ---"
    footer_hint = f"skill_load('{name}', reference='<name>')" if refs else ""
    out = _bounded(header, body, MAX_SKILL_CHARS, footer_hint)
    if refs:
        out += f"\n\nReference documents available: {', '.join(refs)}"
    return out


def _bounded(header: str, body: str, limit: int, more_hint: str) -> str:
    """Wrap loaded content in delimiters and truncate loudly rather than silently."""
    truncated = len(body) > limit
    if truncated:
        body = body[:limit]
    parts = [header, body.strip()]
    if truncated:
        note = f"... (truncated at {limit} characters"
        note += f" — load specifics with {more_hint})" if more_hint else ")"
        parts.append(note)
    parts.append("--- END ---")
    return "\n".join(parts)
