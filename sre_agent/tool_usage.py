"""Tool usage recording — fire-and-forget functions for tracking tool calls and turns."""

from __future__ import annotations

import json
import logging

logger = logging.getLogger("pulse_agent.tool_usage")

# Secret field names to redact
_SECRET_FIELDS = {
    "token",
    "password",
    "key",
    "secret",
    "credential",
    "yaml_content",
    "new_content",
    "content",
}

_MAX_STRING_LEN = 256
_MAX_JSON_BYTES = 1024


def sanitize_input(input_data: dict | None) -> dict | None:
    """Sanitize tool input data for storage.

    - Returns None if input is None
    - Returns {} if input is empty
    - Strips secret fields (replaces with <redacted N chars>)
    - Truncates strings longer than 256 chars
    - Caps total JSON size at ~1KB
    """
    if input_data is None:
        return None

    if not input_data:
        return {}

    # First pass: redact secrets and truncate strings
    sanitized = {}
    for key, value in input_data.items():
        if key.lower() in _SECRET_FIELDS:
            if isinstance(value, str):
                sanitized[key] = f"<redacted {len(value)} chars>"
            else:
                sanitized[key] = "<redacted>"
        elif isinstance(value, str):
            if len(value) > _MAX_STRING_LEN:
                sanitized[key] = value[:_MAX_STRING_LEN] + "..."
            else:
                sanitized[key] = value
        else:
            sanitized[key] = value

    # Second pass: cap total size
    encoded = json.dumps(sanitized)
    if len(encoded) <= _MAX_JSON_BYTES:
        return sanitized

    # Drop keys until we fit
    result = {}
    for key, value in sanitized.items():
        result[key] = value
        if len(json.dumps(result)) > _MAX_JSON_BYTES:
            del result[key]
            break

    return result


def record_tool_call(
    *,
    session_id: str,
    turn_number: int,
    agent_mode: str,
    tool_name: str,
    tool_category: str | None,
    input_data: dict | None,
    status: str,
    error_message: str | None,
    error_category: str | None,
    duration_ms: int,
    result_bytes: int,
    requires_confirmation: bool,
    was_confirmed: bool | None,
    tool_source: str = "native",
) -> None:
    """Record a tool call to the tool_usage table.

    Fire-and-forget: swallows all exceptions, logs at debug level.
    Uses %s placeholders for PostgreSQL.
    tool_source is 'native' for built-in Pulse tools or 'mcp' for MCP server tools.
    """
    try:
        from .db import get_database

        db = get_database()
        sanitized = sanitize_input(input_data)

        db.execute(
            "INSERT INTO tool_usage "
            "(session_id, turn_number, agent_mode, tool_name, tool_category, "
            "input_summary, status, error_message, error_category, "
            "duration_ms, result_bytes, requires_confirmation, was_confirmed, tool_source) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                session_id,
                turn_number,
                agent_mode,
                tool_name,
                tool_category,
                json.dumps(sanitized) if sanitized is not None else None,
                status,
                error_message,
                error_category,
                duration_ms,
                result_bytes,
                requires_confirmation,
                was_confirmed,
                tool_source,
            ),
        )
        db.commit()
        logger.debug(
            f"Recorded tool call: {tool_name} (session={session_id}, turn={turn_number}, status={status}, source={tool_source})"
        )
    except Exception as e:
        logger.debug(f"Failed to record tool call: {e}")


def record_turn(
    *,
    session_id: str,
    turn_number: int,
    agent_mode: str,
    query_summary: str,
    tools_offered: list[str],
    tools_called: list[str],
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    routing_decision: dict | None = None,
) -> None:
    """Record a turn to the tool_turns table.

    Fire-and-forget: swallows all exceptions, logs at debug level.
    Uses ON CONFLICT upsert on (session_id, turn_number).
    Truncates query_summary to 200 chars.
    """
    try:
        import json as _json

        from .db import get_database

        db = get_database()

        if len(query_summary) > 200:
            query_summary = query_summary[:200]

        routing_skill = routing_decision.get("skill_name") if routing_decision else None
        routing_score = routing_decision.get("keyword_score") if routing_decision else None
        routing_competing = (
            _json.dumps(routing_decision.get("competing_scores", {}))
            if routing_decision and routing_decision.get("competing_scores")
            else None
        )
        routing_used_llm = routing_decision.get("used_llm_fallback", False) if routing_decision else False

        db.execute(
            "INSERT INTO tool_turns "
            "(session_id, turn_number, agent_mode, query_summary, tools_offered, tools_called, "
            "input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, "
            "routing_skill, routing_score, routing_competing, routing_used_llm) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (session_id, turn_number) DO UPDATE SET "
            "tools_called = EXCLUDED.tools_called, "
            "input_tokens = EXCLUDED.input_tokens, "
            "output_tokens = EXCLUDED.output_tokens, "
            "cache_read_tokens = EXCLUDED.cache_read_tokens, "
            "cache_creation_tokens = EXCLUDED.cache_creation_tokens, "
            "routing_skill = EXCLUDED.routing_skill, "
            "routing_score = EXCLUDED.routing_score, "
            "routing_competing = EXCLUDED.routing_competing, "
            "routing_used_llm = EXCLUDED.routing_used_llm",
            (
                session_id,
                turn_number,
                agent_mode,
                query_summary,
                tools_offered,
                tools_called,
                input_tokens or None,
                output_tokens or None,
                cache_read_tokens or None,
                cache_creation_tokens or None,
                routing_skill,
                routing_score,
                routing_competing,
                routing_used_llm,
            ),
        )
        db.commit()
        logger.debug(
            f"Recorded turn: session={session_id}, turn={turn_number}, skill={routing_skill}, score={routing_score}"
        )
    except Exception as e:
        logger.debug(f"Failed to record turn: {e}")


