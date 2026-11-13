"""PromQL and component sanitization helpers."""

from __future__ import annotations

import re

from ..k8s_tools.validators import _validate_k8s_name, _validate_k8s_namespace
from ..tool_registry import TOOL_REGISTRY, WRITE_TOOL_NAMES

_DOUBLE_BRACE_RE = re.compile(r"\}(\s*)\{")

# Access changes are never a dashboard button: a button runs without the chat
# confirmation card and its binding diff and risk preview.
ACTION_BLOCKED_TOOLS = frozenset({"drain_node", "exec_command", "grant_access", "revoke_access"})


def _fix_promql(query: str) -> str:
    """Fix common PromQL syntax errors.

    Merges double label blocks: metric{a="1"}{b="2"} -> metric{a="1",b="2"}
    """
    if not query or "{" not in query:
        return query
    return _DOUBLE_BRACE_RE.sub(r",", query)


def validate_action_input(action_input: dict) -> str | None:
    """Validate common action_input params. Returns error string or None."""
    ns = action_input.get("namespace")
    if ns:
        err = _validate_k8s_namespace(ns)
        if err:
            return err
    name = action_input.get("name")
    if name:
        err = _validate_k8s_name(name)
        if err:
            return err
    replicas = action_input.get("replicas")
    if replicas is not None:
        try:
            r = int(replicas)
            if r < 0 or r > 100:
                return "Replicas must be 0-100"
        except (ValueError, TypeError):
            return "Replicas must be a number"
    return None


def _sanitize_action_button(comp: dict) -> dict | None:
    """Validate an action_button component at save time.

    Returns the component if valid, or None if it should be rejected.
    Sets ``_is_write`` flag so the frontend knows to show confirmation.
    """
    action = comp.get("action", "")
    if not action or action not in TOOL_REGISTRY:
        return None
    if action in ACTION_BLOCKED_TOOLS:
        return None

    action_input = comp.get("action_input")
    if not isinstance(action_input, dict):
        return None

    if validate_action_input(action_input):
        return None

    comp["_is_write"] = action in WRITE_TOOL_NAMES
    return comp


def _sanitize_components(components: list[dict]) -> list[dict]:
    """Sanitize component specs before saving to views.

    Fixes invalid PromQL in metric_card queries.
    Strips invalid action_button components.
    """
    out: list[dict] = []
    for comp in components:
        if comp.get("kind") == "action_button":
            sanitized = _sanitize_action_button(comp)
            if sanitized is None:
                continue
            out.append(sanitized)
            continue

        if comp.get("kind") == "metric_card" and comp.get("query"):
            comp["query"] = _fix_promql(comp["query"])
        for container_key in ("items", "components"):
            nested = comp.get(container_key)
            if isinstance(nested, list):
                comp[container_key] = _sanitize_components(nested)
        tabs = comp.get("tabs")
        if isinstance(tabs, list):
            for tab in tabs:
                tab_comps = tab.get("components")
                if isinstance(tab_comps, list):
                    tab["components"] = _sanitize_components(tab_comps)
        out.append(comp)
    return out
