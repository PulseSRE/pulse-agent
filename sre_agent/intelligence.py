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


def _compute_query_reliability(days: int) -> str:
    """Compute PromQL query reliability from promql_queries table."""
    try:
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
        if not rows:
            return ""

        preferred: list[str] = []
        avoid: list[str] = []

        for row in rows:
            template = row["query_template"]
            success = row["success_count"]
            failure = row["failure_count"]
            total = success + failure
            rate = success / total if total > 0 else 0

            truncated = template[:80] + "..." if len(template) > 80 else template

            if rate > 0.8 and len(preferred) < 10:
                preferred.append(f"- `{truncated}`: {success}/{total} success → USE THIS")
            elif rate < 0.3 and len(avoid) < 5:
                avoid.append(f"- `{truncated}`: {success}/{total} success → AVOID")

        if not preferred and not avoid:
            return ""

        lines = ["### Query Reliability"]
        if preferred:
            lines.append("**Preferred queries:**")
            lines.extend(preferred)
        if avoid:
            lines.append("**Unreliable queries (avoid):**")
            lines.extend(avoid)
        return "\n".join(lines)
    except Exception:
        logger.debug("Failed to compute query reliability", exc_info=True)
        return ""


def _compute_dashboard_patterns(days: int) -> str:
    """Compute dashboard/view designer usage patterns from tool_usage."""
    try:
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

        if not tool_rows:
            return ""

        lines = ["### Dashboard Patterns"]
        if avg_row and avg_row.get("avg_tools"):
            lines.append(f"Average tools per dashboard session: {avg_row['avg_tools']}")
        lines.append("**Most used tools in view building:**")
        for row in tool_rows:
            lines.append(f"- {row['tool_name']}: {row['call_count']} calls")
        return "\n".join(lines)
    except Exception:
        logger.debug("Failed to compute dashboard patterns", exc_info=True)
        return ""


def _compute_error_hotspots(days: int) -> str:
    """Compute tools with high error rates from tool_usage."""
    try:
        from .db import get_database

        db = get_database()

        hotspot_rows = db.fetchall(
            "SELECT tool_name, "
            "       COUNT(*) FILTER (WHERE status = 'error') as error_count, "
            "       COUNT(*) as total_count "
            "FROM tool_usage "
            "WHERE timestamp > NOW() - INTERVAL '1 day' * ? "
            "GROUP BY tool_name "
            "HAVING COUNT(*) > 5 "
            "   AND COUNT(*) FILTER (WHERE status = 'error')::float / COUNT(*) > 0.05 "
            "ORDER BY COUNT(*) FILTER (WHERE status = 'error')::float / COUNT(*) DESC "
            "LIMIT 5",
            (days,),
        )

        if not hotspot_rows:
            return ""

        lines = ["### Error Hotspots"]
        for row in hotspot_rows:
            tool = row["tool_name"]
            errors = row["error_count"]
            total = row["total_count"]
            error_rate = round(errors / total * 100, 1) if total > 0 else 0

            # Get most common error message for this tool
            err_msg = ""
            try:
                err_row = db.fetchone(
                    "SELECT error_message, COUNT(*) as cnt "
                    "FROM tool_usage "
                    "WHERE tool_name = ? AND status = 'error' "
                    "  AND timestamp > NOW() - INTERVAL '1 day' * ? "
                    "  AND error_message IS NOT NULL "
                    "GROUP BY error_message "
                    "ORDER BY cnt DESC "
                    "LIMIT 1",
                    (tool, days),
                )
                if err_row and err_row.get("error_message"):
                    err_msg = err_row["error_message"][:80]
            except Exception:
                pass

            line = f"- {tool}: {error_rate}% error rate ({errors}/{total})"
            if err_msg:
                line += f' — common: "{err_msg}"'
            lines.append(line)

        return "\n".join(lines)
    except Exception:
        logger.debug("Failed to compute error hotspots", exc_info=True)
        return ""


def _compute_token_efficiency(days: int) -> str:
    """Compute token usage efficiency metrics from tool_turns."""
    try:
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
            return ""

        lines = ["### Token Efficiency"]
        lines.append(f"Average input tokens per turn: {int(row['avg_input'])}")
        lines.append(f"Average output tokens per turn: {int(row['avg_output'])}")
        avg_cache = int(row["avg_cache"])
        if avg_cache:
            total_input = int(row["avg_input"]) or 1
            cache_pct = round((avg_cache / total_input) * 100)
            lines.append(f"Cache hit rate: {cache_pct}%")
        lines.append(f"Total turns analyzed: {row['total_turns']}")
        return "\n".join(lines)
    except Exception:
        logger.debug("Failed to compute token efficiency", exc_info=True)
        return ""


