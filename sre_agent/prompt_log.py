"""Prompt logging — fire-and-forget recording of system prompts sent to Claude.

Tracks prompt content hashes, token costs, and section breakdowns for
version tracking and cost analysis.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re

logger = logging.getLogger("pulse_agent.prompt_log")

# Known section markers in the static prompt for section measurement
_SECTION_MARKERS = [
    "## Intent Analysis",
    "## Component Catalog",
    "## Self-Awareness",
    "## Security",
]


def _measure_sections(static_prompt: str) -> dict[str, int]:
    """Split static_prompt by known markers and measure chars of each section.

    Returns a dict mapping section name to character count.
    Unmatched content before the first marker is labeled 'base_prompt'.
    """
    sections: dict[str, int] = {}

    # Find all marker positions
    positions: list[tuple[int, str]] = []
    for marker in _SECTION_MARKERS:
        idx = static_prompt.find(marker)
        if idx >= 0:
            positions.append((idx, marker))

    positions.sort(key=lambda x: x[0])

    if not positions:
        sections["base_prompt"] = len(static_prompt)
        return sections

    # Content before first marker
    first_pos = positions[0][0]
    if first_pos > 0:
        sections["base_prompt"] = first_pos

    # Each marker section runs until the next marker (or end of string)
    for i, (pos, marker) in enumerate(positions):
        # Normalize marker to a key: "## Intent Analysis" -> "intent_analysis"
        key = re.sub(r"^#+\s*", "", marker).strip().lower().replace(" ", "_")
        end = positions[i + 1][0] if i + 1 < len(positions) else len(static_prompt)
        sections[key] = end - pos

    return sections


def record_prompt(
    *,
    session_id: str,
    turn_number: int,
    skill_name: str,
    skill_version: int,
    static: str,
    dynamic: str,
    token_usage: dict[str, int] | None = None,
) -> None:
    """Fire-and-forget prompt log recording.

    Hashes the full prompt for version tracking, measures section sizes,
    and INSERTs into the prompt_log table.
    """
    try:
        from .db import get_database

        db = get_database()

        combined = static + dynamic
        prompt_hash = hashlib.sha256(combined.encode("utf-8")).hexdigest()[:16]
        sections = _measure_sections(static)

        usage = token_usage or {}
        total_tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)

        db.execute(
            "INSERT INTO prompt_log "
            "(session_id, turn_number, skill_name, skill_version, prompt_hash, "
            "static_chars, dynamic_chars, total_tokens, sections, "
            "input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                session_id,
                turn_number,
                skill_name,
                skill_version,
                prompt_hash,
                len(static),
                len(dynamic),
                total_tokens,
                json.dumps(sections),
                usage.get("input_tokens"),
                usage.get("output_tokens"),
                usage.get("cache_read_tokens"),
                usage.get("cache_creation_tokens"),
            ),
        )
        db.commit()
        logger.debug(
            "Recorded prompt log: skill=%s hash=%s static=%d dynamic=%d tokens=%d",
            skill_name,
            prompt_hash,
            len(static),
            len(dynamic),
            total_tokens,
        )
    except Exception as e:
        logger.debug("Failed to record prompt log: %s", e)


def get_prompt_stats(days: int = 30) -> dict:
    """Aggregate prompt stats: avg tokens by skill, cache hit rate, section breakdown.

    Returns dict with total_prompts, avg_tokens, by_skill, cache_hit_rate, section_avg.
    """
    try:
        from .db import get_database

        db = get_database()

        # Overall stats
        overall = db.fetchone(
            "SELECT COUNT(*) AS total, "
            "COALESCE(ROUND(AVG(total_tokens)), 0) AS avg_tokens, "
            "COALESCE(ROUND(AVG(static_chars)), 0) AS avg_static, "
            "COALESCE(ROUND(AVG(dynamic_chars)), 0) AS avg_dynamic "
            "FROM prompt_log "
            "WHERE timestamp > NOW() - INTERVAL '1 day' * %s",
            (days,),
        )

        # By skill
        by_skill_rows = db.fetchall(
            "SELECT skill_name, COUNT(*) AS count, "
            "COALESCE(ROUND(AVG(total_tokens)), 0) AS avg_tokens, "
            "COALESCE(ROUND(AVG(static_chars)), 0) AS avg_static, "
            "COALESCE(ROUND(AVG(dynamic_chars)), 0) AS avg_dynamic, "
            "COUNT(DISTINCT prompt_hash) AS prompt_versions "
            "FROM prompt_log "
            "WHERE timestamp > NOW() - INTERVAL '1 day' * %s "
            "GROUP BY skill_name ORDER BY count DESC",
            (days,),
        )

        # Cache hit rate (turns with cache_read_tokens > 0)
        cache_row = db.fetchone(
            "SELECT "
            "COUNT(*) FILTER (WHERE cache_read_tokens > 0) AS cache_hits, "
            "COUNT(*) AS total "
            "FROM prompt_log "
            "WHERE timestamp > NOW() - INTERVAL '1 day' * %s "
            "AND input_tokens IS NOT NULL",
            (days,),
        )
        cache_total = cache_row["total"] if cache_row else 0
        cache_hits = cache_row["cache_hits"] if cache_row else 0
        cache_hit_rate = cache_hits / cache_total if cache_total > 0 else 0.0

        # Section averages — aggregate across all prompts
        section_avg: dict[str, float] = {}
        try:
            section_rows = db.fetchall(
                "SELECT sections FROM prompt_log "
                "WHERE timestamp > NOW() - INTERVAL '1 day' * %s "
                "AND sections IS NOT NULL",
                (days,),
            )
            section_counts: dict[str, list[int]] = {}
            for row in section_rows or []:
                secs = row["sections"]
                if isinstance(secs, str):
                    secs = json.loads(secs)
                if isinstance(secs, dict):
                    for k, v in secs.items():
                        section_counts.setdefault(k, []).append(v)
            section_avg = {k: sum(v) / len(v) for k, v in section_counts.items() if v}
        except Exception:
            logger.debug("Failed to compute section token averages", exc_info=True)

        # Skill names for the versions picker
        skill_names = [row["skill_name"] for row in by_skill_rows] if by_skill_rows else []

        return {
            "total_prompts": overall["total"] if overall else 0,
            "avg_tokens": int(overall["avg_tokens"]) if overall else 0,
            "avg_static_chars": int(overall["avg_static"]) if overall else 0,
            "avg_dynamic_chars": int(overall["avg_dynamic"]) if overall else 0,
            "by_skill": [dict(row) for row in by_skill_rows],
            "cache_hit_rate": round(cache_hit_rate, 3),
            "section_avg": section_avg,
            "skill_names": skill_names,
            "days": days,
        }
    except Exception:
        logger.debug("Failed to get prompt stats", exc_info=True)
        return {
            "total_prompts": 0,
            "avg_tokens": 0,
            "avg_static_chars": 0,
            "avg_dynamic_chars": 0,
            "by_skill": [],
            "cache_hit_rate": 0.0,
            "days": days,
        }


def get_prompt_versions(skill_name: str, days: int = 30) -> list[dict]:
    """Track prompt_hash changes over time with enriched metadata.

    Returns list of dicts with human-readable version info: token costs,
    size metrics, section breakdown, and active duration.
    """
    try:
        from .db import get_database

        db = get_database()

        rows = db.fetchall(
            "SELECT prompt_hash, "
            "COUNT(*) AS count, "
            "MIN(timestamp) AS first_seen, "
            "MAX(timestamp) AS last_seen, "
            "MAX(skill_version) AS skill_version, "
            "COALESCE(ROUND(AVG(total_tokens)), 0) AS avg_tokens, "
            "COALESCE(ROUND(AVG(input_tokens)), 0) AS avg_input_tokens, "
            "COALESCE(ROUND(AVG(output_tokens)), 0) AS avg_output_tokens, "
            "COALESCE(ROUND(AVG(cache_read_tokens)), 0) AS avg_cache_read, "
            "MAX(static_chars) AS static_chars, "
            "COALESCE(ROUND(AVG(dynamic_chars)), 0) AS avg_dynamic_chars "
            "FROM prompt_log "
            "WHERE skill_name = %s "
            "AND timestamp > NOW() - INTERVAL '1 day' * %s "
            "GROUP BY prompt_hash "
            "ORDER BY MIN(timestamp) DESC",
            (skill_name, days),
        )

        # Get section breakdown for each hash (use most recent entry per hash)
        section_rows = db.fetchall(
            "SELECT DISTINCT ON (prompt_hash) prompt_hash, sections "
            "FROM prompt_log "
            "WHERE skill_name = %s "
            "AND timestamp > NOW() - INTERVAL '1 day' * %s "
            "AND sections IS NOT NULL "
            "ORDER BY prompt_hash, timestamp DESC",
            (skill_name, days),
        )
        sections_by_hash: dict[str, dict] = {}
        for sr in section_rows or []:
            secs = sr["sections"]
            if isinstance(secs, str):
                secs = json.loads(secs)
            if isinstance(secs, dict):
                sections_by_hash[sr["prompt_hash"]] = secs

        results = []
        for i, row in enumerate(rows):
            first = row["first_seen"]
            last = row["last_seen"]
            duration_days = (last - first).days if first and last else 0

            entry: dict = {
                "prompt_hash": row["prompt_hash"],
                "label": f"v{len(rows) - i}",
                "count": row["count"],
                "first_seen": first.isoformat() if first else None,
                "last_seen": last.isoformat() if last else None,
                "duration_days": duration_days,
                "skill_version": row["skill_version"],
                "avg_tokens": int(row["avg_tokens"]),
                "avg_input_tokens": int(row["avg_input_tokens"]),
                "avg_output_tokens": int(row["avg_output_tokens"]),
                "avg_cache_read": int(row["avg_cache_read"]),
                "static_chars": int(row["static_chars"]) if row["static_chars"] else 0,
                "avg_dynamic_chars": int(row["avg_dynamic_chars"]),
                "is_current": i == 0,
            }

            secs = sections_by_hash.get(row["prompt_hash"])
            if secs:
                entry["sections"] = secs
                entry["total_static_chars"] = sum(secs.values())

            results.append(entry)

        return results
    except Exception:
        logger.debug("Failed to get prompt versions", exc_info=True)
        return []


def get_prompt_log(session_id: str) -> list[dict]:
    """Get prompt log entries for a session.

    Returns list of dicts with all prompt_log columns.
    """
    try:
        from .db import get_database

        db = get_database()

        rows = db.fetchall(
            "SELECT id, timestamp, session_id, turn_number, skill_name, skill_version, "
            "prompt_hash, static_chars, dynamic_chars, total_tokens, sections, "
            "input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens "
            "FROM prompt_log "
            "WHERE session_id = %s "
            "ORDER BY turn_number ASC",
            (session_id,),
        )

        results = []
        for row in rows:
            entry = dict(row)
            if entry["timestamp"]:
                entry["timestamp"] = entry["timestamp"].isoformat()
            if entry["sections"] and isinstance(entry["sections"], str):
                try:
                    entry["sections"] = json.loads(entry["sections"])
                except (json.JSONDecodeError, TypeError):
                    logger.debug("Failed to parse sections JSON in prompt log entry", exc_info=True)
            results.append(entry)

        return results
    except Exception:
        logger.debug("Failed to get prompt log", exc_info=True)
        return []
