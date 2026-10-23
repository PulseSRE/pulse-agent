"""Batch learning for the ORCA skill selector — recomputes channel weights from outcomes."""

from __future__ import annotations

import logging

logger = logging.getLogger("pulse_agent.selector_learning")


# Below this many corrections in the window, every logged selection is the
# selector agreeing with itself, and "which channel agreed with the pick" says
# nothing about which channel is right. Leave the weights alone until routing
# has been corrected often enough to tell channels apart.
MIN_OVERRIDES_TO_LEARN = 3
MIN_ROWS_TO_LEARN = 10


def recompute_channel_weights(days: int = 7) -> dict[str, float]:
    """Recompute channel weights from how often each channel named the right skill.

    The right skill for a logged selection is the one the agent reached for
    with skill_load when that differs from the routed one (``skill_overridden``),
    otherwise the routed skill. A channel whose top-scoring skill was the right
    one gains weight; one that pointed elsewhere loses it.

    This used to measure each channel against the fused pick alone, and the
    log it read held only empty channel scores, so it changed nothing. Returns
    the new weights (normalized to sum 1.0), or {} when there is not enough
    evidence to change them.
    """
    try:
        import json

        from .repositories.selector_learning_repo import get_selector_learning_repo
        from .skill_selector import DEFAULT_WEIGHTS

        repo = get_selector_learning_repo()
        rows = repo.fetch_selection_log(days)

        channel_correct: dict[str, int] = {ch: 0 for ch in DEFAULT_WEIGHTS}
        channel_total: dict[str, int] = {ch: 0 for ch in DEFAULT_WEIGHTS}
        usable = 0
        overrides = 0

        for row in rows:
            try:
                raw = row["channel_scores"]
                scores = json.loads(raw) if isinstance(raw, str) else raw
            except (json.JSONDecodeError, TypeError):
                continue
            if not scores:
                continue
            usable += 1
            overridden = row.get("skill_overridden")
            if overridden:
                overrides += 1
            right = overridden or row["selected_skill"]

            for channel_name, skill_scores in scores.items():
                if channel_name not in channel_total or not skill_scores:
                    continue
                # A channel with no evidence (all zeros) did not vote.
                if max(skill_scores.values()) <= 0:
                    continue
                best = max(skill_scores, key=lambda k: skill_scores[k])
                channel_total[channel_name] += 1
                if best == right:
                    channel_correct[channel_name] += 1

        if usable < MIN_ROWS_TO_LEARN or overrides < MIN_OVERRIDES_TO_LEARN:
            logger.info(
                "Not enough routing evidence to relearn weights (%d selections, %d corrections; need %d and %d)",
                usable,
                overrides,
                MIN_ROWS_TO_LEARN,
                MIN_OVERRIDES_TO_LEARN,
            )
            return {}

        learning_rate = 0.1
        new_weights = dict(DEFAULT_WEIGHTS)
        for ch in new_weights:
            if channel_total[ch] > 0:
                precision = channel_correct[ch] / channel_total[ch]
                new_weights[ch] *= 1 + learning_rate * (precision - 0.5)

        total = sum(new_weights.values())
        if total > 0:
            new_weights = {k: round(v / total, 4) for k, v in new_weights.items()}

        logger.info(
            "Recomputed channel weights: %s (from %d selections, %d corrections)", new_weights, usable, overrides
        )
        repo.persist_weights(new_weights)
        return new_weights

    except Exception:
        logger.debug("Weight recomputation failed", exc_info=True)
        return {}


def load_learned_weights() -> dict[str, float] | None:
    """Load the most recently persisted weights from DB.

    Returns None if no learned weights exist.
    """
    try:
        from .repositories.selector_learning_repo import get_selector_learning_repo

        weights = get_selector_learning_repo().load_learned_weights()
        if weights:
            logger.info("Loaded learned weights from DB: %s", weights)
        return weights
    except Exception:
        logger.debug("Failed to load learned weights", exc_info=True)
        return None


def identify_skill_gaps(days: int = 30) -> list[dict]:
    """Find recurring query patterns with no good skill match."""
    try:
        from .repositories.selector_learning_repo import get_selector_learning_repo

        rows = get_selector_learning_repo().fetch_missing_tool_queries(days)

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


def prune_low_performers(days: int = 30, min_invocations: int = 10) -> list[dict]:
    """Identify skills with high override rate.

    Returns dicts with the evidence (``skill``, ``override_rate``, ``overrides``,
    ``total``) so the caller can show a person *why* a skill was flagged rather
    than just its name.
    """
    try:
        from .repositories.selector_learning_repo import get_selector_learning_repo

        rows = get_selector_learning_repo().fetch_override_rates(days, min_invocations)

        flagged = []
        for row in rows:
            override_rate = row["overrides"] / row["total"] if row["total"] > 0 else 0
            if override_rate > 0.3:
                flagged.append(
                    {
                        "skill": row["selected_skill"],
                        "override_rate": round(override_rate, 3),
                        "overrides": row["overrides"],
                        "total": row["total"],
                    }
                )
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