def _compute_harness_effectiveness(days: int) -> str:
    """Compute tool selection accuracy and wasted tools from tool_turns."""
    try:
        from .db import get_database

        db = get_database()

        # Selection accuracy: avg ratio of tools_called / tools_offered
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
            return ""

        accuracy_pct = round(acc_row["accuracy"] * 100)
        avg_called = int(acc_row["avg_called"])
        avg_offered = int(acc_row["avg_offered"])

        lines = ["### Harness Effectiveness"]
        lines.append(
            f"Tool selection accuracy: {accuracy_pct}% (avg {avg_called} of {avg_offered} offered tools used per turn)"
        )

        # Wasted tools: offered 20+ times but called <5% of the time
        wasted_rows = db.fetchall(
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
            "AND COALESCE(c.called_count, 0)::float / o.offered_count < 0.05 "
            "ORDER BY o.offered_count DESC "
            "LIMIT 10",
            (days, days),
        )

        if wasted_rows:
            lines.append("Wasted tools (offered but rarely used):")
            for row in wasted_rows:
                offered = row["offered_count"]
                called = row["called_count"]
                pct = round(called / offered * 100) if offered else 0
                lines.append(f"- {row['tool_name']}: offered {offered}x, used {called}x ({pct}%)")

        return "\n".join(lines)
    except Exception:
        logger.debug("Failed to compute harness effectiveness", exc_info=True)
        return ""


def _compute_routing_accuracy(days: int) -> str:
    """Compute mode routing accuracy from mode switches within sessions."""
    try:
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
            return ""

        switches = row["switches"]
        total = row["total"]
        switch_pct = round(switches / total * 100) if total else 0
        accuracy = 100 - switch_pct

        return (
            f"### Routing Accuracy\n"
            f"Mode routing accuracy: {accuracy}% "
            f"({switch_pct}% of multi-turn sessions had mode switches)"
        )
    except Exception:
        logger.debug("Failed to compute routing accuracy", exc_info=True)
        return ""


def _compute_feedback_analysis(days: int) -> str:
    """Correlate feedback with tools to find tools with negative feedback."""
    try:
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

        if not rows:
            return ""

        lines = ["### Feedback Analysis", "Tools with negative feedback:"]
        for row in rows:
            lines.append(f"- {row['tool_name']}: {row['negative']}/{row['total']} negative")

        return "\n".join(lines)
    except Exception:
        logger.debug("Failed to compute feedback analysis", exc_info=True)
        return ""


def _compute_token_trending(days: int) -> str:
    """Compute week-over-week token usage trending."""
    try:
        from .db import get_database

        db = get_database()

        row = db.fetchone(
            "SELECT "
            "    AVG(input_tokens) FILTER (WHERE timestamp > NOW() - INTERVAL '1 day' * ?) as current_avg, "
            "    AVG(input_tokens) FILTER (WHERE timestamp BETWEEN "
            "NOW() - INTERVAL '1 day' * ? AND NOW() - INTERVAL '1 day' * ?) as prev_avg, "
            "    AVG(output_tokens) FILTER (WHERE timestamp > NOW() - INTERVAL '1 day' * ?) as current_output "
            "FROM tool_turns "
            "WHERE input_tokens IS NOT NULL",
            (days, days * 2, days, days),
        )

        if not row or row.get("current_avg") is None:
            return ""

        current_avg = int(row["current_avg"])
        current_output = int(row["current_output"]) if row.get("current_output") else 0

        lines = ["### Token Trending"]

        prev_avg = row.get("prev_avg")
        if prev_avg and prev_avg > 0:
            change_pct = round((current_avg - prev_avg) / prev_avg * 100)
            arrow = "\u2193" if change_pct < 0 else "\u2191"
            lines.append(f"Avg input: {current_avg:,} tokens ({arrow}{abs(change_pct)}% from last week)")
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

        rows = db.fetchall(
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
            "SELECT o.tool_name "
            "FROM offered o "
            "LEFT JOIN called c ON o.tool_name = c.tool_name "
            "WHERE o.offered_count >= 20 "
            "AND COALESCE(c.called_count, 0)::float / o.offered_count < 0.02 "
            "ORDER BY o.offered_count DESC",
            (days, days),
        )

        return [row["tool_name"] for row in rows] if rows else []
    except Exception:
        logger.debug("Failed to get wasted tools", exc_info=True)
        return []


