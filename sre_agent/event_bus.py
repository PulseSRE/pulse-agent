"""Agent event bus — typed alternative to individual callbacks.

Provides ``EventBus`` as an opt-in replacement for the 7 individual callback
parameters on ``run_agent_streaming()``.  Existing callers pass individual
callbacks; ``EventBus.from_callbacks()`` wraps them transparently.

``on_confirm`` is special — it's bidirectional (returns ``bool``).
All other events are fire-and-forget.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol


class AgentEventHandler(Protocol):
    """Protocol for handling agent events.

    Implement this to receive all events from the agent loop.
    ``on_confirm`` is the only event that returns a value.
    """

    async def on_text(self, text: str) -> None: ...
    async def on_thinking(self, text: str) -> None: ...
    async def on_tool_use(self, tool_name: str) -> None: ...
    async def on_tool_result(self, result: dict) -> None: ...
    async def on_component(self, tool_name: str, spec: dict) -> None: ...
    async def on_usage(self, **kwargs: Any) -> None: ...
    async def on_confirm(self, tool_name: str, input_data: dict) -> bool: ...


async def _invoke_optional(callback: Any, *args: Any, **kwargs: Any) -> Any:
    """Invoke a callback that may be sync, async, or None."""
    if callback is None:
        return None
    result = callback(*args, **kwargs)
    if asyncio.iscoroutine(result):
        return await result
    return result


@dataclass
class EventBus:
    """Adapter that implements the event handler interface using individual callbacks.

    Use ``EventBus.from_callbacks()`` to wrap the existing callback-style API.
    New consumers can subclass or implement ``AgentEventHandler`` directly.
    """

    _on_text: Any = None
    _on_thinking: Any = None
    _on_tool_use: Any = None
    _on_confirm: Any = None
    _on_component: Any = None
    _on_tool_result: Any = None
    _on_usage: Any = None

    async def on_text(self, text: str) -> None:
        if self._on_text is not None:
            await _invoke_optional(self._on_text, text)

    async def on_thinking(self, text: str) -> None:
        if self._on_thinking is not None:
            await _invoke_optional(self._on_thinking, text)

    async def on_tool_use(self, tool_name: str) -> None:
        if self._on_tool_use is not None:
            await _invoke_optional(self._on_tool_use, tool_name)

    async def on_tool_result(self, result: dict) -> None:
        if self._on_tool_result is not None:
            await _invoke_optional(self._on_tool_result, result)

    async def on_component(self, tool_name: str, spec: dict) -> None:
        if self._on_component is not None:
            await _invoke_optional(self._on_component, tool_name, spec)

    async def on_usage(self, **kwargs: Any) -> None:
        if self._on_usage is not None:
            await _invoke_optional(self._on_usage, **kwargs)

    async def on_confirm(self, tool_name: str, input_data: dict) -> bool:
        if self._on_confirm is None:
            return False
        result = await _invoke_optional(self._on_confirm, tool_name, input_data)
        return bool(result) if result is not None else False

    @classmethod
    def from_callbacks(
        cls,
        on_text: Any = None,
        on_thinking: Any = None,
        on_tool_use: Any = None,
        on_confirm: Any = None,
        on_component: Any = None,
        on_tool_result: Any = None,
        on_usage: Any = None,
    ) -> EventBus:
        """Create an EventBus from individual callback functions."""
        return cls(
            _on_text=on_text,
            _on_thinking=on_thinking,
            _on_tool_use=on_tool_use,
            _on_confirm=on_confirm,
            _on_component=on_component,
            _on_tool_result=on_tool_result,
            _on_usage=on_usage,
        )
