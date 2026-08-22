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


@beta_tool
def remember_environment_fact(key: str, value: str, scope: str = "cluster", source: str = "") -> str:
    """Record something true about THIS cluster so it does not have to be re-derived.

    Use for facts that change rarely and that change how you diagnose: who owns a
    namespace, how long Prometheus retains data, that ArgoCD owns production so
    manual edits get reverted, local naming conventions. Do not use it for
    measurements — those are baselines, and they change continuously.

    Args:
        key: Short identifier, e.g. 'prometheus_retention' or 'payments_owner'.
        value: The fact itself, in plain language.
        scope: 'cluster' for cluster-wide, otherwise a namespace name.
        source: Where this came from — the operator who said it, or the tool that showed it.
    """
    from .environment import get_cluster_memory

    if get_cluster_memory().remember_fact(key, value, scope=scope, source=source):
        return f"Recorded: [{scope}] {key} = {value}"
    return "Error: could not record that fact (key and value must both be non-empty)."


@beta_tool
def get_environment_facts(scope: str = "") -> str:
    """Recall what is known about this cluster — ownership, retention, conventions, quirks.

    Call this early in an investigation. It is what stops you giving generic advice
    that ignores how this particular cluster is run.

    Args:
        scope: Limit to one scope ('cluster' or a namespace). Empty returns all.
    """
    from .environment import get_cluster_memory

    facts = get_cluster_memory().get_facts(scope)
    if not facts:
        where = f" for scope '{scope}'" if scope else ""
        return f"No environment facts recorded{where} yet. Use remember_environment_fact to record one."

    by_scope: dict[str, list[str]] = {}
    for fact in facts:
        by_scope.setdefault(fact.scope, []).append(f"  {fact.render()}")
    lines = [f"{len(facts)} known fact(s) about this cluster:", ""]
    for scope_name, entries in sorted(by_scope.items()):
        lines.append(f"[{scope_name}]")
        lines.extend(entries)
        lines.append("")
    return "\n".join(lines).rstrip()


@beta_tool
def compare_to_baseline(namespace: str, workload: str, metric: str, observed: float) -> str:
    """Say whether an observed value is normal FOR THIS WORKLOAD.

    A number on its own is not a finding. Use this before reporting that something
    is high or low, so you report '3x this service's normal' rather than a raw
    figure the operator has to interpret.

    Args:
        namespace: Kubernetes namespace.
        workload: Workload name, e.g. 'checkout-api'.
        metric: Metric name, e.g. 'memory_bytes', 'cpu_cores', 'error_rate'.
        observed: The value you measured.
    """
    from .environment import get_cluster_memory

    baseline = get_cluster_memory().get_baseline(namespace, workload, metric)
    if baseline is None:
        return (
            f"No baseline recorded for {namespace}/{workload} {metric}. "
            f"Observed {observed:g} — report it as a raw value and say there is no baseline to compare against."
        )
    return f"{namespace}/{workload}: {baseline.compare(observed)}"


@beta_tool
def search_conversations(query: str, owner: str = "", limit: int = 5) -> str:
    """Search past conversations for something discussed before.

    Use when the user refers to earlier work ("the thing we found last week") or
    when you suspect this problem has come up before. Searches only this user's
    own sessions.

    Args:
        query: Phrase to look for.
        owner: Session owner. Leave empty to use the current user.
        limit: Maximum results.
    """
    from ..chat_history import search_messages

    if len(query.strip()) < 3:
        return "Error: query must be at least 3 characters."

    rows = search_messages(owner, query, limit)
    if not rows:
        return f"Nothing in past conversations matched '{query}'."

    lines = [f"{len(rows)} past mention(s) of '{query}':", ""]
    for row in rows:
        content = str(row.get("content", "")).strip().replace("\n", " ")
        if len(content) > 200:
            content = content[:200] + "..."
        lines.append(f"  [{row.get('title', 'untitled')}] {row.get('role', '?')}: {content}")
    return "\n".join(lines)
