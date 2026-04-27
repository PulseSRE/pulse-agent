"""Typed view mutation system — dispatch table replacing if/elif chain."""

from .registry import MUTATION_REGISTRY, get_mutation, register_mutation

__all__ = ["MUTATION_REGISTRY", "get_mutation", "register_mutation"]
