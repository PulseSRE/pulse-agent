"""Helpers that keep activity IO off the event loop the worker shares.

The worker runs inside the agent process, on the same event loop as the API
server, WebSockets and monitor. Activities that call the (synchronous)
Kubernetes client or database inline block all of that for as long as the call
takes, and the Kubernetes client has no request timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any

from temporalio import activity

#: How often a long activity reports it is alive. Well inside the workflow's
#: 30s heartbeat timeout, so one late beat does not fail an apply.
_HEARTBEAT_EVERY = 10.0


async def _off_loop(fn: Callable[..., Any], *args: Any) -> Any:
    """Run blocking Kubernetes/database work on a thread, not the event loop.

    The worker shares its event loop with the agent's API server, WebSockets
    and monitor. The Kubernetes client has no request timeout, so a call to a
    stalled API server run inline froze the whole agent: measured against a
    server that accepts and never answers, a 45s sleep had not returned after
    120s. The inline auto-fix path already uses a thread; this matches it.
    """
    return await asyncio.to_thread(fn, *args)


async def _beating(work: Any, detail: str) -> Any:
    """Await ``work`` while heartbeating, so a dead worker is seen in seconds.

    A blocking call cannot heartbeat for itself; without this, the 30s
    heartbeat timeout fails any fix that takes longer than that to apply.
    """

    async def beat() -> None:
        while True:
            await asyncio.sleep(_HEARTBEAT_EVERY)
            if activity.in_activity():
                activity.heartbeat(detail)

    task = asyncio.create_task(beat())
    try:
        return await work
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
