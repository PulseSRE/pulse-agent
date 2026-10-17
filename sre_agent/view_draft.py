"""The dashboard being assembled in the current agent turn, visible to tools.

Widgets reach the session through ``on_component`` as data tools run, and
the view is saved only after the agent's turn ends (``agent_ws`` processes
the ``view_spec`` signal). Until now ``create_dashboard`` could not see that
draft, so it always answered "Here is your dashboard" and the quality
engine's findings went only to the UI, after the fact. The model never
learned a widget was empty or invalid, and could not fix it: by then the turn
was over, and mid-turn the view did not exist yet for the update tools.

``agent_ws`` binds the turn's component list here before running the agent.
Tool threads see it because the agent loop runs tools in a copy of the
calling context, and the list is shared by reference.

Caveat: a widget reaches the list once its tool's batch completes. If the
model calls a data tool and ``create_dashboard`` in the same batch, the check
does not see that widget. It therefore only blocks on problems found in
widgets that are present, never on what might be missing.
"""

from __future__ import annotations

import copy
from contextvars import ContextVar, Token
from dataclasses import dataclass, field


@dataclass
class DashboardDraft:
    components: list[dict]
    # create_dashboard calls that were held back for fixes this turn. The
    # check holds a save back at most once, so it can never loop.
    held: int = 0
    excluded: set[str] = field(default_factory=set)


_DRAFT: ContextVar[DashboardDraft | None] = ContextVar("dashboard_draft", default=None)


def bind(components: list[dict]) -> Token:
    """Expose this turn's component list to tools. Returns a token for ``unbind``."""
    return _DRAFT.set(DashboardDraft(components=components))


def unbind(token: Token) -> None:
    _DRAFT.reset(token)


def current() -> DashboardDraft | None:
    return _DRAFT.get()


def parse_titles(exclude: str) -> set[str]:
    """``"A | B"`` -> ``{"a", "b"}`` (case-insensitive widget titles)."""
    return {t.strip().lower() for t in (exclude or "").split("|") if t.strip()}


def apply_exclusions(components: list[dict], titles: set[str]) -> list[dict]:
    """Drop top-level widgets whose title is in ``titles`` (case-insensitive)."""
    if not titles:
        return list(components)
    return [c for c in components if (c.get("title") or "").strip().lower() not in titles]


# Findings that make a saved page look broken: the widget renders empty or
# not at all. Everything else the engine reports is advisory.
_BLOCKING_WARNING_PREFIXES = ("Empty widget",)
# Engine errors about the dashboard as a whole, not a broken widget. Holding
# a save for these would push the model to pad or trim to hit a count.
_ADVISORY_ERROR_MARKERS = ("must have at least", "must have at most")


def precheck(components: list[dict]) -> tuple[list[str], list[str]]:
    """Return ``(blocking, advisory)`` findings for a draft dashboard."""
    from .quality_engine import evaluate_components

    if not components:
        return [], []
    # Deep copy: the engine normalizes specs in place; the draft must stay as saved.
    result = evaluate_components(copy.deepcopy(components), positions=None)
    blocking: list[str] = []
    advisory: list[str] = []
    for err in result.errors:
        (advisory if any(m in err for m in _ADVISORY_ERROR_MARKERS) else blocking).append(err)
    for warning in result.warnings:
        (blocking if warning.startswith(_BLOCKING_WARNING_PREFIXES) else advisory).append(warning)
    advisory.extend(result.suggestions)
    return blocking, advisory
