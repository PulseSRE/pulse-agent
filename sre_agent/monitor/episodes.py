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

from .layers import can_explain_finding, can_head_episode_finding, layer_for_finding

logger = logging.getLogger("pulse_agent.monitor")

OPEN_STATUS = "open"
CLOSED_STATUS = "closed"

# How far back a symptom may have started and still count as caused by the
# episode. Slightly generous: scan cycles are 60s and a cause is often detected
# a cycle or two after the damage starts.
_ATTACH_GRACE_SECONDS = 180

# The same window, for conditions that report their own onset rather than
# relying on when Pulse noticed them. Firing alerts are the case: Prometheus
# holds a `for:` duration before an alert fires at all, and those durations
# differ per rule, so two alerts describing one event can start minutes apart.
# On the reference cluster the control-plane memory alert and the OLM install
# loop began six minutes apart and were plainly one event; 180 seconds would
# have split them.
_ONSET_GRACE_SECONDS = 15 * 60

# ...and how long *after* the cause a symptom may begin and still be its
# symptom. There was no upper bound at all, which turns a long-running cause
# into a magnet: measured on the reference cluster, a memory alert firing for
# thirty hours had collected 22 symptoms, among them a missing PVC — something
# memory pressure does not cause and cannot cause. Everything that broke during
# those thirty hours qualified, because "started after the cause" was the whole
# test.
#
# A cascade propagates on the order of minutes: memory pressure starves the API
# server, the API server times out probes, the probes kill pods. Two hours is
# generous for that and still says no to a coincidence a day later. It is a
# judgement rather than a measurement, which is why it is one constant with its
# reasoning attached — and why erring short is right: a missed correlation
# costs less than a confident story about the wrong cause.
_ONSET_SPREAD_SECONDS = 2 * 3600

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


def open_or_touch(finding: dict[str, Any], claimed: dict[str, str] | None = None) -> str | None:
    """Open an episode for a cause-capable finding, or mark an existing one live.

    Returns the episode id, or None if this finding cannot head an episode.
    """
    category = finding.get("category", "")
    if not can_head_episode_finding(finding):
        return None

    key = _cause_key(finding)
    if not key:
        return None

    repo = _repo()
    owner = (symptom_keys_by_episode() if claimed is None else claimed).get(key)
    if owner is not None:
        # Something deeper already explains this. On the reference cluster the
        # OLM install loop was both a platform-layer cause and a symptom of the
        # control-plane memory beneath it; heading its own episode as well
        # would report one event twice, with the same symptoms under each.
        return None
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
        cause_layer=layer_for_finding(finding),
        started_at=now,
        correlation_key=key,
        recurrence_of=prior["id"] if prior else None,
        # When the condition itself began, where it knows. The episode's own
        # start is when Pulse first managed to build one, which on a cluster
        # that has been unhappy for a day is a day too late to ask what changed.
        cause_started_at=_onset_of(finding),
    )
    logger.info(
        "Episode %s opened: %s%s",
        episode_id,
        finding.get("title", "")[:60],
        " (recurrence)" if prior else "",
    )
    return episode_id


def _onset_of(finding: dict) -> int | None:
    """When the condition itself began, if it knows.

    Only firing alerts carry this today, from Prometheus. Everything else
    returns None and falls back to when Pulse first saw it.
    """
    value = finding.get("startedAt")
    return int(value) if isinstance(value, int | float) else None


def attach_symptoms(
    episode_id: str,
    cause: dict | str,
    findings: list[dict],
    first_seen: dict[str, int],
    claimed: dict[str, str] | None = None,
) -> int:
    """Attach findings this episode can explain. Returns how many were attached.

    ``cause`` is the finding the episode was opened around. A bare category
    string is still accepted and behaves exactly as before — no declared layer,
    no onset — which is what every caller that does not have the finding to
    hand should pass.

    ``first_seen`` maps a finding's correlation key to when the monitor first
    saw that condition. It is the fallback, not the preferred signal: it lives
    in memory on the monitor and is lost on every restart, so after a redeploy
    every standing problem on the cluster claims to have started at the same
    second. Where a finding reports its own onset, that is used instead and the
    comparison is made against the *cause's* onset rather than against when
    Pulse got around to opening the episode.
    """
    cause_finding: dict = {"category": cause} if isinstance(cause, str) else cause

    repo = _repo()
    episode = repo.get(episode_id)
    if not episode or episode["status"] != OPEN_STATUS:
        return 0

    started = int(episode["started_at"])
    cutoff = started - _ATTACH_GRACE_SECONDS
    cause_onset = _onset_of(cause_finding)
    detached = repo.detached_keys(episode_id)
    owned = symptom_keys_by_episode() if claimed is None else claimed
    attached = 0

    for finding in findings:
        if not can_explain_finding(cause_finding, finding):
            continue
        key = _cause_key(finding)
        if not key or key in detached:
            # An operator already said this one was not related. Never re-attach.
            continue
        owner = owned.get(key)
        if owner is not None and owner != episode_id:
            # Already a symptom of another open episode. One event with a
            # cause means one cause: letting three episodes each list the same
            # TargetDown is the "N findings that are wrong" problem wearing a
            # different hat. The deepest, oldest cause claims it first — see
            # the ordering in the monitor's correlation pass.
            continue
        symptom_onset = _onset_of(finding)
        if cause_onset is not None and symptom_onset is not None:
            if symptom_onset < cause_onset - _ONSET_GRACE_SECONDS:
                # It was already firing before the cause began. Measured on the
                # reference cluster: an unconfigured Alertmanager receiver had
                # been alerting for fifty hours. Nothing caused it, and it is
                # not evidence about anything that started yesterday.
                continue
            if symptom_onset > cause_onset + _ONSET_SPREAD_SECONDS:
                # It began long after the cause did. A cause still firing a day
                # later is not an explanation for everything that has broken
                # since — that is coincidence with a long tail, and attaching it
                # tells an operator a confident story about the wrong cause.
                continue
        elif first_seen.get(key, started) < cutoff:
            # Already broken before the cause appeared.
            continue
        category = finding.get("category", "")
        resources = finding.get("resources") or []
        namespace = resources[0].get("namespace", "") if resources else ""
        if repo.attach(episode_id, key, category, finding.get("title", "")[:400], namespace, _now()):
            owned[key] = episode_id
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

    # Anchor on when the *condition* began, not on when Pulse opened an episode
    # for it. Observed live: a cause firing for 30 hours, an episode 12 minutes
    # old, and a change window covering the half hour before the episode — a
    # day after anything that could have caused it. Conditions that do not
    # report their own onset still fall back to the episode's start, which is
    # the best that is known for them.
    started = int(episode.get("cause_started_at") or episode["started_at"])
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