# ══════════════════════════════════════════════════════════════════════════════
# Structured Intelligence Sections (for Toolbox Analytics UI)
# ══════════════════════════════════════════════════════════════════════════════


def _compute_query_reliability_structured(days: int) -> dict:
    """Structured version of _compute_query_reliability."""
    try:
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
        if not rows:
            return {"preferred": [], "unreliable": []}

        preferred: list[dict] = []
        unreliable: list[dict] = []

        for row in rows:
            template = row["query_template"]
            success = row["success_count"]
            failure = row["failure_count"]
            total = success + failure
            rate = success / total if total > 0 else 0

            if rate > 0.8 and len(preferred) < 10:
                preferred.append({"query": template, "success_rate": round(rate, 2), "total": total})
            elif rate < 0.3 and len(unreliable) < 5:
                unreliable.append({"query": template, "success_rate": round(rate, 2), "total": total})

        return {"preferred": preferred, "unreliable": unreliable}
    except Exception:
        logger.debug("Failed to compute query reliability structured", exc_info=True)
        return {"preferred": [], "unreliable": []}


def _compute_error_hotspots_structured(days: int) -> list[dict]:
    """Structured version of _compute_error_hotspots."""
    try:
        from .db import get_database

        db = get_database()

        hotspot_rows = db.fetchall(
            "SELECT tool_name, "
            "       COUNT(*) FILTER (WHERE status = 'error') as error_count, "
            "       COUNT(*) as total_count "
            "FROM tool_usage "
            "WHERE timestamp > NOW() - INTERVAL '1 day' * ? "
            "GROUP BY tool_name "
            "HAVING COUNT(*) > 5 "
            "   AND COUNT(*) FILTER (WHERE status = 'error')::float / COUNT(*) > 0.05 "
            "ORDER BY COUNT(*) FILTER (WHERE status = 'error')::float / COUNT(*) DESC "
            "LIMIT 5",
            (days,),
        )

        if not hotspot_rows:
            return []

        result = []
        for row in hotspot_rows:
            tool = row["tool_name"]
            errors = row["error_count"]
            total = row["total_count"]
            error_rate = round(errors / total, 2) if total > 0 else 0

            # Get most common error message for this tool
            err_msg = ""
            try:
                err_row = db.fetchone(
                    "SELECT error_message, COUNT(*) as cnt "
                    "FROM tool_usage "
                    "WHERE tool_name = ? AND status = 'error' "
                    "  AND timestamp > NOW() - INTERVAL '1 day' * ? "
                    "  AND error_message IS NOT NULL "
                    "GROUP BY error_message "
                    "ORDER BY cnt DESC "
                    "LIMIT 1",
                    (tool, days),
                )
                if err_row and err_row.get("error_message"):
                    err_msg = err_row["error_message"][:80]
            except Exception:
                pass

            result.append(
                {
                    "tool": tool,
                    "error_rate": error_rate,
                    "total": total,
                    "common_error": err_msg,
                }
            )

        return result
    except Exception:
        logger.debug("Failed to compute error hotspots structured", exc_info=True)
        return []


def _compute_token_efficiency_structured(days: int) -> dict:
    """Structured version of _compute_token_efficiency."""
    try:
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
            return {"avg_input": 0, "avg_output": 0, "cache_hit_rate": 0.0}

        avg_input = int(row["avg_input"])
        avg_output = int(row["avg_output"])
        avg_cache = int(row["avg_cache"])
        cache_pct = round((avg_cache / avg_input) * 100, 1) if avg_input > 0 else 0.0

        return {
            "avg_input": avg_input,
            "avg_output": avg_output,
            "cache_hit_rate": cache_pct,
        }
    except Exception:
        logger.debug("Failed to compute token efficiency structured", exc_info=True)
        return {"avg_input": 0, "avg_output": 0, "cache_hit_rate": 0.0}