def update_turn_feedback(
    *,
    session_id: str,
    feedback: str,
) -> None:
    """Update the most recent turn for a session with feedback.

    Fire-and-forget: swallows all exceptions, logs at debug level.
    Uses subquery to find the latest turn by turn_number.
    """
    try:
        from .db import get_database

        db = get_database()

        db.execute(
            "UPDATE tool_turns SET feedback = %s "
            "WHERE id = (SELECT id FROM tool_turns WHERE session_id = %s ORDER BY turn_number DESC LIMIT 1)",
            (feedback, session_id),
        )
        db.commit()
        logger.debug(f"Updated turn feedback: session={session_id}, feedback={feedback}")
    except Exception as e:
        logger.debug(f"Failed to update turn feedback: {e}")


def query_usage(
    *,
    tool_name: str | None = None,
    agent_mode: str | None = None,
    status: str | None = None,
    session_id: str | None = None,
    tool_source: str | None = None,
    time_from: str | None = None,
    time_to: str | None = None,
    page: int = 1,
    per_page: int = 50,
) -> dict:
    """Query the tool_usage table with optional filters and pagination.

    Args:
        tool_name: Filter by tool name
        agent_mode: Filter by agent mode (sre, security, etc)
        status: Filter by status (success, error)
        session_id: Filter by session ID
        time_from: ISO timestamp lower bound (inclusive)
        time_to: ISO timestamp upper bound (inclusive)
        page: Page number (1-indexed)
        per_page: Results per page (max 200)

    Returns:
        {
            "entries": [...],  # list of dicts with all tool_usage columns + query_summary
            "total": int,
            "page": int,
            "per_page": int
        }
    """
    from .db import get_database

    db = get_database()

    # Cap per_page at 200
    per_page = min(per_page, 200)
    offset = (page - 1) * per_page

    # Build WHERE clause dynamically
    where_clauses = []
    params = []

    if tool_name is not None:
        where_clauses.append("u.tool_name = %s")
        params.append(tool_name)

    if agent_mode is not None:
        where_clauses.append("u.agent_mode = %s")
        params.append(agent_mode)

    if status is not None:
        where_clauses.append("u.status = %s")
        params.append(status)

    if session_id is not None:
        where_clauses.append("u.session_id = %s")
        params.append(session_id)

    if tool_source is not None:
        where_clauses.append("u.tool_source = %s")
        params.append(tool_source)

    if time_from is not None:
        where_clauses.append("u.timestamp >= %s")
        params.append(time_from)

    if time_to is not None:
        where_clauses.append("u.timestamp <= %s")
        params.append(time_to)

    where_sql = ""
    if where_clauses:
        where_sql = "WHERE " + " AND ".join(where_clauses)

    # Count total matching rows
    count_sql = f"SELECT COUNT(*) AS total FROM tool_usage u {where_sql}"
    count_row = db.fetchone(count_sql, tuple(params))
    total = count_row["total"] if count_row else 0

    # Fetch paginated results with LEFT JOIN on tool_turns
    query_sql = f"""
        SELECT
            u.id, u.timestamp, u.session_id, u.turn_number, u.agent_mode,
            u.tool_name, u.tool_category, u.input_summary, u.status,
            u.error_message, u.error_category, u.duration_ms, u.result_bytes,
            u.requires_confirmation, u.was_confirmed, u.tool_source,
            t.query_summary
        FROM tool_usage u
        LEFT JOIN tool_turns t ON u.session_id = t.session_id AND u.turn_number = t.turn_number
        {where_sql}
        ORDER BY u.timestamp DESC
        LIMIT %s OFFSET %s
    """

    rows = db.fetchall(query_sql, tuple(params) + (per_page, offset))

    # Convert rows to dicts with ISO timestamps and parsed input_summary
    entries = []
    for row in rows:
        entry = dict(row)
        # Convert timestamp to ISO string
        if entry["timestamp"]:
            entry["timestamp"] = entry["timestamp"].isoformat()
        # Parse input_summary from string to dict if needed
        if entry["input_summary"] and isinstance(entry["input_summary"], str):
            try:
                entry["input_summary"] = json.loads(entry["input_summary"])
            except (json.JSONDecodeError, TypeError):
                pass
        entries.append(entry)

    return {"entries": entries, "total": total, "page": page, "per_page": per_page}


