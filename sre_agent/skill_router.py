"""Skill Router — query classification and routing logic.

Determines which skill should handle a given query using:
- Hard pre-route: deterministic regex patterns
- ORCA: multi-signal scoring (keywords, components, temporal signals)
- LLM fallback: lightweight Claude call for ambiguous queries
- Handoff: keyword-based delegation between skills
"""

from __future__ import annotations

import contextlib
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


_ROUTING_OFFLINE = False


def routing_is_offline() -> bool:
    """Whether routing has been asked to make no network calls."""
    return _ROUTING_OFFLINE


@contextlib.contextmanager
def offline_routing():
    """Route with ORCA only, making no network call of any kind.

    The selector eval measures *routing* latency and asserts a p99 bound, so
    any network call inside routing turns that assertion into a measurement of
    somebody else's availability.

    This was originally `llm_fallback_disabled`, which closed exactly one of
    the two doors: the LLM fallback. The other was the selector's SLO context
    lookup, which queries Prometheus — and on a CI runner with no Prometheus it
    stalled one scenario for five seconds, blowing the same 500ms bound the
    first fix was meant to protect. Naming the guard after the property
    (offline) rather than one mechanism (the fallback) is the point: the next
    network call added to routing has an obvious place to be excluded, instead
    of quietly reopening the measurement.

    Production routing is unaffected.
    """
    global _ROUTING_OFFLINE
    previous = _ROUTING_OFFLINE
    _ROUTING_OFFLINE = True
    try:
        yield
    finally:
        _ROUTING_OFFLINE = previous


def get_last_routing_decision() -> dict | None:
    """Return the last routing decision, or None if no routing has occurred."""
    d = _last_routing_decision_var.get()
    return dict(d) if d else None


def reset_hard_pre_route() -> None:
    """Clear cached pre-route rules so they are rebuilt on next query."""
    global _HARD_PRE_ROUTE
    _HARD_PRE_ROUTE = []


def _init_hard_pre_route() -> None:
    """Build hard pre-route rules from skill trigger_patterns, ordered by route_priority."""
    global _HARD_PRE_ROUTE
    if _HARD_PRE_ROUTE:
        return

    from .skill_loader import list_skills

    skills = sorted(list_skills(), key=lambda s: s.route_priority)

    for skill in skills:
        # Pre-route is automatic routing too. Without this check a skill barred
        # from ORCA selection (unreviewed or quarantined — both gates zero its
        # fused score) would still serve traffic through its trigger patterns,
        # which would make either gate a fiction for any skill that has them.
        if not skill.reviewed or skill.quarantined:
            continue
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


# A follow-up that refers back to the conversation ("scale it back", "do that
# again", "try the other one") is a continuation, not a request for a different
# specialist. Re-routing on an incidental clause produced the worst result in the
# eval suite: "Scale it back to 3 then, we don't have the capacity" matched the
# capacity_planner trigger, the turn switched to a skill without scale_deployment,
# and that skill correctly disowned an action the conversation had taken two turns
# earlier — reading, to the operator, as the assistant denying its own work.
_BACK_REFERENCE = re.compile(
    r"\b(it|its|that|those|them|these|again|back|instead|the same|previous|earlier)\b",
    re.IGNORECASE,
)


def is_continuation(query: str) -> bool:
    """Whether a turn reads as a follow-up to the conversation rather than a new task."""
    return bool(_BACK_REFERENCE.search(query or ""))


# An explicit authoring imperative — "create a skill", "build me a runbook",
# "edit the plan" — is a new task by definition, whatever pronouns follow it.
# "Create a skill called etcd-defrag ... It should check member DB sizes"
# contains an incidental "It", read as a continuation, and stuck to whatever
# specialist the thread last used — which then lacked the skill-management
# tools entirely. This is deliberately narrower than trigger patterns at
# large: bare topic words like "capacity" must NOT break stickiness, or
# "Scale it back to 3, we don't have the capacity" re-routes mid-action —
# the exact regression the continuation guard exists to prevent.
_AUTHORING_REQUEST = re.compile(
    r"\b(create|build|make|write|edit|update|delete)\b[^.!?\n]{0,30}\b(skill|runbook|plan\s+template|playbook)\b",
    re.IGNORECASE,
)


def is_authoring_request(query: str) -> bool:
    """Whether the turn explicitly asks to author agent behaviour (skill/runbook/plan)."""
    return bool(_AUTHORING_REQUEST.search(query or ""))


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
    if result.source == "fallback" and not best_skill and not _ROUTING_OFFLINE:
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

    if not settings.routing.multi_skill:
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
                model="claude-sonnet-5",
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

        name = (
            next((b.text for b in response.content if getattr(b, "text", None) is not None), "")
            .strip()
            .lower()
            .replace(" ", "_")
        )
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
