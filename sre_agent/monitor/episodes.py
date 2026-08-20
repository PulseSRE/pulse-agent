"""Episodes — one event, with a cause, rather than N equal things that are wrong.

An episode opens when a finding appears that is capable of explaining others
(see ``layers``). Findings that appear at or after that moment, from a layer
the cause can explain, attach as symptoms rather than standing on their own.

Two rules do most of the work, and both exist to stop over-attachment:

  * A symptom must have been *first seen* at or after the episode started. A
    pod that was already crashlooping an hour before etcd wobbled was not
    caused by etcd, and saying so would be worse than saying nothing.
  * A cause may only absorb layers strictly beneath it. Same-layer siblings
    are burst correlation's job, which already exists.

When the cause clears, the episode closes and its symptoms are released — they
have to stand on their own again, because if they are still failing after the
cause is gone then they were never only symptoms.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from .layers import can_explain, can_head_episode, layer_of

logger = logging.getLogger("pulse_agent.monitor")

OPEN_STATUS = "open"
CLOSED_STATUS = "closed"

# How far back a symptom may have been first seen and still count as caused by
# the episode. Slightly generous: scan cycles are 60s and a cause is often
# detected a cycle or two after the damage starts.
_ATTACH_GRACE_SECONDS = 180

# Two episodes with the same cause inside this window are the same recurring
# problem rather than unrelated events.
_RECURRENCE_WINDOW_SECONDS = 24 * 3600


def _now() -> int:
    return int(time.time())


def _repo():
    from ..repositories.episode_repo import get_episode_repo

    return get_episode_repo()


def _cause_key(finding: dict[str, Any]) -> str:
    """Stable identity for the *condition*, so a re-detected cause reuses its episode."""
    from ..inbox import _finding_corr_key

    return _finding_corr_key(finding)


def open_or_touch(finding: dict[str, Any]) -> str | None:
    """Open an episode for a cause-capable finding, or mark an existing one live.

    Returns the episode id, or None if this finding cannot head an episode.
    """
    category = finding.get("category", "")
    if not can_head_episode(category, finding.get("findingType", "current")):
        return None

    key = _cause_key(finding)
    if not key:
        return None

    repo = _repo()
    now = _now()
    existing = repo.find_open_by_correlation(key)
    if existing:
        repo.touch(existing["id"], now)
        return existing["id"]

    prior = repo.find_recent_closed_by_correlation(key, now - _RECURRENCE_WINDOW_SECONDS)
    episode_id = f"ep-{uuid.uuid4().hex[:12]}"
    repo.create(
        episode_id=episode_id,
        cause_category=category,
        cause_title=finding.get("title", "")[:400],
        cause_finding_id=finding.get("id", ""),
        cause_layer=layer_of(category),
        started_at=now,
        correlation_key=key,
        recurrence_of=prior["id"] if prior else None,
    )
    logger.info(
        "Episode %s opened: %s%s",
        episode_id,
        finding.get("title", "")[:60],
        " (recurrence)" if prior else "",
    )
    return episode_id


def attach_symptoms(episode_id: str, cause_category: str, findings: list[dict], first_seen: dict[str, int]) -> int:
    """Attach findings this episode can explain. Returns how many were attached.

    ``first_seen`` maps a finding's correlation key to when the monitor first
    saw that condition, which is the only way to tell a symptom from something
    that was already broken.
    """
    repo = _repo()
    episode = repo.get(episode_id)
    if not episode or episode["status"] != OPEN_STATUS:
        return 0

    started = int(episode["started_at"])
    cutoff = started - _ATTACH_GRACE_SECONDS
    detached = repo.detached_keys(episode_id)
    attached = 0

    for finding in findings:
        category = finding.get("category", "")
        if not can_explain(cause_category, category):
            continue
        key = _cause_key(finding)
        if not key or key in detached:
            # An operator already said this one was not related. Never re-attach.
            continue
        if first_seen.get(key, started) < cutoff:
            # Already broken before the cause appeared.
            continue
        resources = finding.get("resources") or []
        namespace = resources[0].get("namespace", "") if resources else ""
        if repo.attach(episode_id, key, category, finding.get("title", "")[:400], namespace, _now()):
            attached += 1

    if attached:
        repo.refresh_rollup(episode_id)
    return attached


def close(episode_id: str, reason: str = "cause cleared") -> None:
    """Close an episode and release its symptoms to stand on their own."""
    repo = _repo()
    repo.close(episode_id, _now())
    logger.info("Episode %s closed: %s", episode_id, reason)


def close_for_correlation(correlation_key: str) -> bool:
    """Close whatever open episode this cause heads. True if one was closed."""
    episode = _repo().find_open_by_correlation(correlation_key)
    if not episode:
        return False
    close(episode["id"])
    return True


def dismiss(episode_id: str, actor: str) -> bool:
    """Close an episode because an operator says it is over.

    If the cause genuinely re-fires later it opens a *new* episode with
    recurrence_of set, rather than staying dismissed. Silently suppressing a
    live problem forever is how alerting systems lose the people using them.
    """
    if not _repo().dismiss(episode_id, actor, _now()):
        return False
    logger.info("Episode %s dismissed by %s", episode_id, actor)
    return True


def detach(episode_id: str, correlation_key: str, actor: str) -> bool:
    """Record that an operator says this symptom was not caused by this episode.

    Kept rather than deleted. This is the only ground truth the system ever
    receives about its own correlation, produced as a by-product of somebody
    doing their job, and it is worth more than anything inferred.
    """
    repo = _repo()
    if not repo.detach(episode_id, correlation_key, actor, _now()):
        return False
    repo.refresh_rollup(episode_id)
    logger.info("Episode %s: %s detached by %s", episode_id, correlation_key, actor)
    return True


def symptom_keys_by_episode() -> dict[str, str]:
    """Correlation key -> episode id, for every attached symptom of an open episode.

    The inbox uses this to rank a symptom underneath its cause instead of
    beside it.
    """
    try:
        return _repo().open_symptom_index()
    except Exception:
        logger.exception("Could not read the episode symptom index")
        return {}


def recurrence_summary(episode_id: str) -> dict[str, Any]:
    """How often this cause has come back, and whether it is getting worse.

    Returns occurrences (this one included), the window they span, and the
    interval between them when it is regular enough to name — a cause that
    returns on a fixed cadence is a different problem from one that returns at
    random, and the cadence is usually the clue.
    """
    repo = _repo()
    chain = repo.recurrence_chain(episode_id)
    if not chain:
        return {"occurrences": 1, "recurring": False}

    this = repo.get(episode_id)
    starts = [int(this["started_at"])] + [int(e["started_at"]) for e in chain]
    starts.sort(reverse=True)
    gaps = [starts[i] - starts[i + 1] for i in range(len(starts) - 1)]

    summary: dict[str, Any] = {
        "occurrences": len(starts),
        "recurring": True,
        "first_seen": starts[-1],
        "window_seconds": starts[0] - starts[-1],
        "prior_episode_ids": [e["id"] for e in chain],
    }
    if gaps:
        mean = sum(gaps) / len(gaps)
        # Regular enough to call a cadence: every gap within 25% of the mean.
        if mean > 0 and all(abs(g - mean) <= mean * 0.25 for g in gaps):
            summary["interval_seconds"] = int(mean)
    return summary


# How far before an episode started to look for changes. A rollout or a config
# edit that preceded the damage by more than this is unlikely to be the reason,
# and widening it turns "what changed" into "everything that ever changed".
_CHANGE_LOOKBACK_SECONDS = 30 * 60


def changes_around(episode_id: str) -> list[dict[str, Any]]:
    """Config, RBAC and deployment activity in the window before an episode began.

    The first question in any incident is "what changed", and the product has
    been collecting the answer all along — audit_config, audit_rbac and
    audit_deployment file their findings as ordinary inbox rows, where they sit
    among everything else and answer nothing.

    Placing them on the episode's timeline is the whole difference between data
    and an answer. Nothing here claims causation: it reports what happened
    shortly before, in time order, and lets the reader draw the line.
    """
    repo = _repo()
    episode = repo.get(episode_id)
    if not episode:
        return []

    started = int(episode["started_at"])
    window_start = started - _CHANGE_LOOKBACK_SECONDS

    from ..repositories.inbox_repo import get_inbox_repo

    try:
        rows = get_inbox_repo().fetch_items_by_category_window(
            ("audit_config", "audit_rbac", "audit_deployment"), window_start, started
        )
    except Exception:
        logger.exception("Could not read change history for episode %s", episode_id)
        return []

    changes = []
    for row in rows or []:
        created = int(row["created_at"])
        changes.append(
            {
                # The correlation key is built as category:namespace:resource,
                # and category is not a column of its own.
                "category": str(row["correlation_key"] or "").split(":", 1)[0],
                "title": row["title"],
                "namespace": row["namespace"] or "",
                "at": created,
                "seconds_before": started - created,
            }
        )
    changes.sort(key=lambda c: c["at"])
    return changes


def investigation_for(episode_id: str) -> dict[str, Any] | None:
    """The investigation already run against this episode's cause, if any.

    Causes are eligible for automatic investigation, so by the time an operator
    opens the card the work has usually been attempted. Offering a fresh
    "ask the AI" without showing that would give two routes to the same call
    and imply nothing had been tried — and on a cluster where the backend is
    failing, quietly hide that it was tried and failed.

    A failed attempt is returned rather than hidden. An empty panel reads as
    "nothing worth investigating", which is exactly the wrong conclusion.
    """
    repo = _repo()
    episode = repo.get(episode_id)
    if not episode:
        return None
    finding_id = episode.get("cause_finding_id")
    if not finding_id:
        return None

    from ..db import get_database

    try:
        row = get_database().fetchone(
            "SELECT id, status, summary, suspected_cause, recommended_fix, confidence, error, timestamp "
            "FROM investigations WHERE finding_id = %s ORDER BY timestamp DESC LIMIT 1",
            (finding_id,),
        )
    except Exception:
        logger.exception("Could not read the investigation for episode %s", episode_id)
        return None
    if not row:
        return None

    record = dict(row)
    record["failed"] = record.get("status") == "failed"
    return record


def list_open() -> list[dict]:
    """Open episodes, newest first, each with its symptom rollup."""
    episodes = _repo().list_open()
    for episode in episodes:
        raw = episode.get("namespaces") or "[]"
        try:
            episode["namespaces"] = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            episode["namespaces"] = []
    return episodes
