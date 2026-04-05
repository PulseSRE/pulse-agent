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


def get_intelligence_context(mode: str = "sre", max_age_days: int = 7) -> str:
    """Compute intelligence summary from analytics data."""
    now = time.time()
    cached = _intelligence_cache.get(mode)
    if cached and now - cached[1] < _CACHE_TTL:
        return cached[0]

    try:
        sections: list[str] = []
        qr = _compute_query_reliability(max_age_days)
        if qr:
            sections.append(qr)
        dp = _compute_dashboard_patterns(max_age_days)
        if dp:
            sections.append(dp)
        eh = _compute_error_hotspots(max_age_days)
        if eh:
            sections.append(eh)
        te = _compute_token_efficiency(max_age_days)
        if te:
            sections.append(te)

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
            f"SELECT query_template, success_count, failure_count "
            f"FROM promql_queries "
            f"WHERE (last_success > NOW() - INTERVAL '{days} days' "
            f"   OR last_failure > NOW() - INTERVAL '{days} days') "
            f"AND success_count + failure_count >= 3 "
            f"ORDER BY success_count + failure_count DESC "
            f"LIMIT 20",
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
            f"SELECT tool_name, COUNT(*) as call_count "
            f"FROM tool_usage "
            f"WHERE agent_mode = 'view_designer' "
            f"  AND timestamp > NOW() - INTERVAL '{days} days' "
            f"  AND status = 'success' "
            f"GROUP BY tool_name "
            f"ORDER BY call_count DESC "
            f"LIMIT 10",
        )

        avg_row = db.fetchone(
            f"SELECT AVG(tool_count)::int as avg_tools FROM ("
            f"    SELECT session_id, COUNT(*) as tool_count "
            f"    FROM tool_usage "
            f"    WHERE agent_mode = 'view_designer' "
            f"      AND timestamp > NOW() - INTERVAL '{days} days' "
            f"    GROUP BY session_id"
            f") sub",
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
            f"SELECT tool_name, "
            f"       COUNT(*) FILTER (WHERE status = 'error') as error_count, "
            f"       COUNT(*) as total_count "
            f"FROM tool_usage "
            f"WHERE timestamp > NOW() - INTERVAL '{days} days' "
            f"GROUP BY tool_name "
            f"HAVING COUNT(*) > 5 "
            f"   AND COUNT(*) FILTER (WHERE status = 'error')::float / COUNT(*) > 0.05 "
            f"ORDER BY COUNT(*) FILTER (WHERE status = 'error')::float / COUNT(*) DESC "
            f"LIMIT 5",
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
                    f"SELECT error_message, COUNT(*) as cnt "
                    f"FROM tool_usage "
                    f"WHERE tool_name = ? AND status = 'error' "
                    f"  AND timestamp > NOW() - INTERVAL '{days} days' "
                    f"  AND error_message IS NOT NULL "
                    f"GROUP BY error_message "
                    f"ORDER BY cnt DESC "
                    f"LIMIT 1",
                    (tool,),
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
            f"SELECT COALESCE(ROUND(AVG(input_tokens)), 0) AS avg_input, "
            f"COALESCE(ROUND(AVG(output_tokens)), 0) AS avg_output, "
            f"COALESCE(ROUND(AVG(cache_read_tokens)), 0) AS avg_cache, "
            f"COUNT(*) AS total_turns "
            f"FROM tool_turns "
            f"WHERE input_tokens IS NOT NULL "
            f"AND timestamp > NOW() - INTERVAL '{days} days'",
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
