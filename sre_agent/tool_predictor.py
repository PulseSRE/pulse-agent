"""Adaptive tool selection engine — learns which tools to offer per query.

Three-tier prediction:
1. TF-IDF token scoring (hot path, zero cost, sub-ms)
2. LLM picker via Haiku (cold-start fallback, self-eliminating)
3. Chain bigrams + co-occurrence (mid-turn expansion)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from itertools import combinations

logger = logging.getLogger("pulse_agent.tool_predictor")

_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "in",
        "my",
        "me",
        "can",
        "you",
        "please",
        "what",
        "is",
        "are",
        "do",
        "how",
        "this",
        "that",
        "it",
        "for",
        "to",
        "of",
        "and",
        "or",
        "show",
        "tell",
        "get",
        "why",
        "with",
        "all",
        "about",
        "i",
        "need",
        "want",
        "help",
        "check",
        "look",
        "at",
        "on",
        "from",
        "be",
        "been",
        "being",
        "has",
        "have",
        "had",
        "was",
        "were",
        "will",
        "would",
        "could",
        "should",
        "does",
        "did",
        "just",
        "also",
        "some",
        "if",
        "so",
        "but",
        "not",
        "no",
        "there",
        "their",
        "they",
        "them",
        "its",
        "any",
        "more",
        "very",
        "too",
        "into",
        "up",
        "out",
    }
)

_TOKEN_REGEX = re.compile(r"[^a-z0-9_-]+")


def extract_tokens(query: str) -> list[str]:
    """Tokenize query into meaningful tokens for TF-IDF tool prediction.

    Rules:
    - Lowercase input
    - Split on whitespace + punctuation using regex [^a-z0-9_-]+
    - Drop stopwords
    - Keep K8s compound terms intact (e.g., "crashloopbackoff")
    - Generate bigrams from consecutive non-stopword tokens
    - Deduplicate tokens
    - Return empty list for empty/whitespace-only input

    Args:
        query: User query string

    Returns:
        List of unique tokens (unigrams + bigrams)
    """
    if not query or not query.strip():
        return []

    # Lowercase and split
    normalized = query.lower()
    unigrams = [token for token in _TOKEN_REGEX.split(normalized) if token]

    # Filter stopwords
    filtered = [token for token in unigrams if token not in _STOPWORDS]

    # Generate bigrams from consecutive non-stopword tokens
    bigrams = []
    for i in range(len(filtered) - 1):
        bigrams.append(f"{filtered[i]} {filtered[i + 1]}")

    # Combine and deduplicate
    all_tokens = filtered + bigrams
    return list(dict.fromkeys(all_tokens))  # Preserves order while deduplicating


def _get_db():
    """Get database connection. Separate function for easy mocking."""
    from .db import get_database

    return get_database()


def learn(
    *,
    query: str,
    tools_called: list[str],
    tools_offered: list[str],
) -> None:
    """Record a completed turn to update predictions and co-occurrence.

    Fire-and-forget: swallows all exceptions.
    """
    if not tools_called:
        return

    try:
        db = _get_db()
        tokens = extract_tokens(query)
        if not tokens:
            return

        called_set = set(tools_called)
        not_called = set(tools_offered) - called_set

        # Positive signals: tokens x tools_called
        for token in tokens:
            for tool in tools_called:
                db.execute(
                    "INSERT INTO tool_predictions (token, tool_name, score, hit_count, miss_count, last_seen) "
                    "VALUES (%s, %s, 1.0, 1, 0, NOW()) "
                    "ON CONFLICT (token, tool_name) DO UPDATE SET "
                    "score = tool_predictions.score + 1.0, "
                    "hit_count = tool_predictions.hit_count + 1, "
                    "last_seen = NOW()",
                    (token, tool),
                )

        # Negative signals: tokens x tools_not_called
        for token in tokens:
            for tool in not_called:
                db.execute(
                    "INSERT INTO tool_predictions (token, tool_name, score, hit_count, miss_count, last_seen) "
                    "VALUES (%s, %s, 0.0, 0, 1, NOW()) "
                    "ON CONFLICT (token, tool_name) DO UPDATE SET "
                    "miss_count = tool_predictions.miss_count + 1, "
                    "last_seen = NOW()",
                    (token, tool),
                )

        # Co-occurrence: pairs of tools called together
        for tool_a, tool_b in combinations(sorted(tools_called), 2):
            db.execute(
                "INSERT INTO tool_cooccurrence (tool_a, tool_b, frequency) "
                "VALUES (%s, %s, 1) "
                "ON CONFLICT (tool_a, tool_b) DO UPDATE SET "
                "frequency = tool_cooccurrence.frequency + 1",
                (tool_a, tool_b),
            )

        db.commit()
    except Exception:
        logger.debug("Failed to record tool predictions", exc_info=True)


_CONFIDENCE_THRESHOLD = 5  # min total hit_count to trust TF-IDF


@dataclass
class PredictionResult:
    """Result of tool prediction."""

    tools: list[str] = field(default_factory=list)
    confidence: str = "low"  # "high" or "low"
    source: str = "none"  # "tfidf", "llm", "category", "none"


def predict_tools(query: str, *, top_k: int = 10) -> PredictionResult:
    """Predict which tools are most relevant for a query using TF-IDF scoring.

    Returns a PredictionResult with the predicted tool names and confidence level.
    """
    tokens = extract_tokens(query)
    if not tokens:
        return PredictionResult()

    try:
        db = _get_db()
    except Exception:
        return PredictionResult()

    placeholders = ", ".join(["%s"] * len(tokens))

    try:
        rows = db.fetchall(
            f"SELECT tool_name, "
            f"SUM(score - miss_count * 0.3) AS total_score, "
            f"SUM(hit_count) AS total_hits "
            f"FROM tool_predictions "
            f"WHERE token IN ({placeholders}) "
            f"GROUP BY tool_name "
            f"HAVING SUM(score - miss_count * 0.3) > 0 "
            f"ORDER BY total_score DESC "
            f"LIMIT %s",
            (*tokens, top_k),
        )
    except Exception:
        logger.debug("TF-IDF lookup failed", exc_info=True)
        return PredictionResult()

    if not rows:
        return PredictionResult()

    max_hits = max(r["total_hits"] for r in rows)
    if max_hits < _CONFIDENCE_THRESHOLD:
        return PredictionResult(confidence="low")

    predicted = list(dict.fromkeys(r["tool_name"] for r in rows))

    # Co-occurrence expansion
    predicted_set = set(predicted)
    expanded = _expand_cooccurrence(db, predicted, top_k)
    final = predicted + [t for t in expanded if t not in predicted_set]

    return PredictionResult(tools=final[: top_k + 5], confidence="high", source="tfidf")


def _expand_cooccurrence(db, tools: list[str], limit: int = 5) -> list[str]:
    """Find tools that frequently co-occur with the predicted set."""
    if not tools:
        return []

    placeholders = ", ".join(["%s"] * len(tools))
    try:
        rows = db.fetchall(
            f"SELECT tool_b, SUM(frequency) AS frequency FROM tool_cooccurrence "
            f"WHERE tool_a IN ({placeholders}) AND tool_b NOT IN ({placeholders}) "
            f"GROUP BY tool_b "
            f"ORDER BY frequency DESC LIMIT %s",
            (*tools, *tools, limit),
        )
        return [r["tool_b"] for r in rows]
    except Exception:
        return []


def llm_pick_tools(
    *,
    query: str,
    tool_names: list[str],
    top_k: int = 10,
) -> list[str]:
    """Use Haiku to pick the most relevant tools for a query.

    Sends only tool names (~200 tokens), not full schemas.
    Returns validated tool names (filtered against tool_names).
    """
    try:
        from .agent import create_client

        client = create_client()
        tool_list = ", ".join(tool_names)

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": query}],
            system=(
                f"You are a tool selector. Given a user query about Kubernetes/OpenShift, "
                f"pick the {top_k} most relevant tools from this list:\n{tool_list}\n\n"
                f"Reply with ONLY comma-separated tool names, nothing else."
            ),
        )

        raw = response.content[0].text.strip()
        valid_set = set(tool_names)
        picked = [t.strip() for t in raw.split(",") if t.strip() in valid_set]
        return picked[:top_k]

    except Exception:
        logger.debug("LLM tool picker failed", exc_info=True)
        return []


ALWAYS_INCLUDE_SLIM = {
    "list_pods",
    "get_events",
    "namespace_summary",
    "record_audit_entry",
    "list_my_skills",
}

_MIN_TOOL_SET_SIZE = 8


def select_tools_adaptive(
    query: str,
    *,
    all_tool_map: dict,
    fallback_categories: list[str] | None = None,
) -> tuple[list[dict], dict, list[str]]:
    """Adaptive tool selection: TF-IDF -> LLM -> category fallback.

    Returns (tool_defs, tool_map, offered_names) — same signature as select_tools().
    """
    from .skill_loader import TOOL_CATEGORIES

    # Phase 1: TF-IDF prediction
    result = predict_tools(query)

    if result.confidence == "high" and result.tools:
        tool_names = set(result.tools)
        source = "tfidf"
    else:
        # Phase 2: LLM fallback
        llm_tools = llm_pick_tools(
            query=query,
            tool_names=list(all_tool_map.keys()),
        )
        if llm_tools:
            tool_names = set(llm_tools)
            source = "llm"

            # Bootstrap TF-IDF from LLM picks (fire-and-forget)
            try:
                learn(query=query, tools_called=llm_tools, tools_offered=list(all_tool_map.keys()))
            except Exception:
                pass
        else:
            # Phase 3: Category fallback
            tool_names = set()
            for cat_name in fallback_categories or []:
                cat = TOOL_CATEGORIES.get(cat_name, {})
                tool_names.update(cat.get("tools", []))
            source = "category"

    # Always include slim set
    tool_names.update(ALWAYS_INCLUDE_SLIM)

    # Enforce minimum set size by padding from categories
    if len(tool_names) < _MIN_TOOL_SET_SIZE and fallback_categories:
        for cat_name in fallback_categories:
            cat = TOOL_CATEGORIES.get(cat_name, {})
            tool_names.update(cat.get("tools", []))
            if len(tool_names) >= _MIN_TOOL_SET_SIZE:
                break

    # Build final tool map — only include tools that actually exist in the registry
    tool_map = {n: t for n, t in all_tool_map.items() if n in tool_names}
    tool_defs = [t.to_dict() for t in tool_map.values()]
    offered = list(tool_map.keys())

    logger.info(
        "Adaptive tool selection: source=%s, predicted=%d, final=%d (query: %.50s)",
        source,
        len(result.tools) if result.tools else 0,
        len(tool_map),
        query,
    )

    return tool_defs, tool_map, offered


def expand_tool_set(
    *,
    called_tool: str,
    current_tool_map: dict,
    all_tool_map: dict,
    min_probability: float = 0.5,
) -> tuple[list[dict], dict]:
    """Expand the tool set based on chain bigrams and co-occurrence.

    Called after each tool execution to dynamically add predicted next-tools.
    Returns updated (tool_defs, tool_map).
    """
    from .tool_chains import _chain_hints_cache

    new_tools: dict[str, object] = {}

    # Chain bigram expansion
    suggestions = _chain_hints_cache.get(called_tool, [])
    for next_tool, prob in suggestions:
        if prob >= min_probability and next_tool not in current_tool_map and next_tool in all_tool_map:
            new_tools[next_tool] = all_tool_map[next_tool]

    # Co-occurrence expansion
    try:
        db = _get_db()
        rows = db.fetchall(
            "SELECT tool_b, frequency FROM tool_cooccurrence "
            "WHERE tool_a = %s AND frequency >= 3 "
            "ORDER BY frequency DESC LIMIT 3",
            (called_tool,),
        )
        for r in rows:
            t = r["tool_b"]
            if t not in current_tool_map and t in all_tool_map:
                new_tools[t] = all_tool_map[t]
    except Exception:
        pass

    if not new_tools:
        return [t.to_dict() for t in current_tool_map.values()], dict(current_tool_map)

    expanded = {**current_tool_map, **new_tools}
    logger.debug(
        "Chain expansion after %s: added %s",
        called_tool,
        ", ".join(new_tools.keys()),
    )
    return [t.to_dict() for t in expanded.values()], expanded


def decay_scores(*, factor: float = 0.95, prune_days: int = 30) -> None:
    """Apply daily decay to prediction scores and prune stale entries.

    Call from a daily cron or at startup.
    """
    try:
        db = _get_db()
        db.execute(
            "UPDATE tool_predictions SET score = score * %s",
            (factor,),
        )
        db.execute(
            "DELETE FROM tool_predictions WHERE last_seen < NOW() - INTERVAL '%s days'",
            (prune_days,),
        )
        db.commit()
        logger.info("Decayed prediction scores by %.2f, pruned entries older than %d days", factor, prune_days)
    except Exception:
        logger.debug("Failed to decay prediction scores", exc_info=True)
