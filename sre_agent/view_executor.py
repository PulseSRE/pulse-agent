"""Execute a viewPlan to build an investigation dashboard layout."""

from __future__ import annotations

import atexit
import concurrent.futures
import logging
import time
from typing import Any

from .component_registry import get_valid_kinds
from .k8s_client import user_token_context
from .tool_registry import TOOL_REGISTRY, WRITE_TOOL_NAMES

logger = logging.getLogger("pulse_agent.view_executor")

_MAX_WIDGETS = 6
_TOOL_TIMEOUT = 10
_STALENESS_THRESHOLD = 1800
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=6, thread_name_prefix="view")
atexit.register(_executor.shutdown, wait=False)


def _resolve_tool(tool_name: str) -> Any:
    """Resolve a tool name to its registered tool object, or None. Separate function for testability."""
    return TOOL_REGISTRY.get(tool_name)


def _record_tool_call(tool_name: str, args: dict, result: Any, item_id: str) -> None:
    """Fire-and-forget tool usage recording for audit trail."""
    try:
        from .tool_usage import record_tool_call

        record_tool_call(
            tool_name=tool_name,
            input_data=args,
            session_id=item_id,
            turn_number=0,
            agent_mode="view_executor",
            tool_category=None,
            status="success",
            error_message=None,
            error_category=None,
            duration_ms=0,
            result_bytes=len(str(result)[:200]) if result else 0,
            requires_confirmation=False,
            was_confirmed=None,
        )
    except Exception:
        logger.debug("Failed to record tool call %s", tool_name, exc_info=True)


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


def _execute_tool_widget(
    widget: dict[str, Any], item_id: str = "", user_token: str | None = None
) -> dict[str, Any] | None:
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

    with user_token_context(user_token):
        result = tool_obj.call(args)

    _record_tool_call(tool_name, args, result, item_id)

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


def execute_view_plan(
    view_plan: list[dict[str, Any]], item: dict[str, Any], user_token: str | None = None
) -> list[dict[str, Any]]:
    """Execute a viewPlan and return assembled component layout.

    Always prepends confidence badge + investigation summary header.
    Skips tool-backed widgets if plan is stale (>30min old).
    Tool-backed widgets execute in parallel for speed.
    """
    layout = _build_header_widgets(item)
    item_id = item.get("id", "")

    view_plan_at = item.get("metadata", {}).get("view_plan_at", 0)
    is_stale = view_plan_at > 0 and (time.time() - view_plan_at) > _STALENESS_THRESHOLD

    if is_stale and any("tool" in w for w in view_plan):
        investigated_at = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(view_plan_at))
        age_min = int((time.time() - view_plan_at) / 60)
        layout.append(
            {
                "kind": "info_card_grid",
                "title": "Data May Be Outdated",
                "props": {
                    "cards": [
                        {
                            "label": "Note",
                            "value": f"Investigated at {investigated_at} ({age_min}min ago). Live data widgets were skipped. Open the agent chat to get fresh diagnostics.",
                        }
                    ],
                },
            }
        )

    valid_kinds = get_valid_kinds()

    # Separate props-only and tool-backed widgets, preserving order
    props_widgets: list[tuple[int, dict]] = []
    tool_widgets: list[tuple[int, dict]] = []

    for i, widget in enumerate(view_plan[:_MAX_WIDGETS]):
        if widget.get("kind", "") not in valid_kinds:
            continue
        if "tool" in widget:
            if not is_stale:
                tool_widgets.append((i, widget))
        elif "props" in widget:
            props_widgets.append((i, widget))

    # Execute tool widgets in parallel
    futures: dict[concurrent.futures.Future, tuple[int, dict]] = {}
    for idx, widget in tool_widgets:
        future = _executor.submit(_execute_tool_widget, widget, item_id, user_token)
        futures[future] = (idx, widget)

    # Collect results, keyed by original index for ordering
    results: dict[int, dict] = {}

    for idx, widget in props_widgets:
        results[idx] = {"kind": widget["kind"], "title": widget.get("title", ""), "props": widget["props"]}

    try:
        for future in concurrent.futures.as_completed(futures, timeout=_TOOL_TIMEOUT + 5):
            idx, widget = futures[future]
            try:
                component = future.result(timeout=1)
                if component:
                    results[idx] = component
            except Exception:
                logger.warning("Widget %s failed", widget.get("title", ""), exc_info=True)
    except TimeoutError:
        logger.warning("View plan execution timed out, some widgets skipped")

    # Append in original plan order
    for idx in sorted(results):
        layout.append(results[idx])

    return layout
