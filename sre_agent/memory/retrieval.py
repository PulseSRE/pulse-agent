"""Memory retrieval and context assembly for system prompt augmentation."""

from __future__ import annotations

import json

from .store import IncidentStore

MAX_MEMORY_CHARS = 1500


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
