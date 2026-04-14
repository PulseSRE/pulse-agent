"""Batch learning for the ORCA skill selector — recomputes channel weights from outcomes."""

from __future__ import annotations

import logging

logger = logging.getLogger("pulse_agent.selector_learning")


def recompute_channel_weights(days: int = 7) -> dict[str, float]:
    """Analyze skill_selection_log to recompute optimal channel weights.

    For each logged selection:
    - If skill was NOT overridden and tools were used → positive
    - If skill was overridden → negative
    - If tools_requested_missing is non-empty → partial negative

    Returns new weights dict (normalized to sum=1.0).
    """
    try:
        from .db import get_database

        db = get_database()
        rows = db.fetchall(
            "SELECT channel_scores, selected_skill, skill_overridden, "
            "tools_requested_missing "
            "FROM skill_selection_log "
            "WHERE timestamp > NOW() - INTERVAL '%s days' "
            "AND channel_scores IS NOT NULL",
            (days,),
        )

        if len(rows) < 10:
            logger.info("Not enough data for weight recomputation (%d rows, need 10+)", len(rows))
            return {}

        import json

        from .skill_selector import DEFAULT_WEIGHTS

        # Count correct/incorrect per channel
        channel_correct: dict[str, int] = {ch: 0 for ch in DEFAULT_WEIGHTS}
        channel_total: dict[str, int] = {ch: 0 for ch in DEFAULT_WEIGHTS}

        for row in rows:
            try:
                scores = (
                    json.loads(row["channel_scores"])
                    if isinstance(row["channel_scores"], str)
                    else row["channel_scores"]
                )
            except (json.JSONDecodeError, TypeError):
                continue

            overridden = bool(row.get("skill_overridden"))
            selected = row["selected_skill"]

            for channel_name, skill_scores in scores.items():
                if channel_name not in channel_total:
                    continue
                if not skill_scores:
                    continue

                # Did this channel pick the right skill?
                best_in_channel = max(skill_scores, key=lambda k: skill_scores[k]) if skill_scores else None
                channel_total[channel_name] += 1
                if best_in_channel == selected and not overridden:
                    channel_correct[channel_name] += 1

        # Compute precision per channel
        learning_rate = 0.1
        new_weights = dict(DEFAULT_WEIGHTS)
        for ch in new_weights:
            if channel_total[ch] > 0:
                precision = channel_correct[ch] / channel_total[ch]
                new_weights[ch] *= 1 + learning_rate * (precision - 0.5)

        # Normalize
        total = sum(new_weights.values())
        if total > 0:
            new_weights = {k: round(v / total, 4) for k, v in new_weights.items()}

        logger.info("Recomputed channel weights: %s (from %d samples)", new_weights, len(rows))
        return new_weights

    except Exception:
        logger.debug("Weight recomputation failed", exc_info=True)
        return {}


def identify_skill_gaps(days: int = 30) -> list[dict]:
    """Find recurring query patterns with no good skill match."""
    try:
        from .db import get_database

        db = get_database()
        rows = db.fetchall(
            "SELECT query_summary, selected_skill, threshold_used "
            "FROM skill_selection_log "
            "WHERE timestamp > NOW() - INTERVAL '%s days' "
            "AND (tools_requested_missing IS NOT NULL AND array_length(tools_requested_missing, 1) > 0) "
            "ORDER BY timestamp DESC "
            "LIMIT 50",
            (days,),
        )

        if not rows:
            return []

        # Group by query pattern
        from .tool_predictor import extract_tokens

        pattern_counts: dict[str, int] = {}
        for row in rows:
            tokens = extract_tokens(row["query_summary"])
            key = " ".join(sorted(tokens[:5]))
            pattern_counts[key] = pattern_counts.get(key, 0) + 1

        gaps = [
            {"pattern": pattern, "occurrences": count}
            for pattern, count in sorted(pattern_counts.items(), key=lambda x: -x[1])
            if count >= 3
        ]

        return gaps

    except Exception:
        logger.debug("Skill gap identification failed", exc_info=True)
        return []


def prune_low_performers(days: int = 30, min_invocations: int = 10) -> list[str]:
    """Identify skills with high override rate."""
    try:
        from .db import get_database

        db = get_database()
        rows = db.fetchall(
            "SELECT selected_skill, "
            "COUNT(*) as total, "
            "SUM(CASE WHEN skill_overridden IS NOT NULL THEN 1 ELSE 0 END) as overrides "
            "FROM skill_selection_log "
            "WHERE timestamp > NOW() - INTERVAL '%s days' "
            "GROUP BY selected_skill "
            "HAVING COUNT(*) >= %s",
            (days, min_invocations),
        )

        flagged = []
        for row in rows:
            override_rate = row["overrides"] / row["total"] if row["total"] > 0 else 0
            if override_rate > 0.3:
                flagged.append(row["selected_skill"])
                logger.info(
                    "Flagged skill '%s': %.0f%% override rate (%d/%d)",
                    row["selected_skill"],
                    override_rate * 100,
                    row["overrides"],
                    row["total"],
                )

        return flagged

    except Exception:
        logger.debug("Skill pruning failed", exc_info=True)
        return []
