"""Eval run history — persists eval results to DB for trend tracking.

Records every eval suite run (CLI, API, CI) and provides query functions
for the UI to show score trends over time.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger("pulse_agent.evals.history")


def record_eval_run(
    *,
    suite_name: str,
    source: str = "cli",
    model: str = "",
    scenario_count: int,
    passed_count: int,
    gate_passed: bool,
    average_overall: float,
    dimensions: dict | None = None,
    blocker_counts: dict | None = None,
    scenarios: list[dict] | None = None,
    prompt_audit: dict | None = None,
    judge_avg: float | None = None,
) -> None:
    """Record an eval run to the database. Fire-and-forget."""
    try:
        from ..db import get_database

        db = get_database()
        db.execute(
            "INSERT INTO eval_runs "
            "(suite_name, source, model, scenario_count, passed_count, gate_passed, "
            "average_overall, dimensions, blocker_counts, scenarios, prompt_audit, judge_avg) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                suite_name,
                source,
                model,
                scenario_count,
                passed_count,
                gate_passed,
                average_overall,
                json.dumps(dimensions) if dimensions else None,
                json.dumps(blocker_counts) if blocker_counts else None,
                json.dumps(scenarios) if scenarios else None,
                json.dumps(prompt_audit) if prompt_audit else None,
                judge_avg,
            ),
        )
        db.commit()
    except Exception:
        logger.debug("Failed to record eval run: %s", suite_name, exc_info=True)


def get_eval_history(
    *,
    suite_name: str | None = None,
    days: int = 30,
    limit: int = 100,
) -> list[dict]:
    """Query eval run history for trend display."""
    try:
        from ..db import get_database

        db = get_database()
        where_parts: list[str] = ["timestamp > NOW() - INTERVAL '1 day' * %s"]
        params: list = [days]

        if suite_name:
            where_parts.append("suite_name = %s")
            params.append(suite_name)

        where = "WHERE " + " AND ".join(where_parts)

        rows = db.fetchall(
            f"SELECT id, timestamp, suite_name, source, model, scenario_count, "
            f"passed_count, gate_passed, average_overall, dimensions, "
            f"blocker_counts, judge_avg "
            f"FROM eval_runs {where} "
            f"ORDER BY timestamp DESC LIMIT %s",
            tuple(params + [limit]),
        )

        results = []
        for row in rows:
            entry = dict(row)
            if entry.get("timestamp"):
                entry["timestamp"] = (
                    entry["timestamp"].isoformat()
                    if hasattr(entry["timestamp"], "isoformat")
                    else str(entry["timestamp"])
                )
            # Parse JSONB fields
            for field in ("dimensions", "blocker_counts"):
                if isinstance(entry.get(field), str):
                    try:
                        entry[field] = json.loads(entry[field])
                    except (ValueError, TypeError):
                        logger.debug("Failed to parse JSONB field '%s' in eval history entry", field)
            results.append(entry)
        return results
    except Exception:
        logger.debug("Failed to query eval history", exc_info=True)
        return []


def get_eval_trend(
    *,
    suite_name: str = "release",
    days: int = 30,
) -> dict:
    """Get trend summary for a suite — latest score, previous score, delta."""
    try:
        from ..db import get_database

        db = get_database()
        rows = db.fetchall(
            "SELECT timestamp, average_overall, gate_passed, judge_avg "
            "FROM eval_runs "
            "WHERE suite_name = %s AND timestamp > NOW() - INTERVAL '1 day' * %s "
            "ORDER BY timestamp DESC LIMIT 50",
            (suite_name, days),
        )
        if not rows:
            return {"suite": suite_name, "runs": 0}

        latest = rows[0]
        previous = rows[1] if len(rows) > 1 else None

        result = {
            "suite": suite_name,
            "runs": len(rows),
            "latest_score": latest["average_overall"],
            "latest_gate": latest["gate_passed"],
            "latest_judge": latest.get("judge_avg"),
            "latest_ts": (
                latest["timestamp"].isoformat()
                if hasattr(latest["timestamp"], "isoformat")
                else str(latest["timestamp"])
            ),
        }

        if previous:
            delta = round(latest["average_overall"] - previous["average_overall"], 4)
            result["previous_score"] = previous["average_overall"]
            result["delta"] = delta
            result["trend"] = "up" if delta > 0.01 else "down" if delta < -0.01 else "stable"
        else:
            result["trend"] = "new"

        # Sparkline data (last N scores, oldest first)
        result["sparkline"] = [round(r["average_overall"], 4) for r in reversed(rows)]

        return result
    except Exception:
        logger.debug("Failed to compute eval trend", exc_info=True)
        return {"suite": suite_name, "runs": 0}
