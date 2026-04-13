"""Intelligence Loop — feeds analytics data back into the agent system prompt.

Computes query reliability, dashboard patterns, and error hotspots from the
last 7 days of tool_usage and promql_queries data. Injected into the system
prompt via harness.py get_cluster_context().
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger("pulse_agent.intelligence")

_intelligence_cache: dict[str, tuple[str, float]] = {}
_CACHE_TTL = 600  # 10 minutes

# Section IDs for ablation testing via PULSE_PROMPT_EXCLUDE_SECTIONS env var
_SECTION_REGISTRY = {
    "intelligence_query_reliability": "_compute_query_reliability",
    "intelligence_dashboard_patterns": "_compute_dashboard_patterns",
    "intelligence_error_hotspots": "_compute_error_hotspots",
    "intelligence_token_efficiency": "_compute_token_efficiency",
    "intelligence_harness_effectiveness": "_compute_harness_effectiveness",
    "intelligence_routing_accuracy": "_compute_routing_accuracy",
    "intelligence_feedback_analysis": "_compute_feedback_analysis",
    "intelligence_token_trending": "_compute_token_trending",
}


def _get_excluded_sections() -> set[str]:
    """Return set of section IDs excluded via env var (for ablation testing)."""
    import os

    raw = os.environ.get("PULSE_PROMPT_EXCLUDE_SECTIONS", "")
    if not raw:
        return set()
    return {s.strip() for s in raw.split(",") if s.strip()}


def get_intelligence_context(mode: str = "sre", max_age_days: int = 7) -> str:
    """Compute intelligence summary from analytics data."""
    now = time.time()
    excluded = _get_excluded_sections()

    # Skip cache if ablation is active (env var changes between runs)
    if not excluded:
        cached = _intelligence_cache.get(mode)
        if cached and now - cached[1] < _CACHE_TTL:
            return cached[0]

    try:
        sections: list[str] = []
        if "intelligence_query_reliability" not in excluded:
            qr = _compute_query_reliability(max_age_days)
            if qr:
                sections.append(qr)
        if "intelligence_dashboard_patterns" not in excluded:
            dp = _compute_dashboard_patterns(max_age_days)
            if dp:
                sections.append(dp)
        if "intelligence_error_hotspots" not in excluded:
            eh = _compute_error_hotspots(max_age_days)
            if eh:
                sections.append(eh)
        if "intelligence_token_efficiency" not in excluded:
            te = _compute_token_efficiency(max_age_days)
            if te:
                sections.append(te)
        if "intelligence_harness_effectiveness" not in excluded:
            he = _compute_harness_effectiveness(max_age_days)
            if he:
                sections.append(he)
        if "intelligence_routing_accuracy" not in excluded:
            ra = _compute_routing_accuracy(max_age_days)
            if ra:
                sections.append(ra)
        if "intelligence_feedback_analysis" not in excluded:
            fa = _compute_feedback_analysis(max_age_days)
            if fa:
                sections.append(fa)
        if "intelligence_token_trending" not in excluded:
            tt = _compute_token_trending(max_age_days)
            if tt:
                sections.append(tt)

        if not sections:
            result = ""
        else:
            result = f"## Agent Intelligence (last {max_age_days} days)\n\n" + "\n\n".join(sections)

        _intelligence_cache[mode] = (result, now)
        return result
    except Exception:
        logger.debug("Failed to compute intelligence context", exc_info=True)
        return ""


def _fetch_query_reliability_data(days: int) -> dict:
    """Fetch query reliability data (shared by text and structured versions)."""
    from .db import get_database

    db = get_database()
    rows = db.fetchall(
        "SELECT query_template, success_count, failure_count "
        "FROM promql_queries "
        "WHERE (last_success > NOW() - INTERVAL '1 day' * ? "
        "   OR last_failure > NOW() - INTERVAL '1 day' * ?) "
        "AND success_count + failure_count >= 3 "
        "ORDER BY success_count + failure_count DESC "
        "LIMIT 20",
        (days, days),
    )
    preferred: list[dict] = []
    unreliable: list[dict] = []

    for row in rows or []:
        template = row["query_template"]
        success = row["success_count"]
        failure = row["failure_count"]
        total = success + failure
        rate = success / total if total > 0 else 0

        entry = {"query": template, "success_rate": round(rate, 2), "total": total}
        if rate > 0.8 and len(preferred) < 10:
            preferred.append(entry)
        elif rate < 0.3 and len(unreliable) < 5:
            unreliable.append(entry)

    return {"preferred": preferred, "unreliable": unreliable}


def _compute_query_reliability(days: int) -> str:
    """Compute PromQL query reliability from promql_queries table."""
    try:
        data = _fetch_query_reliability_data(days)
        if not data["preferred"] and not data["unreliable"]:
            return ""

        lines = ["### Query Reliability"]
        if data["preferred"]:
            lines.append("**Preferred queries:**")
            for q in data["preferred"]:
                truncated = q["query"][:80] + "..." if len(q["query"]) > 80 else q["query"]
                lines.append(f"- `{truncated}`: {q['total']} calls, {q['success_rate'] * 100:.0f}% success → USE THIS")
        if data["unreliable"]:
            lines.append("**Unreliable queries (avoid):**")
            for q in data["unreliable"]:
                truncated = q["query"][:80] + "..." if len(q["query"]) > 80 else q["query"]
                lines.append(f"- `{truncated}`: {q['total']} calls, {q['success_rate'] * 100:.0f}% success → AVOID")
        return "\n".join(lines)
    except Exception:
        logger.debug("Failed to compute query reliability", exc_info=True)
        return ""


def _fetch_dashboard_patterns_data(days: int) -> dict:
    """Fetch dashboard pattern data (shared by text and structured versions)."""
    from .db import get_database

    db = get_database()
    tool_rows = db.fetchall(
        "SELECT tool_name, COUNT(*) as call_count "
        "FROM tool_usage "
        "WHERE agent_mode = 'view_designer' "
        "  AND timestamp > NOW() - INTERVAL '1 day' * ? "
        "  AND status = 'success' "
        "GROUP BY tool_name "
        "ORDER BY call_count DESC "
        "LIMIT 10",
        (days,),
    )
    avg_row = db.fetchone(
        "SELECT AVG(tool_count)::int as avg_tools FROM ("
        "    SELECT session_id, COUNT(*) as tool_count "
        "    FROM tool_usage "
        "    WHERE agent_mode = 'view_designer' "
        "      AND timestamp > NOW() - INTERVAL '1 day' * ? "
        "    GROUP BY session_id"
        ") sub",
        (days,),
    )
    top_components = [{"kind": r["tool_name"], "count": r["call_count"]} for r in tool_rows or []]
    avg_widgets = avg_row["avg_tools"] if avg_row and avg_row.get("avg_tools") else 0
    return {"top_components": top_components, "avg_widgets": avg_widgets}


def _compute_dashboard_patterns(days: int) -> str:
    """Compute dashboard/view designer usage patterns from tool_usage."""
    try:
        data = _fetch_dashboard_patterns_data(days)
        if not data["top_components"]:
            return ""
        lines = ["### Dashboard Patterns"]
        if data["avg_widgets"]:
            lines.append(f"Average tools per dashboard session: {data['avg_widgets']}")
        lines.append("**Most used tools in view building:**")
        for c in data["top_components"]:
            lines.append(f"- {c['kind']}: {c['count']} calls")
        return "\n".join(lines)
    except Exception:
        logger.debug("Failed to compute dashboard patterns", exc_info=True)
        return ""


def _fetch_error_hotspots(days: int) -> list[dict]:
    """Fetch error hotspot data with top error messages in a single batch query."""
    from .db import get_database

    db = get_database()

    # Single query: hotspots + top error message per tool via window function
    rows = db.fetchall(
        "WITH hotspots AS ("
        "    SELECT tool_name, "
        "           COUNT(*) FILTER (WHERE status = 'error') as error_count, "
        "           COUNT(*) as total_count "
        "    FROM tool_usage "
        "    WHERE timestamp > NOW() - INTERVAL '1 day' * ? "
        "    GROUP BY tool_name "
        "    HAVING COUNT(*) > 5 "
        "       AND COUNT(*) FILTER (WHERE status = 'error')::float / COUNT(*) > 0.05 "
        "    ORDER BY COUNT(*) FILTER (WHERE status = 'error')::float / COUNT(*) DESC "
        "    LIMIT 5"
        "), "
        "top_errors AS ("
        "    SELECT tool_name, error_message, COUNT(*) as cnt, "
        "           ROW_NUMBER() OVER (PARTITION BY tool_name ORDER BY COUNT(*) DESC) as rn "
        "    FROM tool_usage "
        "    WHERE tool_name IN (SELECT tool_name FROM hotspots) "
        "      AND status = 'error' "
        "      AND timestamp > NOW() - INTERVAL '1 day' * ? "
        "      AND error_message IS NOT NULL "
        "    GROUP BY tool_name, error_message"
        ") "
        "SELECT h.tool_name, h.error_count, h.total_count, "
        "       COALESCE(SUBSTRING(te.error_message, 1, 80), '') as common_error "
        "FROM hotspots h "
        "LEFT JOIN top_errors te ON h.tool_name = te.tool_name AND te.rn = 1 "
        "ORDER BY h.error_count::float / h.total_count DESC",
        (days, days),
    )

    result = []
    for row in rows or []:
        total = row["total_count"]
        errors = row["error_count"]
        result.append(
            {
                "tool": row["tool_name"],
                "error_count": errors,
                "total_count": total,
                "error_rate": round(errors / total * 100, 1) if total > 0 else 0,
                "common_error": row["common_error"],
            }
        )
    return result


def _compute_error_hotspots(days: int) -> str:
    """Compute tools with high error rates from tool_usage."""
    try:
        hotspots = _fetch_error_hotspots(days)
        if not hotspots:
            return ""

        lines = ["### Error Hotspots"]
        for h in hotspots:
            line = f"- {h['tool']}: {h['error_rate']}% error rate ({h['error_count']}/{h['total_count']})"
            if h["common_error"]:
                line += f' — common: "{h["common_error"]}"'
            lines.append(line)

        return "\n".join(lines)
    except Exception:
        logger.debug("Failed to compute error hotspots", exc_info=True)
        return ""


def _fetch_token_efficiency_data(days: int) -> dict:
    """Fetch token efficiency data (shared by text and structured versions)."""
    from .db import get_database

    db = get_database()
    row = db.fetchone(
        "SELECT COALESCE(ROUND(AVG(input_tokens)), 0) AS avg_input, "
        "COALESCE(ROUND(AVG(output_tokens)), 0) AS avg_output, "
        "COALESCE(ROUND(AVG(cache_read_tokens)), 0) AS avg_cache, "
        "COUNT(*) AS total_turns "
        "FROM tool_turns "
        "WHERE input_tokens IS NOT NULL "
        "AND timestamp > NOW() - INTERVAL '1 day' * ?",
        (days,),
    )
    if not row or not row.get("total_turns"):
        return {"avg_input": 0, "avg_output": 0, "cache_hit_rate": 0.0, "total_turns": 0}
    avg_input = int(row["avg_input"])
    avg_output = int(row["avg_output"])
    avg_cache = int(row["avg_cache"])
    cache_pct = round((avg_cache / avg_input) * 100, 1) if avg_input > 0 else 0.0
    return {
        "avg_input": avg_input,
        "avg_output": avg_output,
        "cache_hit_rate": cache_pct,
        "total_turns": row["total_turns"],
    }


def _compute_token_efficiency(days: int) -> str:
    """Compute token usage efficiency metrics from tool_turns."""
    try:
        data = _fetch_token_efficiency_data(days)
        if not data["total_turns"]:
            return ""
        lines = ["### Token Efficiency"]
        lines.append(f"Average input tokens per turn: {data['avg_input']}")
        lines.append(f"Average output tokens per turn: {data['avg_output']}")
        if data["cache_hit_rate"]:
            lines.append(f"Cache hit rate: {data['cache_hit_rate']}%")
        lines.append(f"Total turns analyzed: {data['total_turns']}")
        return "\n".join(lines)
    except Exception:
        logger.debug("Failed to compute token efficiency", exc_info=True)
        return ""


def _query_wasted_tools(db, days: int, threshold: float = 0.05, limit: int | None = 10) -> list[dict]:
    """Query tools that are offered frequently but rarely called."""
    query = (
        "WITH offered AS ("
        "    SELECT unnest(tools_offered) as tool_name, COUNT(*) as offered_count "
        "    FROM tool_turns "
        "    WHERE timestamp > NOW() - INTERVAL '1 day' * ? AND tools_offered IS NOT NULL "
        "    GROUP BY 1"
        "), "
        "called AS ("
        "    SELECT unnest(tools_called) as tool_name, COUNT(*) as called_count "
        "    FROM tool_turns "
        "    WHERE timestamp > NOW() - INTERVAL '1 day' * ? AND tools_called IS NOT NULL "
        "    GROUP BY 1"
        ") "
        "SELECT o.tool_name, o.offered_count, COALESCE(c.called_count, 0) as called_count "
        "FROM offered o "
        "LEFT JOIN called c ON o.tool_name = c.tool_name "
        "WHERE o.offered_count >= 20 "
        f"AND COALESCE(c.called_count, 0)::float / o.offered_count < {threshold} "
        "ORDER BY o.offered_count DESC"
    )
    if limit is not None:
        query += f" LIMIT {limit}"
    return db.fetchall(query, (days, days)) or []


def _fetch_harness_effectiveness_data(days: int) -> dict:
    """Fetch harness effectiveness data (shared by text and structured versions)."""
    from .db import get_database

    db = get_database()
    acc_row = db.fetchone(
        "SELECT AVG(array_length(tools_called, 1)::float "
        "/ NULLIF(array_length(tools_offered, 1), 0)) as accuracy, "
        "COALESCE(ROUND(AVG(array_length(tools_called, 1))), 0) as avg_called, "
        "COALESCE(ROUND(AVG(array_length(tools_offered, 1))), 0) as avg_offered "
        "FROM tool_turns "
        "WHERE tools_offered IS NOT NULL AND tools_called IS NOT NULL "
        "AND timestamp > NOW() - INTERVAL '1 day' * ?",
        (days,),
    )
    if not acc_row or acc_row.get("accuracy") is None:
        return {"accuracy": 0.0, "avg_called": 0, "avg_offered": 0, "wasted": []}
    wasted_rows = _query_wasted_tools(db, days, threshold=0.05, limit=10)
    wasted = [{"tool": r["tool_name"], "offered": r["offered_count"], "used": r["called_count"]} for r in wasted_rows]
    return {
        "accuracy": round(acc_row["accuracy"] * 100, 1),
        "avg_called": int(acc_row["avg_called"]),
        "avg_offered": int(acc_row["avg_offered"]),
        "wasted": wasted,
    }


def _compute_harness_effectiveness(days: int) -> str:
    """Compute tool selection accuracy and wasted tools from tool_turns."""
    try:
        data = _fetch_harness_effectiveness_data(days)
        if not data["accuracy"]:
            return ""
        lines = ["### Harness Effectiveness"]
        lines.append(
            f"Tool selection accuracy: {data['accuracy']:.0f}% "
            f"(avg {data['avg_called']} of {data['avg_offered']} offered tools used per turn)"
        )
        if data["wasted"]:
            lines.append("Wasted tools (offered but rarely used):")
            for w in data["wasted"]:
                pct = round(w["used"] / w["offered"] * 100) if w["offered"] else 0
                lines.append(f"- {w['tool']}: offered {w['offered']}x, used {w['used']}x ({pct}%)")
        return "\n".join(lines)
    except Exception:
        logger.debug("Failed to compute harness effectiveness", exc_info=True)
        return ""


def _fetch_routing_accuracy_data(days: int) -> dict:
    """Fetch routing accuracy data including misroute details."""
    from .db import get_database

    db = get_database()
    row = db.fetchone(
        "SELECT "
        "    COUNT(*) FILTER (WHERE agent_mode != prev_mode) as switches, "
        "    COUNT(*) as total "
        "FROM ("
        "    SELECT agent_mode, "
        "           LAG(agent_mode) OVER (PARTITION BY session_id ORDER BY turn_number) as prev_mode "
        "    FROM tool_turns "
        "    WHERE timestamp > NOW() - INTERVAL '1 day' * ?"
        ") sub "
        "WHERE prev_mode IS NOT NULL",
        (days,),
    )
    if not row or not row.get("total"):
        return {"mode_switch_rate": 0.0, "total_sessions": 0, "misroutes": []}
    switches = row["switches"]
    total = row["total"]
    switch_pct = round(switches / total * 100, 1) if total else 0

    # Fetch misroute details: which skill was routed to, what it switched to, and the query
    misroutes: list[dict] = []
    misroute_rows = db.fetchall(
        "SELECT prev_skill, next_skill, query_summary, prev_score "
        "FROM ("
        "    SELECT routing_skill as prev_skill, "
        "           LEAD(routing_skill) OVER (PARTITION BY session_id ORDER BY turn_number) as next_skill, "
        "           query_summary, "
        "           routing_score as prev_score "
        "    FROM tool_turns "
        "    WHERE timestamp > NOW() - INTERVAL '1 day' * ? "
        "      AND routing_skill IS NOT NULL"
        ") sub "
        "WHERE next_skill IS NOT NULL AND prev_skill != next_skill "
        "ORDER BY prev_score ASC NULLS FIRST "
        "LIMIT 20",
        (days,),
    )
    for mr in misroute_rows or []:
        misroutes.append(
            {
                "from_skill": mr["prev_skill"],
                "to_skill": mr["next_skill"],
                "query": mr.get("query_summary", ""),
                "score": mr.get("prev_score"),
            }
        )

    return {"mode_switch_rate": switch_pct, "total_sessions": total, "misroutes": misroutes}


def _compute_routing_accuracy(days: int) -> str:
    """Compute mode routing accuracy from mode switches within sessions."""
    try:
        data = _fetch_routing_accuracy_data(days)
        if not data["total_sessions"]:
            return ""
        accuracy = 100 - data["mode_switch_rate"]
        parts = [
            f"### Routing Accuracy\n"
            f"Mode routing accuracy: {accuracy:.0f}% "
            f"({data['mode_switch_rate']}% of multi-turn sessions had mode switches)"
        ]
        misroutes = data.get("misroutes", [])
        if misroutes:
            parts.append(f"\nRecent misroutes ({len(misroutes)}):")
            for mr in misroutes[:5]:
                parts.append(
                    f'  {mr["from_skill"]}→{mr["to_skill"]}: "{mr["query"][:60]}" (score={mr.get("score", "?")})'
                )
        return "\n".join(parts)
    except Exception:
        logger.debug("Failed to compute routing accuracy", exc_info=True)
        return ""


def _fetch_feedback_analysis_data(days: int) -> dict:
    """Fetch feedback analysis data (shared by text and structured versions)."""
    from .db import get_database

    db = get_database()
    rows = db.fetchall(
        "SELECT u.tool_name, "
        "       COUNT(*) FILTER (WHERE t.feedback = 'negative') as negative, "
        "       COUNT(*) as total "
        "FROM tool_turns t "
        "JOIN tool_usage u ON t.session_id = u.session_id AND t.turn_number = u.turn_number "
        "WHERE t.feedback IS NOT NULL "
        "AND t.timestamp > NOW() - INTERVAL '1 day' * ? "
        "GROUP BY u.tool_name "
        "HAVING COUNT(*) FILTER (WHERE t.feedback = 'negative') > 0 "
        "ORDER BY COUNT(*) FILTER (WHERE t.feedback = 'negative')::float / COUNT(*) DESC "
        "LIMIT 5",
        (days,),
    )
    return {"negative": [{"tool": r["tool_name"], "count": r["negative"]} for r in rows or []]}


def _compute_feedback_analysis(days: int) -> str:
    """Correlate feedback with tools to find tools with negative feedback."""
    try:
        data = _fetch_feedback_analysis_data(days)
        if not data["negative"]:
            return ""
        lines = ["### Feedback Analysis", "Tools with negative feedback:"]
        for f in data["negative"]:
            lines.append(f"- {f['tool']}: {f['count']} negative")
        return "\n".join(lines)
    except Exception:
        logger.debug("Failed to compute feedback analysis", exc_info=True)
        return ""


def _fetch_token_trending_data(days: int) -> dict:
    """Fetch token trending data (shared by text and structured versions)."""
    from .db import get_database

    db = get_database()
    row = db.fetchone(
        "SELECT "
        "    AVG(input_tokens) FILTER (WHERE timestamp > NOW() - INTERVAL '1 day' * ?) as current_input, "
        "    AVG(input_tokens) FILTER (WHERE timestamp BETWEEN "
        "NOW() - INTERVAL '1 day' * ? AND NOW() - INTERVAL '1 day' * ?) as prev_input, "
        "    AVG(output_tokens) FILTER (WHERE timestamp > NOW() - INTERVAL '1 day' * ?) as current_output, "
        "    AVG(output_tokens) FILTER (WHERE timestamp BETWEEN "
        "NOW() - INTERVAL '1 day' * ? AND NOW() - INTERVAL '1 day' * ?) as prev_output, "
        "    AVG(cache_read_tokens) FILTER (WHERE timestamp > NOW() - INTERVAL '1 day' * ?) as current_cache, "
        "    AVG(cache_read_tokens) FILTER (WHERE timestamp BETWEEN "
        "NOW() - INTERVAL '1 day' * ? AND NOW() - INTERVAL '1 day' * ?) as prev_cache "
        "FROM tool_turns "
        "WHERE input_tokens IS NOT NULL",
        (days, days * 2, days, days, days * 2, days, days, days * 2, days),
    )
    if not row or row.get("current_input") is None:
        return {"input_delta_pct": 0.0, "output_delta_pct": 0.0, "cache_delta_pct": 0.0}

    def _delta(cur, prev):
        return round((cur - prev) / prev * 100, 1) if prev and prev > 0 else 0.0

    ci, pi = (row["current_input"] or 0), (row["prev_input"] or 0)
    co, po = (row["current_output"] or 0), (row["prev_output"] or 0)
    cc, pc = (row["current_cache"] or 0), (row["prev_cache"] or 0)

    return {
        "input_delta_pct": _delta(ci, pi),
        "output_delta_pct": _delta(co, po),
        "cache_delta_pct": _delta(cc, pc),
        "_current_input": int(ci),
        "_current_output": int(co),
    }


def _compute_token_trending(days: int) -> str:
    """Compute week-over-week token usage trending."""
    try:
        data = _fetch_token_trending_data(days)
        current_avg = data.get("_current_input", 0)
        current_output = data.get("_current_output", 0)
        if not current_avg:
            return ""

        lines = ["### Token Trending"]
        if data["input_delta_pct"]:
            arrow = "\u2193" if data["input_delta_pct"] < 0 else "\u2191"
            lines.append(f"Avg input: {current_avg:,} tokens ({arrow}{abs(data['input_delta_pct'])}% from last week)")
        else:
            lines.append(f"Avg input: {current_avg:,} tokens")
        lines.append(f"Avg output: {current_output:,} tokens")
        return "\n".join(lines)
    except Exception:
        logger.debug("Failed to compute token trending", exc_info=True)
        return ""


def get_wasted_tools(days: int = 7) -> list[str]:
    """Return tool names that are offered frequently but rarely used.

    Used by harness.py to auto-deprioritize unused tools.
    """
    try:
        from .db import get_database

        db = get_database()
        rows = _query_wasted_tools(db, days, threshold=0.02, limit=None)
        return [row["tool_name"] for row in rows]
    except Exception:
        logger.debug("Failed to get wasted tools", exc_info=True)
        return []


# ══════════════════════════════════════════════════════════════════════════════
# Structured Intelligence Sections (for Toolbox Analytics UI)
# ══════════════════════════════════════════════════════════════════════════════


def _compute_query_reliability_structured(days: int) -> dict:
    """Structured version of _compute_query_reliability."""
    try:
        return _fetch_query_reliability_data(days)
    except Exception:
        logger.debug("Failed to compute query reliability structured", exc_info=True)
        return {"preferred": [], "unreliable": []}


def _compute_error_hotspots_structured(days: int) -> list[dict]:
    """Structured version of _compute_error_hotspots."""
    try:
        hotspots = _fetch_error_hotspots(days)
        return [
            {
                "tool": h["tool"],
                "error_rate": round(h["error_rate"] / 100, 2),
                "total": h["total_count"],
                "common_error": h["common_error"],
            }
            for h in hotspots
        ]
    except Exception:
        logger.debug("Failed to compute error hotspots structured", exc_info=True)
        return []


def _compute_token_efficiency_structured(days: int) -> dict:
    """Structured version of _compute_token_efficiency."""
    try:
        data = _fetch_token_efficiency_data(days)
        return {
            "avg_input": data["avg_input"],
            "avg_output": data["avg_output"],
            "cache_hit_rate": data["cache_hit_rate"],
        }
    except Exception:
        logger.debug("Failed to compute token efficiency structured", exc_info=True)
        return {"avg_input": 0, "avg_output": 0, "cache_hit_rate": 0.0}


def _compute_harness_effectiveness_structured(days: int) -> dict:
    """Structured version of _compute_harness_effectiveness."""
    try:
        data = _fetch_harness_effectiveness_data(days)
        return {"accuracy": data["accuracy"], "wasted": data["wasted"]}
    except Exception:
        logger.debug("Failed to compute harness effectiveness structured", exc_info=True)
        return {"accuracy": 0.0, "wasted": []}


def _compute_routing_accuracy_structured(days: int) -> dict:
    """Structured version of _compute_routing_accuracy."""
    try:
        return _fetch_routing_accuracy_data(days)
    except Exception:
        logger.debug("Failed to compute routing accuracy structured", exc_info=True)
        return {"mode_switch_rate": 0.0, "total_sessions": 0}


def _compute_feedback_analysis_structured(days: int) -> dict:
    """Structured version of _compute_feedback_analysis."""
    try:
        return _fetch_feedback_analysis_data(days)
    except Exception:
        logger.debug("Failed to compute feedback analysis structured", exc_info=True)
        return {"negative": []}


def _compute_token_trending_structured(days: int) -> dict:
    """Structured version of _compute_token_trending."""
    try:
        data = _fetch_token_trending_data(days)
        return {k: v for k, v in data.items() if not k.startswith("_")}
    except Exception:
        logger.debug("Failed to compute token trending structured", exc_info=True)
        return {"input_delta_pct": 0.0, "output_delta_pct": 0.0, "cache_delta_pct": 0.0}


def _compute_dashboard_patterns_structured(days: int) -> dict:
    """Structured version of _compute_dashboard_patterns."""
    try:
        return _fetch_dashboard_patterns_data(days)
    except Exception:
        logger.debug("Failed to compute dashboard patterns structured", exc_info=True)
        return {"top_components": [], "avg_widgets": 0}


def get_intelligence_sections(mode: str = "sre", days: int = 7) -> dict:
    """Compute all intelligence sections as structured data for Toolbox Analytics UI.

    Returns:
        dict with keys: query_reliability, error_hotspots, token_efficiency,
                        harness_effectiveness, routing_accuracy, feedback_analysis,
                        token_trending, dashboard_patterns
    """
    return {
        "query_reliability": _compute_query_reliability_structured(days),
        "error_hotspots": _compute_error_hotspots_structured(days),
        "token_efficiency": _compute_token_efficiency_structured(days),
        "harness_effectiveness": _compute_harness_effectiveness_structured(days),
        "routing_accuracy": _compute_routing_accuracy_structured(days),
        "feedback_analysis": _compute_feedback_analysis_structured(days),
        "token_trending": _compute_token_trending_structured(days),
        "dashboard_patterns": _compute_dashboard_patterns_structured(days),
    }
