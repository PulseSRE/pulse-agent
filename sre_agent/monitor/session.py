"""MonitorClient — per-WebSocket client for the /ws/monitor endpoint.

Each client subscribes to the singleton ClusterMonitor and receives events
filtered by its own preferences (e.g. disabled scanners).

Backward compatibility: ``MonitorSession`` is an alias for ``MonitorClient``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("pulse_agent.monitor")


class MonitorClient:
    """Per-WebSocket client that receives broadcast events from ClusterMonitor."""

    def __init__(self, websocket: Any, trust_level: int = 1, auto_fix_categories: list[str] | None = None):
        self.websocket = websocket
        self.trust_level = trust_level
        self.auto_fix_categories = set(auto_fix_categories or [])
        self.disabled_scanners: set[str] = set()
        self._pending_action_approvals: dict[str, asyncio.Future] = {}

    def resolve_action_response(self, action_id: str, approved: bool) -> bool:
        """Resolve an outstanding action approval request."""
        future = self._pending_action_approvals.get(action_id)
        if not future or future.done():
            return False
        future.set_result(bool(approved))
        return True

    async def send(self, data: dict) -> bool:
        """Send JSON to this client's WebSocket. Returns False if connection lost."""
        try:
            await self.websocket.send_json(data)
            return True
        except Exception:
            return False

    async def on_event(self, data: dict) -> None:
        """Called by ClusterMonitor.broadcast(). Applies per-client filtering then sends.

        Currently filters findings by disabled_scanners. Non-finding events pass through.
        """
        # Filter findings from scanners this client has disabled
        if data.get("type") == "finding" and data.get("category") in self.disabled_scanners:
            return
        await self.send(data)


# Backward compatibility alias — existing code and tests that import MonitorSession still work.
MonitorSession = MonitorClient
