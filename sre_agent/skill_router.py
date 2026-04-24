"""Skill Router — query classification and routing logic.

Determines which skill should handle a given query using:
- Hard pre-route: deterministic regex patterns
- ORCA: multi-signal scoring (keywords, components, temporal signals)
- LLM fallback: lightweight Claude call for ambiguous queries
- Handoff: keyword-based delegation between skills
"""

from __future__ import annotations

import hashlib
import logging
import re
import time

logger = logging.getLogger("pulse_agent.skill_router")

# Hard pre-route patterns: (compiled_regex, skill_name)
# These override ORCA when the query unambiguously matches a skill.
_HARD_PRE_ROUTE: list[tuple[re.Pattern, str]] = []

# LLM classification cache
_llm_cache: dict[str, tuple[str, float]] = {}  # query_hash → (skill_name, timestamp)
_LLM_CACHE_TTL = 300  # 5 minutes
_LLM_CACHE_MAX = 100

# Last routing decision — per-context to prevent cross-session corruption
import contextvars

_last_routing_decision_var: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "_last_routing_decision", default=None
)


def get_last_routing_decision() -> dict | None:
    """Return the last routing decision, or None if no routing has occurred."""
    d = _last_routing_decision_var.get()
    return dict(d) if d else None


def _init_hard_pre_route() -> None:
    """Build hard pre-route rules from skill trigger_patterns."""
    global _HARD_PRE_ROUTE
    if _HARD_PRE_ROUTE:
        return

    from .skill_loader import list_skills

    skills = {s.name: s for s in list_skills()}

    # High-priority deterministic rules checked FIRST (order matters).
    # MCP/Helm queries must match before view_designer (which matches "chart")
    _HARD_PRE_ROUTE.extend(
        [
            (
                re.compile(
                    r"helm\s+(release|chart|list|install|upgrade|rollback|uninstall|history|values)", re.IGNORECASE
                ),
                "sre",
            ),
            (re.compile(r"(tekton|pipeline|pipelinerun|taskrun)\b", re.IGNORECASE), "sre"),
            (re.compile(r"(service\s+mesh|istio|kiali|ossm)\b", re.IGNORECASE), "sre"),
            (re.compile(r"(kubevirt|virtual\s+machine)\b", re.IGNORECASE), "sre"),
            (
                re.compile(
                    r"(create|build|make|design)\s+(me\s+)?(a\s+)?(\w+\s+)?(dashboard|view|live\s+table)", re.IGNORECASE
                ),
                "view_designer",
            ),
            (
                re.compile(
                    r"(edit|update|modify|fix|optimize)\s+(the\s+)?(dashboard|view|layout|widget)", re.IGNORECASE
                ),
                "view_designer",
            ),
            (re.compile(r"add\s+(a\s+)?(chart|table|widget|metric|column)", re.IGNORECASE), "view_designer"),
            (
                re.compile(r"(remove|hide|show|rename|reorder)\s+.{0,30}(column|widget|chart)", re.IGNORECASE),
                "view_designer",
            ),
            (re.compile(r"(sort|filter)\s+(by|the)\s+", re.IGNORECASE), "view_designer"),
            (re.compile(r"custom_view|/custom/", re.IGNORECASE), "view_designer"),
            (
                re.compile(r"(postmortem|post.mortem|incident\s+review|root\s+cause\s+report)\b", re.IGNORECASE),
                "postmortem",
            ),
            (re.compile(r"(slo\b|service\s+level|error\s+budget|burn\s+rate)", re.IGNORECASE), "slo_management"),
            (re.compile(r"(capacity\s+plan|forecast|projection|right.?siz)", re.IGNORECASE), "capacity_planner"),
            (
                re.compile(
                    r"(since\s+(the\s+)?upgrade|after\s+(the\s+)?upgrade|post.?upgrade|unstable)", re.IGNORECASE
                ),
                "sre",
            ),
        ]
    )
    # Then skill-defined trigger_patterns (lower priority)
    for skill in skills.values():
        for pattern in skill.trigger_patterns:
            try:
                _HARD_PRE_ROUTE.append((re.compile(pattern, re.IGNORECASE), skill.name))
            except re.error:
                logger.debug("Invalid trigger_pattern regex for skill %s: %s", skill.name, pattern, exc_info=True)


def _hard_pre_route(query: str):
    """Check deterministic pre-route rules before ORCA.

    Returns: Skill object or None
    """
    if not _HARD_PRE_ROUTE:
        _init_hard_pre_route()

    from .skill_loader import get_skill

    for pattern, skill_name in _HARD_PRE_ROUTE:
        if pattern.search(query):
            skill = get_skill(skill_name)
            if skill:
                logger.info("Hard pre-route: '%s' → %s (pattern: %s)", query[:60], skill_name, pattern.pattern)
                return skill
    return None


