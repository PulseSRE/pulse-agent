"""REST endpoints for episodes.

An episode is one event with a cause, as opposed to the N separate findings it
produced. These endpoints exist so the inbox can show the cause with its
symptoms folded underneath, instead of ranking them beside each other.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from ..monitor.episodes import (
    changes_around,
    detach,
    dismiss,
    investigation_for,
    list_open,
    recurrence_summary,
)
from ..repositories.episode_repo import get_episode_repo
from .auth import get_owner, verify_token

logger = logging.getLogger("pulse_agent.api")

router = APIRouter(tags=["episodes"], dependencies=[Depends(verify_token)])


@router.get("/episodes")
async def rest_list_episodes():
    """Open episodes, newest first, each with its symptom rollup."""
    return {"episodes": list_open()}


@router.get("/episodes/{episode_id}")
async def rest_get_episode(episode_id: str):
    """One episode: its symptoms, what changed just before it, and its history.

    The three things an SRE asks in order — what is broken, what changed, and
    has this happened before — answered in one response rather than three
    screens.
    """
    repo = get_episode_repo()
    episode = repo.get(episode_id)
    if not episode:
        raise HTTPException(status_code=404, detail=f"Episode {episode_id} not found")
    return {
        "episode": dict(episode),
        "symptoms": [dict(s) for s in repo.symptoms(episode_id)],
        "changes": changes_around(episode_id),
        "recurrence": recurrence_summary(episode_id),
        "investigation": investigation_for(episode_id),
    }


@router.post("/episodes/{episode_id}/detach")
async def rest_detach_symptom(episode_id: str, body: dict, owner: str = Depends(get_owner)):
    """Record that a symptom was not actually caused by this episode.

    The detachment is stored, not deleted, and the symptom is never re-attached.
    An operator correcting the correlation is the only ground truth this system
    gets about its own accuracy, and it arrives as a by-product of them doing
    their job — worth keeping.
    """
    correlation_key = (body or {}).get("correlationKey", "").strip()
    if not correlation_key:
        raise HTTPException(status_code=400, detail="correlationKey is required")
    if not detach(episode_id, correlation_key, owner):
        raise HTTPException(
            status_code=404,
            detail=f"{correlation_key} is not attached to {episode_id}",
        )
    return {"status": "detached", "episodeId": episode_id, "correlationKey": correlation_key}


@router.post("/episodes/{episode_id}/dismiss")
async def rest_dismiss_episode(episode_id: str, owner: str = Depends(get_owner)):
    """Close an episode an operator says is over.

    The cause re-firing later opens a new episode rather than reviving this
    one, so dismissing cannot hide a problem that comes back.
    """
    if not dismiss(episode_id, owner):
        raise HTTPException(status_code=404, detail=f"No open episode {episode_id}")
    return {"status": "dismissed", "episodeId": episode_id, "dismissedBy": owner}
