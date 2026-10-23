"""Who the current chat request is for, visible to tools.

Set once per agent WebSocket connection. Tools run in an executor, but
``agent._in_context`` copies the context into it, so a tool reads the user of
the conversation that called it rather than trusting a parameter the model
filled in.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field

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


@dataclass
class TurnSignals:
    """What happened in one agent turn that routing and tool learning need.

    Created by ``run_agent_streaming`` and read by whoever records the turn
    afterwards. Tools run in an executor with a *copy* of the context, so they
    see this same object and can append to it.
    """

    # The tools the model was actually offered, after adaptive selection —
    # not the skill's full tool set, which is a superset it never saw.
    offered_tools: list[str] | None = None
    # Skills the agent pulled in by name with skill_load. Loading a different
    # skill than routing chose is the agent saying routing picked wrong.
    loaded_skills: list[str] = field(default_factory=list)


_turn: ContextVar[TurnSignals | None] = ContextVar("pulse_turn_signals", default=None)


def begin_turn() -> TurnSignals:
    signals = TurnSignals()
    _turn.set(signals)
    return signals


def current_turn() -> TurnSignals | None:
    return _turn.get()
