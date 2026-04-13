"""Adaptive tool selection engine — learns which tools to offer per query.

Three-tier prediction:
1. TF-IDF token scoring (hot path, zero cost, sub-ms)
2. LLM picker via Haiku (cold-start fallback, self-eliminating)
3. Chain bigrams + co-occurrence (mid-turn expansion)
"""

from __future__ import annotations

import logging
import re

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
