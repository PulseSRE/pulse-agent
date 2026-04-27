"""Base classes for typed view mutations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MutationContext:
    """Inputs for a mutation — typed wrapper around the tool parameters."""

    view_id: str
    view: dict
    owner: str
    widget_index: int = -1
    new_title: str = ""
    new_description: str = ""
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class MutationResult:
    """Output of a mutation."""

    success: bool
    message: str
    view_id: str = ""


class ViewMutation(ABC):
    """Base class for a single mutation action."""

    action: str

    @abstractmethod
    def validate(self, ctx: MutationContext) -> str | None:
        """Return an error message, or None if valid."""

    @abstractmethod
    def apply(self, ctx: MutationContext) -> MutationResult:
        """Apply the mutation and return the result."""

    def _get_layout(self, ctx: MutationContext) -> list[dict]:
        return ctx.view.get("layout", [])

    def _check_widget_index(self, ctx: MutationContext) -> str | None:
        layout = self._get_layout(ctx)
        if ctx.widget_index < 0 or ctx.widget_index >= len(layout):
            return f"Invalid widget index {ctx.widget_index}. View has {len(layout)} widgets (0-{len(layout) - 1})."
        return None

    def _save_layout(self, ctx: MutationContext, layout: list[dict]) -> None:
        from .. import db

        db.update_view(ctx.view_id, ctx.owner, layout=layout)
