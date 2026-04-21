"""View lifecycle event bus — publishes events for WebSocket broadcast.

REST endpoints publish events (claim, action execution, status transition).
WebSocket connections consume them to notify other connected clients.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("pulse_agent.api.view_events")

_MAX_PENDING = 100


@dataclass
class ViewEvent:
    event_type: str
    view_id: str
    actor: str
    data: dict[str, Any] = field(default_factory=dict)


class ViewEventBus:
    """Simple in-memory pub/sub for view lifecycle events."""

    def __init__(self) -> None:
        self._queues: list[asyncio.Queue[ViewEvent]] = []

    def subscribe(self) -> asyncio.Queue[ViewEvent]:
        q: asyncio.Queue[ViewEvent] = asyncio.Queue(maxsize=_MAX_PENDING)
        self._queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[ViewEvent]) -> None:
        try:
            self._queues.remove(q)
        except ValueError:
            pass

    def publish(self, event: ViewEvent) -> None:
        for q in self._queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass


_bus: ViewEventBus | None = None


def get_event_bus() -> ViewEventBus:
    global _bus
    if _bus is None:
        _bus = ViewEventBus()
    return _bus


def publish_view_event(event_type: str, view_id: str, actor: str, data: dict[str, Any] | None = None) -> None:
    """Fire-and-forget publish. Silently swallows errors."""
    try:
        get_event_bus().publish(ViewEvent(event_type=event_type, view_id=view_id, actor=actor, data=data or {}))
    except Exception:
        pass
