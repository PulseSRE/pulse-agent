"""Core agent WebSocket streaming logic and confirmation flow."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..agent import create_client, run_agent_streaming
from ..config import get_settings
from .sanitize import _sanitize_components

if TYPE_CHECKING:
    from fastapi import WebSocket

logger = logging.getLogger("pulse_agent.api")


@dataclass
class SkillOutput:
    """Result of a single skill execution."""

    text: str = ""
    tools_called: list[str] = field(default_factory=list)
    components: list[dict] = field(default_factory=list)
    token_usage: dict[str, int] = field(default_factory=dict)


class SkillExecutor:
    """Runs a skill via run_agent_streaming with WebSocket event forwarding.

    When skill_tag is empty (single-skill): full streaming to WebSocket.
    When skill_tag is set (parallel): tool events only, text/thinking suppressed.
    """

    def __init__(self, websocket: WebSocket, session_id: str, loop: asyncio.AbstractEventLoop):
        self.websocket = websocket
        self.session_id = session_id
        self.loop = loop

    async def run(
        self,
        config: dict,
        messages: list[dict],
        client,
        write_tools: set[str],
        mode: str,
        *,
        skill_tag: str = "",
        current_user: str = "anonymous",
    ) -> SkillOutput:
        """Run one skill and return structured output."""
        _lock = threading.Lock()
        tools_called: list[str] = []
        components: list[dict] = []
        token_usage: dict[str, int] = {}

        async def _safe_send(data: dict):
            try:
                await self.websocket.send_json(data)
            except Exception:
                pass

        def _schedule_send(data: dict):
            asyncio.run_coroutine_threadsafe(_safe_send(data), self.loop)

        # --- Callbacks ---

        def on_text(delta: str):
            if not skill_tag:
                _schedule_send({"type": "text_delta", "text": delta})

        def on_thinking(delta: str):
            if not skill_tag:
                _schedule_send({"type": "thinking_delta", "thinking": delta})

        def on_tool_use(name: str):
            with _lock:
                tools_called.append(name)
            if skill_tag:
                _schedule_send({"type": "skill_progress", "skill": skill_tag, "status": "tool_use", "tool": name})
            else:
                _schedule_send({"type": "tool_use", "tool": name})

        def on_component(name: str, spec: dict):
            with _lock:
                components.append(spec)
            if not skill_tag:
                _schedule_send({"type": "component", "spec": spec, "tool": name})

        def on_usage(**kwargs):
            with _lock:
                for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"):
                    token_usage[key] = token_usage.get(key, 0) + kwargs.get(key, 0)

        def on_confirm(tool_name: str, tool_input: dict) -> bool:
            if skill_tag and not write_tools:
                logger.warning("Secondary skill '%s' attempted confirmation — denied", skill_tag)
                return False
            try:
                if not _ws_alive.get(self.session_id, True):
                    return False
                confirm_future = asyncio.run_coroutine_threadsafe(
                    _create_and_register_future(self.session_id, tool_name, tool_input, self.websocket),
                    self.loop,
                ).result(timeout=5)
                waiter: concurrent.futures.Future[bool] = concurrent.futures.Future()

                def _on_done(f):
                    try:
                        waiter.set_result(f.result())
                    except Exception:
                        waiter.set_result(False)

                self.loop.call_soon_threadsafe(confirm_future.add_done_callback, _on_done)
                approved = waiter.result(timeout=120)
                logger.info("Confirmation resolved: tool=%s approved=%s", tool_name, approved)
                return approved
            except Exception as e:
                logger.error("Confirmation failed: %s", e)
                _schedule_send({"type": "error", "message": "Confirmation timed out or failed. Operation cancelled."})
                return False
            finally:
                _pending_confirms.pop(self.session_id, None)

        _base_tool_result_handler = _build_tool_result_handler(self.session_id, skill_tag or mode, write_tools)

        def on_tool_result(info: dict):
            _base_tool_result_handler(info)
            if skill_tag and info.get("status") == "success":
                _schedule_send(
                    {
                        "type": "skill_progress",
                        "skill": skill_tag,
                        "status": "tool_complete",
                        "tool": info["tool_name"],
                        "duration_ms": info.get("duration_ms", 0),
                    }
                )

        effective_system = config["system_prompt"]
        if get_settings().memory:
            try:
                from ..memory import get_manager

                manager = get_manager()
                if manager:
                    last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
                    if isinstance(last_user, str) and last_user:
                        effective_system = manager.augment_prompt(config["system_prompt"], last_user)
            except Exception:
                logger.debug("Memory retrieval failed for skill %s", skill_tag or mode, exc_info=True)

        full_response = await asyncio.to_thread(
            run_agent_streaming,
            client=client,
            messages=messages if not skill_tag else list(messages),
            system_prompt=effective_system,
            tool_defs=config["tool_defs"],
            tool_map=config["tool_map"],
            write_tools=write_tools,
            on_text=on_text,
            on_thinking=on_thinking,
            on_tool_use=on_tool_use,
            on_confirm=on_confirm,
            on_component=on_component,
            on_tool_result=on_tool_result,
            on_usage=on_usage,
            mode=skill_tag or mode,
        )

        text = full_response if isinstance(full_response, str) else full_response[0]

        return SkillOutput(
            text=text,
            tools_called=list(tools_called),
            components=list(components),
            token_usage=dict(token_usage),
        )


# WebSocket connection liveness tracking
_ws_alive: dict[str, bool] = {}

# Hallucination detection counter — tracks suspected tool hallucinations
_hallucination_count: int = 0

# Pending confirmation requests keyed by session ID (uuid4, NOT id(websocket))
_pending_confirms: dict[str, asyncio.Future] = {}
# JIT nonces for confirmation -- prevents replay/forgery
_pending_nonces: dict[str, str] = {}
# Timestamps for TTL-based cleanup
_pending_timestamps: dict[str, float] = {}
# TTL for stale pending state (2 minutes)
_PENDING_TTL_SECONDS = 120

# Max WebSocket message size (1MB)
MAX_MESSAGE_SIZE = 1_048_576

# Rate limiting: max messages per minute per connection
MAX_MESSAGES_PER_MINUTE = 10


def _build_tool_result_handler(session_id: str, agent_mode: str, write_tools: set[str]):
    """Build an on_tool_result callback that records to tool_usage table."""
    # Cache MCP tool names for source detection (built once per session)
    _mcp_names: set[str] | None = None

    def _get_mcp_names() -> set[str]:
        nonlocal _mcp_names
        if _mcp_names is None:
            try:
                from ..mcp_client import list_mcp_tools

                _mcp_names = {t["name"] for t in list_mcp_tools()}
            except Exception:
                _mcp_names = set()
        return _mcp_names

    def on_tool_result(info: dict):
        global _hallucination_count
        try:
            from ..skill_loader import get_tool_category
            from ..tool_usage import record_tool_call

            tool_name = info["tool_name"]
            tool_source = "mcp" if tool_name in _get_mcp_names() else "native"

            record_tool_call(
                session_id=session_id,
                turn_number=info["turn_number"],
                agent_mode=agent_mode,
                tool_name=tool_name,
                tool_category=get_tool_category(tool_name),
                input_data=info.get("input"),
                status=info["status"],
                error_message=info.get("error_message"),
                error_category=info.get("error_category"),
                duration_ms=info.get("duration_ms", 0),
                result_bytes=info.get("result_bytes", 0),
                requires_confirmation=tool_name in write_tools,
                was_confirmed=info.get("was_confirmed"),
                tool_source=tool_source,
            )

            # Hallucination detection: unknown tools or empty results
            if info.get("status") == "error":
                err_msg = (info.get("error_message") or "").lower()
                if "unknown tool" in err_msg or "not found" in err_msg or "no such tool" in err_msg:
                    _hallucination_count += 1
                    logger.warning(
                        "Suspected tool hallucination (#%d): tool=%s error=%s session=%s",
                        _hallucination_count,
                        tool_name,
                        info.get("error_message", "")[:200],
                        session_id,
                    )

            result_bytes = info.get("result_bytes", 0)
            if info.get("status") == "success" and result_bytes == 0:
                logger.warning(
                    "Tool returned empty result: tool=%s session=%s",
                    tool_name,
                    session_id,
                )
        except Exception:
            logger.debug("Tool result recording failed", exc_info=True)

    return on_tool_result


async def _run_agent_ws(
    websocket: WebSocket,
    messages: list[dict],
    system_prompt: str | list[dict[str, Any]],
    tool_defs: list,
    tool_map: dict,
    write_tools: set[str],
    session_id: str,
    current_user: str = "anonymous",
    mode: str = "sre",
    turn_number: int = 1,
    user_query: str = "",
):
    """Run an agent turn and stream results over WebSocket."""
    from ..view_tools import set_current_user

    set_current_user(current_user)
    client = create_client()
    ws_id = session_id
    _turn_starts[ws_id] = time.monotonic()
    loop = asyncio.get_running_loop()

    # Start memory timing before agent runs
    manager = None
    if get_settings().memory:
        try:
            from ..memory import get_manager

            manager = get_manager()
            if manager:
                manager.start_turn()
        except Exception:
            logger.debug("Memory manager init failed", exc_info=True)

    # Run via SkillExecutor — handles callbacks, memory augmentation, tool recording
    executor = SkillExecutor(websocket, session_id, loop)
    config = {
        "system_prompt": system_prompt,
        "tool_defs": tool_defs,
        "tool_map": tool_map,
    }
    output = await executor.run(
        config,
        messages,
        client,
        write_tools,
        mode,
        current_user=current_user,
    )

    full_response = output.text
    session_tools = output.tools_called
    session_components = output.components
    turn_token_usage = output.token_usage

    # Process structured signals from tool results -- no regex scanning needed.
    # Tools return signals as "__SIGNAL__" + JSON in their text result.
    from ..view_tools import SIGNAL_PREFIX

    _view_updated_ids = set()

    def _extract_signals(messages_list):
        """Extract structured signals from tool_result content blocks ONLY.

        Security: only scans tool_result blocks (role=user, type=tool_result).
        User-typed messages are never scanned, preventing signal injection.
        """
        signals = []
        for msg in messages_list:
            # Only scan tool result messages (role=user with list content containing tool_result blocks)
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                text = block.get("content", "")
                if text and SIGNAL_PREFIX in text:
                    try:
                        json_str = text.split(SIGNAL_PREFIX, 1)[1].strip()
                        signals.append(json.loads(json_str))
                    except (json.JSONDecodeError, IndexError):
                        pass
        return signals

    for sig in _extract_signals(messages):
        sig_type = sig.get("type")

        if sig_type == "view_spec" and session_components:
            import time as _time

            from .. import db as _db
            from ..quality_engine import evaluate_components

            _sanitize_components(session_components)

            # Validate and deduplicate components before saving
            qr = evaluate_components(session_components, positions=None)
            session_components = qr.components  # Use deduped list
            if qr.deduped_count > 0:
                logger.info("Deduped %d duplicate components from view", qr.deduped_count)

            if not qr.valid:
                # Log warnings but SAVE ANYWAY -- the agent needs the view to exist
                # so critique_view can score it and guide fixes. Blocking here causes
                # the agent to enter a confused loop ("view not found").
                logger.warning(
                    "View has validation issues (saving anyway): %s",
                    "; ".join(qr.errors),
                )
                await websocket.send_json(
                    {
                        "type": "view_validation_warning",
                        "errors": qr.errors,
                        "warnings": qr.warnings,
                        "deduped_count": qr.deduped_count,
                    }
                )

            view_id = sig.get("view_id", f"cv-{uuid.uuid4().hex[:12]}")
            view_title = sig.get("title", "Custom View")
            view_desc = sig.get("description", "")
            view_template = sig.get("template", "")

            # Lifecycle fields (Phase 3B)
            view_type = sig.get("view_type", "custom")
            view_status = sig.get("status", "active")
            trigger_source = sig.get("trigger_source", "user")
            finding_id = sig.get("finding_id")
            view_visibility = sig.get("visibility", "private")

            # Apply view-type layout template for agent views (hero + tabs)
            from ..layout_engine import build_view_layout, compute_layout

            if view_type != "custom":
                session_components = build_view_layout(session_components, view_type, view_status)

            positions = compute_layout(session_components)

            # Dedup: session view (same title) > finding_id > title match
            existing = None
            if _view_updated_ids:
                for vid in _view_updated_ids:
                    candidate = _db.get_view(vid, current_user)
                    if candidate and candidate.get("title") == view_title:
                        existing = candidate
                        break
            if not existing and finding_id:
                existing = _db.get_view_by_finding(finding_id)
            if not existing:
                existing = _db.get_view_by_title(current_user, view_title)
            if existing:
                old_layout = existing.get("layout", [])
                merged_layout = old_layout + session_components
                # Re-validate the merged layout (dedup + structural checks)
                qr_merged = evaluate_components(merged_layout, positions=None)
                merged_layout = qr_merged.components  # Always use deduped
                if not qr_merged.valid:
                    logger.warning("Merged view has issues (saving anyway): %s", "; ".join(qr_merged.errors))
                positions = compute_layout(merged_layout)
                update_kwargs: dict = {"layout": merged_layout, "description": view_desc}
                if positions:
                    update_kwargs["positions"] = positions
                # Use view's actual owner for update (identity may differ)
                actual_owner = existing.get("owner", current_user)
                _db.update_view(existing["id"], actual_owner, _snapshot=True, _action="agent_update", **update_kwargs)
                _view_updated_ids.add(existing["id"])
                logger.info(
                    "Updated existing view: id=%s title=%s (+%d components)",
                    existing["id"],
                    view_title,
                    len(session_components),
                )
                # Emit view_spec so the UI navigates to the updated view
                spec = {
                    "id": existing["id"],
                    "title": view_title,
                    "description": view_desc,
                    "layout": merged_layout,
                    "positions": positions or {},
                    "generatedAt": int(_time.time() * 1000),
                    "view_type": existing.get("view_type", view_type),
                    "status": existing.get("status", view_status),
                    "visibility": existing.get("visibility", view_visibility),
                }
                await websocket.send_json({"type": "view_spec", "spec": spec})
                # Don't clear session_components -- agent may call create_dashboard again in same turn
            else:
                _db.save_view(
                    current_user,
                    view_id,
                    view_title,
                    view_desc,
                    session_components,
                    positions=positions,
                    view_type=view_type,
                    status=view_status,
                    trigger_source=trigger_source,
                    finding_id=finding_id,
                    visibility=view_visibility,
                )
                _view_updated_ids.add(view_id)
                logger.info(
                    "Saved new view: id=%s title=%s type=%s components=%d",
                    view_id,
                    view_title,
                    view_type,
                    len(session_components),
                )
                spec = {
                    "id": view_id,
                    "title": view_title,
                    "description": view_desc,
                    "layout": session_components,
                    "positions": positions or {},
                    "generatedAt": int(_time.time() * 1000),
                    "view_type": view_type,
                    "status": view_status,
                    "trigger_source": trigger_source,
                    "finding_id": finding_id,
                    "visibility": view_visibility,
                }
                if view_template:
                    spec["templateId"] = view_template

                await websocket.send_json({"type": "view_spec", "spec": spec})
                # Don't clear -- components persist for the rest of the turn

        elif sig_type == "view_updated":
            _view_updated_ids.add(sig.get("view_id", ""))

        elif sig_type == "add_widget" and session_components:
            from .. import db as _db

            vid = sig.get("view_id", "")
            _view_updated_ids.add(vid)
            latest_component = session_components[-1]
            view = _db.get_view(vid, current_user)
            if view:
                existing_layout = view.get("layout", [])
                new_kind = latest_component.get("kind", "")
                new_title = latest_component.get("title", "")
                already_exists = any(w.get("kind") == new_kind and w.get("title") == new_title for w in existing_layout)
                if already_exists:
                    logger.info("Skipping duplicate widget: kind=%s title=%s", new_kind, new_title)
                else:
                    new_layout = existing_layout + [latest_component]
                    _db.update_view(vid, current_user, _snapshot=True, _action="add_widget", layout=new_layout)

    for vid in _view_updated_ids:
        if not vid:
            continue
        try:
            await websocket.send_json({"type": "view_updated", "viewId": vid})
        except Exception:
            pass

    # Record turn-level data (tools + token usage + routing decision)
    try:
        from ..skill_loader import get_last_routing_decision
        from ..tool_usage import record_turn

        offered = [td.get("name", "") for td in tool_defs if isinstance(td, dict)]

        record_turn(
            session_id=ws_id,
            turn_number=turn_number,
            agent_mode=mode,
            query_summary=user_query[:200],
            tools_offered=offered,
            tools_called=session_tools,
            routing_decision=get_last_routing_decision(),
            **turn_token_usage,
        )
    except Exception:
        logger.debug("Failed to record turn", exc_info=True)

    # Record prompt log (what system prompt was sent, with token costs)
    try:
        from ..prompt_builder import get_last_assembled
        from ..prompt_log import record_prompt

        assembled = get_last_assembled()
        if assembled:
            record_prompt(
                session_id=ws_id,
                turn_number=turn_number,
                token_usage=turn_token_usage,
                **{k: v for k, v in assembled.items() if k in ("static", "dynamic", "skill_name", "skill_version")},
            )
    except Exception:
        logger.debug("Failed to record prompt log", exc_info=True)

    # Record interaction for memory scoring (start_turn was called before agent ran)
    if manager and hasattr(manager, "finish_turn") and user_query:
        try:
            for t in session_tools:
                manager.record_tool_call(t, {})
            manager.finish_turn(user_query, full_response)
        except Exception:
            pass

    # Return response + metadata tuple for the caller
    turn_meta = {
        "tools_called": list(session_tools),
        "tool_count": len(session_tools),
        "duration_ms": int((time.monotonic() - _turn_starts.pop(ws_id, time.monotonic())) * 1000),
        **turn_token_usage,
    }

    return full_response, turn_meta


# Module-level storage for turn metadata (consumed by ws_endpoints after each turn)
_last_turn_meta: dict = {}
# Per-session turn start times (keyed by session_id to avoid cross-session clobbering)
_turn_starts: dict[str, float] = {}


def _cleanup_stale_pending():
    """Remove stale pending confirms/nonces older than TTL."""
    now = time.time()
    stale = [sid for sid, ts in _pending_timestamps.items() if now - ts > _PENDING_TTL_SECONDS]
    for sid in stale:
        future = _pending_confirms.pop(sid, None)
        if future and not future.done():
            future.cancel()
        _pending_nonces.pop(sid, None)
        _pending_timestamps.pop(sid, None)
    if stale:
        logger.info("Cleaned up %d stale pending confirmation(s)", len(stale))


async def _create_and_register_future(ws_id: str, tool_name: str, tool_input: dict, websocket: WebSocket):
    """Create a Future on the event loop and send the confirm request with a JIT nonce."""
    import secrets

    _cleanup_stale_pending()  # Opportunistic cleanup
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    nonce = secrets.token_urlsafe(16)
    _pending_confirms[ws_id] = future
    _pending_nonces[ws_id] = nonce
    _pending_timestamps[ws_id] = time.time()
    await websocket.send_json(
        {
            "type": "confirm_request",
            "tool": tool_name,
            "input": tool_input,
            "nonce": nonce,
        }
    )
    return future


def _make_receive_loop(
    websocket: WebSocket,
    session_id: str,
    messages: list[dict],
    incoming: asyncio.Queue,
):
    """Create a shared WebSocket receive loop for SRE/Security/Auto-agent endpoints.

    Handles: confirm_response (with nonce + memory learning), clear, feedback, message routing.
    """
    from fastapi import WebSocketDisconnect

    async def _receive_loop():
        try:
            while True:
                raw = await websocket.receive_text()
                if len(raw) > MAX_MESSAGE_SIZE:
                    await websocket.send_json({"type": "error", "message": "Message too large"})
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    await websocket.send_json({"type": "error", "message": "Invalid JSON"})
                    continue

                msg_type = data.get("type")

                if msg_type == "confirm_response":
                    future = _pending_confirms.get(session_id)
                    expected_nonce = _pending_nonces.get(session_id)
                    received_nonce = data.get("nonce", "")

                    if not future or future.done():
                        logger.warning("Confirm response received but no pending future (session=%s)", session_id)
                    elif expected_nonce and received_nonce != expected_nonce:
                        logger.warning("Confirm response nonce mismatch -- possible replay (session=%s)", session_id)
                        future.set_result(False)
                    else:
                        approved = data.get("approved", False)
                        future.set_result(approved)
                        logger.info("Confirmation received: approved=%s nonce=%s", approved, received_nonce[:8])
                        try:
                            from ..memory import get_manager

                            manager = get_manager()
                            if manager and approved:
                                manager.update_last_outcome(True)
                        except Exception:
                            pass
                    _pending_nonces.pop(session_id, None)
                    continue

                if msg_type == "clear":
                    messages.clear()
                    await websocket.send_json({"type": "cleared"})
                    continue

                if msg_type == "feedback":
                    resolved = data.get("resolved", False)
                    try:
                        from ..memory import get_manager

                        manager = get_manager()
                        if manager:
                            result = manager.update_last_outcome(resolved)
                            await websocket.send_json(
                                {
                                    "type": "feedback_ack",
                                    "resolved": resolved,
                                    "score": result.get("score", 0) if result else 0,
                                    "runbookExtracted": bool(result and result.get("runbook_id")),
                                }
                            )
                        else:
                            await websocket.send_json({"type": "feedback_ack", "resolved": resolved, "score": 0})
                    except Exception as e:
                        logger.debug("Feedback recording failed: %s", e)
                        await websocket.send_json({"type": "feedback_ack", "resolved": resolved, "score": 0})

                    # Link feedback to tool tracking
                    try:
                        from ..tool_usage import update_turn_feedback

                        update_turn_feedback(session_id=session_id, feedback="positive" if resolved else "negative")
                    except Exception:
                        pass

                    continue

                await incoming.put(data)
        except WebSocketDisconnect:
            _ws_alive[session_id] = False
            await incoming.put(None)
        except Exception:
            _ws_alive[session_id] = False
            await incoming.put(None)

    return _receive_loop
