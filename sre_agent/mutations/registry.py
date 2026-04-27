"""Mutation dispatch registry."""

from __future__ import annotations

from .base import ViewMutation

MUTATION_REGISTRY: dict[str, ViewMutation] = {}

_registered = False


def register_mutation(mutation: ViewMutation) -> ViewMutation:
    MUTATION_REGISTRY[mutation.action] = mutation
    return mutation


def _ensure_registered() -> None:
    """Lazy-import mutation modules on first use."""
    global _registered
    if _registered:
        return
    _registered = True
    from . import table_ops, view_ops, widget_ops  # noqa: F401


def get_mutation(action: str) -> ViewMutation | None:
    _ensure_registered()
    return MUTATION_REGISTRY.get(action)


def get_all_actions() -> list[str]:
    """Return all registered action names."""
    _ensure_registered()
    return sorted(MUTATION_REGISTRY.keys())
