"""Adaptive tool selection engine — learns which tools to offer per query.

Three-tier prediction:
1. TF-IDF token scoring (hot path, zero cost, sub-ms)
2. LLM picker via Haiku (cold-start fallback, self-eliminating)
3. Chain bigrams + co-occurrence (mid-turn expansion)
"""

from __future__ import annotations

import logging
import re
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
