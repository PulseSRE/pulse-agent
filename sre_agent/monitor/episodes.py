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

from .layers import STANDALONE_CATEGORIES, can_explain, layer_of

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
    if category in STANDALONE_CATEGORIES:
        return None
    # Only infrastructure and platform findings head episodes. A workload
    # finding heading one would absorb signal-layer findings across the whole
    # cluster on the strength of a single restarting pod.
    if layer_of(category) > 1:
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
