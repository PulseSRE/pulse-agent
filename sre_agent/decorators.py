"""Typed decorators for Pulse Agent tools.

Wraps anthropic.beta_tool with a relaxed return type so tools can return
either str or tuple[str, dict] (text + component spec) without mypy errors.
Includes optional timing instrumentation for performance tracking.

Supports both ``@beta_tool`` (no args) and ``@beta_tool(category="views", is_write=True)``.
When ``category`` or ``is_write`` are specified, the tool is auto-registered
in the central TOOL_REGISTRY. Otherwise, registration is left to the calling
module (preserving existing explicit ``register_tool()`` patterns).
"""

from __future__ import annotations

import functools
import logging
import time
from collections import deque
from collections.abc import Callable
from typing import Any, TypeVar, Union, cast, overload

from anthropic import beta_tool as _anthropic_beta_tool
from anthropic.lib.tools._beta_functions import BetaFunctionTool

F = TypeVar("F", bound=Callable[..., Any])

#: Return type for tool functions — plain text or text + optional component spec.
ToolReturn = Union[str, "tuple[str, dict[str, Any] | None]"]

#: Type alias for a BetaFunctionTool with optional Pulse metadata attributes.
PulseTool = BetaFunctionTool[Callable[..., Any]]

_logger = logging.getLogger("pulse_agent.tools")

#: Collected timing data — tool_name → deque of recent execution times (seconds).
#: Capped at 1000 entries per tool to prevent unbounded growth.
_MAX_TIMING_ENTRIES = 1000
tool_timings: dict[str, deque[float]] = {}

_PERF_TRACE = False


def enable_perf_trace() -> None:
    """Enable per-tool timing collection."""
    global _PERF_TRACE
    _PERF_TRACE = True


def get_tool_timings() -> dict[str, list[float]]:
    """Return collected timing data."""
    return {k: list(v) for k, v in tool_timings.items()}


def reset_tool_timings() -> None:
    """Clear collected timing data."""
    tool_timings.clear()


def _set_tool_metadata(tool: PulseTool, category: str, is_write: bool) -> None:
    """Attach Pulse-specific metadata to a BetaFunctionTool instance."""
    object.__setattr__(tool, "_category", category)
    object.__setattr__(tool, "_is_write", is_write)


@overload
def beta_tool(fn: F, *, category: str = "", is_write: bool = False) -> PulseTool: ...


@overload
def beta_tool(fn: None = None, *, category: str = "", is_write: bool = False) -> Callable[[F], PulseTool]: ...


def beta_tool(
    fn: F | None = None, *, category: str = "", is_write: bool = False
) -> PulseTool | Callable[[F], PulseTool]:
    """Typed wrapper around anthropic.beta_tool.

    Supports both ``@beta_tool`` (no args) and ``@beta_tool(category="views", is_write=True)``.

    When ``category`` or ``is_write`` is set, auto-registers the tool in the
    central TOOL_REGISTRY. The plain ``@beta_tool`` form does NOT auto-register,
    preserving backward compatibility with modules that call ``register_tool()``
    explicitly.
    """

    def decorator(f: F) -> PulseTool:
        @functools.wraps(f)
        def _timed(*args: Any, **kwargs: Any) -> Any:
            if not _PERF_TRACE:
                return f(*args, **kwargs)
            start = time.monotonic()
            try:
                return f(*args, **kwargs)
            finally:
                elapsed = time.monotonic() - start
                tool_timings.setdefault(f.__name__, deque(maxlen=_MAX_TIMING_ENTRIES)).append(elapsed)
                if elapsed > 2.0:
                    _logger.warning("Slow tool %s: %.2fs", f.__name__, elapsed)

        tool = cast("PulseTool", _anthropic_beta_tool(_timed))

        # Store metadata for introspection
        _set_tool_metadata(tool, category, is_write)

        # Auto-register only when metadata is explicitly provided
        if category or is_write:
            from .tool_registry import register_tool

            register_tool(tool, is_write=is_write, category=category or "general")

        return tool

    if fn is not None:
        # Called as @beta_tool (no parentheses) — no auto-registration
        return decorator(fn)
    # Called as @beta_tool(...) (with parentheses)
    return decorator