def get_usage_stats(
    *,
    time_from: str | None = None,
    time_to: str | None = None,
) -> dict:
    """Get aggregated statistics from the tool_usage table.

    Args:
        time_from: ISO timestamp lower bound (inclusive)
        time_to: ISO timestamp upper bound (inclusive)

    Returns:
        {
            "total_calls": int,
            "unique_tools_used": int,
            "error_rate": float,
            "avg_duration_ms": int,
            "avg_result_bytes": int,
            "by_tool": [{"tool_name": str, "count": int, "error_count": int, "avg_duration_ms": int, "avg_result_bytes": int}],
            "by_mode": [{"mode": str, "count": int}],
            "by_category": [{"category": str, "count": int}],
            "by_status": {"success": int, "error": int}
        }
    """
    from .db import get_database

    db = get_database()

    # Build WHERE clause for time filters
    where_clauses = []
    params = []

    if time_from is not None:
        where_clauses.append("timestamp >= %s")
        params.append(time_from)

    if time_to is not None:
        where_clauses.append("timestamp <= %s")
        params.append(time_to)

    where_sql = ""
    if where_clauses:
        where_sql = "WHERE " + " AND ".join(where_clauses)

    # Overall stats
    overall_sql = f"""
        SELECT
            COUNT(*) AS total_calls,
            COUNT(DISTINCT tool_name) AS unique_tools_used,
            COALESCE(AVG(CASE WHEN status = 'error' THEN 1.0 ELSE 0.0 END), 0) AS error_rate,
            COALESCE(ROUND(AVG(duration_ms)), 0) AS avg_duration_ms,
            COALESCE(ROUND(AVG(result_bytes)), 0) AS avg_result_bytes
        FROM tool_usage
        {where_sql}
    """
    overall = db.fetchone(overall_sql, tuple(params))

    # By tool
    by_tool_sql = f"""
        SELECT
            tool_name,
            COUNT(*) AS count,
            SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS error_count,
            COALESCE(ROUND(AVG(duration_ms)), 0) AS avg_duration_ms,
            COALESCE(ROUND(AVG(result_bytes)), 0) AS avg_result_bytes
        FROM tool_usage
        {where_sql}
        GROUP BY tool_name
        ORDER BY count DESC
    """
    by_tool = db.fetchall(by_tool_sql, tuple(params))

    # By mode
    by_mode_sql = f"""
        SELECT agent_mode AS mode, COUNT(*) AS count
        FROM tool_usage
        {where_sql}
        GROUP BY agent_mode
        ORDER BY count DESC
    """
    by_mode = db.fetchall(by_mode_sql, tuple(params))

    # By category (filter out NULLs)
    category_where_sql = where_sql
    if category_where_sql:
        category_where_sql += " AND tool_category IS NOT NULL"
    else:
        category_where_sql = "WHERE tool_category IS NOT NULL"

    by_category_sql = f"""
        SELECT tool_category AS category, COUNT(*) AS count
        FROM tool_usage
        {category_where_sql}
        GROUP BY tool_category
        ORDER BY count DESC
    """
    by_category = db.fetchall(by_category_sql, tuple(params))

    # By status
    by_status_sql = f"""
        SELECT status, COUNT(*) AS count
        FROM tool_usage
        {where_sql}
        GROUP BY status
    """
    by_status_rows = db.fetchall(by_status_sql, tuple(params))
    by_status = {row["status"]: row["count"] for row in by_status_rows}

    # By source (native vs mcp)
    by_source_sql = f"""
        SELECT COALESCE(tool_source, 'native') AS source, COUNT(*) AS count
        FROM tool_usage
        {where_sql}
        GROUP BY COALESCE(tool_source, 'native')
    """
    by_source_rows = db.fetchall(by_source_sql, tuple(params))
    by_source = {row["source"]: row["count"] for row in by_source_rows}

    # Token usage averages from tool_turns
    token_avg = {}
    try:
        token_where = where_sql.replace("timestamp", "t.timestamp") if where_sql else ""
        token_sql = f"""
            SELECT
                COALESCE(ROUND(AVG(input_tokens)), 0) AS avg_input,
                COALESCE(ROUND(AVG(output_tokens)), 0) AS avg_output,
                COALESCE(ROUND(AVG(cache_read_tokens)), 0) AS avg_cache_read
            FROM tool_turns t
            {token_where}
            {"AND" if token_where else "WHERE"} input_tokens IS NOT NULL
        """
        avg_row = db.fetchone(token_sql, tuple(params))
        if avg_row:
            token_avg = {
                "input": int(avg_row["avg_input"]),
                "output": int(avg_row["avg_output"]),
                "cache_read": int(avg_row["avg_cache_read"]),
            }
    except Exception:
        logger.debug("Failed to compute token averages", exc_info=True)

    stats = {
        "total_calls": overall["total_calls"],
        "unique_tools_used": overall["unique_tools_used"],
        "error_rate": float(overall["error_rate"]),
        "avg_duration_ms": int(overall["avg_duration_ms"]),
        "avg_result_bytes": int(overall["avg_result_bytes"]),
        "by_tool": [dict(row) for row in by_tool],
        "by_mode": [dict(row) for row in by_mode],
        "by_category": [dict(row) for row in by_category],
        "by_status": by_status,
        "by_source": by_source,
    }
    if token_avg:
        stats["token_avg"] = token_avg
    return stats


