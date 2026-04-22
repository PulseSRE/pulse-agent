"""Execute a viewPlan to build an investigation dashboard layout."""

from __future__ import annotations

import concurrent.futures
import logging
import time
from typing import Any

from .component_registry import get_valid_kinds
from .tool_registry import TOOL_REGISTRY, WRITE_TOOL_NAMES

logger = logging.getLogger("pulse_agent.view_executor")

_MAX_WIDGETS = 6
_TOOL_TIMEOUT = 10
_STALENESS_THRESHOLD = 1800
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=3, thread_name_prefix="view")


def _resolve_tool(tool_name: str):
    """Resolve a tool name to its registered tool object, or None."""
    return TOOL_REGISTRY.get(tool_name)


def _build_header_widgets(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Build deterministic header widgets: confidence badge + investigation summary."""
    header: list[dict[str, Any]] = []
    metadata = item.get("metadata", {})

    confidence = metadata.get("investigation_confidence", 0)
    if confidence:
        header.append(
            {
                "kind": "confidence_badge",
                "title": "Investigation Confidence",
                "props": {"score": confidence},
            }
        )

    summary = metadata.get("investigation_summary", "")
    cause = metadata.get("suspected_cause", "")
    fix = metadata.get("recommended_fix", "")
    if summary or cause or fix:
        cards = []
        if summary:
            cards.append({"label": "Summary", "value": summary})
        if cause:
            cards.append({"label": "Suspected Cause", "value": cause})
        if fix:
            cards.append({"label": "Recommended Fix", "value": fix})
        header.append(
            {
                "kind": "info_card_grid",
                "title": "Investigation Findings",
                "props": {"cards": cards},
            }
        )

    return header


def _execute_tool_widget(widget: dict[str, Any]) -> dict[str, Any] | None:
    """Execute a tool-backed widget and return a component spec, or None on failure."""
    tool_name = widget["tool"]

    if tool_name in WRITE_TOOL_NAMES:
        logger.warning("Blocked write tool %s in view plan", tool_name)
        return None

    tool_obj = _resolve_tool(tool_name)
    if tool_obj is None:
        logger.debug("Tool %s not found in registry, skipping widget", tool_name)
        return None

    args = widget.get("args", {})
    if not isinstance(args, dict):
        logger.warning("Widget %s has non-dict args, skipping", widget.get("title", ""))
        return None

    future = _executor.submit(tool_obj.call, args)
    try:
        result = future.result(timeout=_TOOL_TIMEOUT)
    except concurrent.futures.TimeoutError:
        logger.warning("Tool %s timed out after %ds, skipping widget", tool_name, _TOOL_TIMEOUT)
        future.cancel()
        return None
    except Exception:
        logger.debug("Tool %s failed, skipping widget", tool_name, exc_info=True)
        return None

    title = widget.get("title", "")

    if isinstance(result, tuple) and len(result) == 2:
        _text, component = result
        if isinstance(component, dict):
            component["title"] = title
            return component

    return {
        "kind": "info_card_grid",
        "title": title,
        "props": {"cards": [{"label": title, "value": str(result)}]},
    }


def validate_view_plan(view_plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate and filter a viewPlan from the investigation response."""
    valid_kinds = get_valid_kinds()
    validated: list[dict[str, Any]] = []

    for widget in view_plan[:_MAX_WIDGETS]:
        kind = widget.get("kind", "")
        if kind not in valid_kinds:
            logger.debug("Dropping widget with invalid kind: %s", kind)
            continue

        tool_name = widget.get("tool")
        if tool_name:
            if tool_name in WRITE_TOOL_NAMES:
                logger.warning("Dropping write tool %s from view plan", tool_name)
                continue
            if tool_name not in TOOL_REGISTRY:
                logger.debug("Dropping unknown tool %s from view plan", tool_name)
                continue

        validated.append(widget)

    return validated


def execute_view_plan(view_plan: list[dict[str, Any]], item: dict[str, Any]) -> list[dict[str, Any]]:
    """Execute a viewPlan and return assembled component layout.

    Always prepends confidence badge + investigation summary header.
    Skips tool-backed widgets if plan is stale (>30min old).
    """
    valid_kinds = get_valid_kinds()

    layout = _build_header_widgets(item)

    view_plan_at = item.get("metadata", {}).get("view_plan_at", 0)
    is_stale = view_plan_at > 0 and (time.time() - view_plan_at) > _STALENESS_THRESHOLD

    if is_stale and any("tool" in w for w in view_plan):
        layout.append(
            {
                "kind": "info_card_grid",
                "title": "Data May Be Outdated",
                "props": {
                    "cards": [
                        {
                            "label": "Note",
                            "value": "This investigation ran more than 30 minutes ago. Live data widgets were skipped. Open the agent chat to get fresh diagnostics.",
                        }
                    ],
                },
            }
        )

    for widget in view_plan[:_MAX_WIDGETS]:
        kind = widget.get("kind", "")
        if kind not in valid_kinds:
            logger.debug("Skipping widget with invalid kind: %s", kind)
            continue

        if "tool" in widget:
            if is_stale:
                logger.debug("Skipping stale tool widget: %s", widget.get("title", ""))
                continue
            component = _execute_tool_widget(widget)
            if component:
                layout.append(component)
        elif "props" in widget:
            layout.append({"kind": kind, "title": widget.get("title", ""), "props": widget["props"]})

    return layout
