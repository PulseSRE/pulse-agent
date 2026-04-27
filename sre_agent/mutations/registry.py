"""Mutation dispatch registry."""

from __future__ import annotations

from .base import ViewMutation

MUTATION_REGISTRY: dict[str, ViewMutation] = {}


def register_mutation(mutation: ViewMutation) -> ViewMutation:
    MUTATION_REGISTRY[mutation.action] = mutation
    return mutation


def get_mutation(action: str) -> ViewMutation | None:
    return MUTATION_REGISTRY.get(action)


def _auto_register() -> None:
    """Import mutation modules to trigger registration."""
    from . import table_ops, view_ops, widget_ops  # noqa: F401


_auto_register()
