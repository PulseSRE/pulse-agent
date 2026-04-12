"""Skill Analytics — tracks skill usage, handoffs, and performance.

Records every skill invocation for transparency and trend tracking.
Fire-and-forget: all recording functions swallow errors.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("pulse_agent.skill_analytics")


def record_skill_invocation(
    *,
    session_id: str,
    user_id: str,
    skill_name: str,
    skill_version: int,
    query_summary: str = "",
    tools_called: list[str] | None = None,
    handoff_from: str | None = None,
    handoff_to: str | None = None,
    duration_ms: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    feedback: str | None = None,
    eval_score: float | None = None,
) -> None:
    """Record a skill invocation. Fire-and-forget."""
    try:
        from .db import get_database

        db = get_database()
        db.execute(
            "INSERT INTO skill_usage "
            "(session_id, user_id, skill_name, skill_version, query_summary, "
            "tools_called, tool_count, handoff_from, handoff_to, duration_ms, "
            "input_tokens, output_tokens, feedback, eval_score) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                session_id,
                user_id,
                skill_name,
                skill_version,
                (query_summary[:200] if query_summary else ""),
                tools_called or [],
                len(tools_called or []),
                handoff_from,
                handoff_to,
                duration_ms,
                input_tokens,
                output_tokens,
                feedback,
                eval_score,
            ),
        )
        db.commit()
    except Exception:
        logger.debug("Failed to record skill invocation: %s", skill_name, exc_info=True)


def get_skill_stats(days: int = 30) -> dict:
    """Aggregate skill usage statistics."""
    try:
        from .db import get_database

        db = get_database()

        # Per-skill stats
        skill_rows = db.fetchall(
            "SELECT skill_name, COUNT(*) as invocations, "
            "COALESCE(ROUND(AVG(tool_count)), 0) as avg_tools, "
            "COALESCE(ROUND(AVG(duration_ms)), 0) as avg_duration_ms, "
            "COALESCE(ROUND(AVG(input_tokens)), 0) as avg_input_tokens, "
            "COALESCE(ROUND(AVG(output_tokens)), 0) as avg_output_tokens, "
            "COUNT(*) FILTER (WHERE feedback = 'positive') as feedback_positive, "
            "COUNT(*) FILTER (WHERE feedback = 'negative') as feedback_negative "
            "FROM skill_usage "
            "WHERE timestamp > NOW() - INTERVAL '1 day' * %s "
            "GROUP BY skill_name ORDER BY invocations DESC",
            (days,),
        )

        # Top tools per skill in a single query (avoids N+1)
        all_tool_rows = db.fetchall(
            "SELECT skill_name, unnest(tools_called) as tool_name, COUNT(*) as cnt "
            "FROM skill_usage "
            "WHERE timestamp > NOW() - INTERVAL '1 day' * %s AND tools_called IS NOT NULL "
            "GROUP BY skill_name, tool_name ORDER BY skill_name, cnt DESC",
            (days,),
        )
        # Partition top 5 tools per skill
        top_tools_by_skill: dict[str, list[dict]] = {}
        for t in all_tool_rows or []:
            sn = t["skill_name"]
            if sn not in top_tools_by_skill:
                top_tools_by_skill[sn] = []
            if len(top_tools_by_skill[sn]) < 5:
                top_tools_by_skill[sn].append({"name": t["tool_name"], "count": t["cnt"]})

        skills = []
        for row in skill_rows or []:
            skills.append(
                {
                    "name": row["skill_name"],
                    "invocations": row["invocations"],
                    "avg_tools": int(row["avg_tools"]),
                    "avg_duration_ms": int(row["avg_duration_ms"]),
                    "avg_tokens": {
                        "input": int(row["avg_input_tokens"]),
                        "output": int(row["avg_output_tokens"]),
                    },
                    "feedback_positive": row["feedback_positive"],
                    "feedback_negative": row["feedback_negative"],
                    "top_tools": top_tools_by_skill.get(row["skill_name"], []),
                }
            )

        # Handoff flows
        handoff_rows = db.fetchall(
            "SELECT handoff_from, handoff_to, COUNT(*) as cnt "
            "FROM skill_usage "
            "WHERE handoff_from IS NOT NULL AND handoff_to IS NOT NULL "
            "AND timestamp > NOW() - INTERVAL '1 day' * %s "
            "GROUP BY handoff_from, handoff_to ORDER BY cnt DESC",
            (days,),
        )
        handoffs = [{"from": r["handoff_from"], "to": r["handoff_to"], "count": r["cnt"]} for r in (handoff_rows or [])]

        return {
            "skills": skills,
            "handoffs": handoffs,
            "days": days,
        }
    except Exception:
        logger.debug("Failed to get skill stats", exc_info=True)
        return {"skills": [], "handoffs": [], "days": days}


def get_skill_trend(skill_name: str, days: int = 30) -> dict:
    """Usage trend for a specific skill with sparkline data."""
    try:
        from .db import get_database

        db = get_database()
        rows = db.fetchall(
            "SELECT DATE(timestamp) as day, COUNT(*) as cnt, "
            "COALESCE(ROUND(AVG(duration_ms)), 0) as avg_duration "
            "FROM skill_usage "
            "WHERE skill_name = %s AND timestamp > NOW() - INTERVAL '1 day' * %s "
            "GROUP BY DATE(timestamp) ORDER BY day",
            (skill_name, days),
        )

        if not rows:
            return {"skill": skill_name, "runs": 0}

        return {
            "skill": skill_name,
            "runs": sum(r["cnt"] for r in rows),
            "sparkline": [r["cnt"] for r in rows],
            "duration_sparkline": [int(r["avg_duration"]) for r in rows],
            "days_active": len(rows),
        }
    except Exception:
        logger.debug("Failed to get skill trend", exc_info=True)
        return {"skill": skill_name, "runs": 0}


def get_skill_user_breakdown(skill_name: str, days: int = 30) -> list[dict]:
    """Per-user usage breakdown for a skill."""
    try:
        from .db import get_database

        db = get_database()
        rows = db.fetchall(
            "SELECT user_id, COUNT(*) as invocations, "
            "COALESCE(ROUND(AVG(duration_ms)), 0) as avg_duration_ms "
            "FROM skill_usage "
            "WHERE skill_name = %s AND timestamp > NOW() - INTERVAL '1 day' * %s "
            "GROUP BY user_id ORDER BY invocations DESC LIMIT 20",
            (skill_name, days),
        )
        return [
            {"user": r["user_id"], "invocations": r["invocations"], "avg_duration_ms": int(r["avg_duration_ms"])}
            for r in (rows or [])
        ]
    except Exception:
        logger.debug("Failed to get skill user breakdown", exc_info=True)
        return []


def update_skill_feedback(session_id: str, feedback: str) -> None:
    """Link user feedback to the most recent skill invocation in a session."""
    try:
        from .db import get_database

        db = get_database()
        db.execute(
            "UPDATE skill_usage SET feedback = %s "
            "WHERE id = (SELECT id FROM skill_usage WHERE session_id = %s ORDER BY timestamp DESC LIMIT 1)",
            (feedback, session_id),
        )
        db.commit()
    except Exception:
        logger.debug("Failed to update skill feedback", exc_info=True)
