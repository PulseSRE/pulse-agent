"""Memory retrieval and context assembly for system prompt augmentation."""

from __future__ import annotations

import json
import math
from collections import Counter

from .store import IncidentStore

MAX_MEMORY_CHARS = 1500

# ---------------------------------------------------------------------------
# TF-IDF similarity helpers (stdlib only, no external deps)
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    """Simple whitespace + lowercase tokenization."""
    return [w.lower().strip('.,!?()[]{}:;"\'') for w in text.split() if len(w) > 2]


def _tfidf_similarity(query: str, documents: list[str]) -> list[float]:
    """Compute TF-IDF cosine similarity between query and documents."""
    query_tokens = _tokenize(query)
    doc_token_lists = [_tokenize(doc) for doc in documents]

    # IDF
    n_docs = len(documents) + 1
    all_tokens = set(query_tokens)
    for tokens in doc_token_lists:
        all_tokens.update(tokens)

    doc_freq: Counter[str] = Counter()
    for tokens in doc_token_lists:
        for token in set(tokens):
            doc_freq[token] += 1

    idf = {t: math.log((n_docs + 1) / (1 + doc_freq.get(t, 0))) for t in all_tokens}

    # Query vector
    query_tf = Counter(query_tokens)
    query_vec = {t: (query_tf[t] / max(len(query_tokens), 1)) * idf.get(t, 0) for t in query_tokens}

    # Document vectors + cosine similarity
    scores: list[float] = []
    for tokens in doc_token_lists:
        doc_tf = Counter(tokens)
        doc_vec = {t: (doc_tf[t] / max(len(tokens), 1)) * idf.get(t, 0) for t in tokens}

        # Cosine similarity
        dot = sum(query_vec.get(t, 0) * doc_vec.get(t, 0) for t in all_tokens)
        mag_q = math.sqrt(sum(v ** 2 for v in query_vec.values())) or 1
        mag_d = math.sqrt(sum(v ** 2 for v in doc_vec.values())) or 1
        scores.append(dot / (mag_q * mag_d))

    return scores


def build_memory_context(store: IncidentStore, user_query: str) -> str:
    """Build a memory context block to append to the system prompt.

    Returns empty string if no relevant memory found.
    Caps output to ~MAX_MEMORY_CHARS to avoid context window bloat.
    """
    sections = []

    # Similar past incidents (top 3)
    incidents = store.search_incidents(user_query, limit=3)
    if incidents:
        inc_lines = []
        for inc in incidents:
            tools = json.loads(inc["tool_sequence"])
            tool_names = [t["name"] for t in tools[:5]]
            inc_lines.append(
                f"- Query: \"{inc['query'][:100]}\" | "
                f"Tools: {', '.join(tool_names)} | "
                f"Outcome: {inc['outcome']} | Score: {inc['score']:.1f}"
            )
        sections.append("## Past Similar Incidents\n" + "\n".join(inc_lines))

    # Anti-patterns: low-scoring incidents to learn from failures (Improvement #3)
    low_score_incidents = store.search_low_score_incidents(user_query, threshold=0.4, limit=2)
    if low_score_incidents:
        anti_lines = []
        for inc in low_score_incidents:
            tools = json.loads(inc["tool_sequence"])
            tool_names = [t["name"] for t in tools[:5]]
            anti_lines.append(
                f"- Query: \"{inc['query'][:80]}\" | "
                f"Tools: {', '.join(tool_names)} | "
                f"Outcome: {inc['outcome']} | Score: {inc['score']:.1f}"
            )
        sections.append(
            "## Avoid These Approaches (low-scoring past attempts)\n"
            + "\n".join(anti_lines)
        )

    # Matching runbooks (top 2)
    runbooks = store.find_runbooks(user_query, limit=2)
    if runbooks:
        rb_lines = []
        for rb in runbooks:
            steps = json.loads(rb["tool_sequence"])
            step_names = [s["name"] for s in steps]
            total = rb["success_count"] + rb["failure_count"]
            rb_lines.append(
                f"- **{rb['name']}**: {rb['description'][:80]}\n"
                f"  Steps: {' -> '.join(step_names)} "
                f"(success rate: {rb['success_count']}/{total})"
            )
        sections.append("## Learned Runbooks\n" + "\n".join(rb_lines))

    # Relevant patterns (top 2)
    patterns = store.search_patterns(user_query, limit=2)
    if patterns:
        pat_lines = [f"- [{r['pattern_type']}] {r['description']}" for r in patterns]
        sections.append("## Detected Patterns\n" + "\n".join(pat_lines))

    if not sections:
        return ""

    context = "\n\n".join(sections)
    if len(context) > MAX_MEMORY_CHARS:
        context = context[:MAX_MEMORY_CHARS] + "\n... (memory truncated)"

    return (
        "\n\n---\n## Agent Memory (from past interactions)\n"
        "Use this context to inform your approach. Follow proven runbooks when applicable.\n\n"
        + context + "\n---\n"
    )
