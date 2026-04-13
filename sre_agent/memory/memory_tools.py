"""Agent-callable tools for memory access."""

from __future__ import annotations

import json

from ..decorators import beta_tool
from .store import IncidentStore

_store: IncidentStore | None = None


def set_store(store: IncidentStore):
    global _store
    _store = store


@beta_tool
def search_past_incidents(query: str, limit: int = 5) -> str:
    """Search past incidents the agent has resolved before. Use this to find similar issues and their solutions.

    Args:
        query: Search query describing the current issue (e.g. 'pod crashloopbackoff in monitoring namespace').
        limit: Maximum number of results (1-10).
    """
    if _store is None:
        return "Memory system not initialized."
    limit = min(max(1, limit), 10)
    results = _store.search_incidents(query, limit=limit)
    if not results:
        return "No similar past incidents found."

    lines = []
    for r in results:
        tools = json.loads(r["tool_sequence"])
        tool_names = [t["name"] for t in tools[:8]]
        lines.append(
            f"[Incident #{r['id']}] {r['timestamp'][:10]}\n"
            f"  Query: {r['query'][:120]}\n"
            f"  Tools: {' -> '.join(tool_names)}\n"
            f"  Outcome: {r['outcome']} | Score: {r['score']:.1f}\n"
            f"  Resolution: {r['resolution'][:200]}"
        )
    return "\n\n".join(lines)


@beta_tool
def get_learned_runbooks(query: str = "") -> str:
    """Get learned runbooks from past successful resolutions. Returns step-by-step tool sequences that worked before.

    Args:
        query: Optional search query to filter runbooks. Leave empty to list all.
    """
    if _store is None:
        return "Memory system not initialized."

    if query:
        results = _store.find_runbooks(query, limit=5)
    else:
        results = _store.list_runbooks(limit=10)

    if not results:
        return "No runbooks found."

    lines = []
    for rb in results:
        steps = json.loads(rb["tool_sequence"])
        step_list = "\n".join(
            f"    {i + 1}. {s['name']}({json.dumps(s.get('input_summary', {}))})" for i, s in enumerate(steps)
        )
        lines.append(
            f"**{rb['name']}** (success: {rb['success_count']}, failures: {rb['failure_count']})\n"
            f"  {rb['description']}\n"
            f"  Steps:\n{step_list}"
        )
    return "\n\n".join(lines)


@beta_tool
def get_cluster_patterns() -> str:
    """Get detected patterns and recurring issues in this cluster. Shows time-based patterns, frequently recurring problems, and correlations."""
    if _store is None:
        return "Memory system not initialized."

    patterns = _store.list_patterns(limit=10)
    if not patterns:
        return "No patterns detected yet. More incident data needed."

    lines = []
    for r in patterns:
        meta = json.loads(r["metadata"]) if r["metadata"] else {}
        meta_str = f" | {json.dumps(meta)}" if meta else ""
        lines.append(
            f"[{r['pattern_type'].upper()}] {r['description']}\n"
            f"  Frequency: {r['frequency']} | Last seen: {r['last_seen'][:10]}{meta_str}"
        )
    return "\n\n".join(lines)


MEMORY_TOOLS = [search_past_incidents, get_learned_runbooks, get_cluster_patterns]
