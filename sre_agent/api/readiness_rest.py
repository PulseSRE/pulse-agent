"""Authenticated installation diagnostics, separate from process liveness."""

import asyncio
import time

from fastapi import APIRouter, Depends

from ..readiness import collect_readiness
from .auth import verify_token

router = APIRouter(tags=["readiness"])
_lock = asyncio.Lock()
_cached = None
_expires = 0.0


@router.get("/readiness")
async def installation_readiness(_auth=Depends(verify_token)):
    global _cached, _expires
    # Single flight plus a short cache bounds repeated read-only cluster probes.
    async with _lock:
        if _cached is None or time.monotonic() >= _expires:
            _cached = await collect_readiness()
            _expires = time.monotonic() + 60
        return _cached
