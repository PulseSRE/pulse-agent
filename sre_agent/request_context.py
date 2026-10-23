"""Who the current chat request is for, visible to tools.

Set once per agent WebSocket connection. Tools run in an executor, but
``agent._in_context`` copies the context into it, so a tool reads the user of
the conversation that called it rather than trusting a parameter the model
filled in.
"""

from __future__ import annotations

from contextvars import ContextVar

_current_user: ContextVar[str | None] = ContextVar("pulse_current_user", default=None)

ANONYMOUS = "anonymous"


def set_current_user(user: str | None) -> None:
    _current_user.set(user)


def get_current_user() -> str | None:
    """The authenticated user of this request, or None if there is none."""
    user = _current_user.get()
    if not user or user == ANONYMOUS:
        return None
    return user