def _compute_harness_effectiveness_structured(days: int) -> dict:
    """Structured version of _compute_harness_effectiveness."""
    try:
        from .db import get_database

        db = get_database()

        # Selection accuracy: avg ratio of tools_called / tools_offered
        acc_row = db.fetchone(
            "SELECT AVG(array_length(tools_called, 1)::float "
            "/ NULLIF(array_length(tools_offered, 1), 0)) as accuracy "
            "FROM tool_turns "
            "WHERE tools_offered IS NOT NULL AND tools_called IS NOT NULL "
            "AND timestamp > NOW() - INTERVAL '1 day' * ?",
            (days,),
        )

        if not acc_row or acc_row.get("accuracy") is None:
            return {"accuracy": 0.0, "wasted": []}

        accuracy_pct = round(acc_row["accuracy"] * 100, 1)

        # Wasted tools: offered 20+ times but called <5% of the time
        wasted_rows = db.fetchall(
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
            "AND COALESCE(c.called_count, 0)::float / o.offered_count < 0.05 "
            "ORDER BY o.offered_count DESC "
            "LIMIT 10",
            (days, days),
        )

        wasted = [
            {
                "tool": row["tool_name"],
                "offered": row["offered_count"],
                "used": row["called_count"],
            }
            for row in wasted_rows
        ]

        return {"accuracy": accuracy_pct, "wasted": wasted}
    except Exception:
        logger.debug("Failed to compute harness effectiveness structured", exc_info=True)
        return {"accuracy": 0.0, "wasted": []}


def _compute_routing_accuracy_structured(days: int) -> dict:
    """Structured version of _compute_routing_accuracy."""
    try:
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
            return {"mode_switch_rate": 0.0, "total_sessions": 0}

        switches = row["switches"]
        total = row["total"]
        switch_pct = round(switches / total * 100, 1) if total else 0.0

        return {"mode_switch_rate": switch_pct, "total_sessions": total}
    except Exception:
        logger.debug("Failed to compute routing accuracy structured", exc_info=True)
        return {"mode_switch_rate": 0.0, "total_sessions": 0}


def _compute_feedback_analysis_structured(days: int) -> dict:
    """Structured version of _compute_feedback_analysis."""
    try:
        from .db import get_database

        db = get_database()

        rows = db.fetchall(
            "SELECT u.tool_name, "
            "       COUNT(*) FILTER (WHERE t.feedback = 'negative') as negative "
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

        if not rows:
            return {"negative": []}

        negative = [{"tool": row["tool_name"], "count": row["negative"]} for row in rows]

        return {"negative": negative}
    except Exception:
        logger.debug("Failed to compute feedback analysis structured", exc_info=True)
        return {"negative": []}


def _compute_token_trending_structured(days: int) -> dict:
    """Structured version of _compute_token_trending."""
    try:
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

        current_input = row["current_input"] or 0
        prev_input = row["prev_input"] or 0
        current_output = row["current_output"] or 0
        prev_output = row["prev_output"] or 0
        current_cache = row["current_cache"] or 0
        prev_cache = row["prev_cache"] or 0

        input_delta_pct = round((current_input - prev_input) / prev_input * 100, 1) if prev_input > 0 else 0.0
        output_delta_pct = round((current_output - prev_output) / prev_output * 100, 1) if prev_output > 0 else 0.0
        cache_delta_pct = round((current_cache - prev_cache) / prev_cache * 100, 1) if prev_cache > 0 else 0.0

        return {
            "input_delta_pct": input_delta_pct,
            "output_delta_pct": output_delta_pct,
            "cache_delta_pct": cache_delta_pct,
        }
    except Exception:
        logger.debug("Failed to compute token trending structured", exc_info=True)
        return {"input_delta_pct": 0.0, "output_delta_pct": 0.0, "cache_delta_pct": 0.0}


def _compute_dashboard_patterns_structured(days: int) -> dict:
    """Structured version of _compute_dashboard_patterns."""
    try:
        from .db import get_database

        db = get_database()

        # Most used components (from view_components table if available)
        # For now, we'll use tool_usage from view_designer mode
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

        top_components = [{"kind": row["tool_name"], "count": row["call_count"]} for row in tool_rows]
        avg_widgets = avg_row["avg_tools"] if avg_row and avg_row.get("avg_tools") else 0

        return {"top_components": top_components, "avg_widgets": avg_widgets}
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