_AGENT_DESCRIPTIONS = {
    "sre": "Cluster diagnostics, incident triage, and resource management",
    "security": "Security scanning, RBAC analysis, and compliance checks",
    "view_designer": "Dashboard creation and component design",
}


def get_agents_metadata() -> list[dict]:
    """Return metadata for all agent modes."""
    from .orchestrator import build_orchestrated_config
    from .skill_loader import get_mode_categories

    agents = []
    for mode, categories in get_mode_categories().items():
        if mode == "both":
            continue

        config = build_orchestrated_config(mode)

        agents.append(
            {
                "name": mode,
                "description": _AGENT_DESCRIPTIONS.get(mode, ""),
                "tools_count": len(config["tool_defs"]),
                "has_write_tools": len(config["write_tools"]) > 0,
                "categories": categories or [],
            }
        )

    return agents


# ---------------------------------------------------------------------------
# Learned eval prompts from implicit user feedback
# ---------------------------------------------------------------------------

_RETRY_KEYWORDS = frozenset(
    [
        "no ",
        "wrong",
        "not what i",
        "try again",
        "i meant",
        "actually ",
        "instead ",
        "that's not",
        "thats not",
        "retry",
        "redo",
    ]
)


def get_learned_eval_prompts(days: int = 30, limit: int = 50) -> list[tuple[str, list[str], str, str]]:
    """Generate eval prompts from implicit positive user feedback.

    A turn is implicitly positive when the user's next message in the same
    session is a new topic (not a retry/correction detected by keyword check).
    """
    try:
        from .db import get_database

        db = get_database()
        rows = db.fetchall(
            "SELECT t1.query_summary, t1.tools_called, t1.agent_mode, t2.query_summary AS next_query "
            "FROM tool_turns t1 "
            "JOIN tool_turns t2 ON t1.session_id = t2.session_id AND t2.turn_number = t1.turn_number + 1 "
            "WHERE t1.tools_called IS NOT NULL "
            "AND array_length(t1.tools_called, 1) > 0 "
            "AND t1.query_summary IS NOT NULL AND t1.query_summary != '' "
            "AND t1.timestamp > NOW() - INTERVAL '1 day' * ? "
            "ORDER BY t1.timestamp DESC "
            "LIMIT ?",
            (days, limit * 3),
        )
    except Exception:
        logger.debug("Failed to query learned eval prompts", exc_info=True)
        return []

    seen: set[str] = set()
    prompts: list[tuple[str, list[str], str, str]] = []
    for row in rows:
        query = (row["query_summary"] or "").strip()
        next_q = (row["next_query"] or "").lower()
        tools = row["tools_called"] or []
        mode = row["agent_mode"] or "sre"

        if not query or not tools:
            continue

        # Skip if next message looks like a retry
        if any(kw in next_q for kw in _RETRY_KEYWORDS):
            continue

        # Deduplicate by normalized query
        key = query.lower()
        if key in seen:
            continue
        seen.add(key)

        prompts.append((query, list(tools), mode, "Learned from usage"))
        if len(prompts) >= limit:
            break

    return prompts
