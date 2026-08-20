"""REST endpoints for episodes.

An episode is one event with a cause, as opposed to the N separate findings it
produced. These endpoints exist so the inbox can show the cause with its
symptoms folded underneath, instead of ranking them beside each other.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from ..monitor.episodes import detach, list_open
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
    """One episode with the findings currently attached to it."""
    repo = get_episode_repo()
    episode = repo.get(episode_id)
    if not episode:
        raise HTTPException(status_code=404, detail=f"Episode {episode_id} not found")
    return {"episode": dict(episode), "symptoms": [dict(s) for s in repo.symptoms(episode_id)]}


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
