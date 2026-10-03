"""Shared execution-gate facts, independent of models and transport.

Consent and tool classification determine whether execution reaches the
configured namespace/node deny policy. They never override that deny policy.
Administrator-configured MCP servers may explicitly attest a read-only tool;
unknown annotations require confirmation. Investigation plans use the same
chat execution gate; durable approved phases additionally require server trust.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping


def is_explicit_approval(value: object) -> bool:
    """Only boolean True grants execution authority, never truthy input."""
    return value is True


def effective_write_tools(configured: Iterable[str], registered: Iterable[str], offered: Iterable[str]) -> set[str]:
    """Stale caller configuration cannot demote a currently offered write."""
    return set(configured) | (set(registered) & set(offered))


def mcp_requires_confirmation(annotations: object) -> bool:
    """Unknown/mutating tools fail closed; explicit trusted reads stay usable."""
    if not isinstance(annotations, Mapping):
        return True
    return not (annotations.get("readOnlyHint") is True and annotations.get("destructiveHint") is not True)


def approved_phase_can_write(approved: object, server_trust: int) -> bool:
    """Durable phase consent is necessary alongside configured write trust."""
    return is_explicit_approval(approved) and server_trust >= 2