def classify_query(query: str, *, context: dict | None = None):
    """Route a query to the best matching skill.

    ORCA multi-signal routing: keyword + component tags + historical channels
    with weighted score fusion and dynamic thresholds.

    Returns: Skill object
    """
    from .skill_loader import _get_selector, list_skills

    skills = {s.name: s for s in list_skills()}
    if not skills:
        raise ValueError("No skills loaded")

    # Hard pre-route: deterministic regex rules for unambiguous queries.
    # Runs on the ORIGINAL query before typo correction, because the typo
    # corrector can mangle non-K8s terms (e.g. "column" → "volume").
    pre_route = _hard_pre_route(query)
    if pre_route:
        from .skill_selector import SelectionResult, _last_selection_result_var

        _last_selection_result_var.set(
            SelectionResult(
                skill_name=pre_route.name,
                fused_scores={pre_route.name: 1.0},
                channel_scores={},
                threshold_used=0.0,
                source="pre_route",
            )
        )
        return pre_route

    # Apply typo correction (for ORCA, not for pre-route)
    try:
        from .orchestrator import fix_typos

        q = fix_typos(query)
    except ImportError:
        q = query

    selector = _get_selector()
    result = selector.select(q, context=context)

    best_skill = skills.get(result.skill_name)

    # If ORCA didn't find a high-confidence match, try LLM fallback
    if result.source == "fallback" and not best_skill:
        llm_result = _llm_classify(query)
        if llm_result:
            best_skill = llm_result
            result.skill_name = llm_result.name
            result.source = "llm_fallback"

    if not best_skill:
        best_skill = skills.get("sre") or next(iter(skills.values()))
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

    # Update _last_routing_decision for backward compat (per-context)
    _last_routing_decision_var.set(
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


def _run_orca_for_secondary(query: str, primary, *, context: dict | None = None):
    """Run ORCA on the full query to detect a secondary skill via score gap.

    classify_query may have been short-circuited by hard pre-route,
    so we run the selector directly to get fused scores.

    Returns: Skill object or None
    """
    from .orchestrator import split_compound_intent
    from .skill_loader import _get_selector, get_skill
    from .skill_selector import get_last_selection_result

    result = get_last_selection_result()
    if result and result.secondary_skill:
        sec = get_skill(result.secondary_skill)
        if sec:
            return sec

    # ORCA didn't run (hard pre-route short-circuited) — run it now
    if not result or result.source == "pre_route":
        try:
            from .orchestrator import fix_typos

            q = fix_typos(query)
        except ImportError:
            q = query
        selector = _get_selector()
        result = selector.select(q, context=context)
        if result.secondary_skill:
            sec = get_skill(result.secondary_skill)
            if sec:
                return sec

    # Fallback: intent splitting for explicit compound queries
    parts = split_compound_intent(query)
    if len(parts) >= 2:
        for part in parts:
            sub_skill = classify_query(part, context=context)
            if sub_skill.name != primary.name and not _skills_conflict(primary, sub_skill):
                return sub_skill

    return None


def classify_query_multi(query: str, *, context: dict | None = None) -> tuple:
    """Route a query, returning primary + optional secondary skill.

    Always runs ORCA scoring (even when hard pre-route picks the primary)
    so the score gap can detect a secondary skill. Intent splitting is a
    fallback for explicit compound queries that ORCA doesn't catch.

    Returns: (primary_skill, secondary_skill_or_none)
    """
    from .config import get_settings

    settings = get_settings()
    primary = classify_query(query, context=context)

    if not settings.multi_skill:
        return primary, None

    if primary.exclusive:
        return primary, None

    secondary = _run_orca_for_secondary(query, primary, context=context)
    if secondary and _skills_conflict(primary, secondary):
        return primary, None
    return primary, secondary


def _skills_conflict(a, b) -> bool:
    """Check if two skills conflict bidirectionally."""
    if a.name in (b.conflicts_with or []):
        return True
    return b.name in (a.conflicts_with or [])


def _llm_classify(query: str):
    """Use a lightweight LLM call to classify ambiguous queries.

    Caches results (FIFO, 100 entries, 5min TTL) to avoid repeat API calls.
    Returns None on any error (caller falls back to keyword/default).

    Returns: Skill object or None
    """
    from .skill_loader import list_skills

    skills = {s.name: s for s in list_skills()}

    query_hash = hashlib.md5(query.lower().strip().encode()).hexdigest()[:16]

    # Check cache
    cached = _llm_cache.get(query_hash)
    if cached:
        name, ts = cached
        if time.time() - ts < _LLM_CACHE_TTL:
            skill = skills.get(name)
            if skill:
                logger.debug("LLM classify cache hit: '%s' → %s", query[:50], name)
                return skill

    try:
        from .agent import borrow_client

        with borrow_client() as client:
            skill_options = "\n".join(f"- {s.name}: {s.description}" for s in skills.values())
            response = client.messages.create(
                model="claude-sonnet-4-6",
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
        skill = skills.get(name)
        if skill:
            # Cache the result
            _llm_cache[query_hash] = (name, time.time())
            # Evict expired entries first, then oldest if still over cap
            now = time.time()
            expired = [k for k, (_, ts) in _llm_cache.items() if now - ts >= _LLM_CACHE_TTL]
            for k in expired:
                del _llm_cache[k]
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


def check_handoff(current_skill, query: str):
    """Check if the query should trigger a handoff to another skill.

    Returns the target skill if a handoff keyword matches, else None.

    Args:
        current_skill: Skill object
        query: User query string

    Returns: Skill object or None
    """
    from .skill_loader import get_skill

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
