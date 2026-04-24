"""Context building and sanitization for agent prompts."""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("pulse_agent.api")

# Allowed characters in context fields (K8s name rules + slashes/dots)
_SAFE_CONTEXT = re.compile(r"^[a-zA-Z0-9\-._/: ]{0,253}$")


def _sanitize_context_field(value: str) -> str:
    """Sanitize a context field to prevent prompt injection."""
    if not isinstance(value, str):
        return ""
    if not _SAFE_CONTEXT.match(value):
        return ""  # Strict reject: non-matching values are dropped entirely
    return value


def _build_context_prefix(data: dict) -> str:
    """Build a context prefix string from Pulse UI context fields.

    Extracts kind/namespace/name from data["context"], sanitizes them,
    and returns a prefix string to prepend to user content.
    Returns empty string if no valid context is present.
    """
    context = data.get("context")
    if not context or not isinstance(context, dict) or len(str(context)) > 2000:
        return ""

    # Check for custom view context first
    view_id = _sanitize_context_field(context.get("viewId", ""))
    if view_id:
        # User is viewing a custom dashboard -- inject view details
        try:
            from .. import db as _ctx_db

            view = _ctx_db.get_view(view_id)
            if view:
                widget_count = len(view.get("layout", []))
                widget_summary = ", ".join(
                    f"{w.get('kind', '?')}: {w.get('title', 'untitled')}" for w in view.get("layout", [])[:8]
                )
                return (
                    f"[UI Context: Dashboard '{view['title']}' (ID: {view_id}, {widget_count} widgets)]\n"
                    f"Widgets: {widget_summary}\n"
                    f"IMPORTANT: The user is viewing this dashboard. Use view_id='{view_id}' "
                    f"for any update_view_widgets or get_view_details calls.\n\n"
                )
        except Exception:
            logger.debug("Failed to fetch view details for context prefix", exc_info=True)
        return f"[UI Context: Dashboard {view_id}]\n\n"

    kind = _sanitize_context_field(context.get("kind", ""))
    ns = _sanitize_context_field(context.get("namespace", ""))
    name = _sanitize_context_field(context.get("name", ""))

    if not (kind or name or ns):
        return ""

    context_parts = []
    if kind and name:
        context_parts.append(f"Resource: {kind}/{name}")
    elif name:
        context_parts.append(f"Resource: {name}")
    if ns:
        context_parts.append(f"Namespace: {ns}")
    context_str = ", ".join(context_parts)

    if ns:
        return (
            f"[UI Context: {context_str}]\n"
            f"IMPORTANT: Use namespace='{ns}' for any operations on this resource. "
            f"Do NOT default to 'default' namespace.\n\n"
        )
    return f"[UI Context: {context_str}]\n\n"


def _apply_style_hint(data: dict) -> str:
    """Extract communication style from message preferences and return a system prompt hint."""
    prefs = data.get("preferences", {})
    comm_style = prefs.get("communicationStyle", "") if isinstance(prefs, dict) else ""
    if comm_style == "brief":
        return "\n\nUser preference: Be concise. Short answers, bullet points, no verbose explanations."
    elif comm_style == "technical":
        return (
            "\n\nUser preference: Be deeply technical. Include CLI commands, YAML snippets, and implementation details."
        )
    return ""
