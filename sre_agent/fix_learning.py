"""Which fix holds for which cause — learned from verification verdicts.

The fix planner mapped each root cause to one strategy and kept applying it
however often it failed. Restarting a crashlooping pod whose fix comes undone
ten minutes later was "verified", then "recurred", then tried again on the next
pod with the same cause, indefinitely: each resource has an attempt cap, but
nothing carried the lesson from one resource to the next.

This module keeps a ledger of verdicts per applied fix (``fix_outcomes``,
migration 039) and lets that track record steer the plan:

- When a cause has an alternative strategy that is safe to swap in, and the
  default's record is clearly worse, the alternative is chosen instead.
- When the chosen strategy's own record is poor, the plan is escalated to a
  person rather than repeated. Not applying a fix that does not hold is the
  most valuable thing this can learn.

The record is a simple success/failure count with a prior: untried
alternatives start neutral, the reviewed default starts slightly favoured, and
nothing changes on fewer than ``MIN_EVIDENCE`` verdicts. Every steer is
explained in the plan's description, so a person sees why.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger("pulse_agent.fix_learning")

HUMAN_REVIEW = "require_human_review"

# Strategies that can stand in for one another for a cause. Only pairs whose
# executors take the same inputs (the target resource) and are both reviewed
# remedies for that cause belong here. A crashloop without a diagnosis has no
# safe alternative to a restart: doubling memory blind is not a fix.
ALTERNATIVES: dict[str, list[str]] = {
    "oom": ["patch_resources", "restart_controller"],
}

# Beta-style priors (successes, failures). The default strategy was reviewed
# by a person for this cause, so it starts ahead of an untried alternative.
DEFAULT_PRIOR = (2.0, 1.0)
ALTERNATIVE_PRIOR = (1.0, 1.0)

MIN_EVIDENCE = 3  # verdicts on a strategy before its record can escalate it
ESCALATE_BELOW = 0.4  # expected hold rate under which a person decides instead
SWITCH_MARGIN = 0.1  # an alternative must be this much better to replace the default
# Verdicts older than this stop counting. Without a horizon an escalated cause
# would stay escalated for good: no fix is applied, so no new verdict arrives.
WINDOW_DAYS = 30

HELD = "held"
FAILED = "failed"
RECURRED = "recurred"
# A person declined the proposal. The fix never ran, so this says nothing about
# whether it would have held — but it is the operator saying "not this fix for
# this cause", and a strategy people keep declining should stop being proposed
# as an automated fix the same way one that keeps failing does. It counts as
# "did not hold" in the record and is named as a rejection in the explanation.
REJECTED = "rejected"

# Verification statuses that are a verdict on the fix. "unverifiable" and
# "improved" say nothing definite about whether the fix held.
_VERDICT_FOR_STATUS = {"verified": HELD, "still_failing": FAILED, "rejected": REJECTED}


def _db():
    from .db import get_database

    return get_database()


def _now_ms() -> int:
    return int(time.time() * 1000)


def record_verdict(action_id: str, cause: str, strategy: str, status: str) -> bool:
    """Record what verification concluded about one applied fix. True if it was written."""
    verdict = _VERDICT_FOR_STATUS.get(status)
    if not verdict or not action_id or not cause or not strategy or strategy == HUMAN_REVIEW:
        return False
    try:
        db = _db()
        db.execute(
            "INSERT INTO fix_outcomes (action_id, cause, strategy, verdict, recorded_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (action_id) DO UPDATE SET verdict = EXCLUDED.verdict, recorded_at = EXCLUDED.recorded_at",
            (action_id, cause, strategy, verdict, _now_ms()),
        )
        db.commit()
        return True
    except Exception:
        logger.debug("Failed to record fix verdict for %s", action_id, exc_info=True)
        return False


def mark_recurred(action_id: str) -> None:
    """A fix verified as holding did not: the condition came back."""
    try:
        db = _db()
        db.execute(
            "UPDATE fix_outcomes SET verdict = ?, recorded_at = ? WHERE action_id = ?",
            (RECURRED, _now_ms(), action_id),
        )
        db.commit()
    except Exception:
        logger.debug("Failed to mark fix %s recurred", action_id, exc_info=True)


def record_rejection(action_id: str, cause: str, strategy: str, correlation_key: str = "") -> bool:
    """Record that a person declined a proposed fix. True only if a verdict was written.

    A proposal made before the cause was stored with it has no cause to file
    the rejection under; guessing one would teach the wrong lesson.

    At most one rejection counts per condition (``correlation_key``) per
    strategy and cause within the window. A person saying no to the same fix
    for the same workload again is one opinion about that workload, not
    evidence about the strategy everywhere: without this, re-rejecting one
    noisy workload two or three times would switch or escalate the cause for
    the whole cluster.
    """
    if not action_id or not cause or not strategy or strategy == HUMAN_REVIEW:
        return False
    if correlation_key and _already_rejected_here(action_id, cause, strategy, correlation_key):
        logger.info("Rejection of %s not counted again for %s (%s/%s)", action_id, correlation_key, cause, strategy)
        return False
    return record_verdict(action_id, cause, strategy, "rejected")


def _already_rejected_here(action_id: str, cause: str, strategy: str, correlation_key: str) -> bool:
    try:
        row = _db().fetchone(
            "SELECT 1 AS hit FROM fix_outcomes fo JOIN actions a ON a.id = fo.action_id "
            "WHERE a.correlation_key = ? AND fo.verdict = ? AND fo.cause = ? AND fo.strategy = ? "
            "AND fo.recorded_at >= ? AND fo.action_id <> ? LIMIT 1",
            (correlation_key, REJECTED, cause, strategy, _now_ms() - WINDOW_DAYS * 86_400_000, action_id),
        )
    except Exception:
        logger.debug("Could not check earlier rejections for %s", correlation_key, exc_info=True)
        return False
    return row is not None


def _counts(cause: str) -> dict[str, tuple[int, int, int]]:
    """``{strategy: (held, did_not_hold, of_which_rejected)}`` for one cause."""
    try:
        rows = _db().fetchall(
            "SELECT strategy, "
            "SUM(CASE WHEN verdict = 'held' THEN 1 ELSE 0 END) AS held, "
            "SUM(CASE WHEN verdict <> 'held' THEN 1 ELSE 0 END) AS lost, "
            "SUM(CASE WHEN verdict = 'rejected' THEN 1 ELSE 0 END) AS rejected "
            "FROM fix_outcomes WHERE cause = ? AND recorded_at >= ? GROUP BY strategy",
            (cause, _now_ms() - WINDOW_DAYS * 86_400_000),
        )
    except Exception:
        logger.debug("Failed to read fix track record for %s", cause, exc_info=True)
        return {}
    return {r["strategy"]: (int(r["held"] or 0), int(r["lost"] or 0), int(r["rejected"] or 0)) for r in rows or []}


def track_record(cause: str) -> dict[str, tuple[int, int]]:
    """``{strategy: (held, did_not_hold)}`` for one cause (rejections count as not held)."""
    return {strategy: (held, lost) for strategy, (held, lost, _) in _counts(cause).items()}


def rejections(cause: str) -> dict[str, int]:
    """``{strategy: times a person declined it}`` for one cause, within the window."""
    return {strategy: rejected for strategy, (_, _, rejected) in _counts(cause).items() if rejected}


def _expected(record: dict[str, tuple[int, int]], strategy: str, default: str) -> tuple[float, int]:
    held, lost = record.get(strategy, (0, 0))
    a, b = DEFAULT_PRIOR if strategy == default else ALTERNATIVE_PRIOR
    return (held + a) / (held + lost + a + b), held + lost


def _describe(strategy: str, record: dict[str, tuple[int, int]], rejected: dict[str, int] | None = None) -> str:
    held, lost = record.get(strategy, (0, 0))
    text = f"{strategy} held {held} of {held + lost}"
    declined = (rejected or {}).get(strategy, 0)
    if declined:
        text += f" ({declined} declined by a person)"
    return text


def steer(plan):
    """Adjust a FixPlan by what has held for its cause. Returns the same plan."""
    if plan is None or plan.strategy == HUMAN_REVIEW:
        return plan
    cause = plan.cause_category
    counts = _counts(cause)
    if not counts:
        return plan
    record = {strategy: (held, lost) for strategy, (held, lost, _) in counts.items()}
    rejected = {strategy: r for strategy, (_, _, r) in counts.items() if r}

    default = plan.strategy
    candidates = [default] + [s for s in ALTERNATIVES.get(cause, []) if s != default]
    best = max(candidates, key=lambda s: _expected(record, s, default)[0])
    default_score, _ = _expected(record, default, default)
    best_score, _ = _expected(record, best, default)

    reasons: list[str] = []
    if best != default and best_score >= default_score + SWITCH_MARGIN:
        reasons.append(f"switched from {default}: {_describe(default, record, rejected)} for {cause}")
        plan.strategy = best
        if record.get(best):
            reasons.append(_describe(best, record, rejected))
        else:
            reasons.append(f"{best} not tried yet for {cause}")

    chosen_score, chosen_n = _expected(record, plan.strategy, default)
    if chosen_n >= MIN_EVIDENCE and chosen_score < ESCALATE_BELOW:
        reasons.append(f"escalated to a person: {_describe(plan.strategy, record, rejected)} for {cause}")
        plan.strategy = HUMAN_REVIEW
    elif not reasons and chosen_n:
        reasons.append(f"track record: {_describe(plan.strategy, record, rejected)} for {cause}")

    if reasons:
        note = "; ".join(reasons)
        plan.params["track_record"] = note
        plan.description = f"{plan.description} [learned: {note}]"
        if plan.strategy != default:
            logger.info("Fix plan steered by track record: %s", note)
    return plan
