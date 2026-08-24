"""A verification verdict has a time horizon — this module enforces it.

The verification pipeline pronounces a fix "verified" when the finding is gone
and the health gate passes on the next scan. That verdict is true about that
scan and silently assumed to be true forever: the action row keeps saying
verified, the memory keeps the incident as confirmed, and the trajectory
learner has already promoted the diagnosis into a skill. Observed live on
dev05: a crashlooping pod was deleted, the ReplicaSet came back 2/2, the
runbook was learned — and the same pod crashlooped again eight minutes later.
The fix treated a symptom, and every layer of learning recorded it as a cure.

This module runs on each scan over the findings that are NEW this cycle. A new
finding whose correlation key matches an action verified inside the recurrence
window is the same condition coming back, not a fresh incident. When that
happens:

- the action row's verdict is downgraded to ``verified_then_recurred`` and its
  outcome set to ``recurred``, so fix history and calibration stop counting it
  as a success;
- the promoted trajectory for that condition is demoted, so learning history
  says the lesson is dubious;
- the memory's confirmed incident is walked back and a low-score anti-pattern
  recorded in its place;
- if a scaffolded skill was deepened on that case, an inbox item asks a person
  to review it.

Beyond the window, a returning condition is treated as a new incident: every
fix would eventually "recur" on a long enough horizon, and a verdict that can
be revoked forever is as useless as one that cannot be revoked at all.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from ..config import get_settings
from ..repositories.monitor_repo import get_monitor_repo
from .findings import _ts

if TYPE_CHECKING:
    from .cluster_monitor import ClusterMonitor

logger = logging.getLogger("pulse_agent.monitor")

RECURRED = "verified_then_recurred"


def _find_recurred_actions(finding: dict) -> tuple[str, list[dict]]:
    """Verified actions this new finding contradicts. Sync — DB only."""
    from ..inbox import _finding_corr_key

    corr_key = _finding_corr_key(finding)
    if not corr_key:
        return "", []
    window_s = int(get_settings().monitor.recurrence_window)
    if window_s <= 0:
        return corr_key, []
    since_ms = _ts() - window_s * 1000
    return corr_key, get_monitor_repo().find_recent_verified_actions(corr_key, since_ms)


def _downgrade_action(action_row: dict, finding: dict) -> str:
    """Rewrite one verdict. Returns the new evidence text. Sync — DB only."""
    repo = get_monitor_repo()
    verified_at = int(action_row.get("verification_timestamp") or 0)
    minutes = max(0, (_ts() - verified_at) // 60000) if verified_at else 0
    evidence = (
        f"{action_row.get('verification_evidence') or 'Verified on next scan'}"
        f" — RECURRED: the same condition returned {minutes} min after verification"
        f" ({finding.get('title', finding.get('category', ''))[:120]})"
    )
    repo.update_action_verification(action_row["id"], RECURRED, evidence, _ts())
    repo.update_action_outcome(action_row["id"], "recurred")
    return evidence


def _feed_learning(finding: dict, recurred_tool: str, minutes: int) -> None:
    """Retract what each learning layer took from the recurred verdict. Sync."""
    category = str(finding.get("category", ""))
    resources = finding.get("resources", [])
    detail = (
        f"Auto-fix ({recurred_tool or category}) for '{finding.get('title', category)}' "
        f"verified healthy, then the same condition returned {minutes} min later."
    )

    # Trajectory: the promotion already happened on the earlier scan; demote it.
    try:
        from ..trajectory import candidate_key, get_learner

        get_learner().mark_recurred(candidate_key(category, resources), detail)
    except Exception:
        logger.debug("Trajectory demotion on recurrence failed", exc_info=True)

    # Memory: walk back the confirmed incident, record the anti-pattern.
    if get_settings().agent.memory:
        try:
            from ..memory import get_manager

            manager = get_manager()
            if manager:
                namespace = ""
                resource_type = ""
                if resources:
                    namespace = resources[0].get("namespace", "")
                    resource_type = str(resources[0].get("kind", "")).lower()
                result = manager.record_fix_regression(
                    category=category,
                    tool=recurred_tool,
                    namespace=namespace,
                    resource_type=resource_type,
                    recurrence_minutes=minutes,
                )
                logger.info("Fix regression recorded for %s: %s", category, result)
        except Exception:
            logger.warning("Failed to record fix regression in memory", exc_info=True)

    # Skill: if a scaffolded skill was deepened on this case, ask for review.
    try:
        from ..skill_lifecycle import note_recurrence

        note_recurrence(category, detail)
    except Exception:
        logger.debug("Skill recurrence note failed", exc_info=True)


async def process_recurrences(monitor: ClusterMonitor, new_findings: list[dict]) -> None:
    """Downgrade verified verdicts contradicted by this scan's new findings.

    Idempotent across scans: a downgraded row's verification_status is no
    longer ``verified``, so it cannot match a second time.
    """
    for finding in new_findings:
        try:
            corr_key, rows = await asyncio.to_thread(_find_recurred_actions, finding)
        except Exception:
            logger.debug("Recurrence lookup failed for a finding", exc_info=True)
            continue
        if not rows:
            continue

        recurred_tool = ""
        newest_verified = 0
        for row in rows:
            try:
                evidence = await asyncio.to_thread(_downgrade_action, row, finding)
            except Exception:
                logger.warning("Failed to downgrade recurred action %s", row.get("id"), exc_info=True)
                continue
            verified_at = int(row.get("verification_timestamp") or 0)
            if verified_at >= newest_verified:
                newest_verified = verified_at
                recurred_tool = str(row.get("tool") or "")
            await monitor._broadcast_raw(
                {
                    "type": "verification_report",
                    "id": f"v-{uuid.uuid4().hex[:12]}",
                    "actionId": row["id"],
                    "findingId": finding.get("id", ""),
                    "status": RECURRED,
                    "evidence": evidence,
                    "timestamp": _ts(),
                }
            )
            logger.info(
                "Verification downgraded to %s: action=%s condition=%s",
                RECURRED,
                row["id"],
                corr_key,
            )

        minutes = max(0, (_ts() - newest_verified) // 60000) if newest_verified else 0
        # One retraction per condition, keyed off the most recent verdict —
        # the learning layers stored one lesson, not one per action row.
        await asyncio.to_thread(_feed_learning, finding, recurred_tool, minutes)
