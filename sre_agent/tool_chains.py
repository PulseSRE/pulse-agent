"""Tool chain intelligence — discovers common tool sequences and generates hints.

Layer 1: Chain Discovery — mines tool_usage for frequent bigrams and trigrams.
Layer 2: Next-Tool Hints — injects suggestions into the system prompt.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger("pulse_agent.tool_chains")

# In-memory cache: {tool_name: [(next_tool, probability), ...]}
_chain_hints_cache: dict[str, list[tuple[str, float]]] = {}
_cache_timestamp: float = 0


def discover_chains(
    *,
    min_frequency: int = 3,
    limit: int = 20,
) -> dict:
    """Discover frequent tool call bigrams from tool_usage."""
    try:
        from .db import get_database

        db = get_database()

        session_row = db.fetchone("SELECT COUNT(DISTINCT session_id) AS cnt FROM tool_usage")
        total_sessions = session_row["cnt"] if session_row else 0

        if total_sessions == 0:
            return {"bigrams": [], "total_sessions_analyzed": 0}

        bigram_rows = db.fetchall(
            """
            WITH ordered AS (
                SELECT session_id, tool_name,
                       LAG(tool_name) OVER (PARTITION BY session_id ORDER BY turn_number, id) AS prev_tool
                FROM tool_usage
                WHERE status = 'success'
            ),
            bigram_counts AS (
                SELECT prev_tool AS from_tool, tool_name AS to_tool, COUNT(*) AS frequency
                FROM ordered
                WHERE prev_tool IS NOT NULL
                GROUP BY prev_tool, tool_name
                HAVING COUNT(*) >= %s
            ),
            from_totals AS (
                SELECT prev_tool AS tool, COUNT(*) AS total
                FROM ordered
                WHERE prev_tool IS NOT NULL
                GROUP BY prev_tool
            )
            SELECT b.from_tool, b.to_tool, b.frequency,
                   ROUND(b.frequency::numeric / f.total, 4) AS probability
            FROM bigram_counts b
            JOIN from_totals f ON b.from_tool = f.tool
            ORDER BY b.frequency DESC, b.from_tool ASC, b.to_tool ASC
            LIMIT %s
            """,
            (min_frequency, limit),
        )

        bigrams = [
            {
                "from_tool": row["from_tool"],
                "to_tool": row["to_tool"],
                "frequency": row["frequency"],
                "probability": float(row["probability"]),
            }
            for row in bigram_rows
        ]

        return {"bigrams": bigrams, "total_sessions_analyzed": total_sessions}

    except Exception:
        logger.debug("Chain discovery failed", exc_info=True)
        return {"bigrams": [], "total_sessions_analyzed": 0}


def discover_trigrams(
    *,
    min_frequency: int = 3,
    limit: int = 15,
) -> list[dict]:
    """Discover frequent 3-tool sequences from tool_usage.

    Returns sequences like [list_pods → describe_pod → get_pod_logs] with
    frequency and probability (how often tool_c follows the A→B pair).
    """
    try:
        from .db import get_database

        db = get_database()

        rows = db.fetchall(
            """
            WITH ordered AS (
                SELECT session_id, tool_name,
                       LAG(tool_name, 1) OVER (PARTITION BY session_id ORDER BY turn_number, id) AS prev_1,
                       LAG(tool_name, 2) OVER (PARTITION BY session_id ORDER BY turn_number, id) AS prev_2
                FROM tool_usage
                WHERE status = 'success'
            ),
            trigram_counts AS (
                SELECT prev_2 AS tool_a, prev_1 AS tool_b, tool_name AS tool_c,
                       COUNT(*) AS frequency
                FROM ordered
                WHERE prev_1 IS NOT NULL AND prev_2 IS NOT NULL
                GROUP BY prev_2, prev_1, tool_name
                HAVING COUNT(*) >= %s
            ),
            pair_totals AS (
                SELECT prev_2 AS tool_a, prev_1 AS tool_b, COUNT(*) AS total
                FROM ordered
                WHERE prev_1 IS NOT NULL AND prev_2 IS NOT NULL
                GROUP BY prev_2, prev_1
            )
            SELECT t.tool_a, t.tool_b, t.tool_c, t.frequency,
                   ROUND(t.frequency::numeric / p.total, 4) AS probability
            FROM trigram_counts t
            JOIN pair_totals p ON t.tool_a = p.tool_a AND t.tool_b = p.tool_b
            ORDER BY t.frequency DESC
            LIMIT %s
            """,
            (min_frequency, limit),
        )

        return [
            {
                "sequence": [row["tool_a"], row["tool_b"], row["tool_c"]],
                "frequency": row["frequency"],
                "probability": float(row["probability"]),
            }
            for row in (rows or [])
        ]

    except Exception:
        logger.debug("Trigram discovery failed", exc_info=True)
        return []


def refresh_chain_hints(
    *,
    min_probability: float | None = None,
    min_frequency: int | None = None,
) -> None:
    """Refresh the in-memory chain hints cache."""
    global _cache_timestamp

    # Use defaults if settings not available (parallel task may not be complete)
    if min_probability is None:
        try:
            from .config import get_settings

            settings = get_settings()
            min_probability = settings.chain_min_probability
        except (ImportError, AttributeError):
            min_probability = 0.3

    if min_frequency is None:
        try:
            from .config import get_settings

            settings = get_settings()
            min_frequency = settings.chain_min_frequency
        except (ImportError, AttributeError):
            min_frequency = 3

    result = discover_chains(min_frequency=min_frequency, limit=50)

    new_cache: dict[str, list[tuple[str, float]]] = {}
    for bigram in result["bigrams"]:
        if bigram["probability"] >= min_probability:
            from_tool = bigram["from_tool"]
            if from_tool not in new_cache:
                new_cache[from_tool] = []
            new_cache[from_tool].append((bigram["to_tool"], bigram["probability"]))

    _chain_hints_cache.clear()
    _chain_hints_cache.update(new_cache)
    _cache_timestamp = time.time()
    logger.debug("Refreshed chain hints: %d tools with suggestions", len(_chain_hints_cache))


def get_chain_hints_text(max_hints: int = 5) -> str:
    """Generate system prompt text from cached chain hints."""
    if not _chain_hints_cache:
        return ""

    lines = []
    sorted_items = sorted(_chain_hints_cache.items(), key=lambda x: -max(p for _, p in x[1]))
    for count, (from_tool, suggestions) in enumerate(sorted_items):
        if count >= max_hints:
            break
        parts = ", ".join(f"{to} ({int(prob * 100)}%)" for to, prob in suggestions[:3])
        lines.append(f"- After {from_tool}, users typically need: {parts}")

    if not lines:
        return ""

    # Add trigram workflows if available
    trigrams = discover_trigrams(min_frequency=3, limit=3)
    for tri in trigrams:
        if tri["probability"] >= 0.3:
            seq = " → ".join(tri["sequence"])
            lines.append(f"- Common workflow: {seq} ({int(tri['probability'] * 100)}%)")

    return "\n## Tool Usage Patterns\n" + "\n".join(lines)


def ensure_hints_fresh(max_age: float = 300) -> None:
    """Refresh hints if cache is stale (older than max_age seconds)."""
    # Check if hints are enabled
    try:
        from .config import get_settings

        if not get_settings().chain_hints:
            return
    except (ImportError, AttributeError):
        # If config not available, default to enabled
        pass

    now = time.time()
    if now - _cache_timestamp > max_age:
        try:
            refresh_chain_hints()
        except Exception:
            logger.debug("Failed to refresh chain hints", exc_info=True)
