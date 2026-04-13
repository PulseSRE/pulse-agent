"""Typed decorators for Pulse Agent tools.

Wraps anthropic.beta_tool with a relaxed return type so tools can return
either str or tuple[str, dict] (text + component spec) without mypy errors.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar, Union

from anthropic import beta_tool as _anthropic_beta_tool

F = TypeVar("F", bound=Callable[..., Any])

#: Return type for tool functions — plain text or text + optional component spec.
ToolReturn = Union[str, "tuple[str, dict[str, Any] | None]"]


def beta_tool(fn: F) -> F:
    """Typed wrapper around anthropic.beta_tool.

    Allows tool functions to return str | tuple[str, dict] for component specs
    without mypy return-value errors. The single type: ignore here replaces
    30+ ignores across tool files.
    """
    return _anthropic_beta_tool(fn)  # type: ignore[return-value]
