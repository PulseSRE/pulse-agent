"""FastAPI WebSocket server for the Pulse Agent.

Protocol Version: 2 (see API_CONTRACT.md for full specification)

Exposes the SRE and Security agents over WebSocket for integration
with the OpenShift Pulse web UI. V2 adds /ws/monitor for autonomous scanning.
"""

from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from importlib.metadata import version as pkg_version

from fastapi import FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect

from .agent import (
    ALL_TOOLS as SRE_ALL_TOOLS,
)
from .agent import (
    SYSTEM_PROMPT as SRE_SYSTEM_PROMPT,
)
from .agent import (
    TOOL_DEFS as SRE_TOOL_DEFS,
)
from .agent import (
    TOOL_MAP as SRE_TOOL_MAP,
)
from .agent import (
    WRITE_TOOLS,
    create_client,
    run_agent_streaming,
)
from .orchestrator import build_orchestrated_config, classify_intent
from .security_agent import (
    ALL_TOOLS as SEC_ALL_TOOLS,
)
from .security_agent import (
    SECURITY_SYSTEM_PROMPT,
)
from .security_agent import (
    TOOL_DEFS as SEC_TOOL_DEFS,
)
from .security_agent import (
    TOOL_MAP as SEC_TOOL_MAP,
)

logger = logging.getLogger("pulse_agent.api")

_EVAL_STATUS_CACHE: dict | None = None
_EVAL_STATUS_CACHE_TS_MS = 0
_EVAL_STATUS_CACHE_TTL_MS = 60_000
_EVAL_STATUS_LOCK = asyncio.Lock()

# WebSocket connection liveness tracking
_ws_alive: dict[str, bool] = {}

# Pending confirmation requests keyed by session ID (uuid4, NOT id(websocket))
_pending_confirms: dict[str, asyncio.Future] = {}
# JIT nonces for confirmation — prevents replay/forgery
_pending_nonces: dict[str, str] = {}
# Timestamps for TTL-based cleanup
_pending_timestamps: dict[str, float] = {}
# TTL for stale pending state (2 minutes)
_PENDING_TTL_SECONDS = 120

# Max WebSocket message size (1MB)
MAX_MESSAGE_SIZE = 1_048_576

# Rate limiting: max messages per minute per connection
MAX_MESSAGES_PER_MINUTE = 10

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
        # User is viewing a custom dashboard — inject view details
        try:
            from . import db as _ctx_db

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
            pass
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


def _verify_ws_token(websocket) -> str:
    """Verify WebSocket token and return the client token. Closes with 4001 if invalid."""
    client_token = websocket.query_params.get("token", "")
    expected = os.environ.get("PULSE_AGENT_WS_TOKEN", "")
    if not expected or not hmac.compare_digest(client_token, expected):
        return ""
    return client_token


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Verify k8s connectivity and auth config on startup."""
    from .logging_config import configure_logging

    configure_logging()
    # Ensure pulse_agent loggers are at INFO so monitor scan output is visible
    logging.getLogger("pulse_agent").setLevel(logging.INFO)

    if not os.environ.get("PULSE_AGENT_WS_TOKEN"):
        logger.critical(
            "PULSE_AGENT_WS_TOKEN is not set. WebSocket endpoint is UNAUTHENTICATED. "
            "Set this variable or connections will be rejected."
        )
    try:
        from .k8s_client import get_core_client

        get_core_client().list_namespace(limit=1)
        logger.info("Connected to cluster")
    except Exception:
        logger.warning("Cannot connect to cluster — tools may fail")
    # Initialize memory system if enabled
    if os.environ.get("PULSE_AGENT_MEMORY", "1") == "1":
        try:
            from .memory import MemoryManager, set_manager

            manager = MemoryManager()
            set_manager(manager)
            logger.info("Memory system initialized")
        except Exception as e:
            logger.warning("Memory system init failed: %s", e)
    yield


def _get_agent_version() -> str:
    try:
        return pkg_version("openshift-sre-agent")
    except Exception:
        return "dev"


app = FastAPI(title="Pulse Agent API", version=_get_agent_version(), lifespan=lifespan)


PROTOCOL_VERSION = "2"


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/version")
async def version():
    """API protocol version. UI checks this on connect to detect mismatches."""
    return {
        "protocol": PROTOCOL_VERSION,
        "agent": _get_agent_version(),
        "tools": len(SRE_ALL_TOOLS) + len(SEC_ALL_TOOLS),
        "features": ["component_specs", "ws_token_auth", "rate_limiting", "monitor", "fix_history", "predictions"],
    }


@app.get("/health")
async def health(
    authorization: str | None = Header(None),
    token: str | None = Query(None),
):
    _verify_rest_token(authorization, token)
    from .agent import _circuit_breaker
    from .error_tracker import get_tracker

    tracker = get_tracker()
    summary = tracker.get_summary()
    return {
        "status": "degraded" if _circuit_breaker.is_open else "ok",
        "circuit_breaker": {
            "state": _circuit_breaker.state,
            "failure_count": _circuit_breaker.failure_count,
            "recovery_timeout": _circuit_breaker.recovery_timeout,
        },
        "errors": {
            "total": summary["total"],
            "by_category": summary["by_category"],
            "recent": tracker.get_recent(limit=5),
        },
        "investigations": get_investigation_stats(),
        "autofix_paused": is_autofix_paused(),
    }


@app.get("/tools")
async def list_tools(
    authorization: str | None = Header(None),
    token: str | None = Query(None),
):
    """List all available tools grouped by mode, with write-op flags."""
    _verify_rest_token(authorization, token)
    from .harness import get_tool_category

    return {
        "sre": [
            {
                "name": t.name,
                "description": t.description,
                "requires_confirmation": t.name in WRITE_TOOLS,
                "category": get_tool_category(t.name),
            }
            for t in SRE_ALL_TOOLS
        ],
        "security": [
            {
                "name": t.name,
                "description": t.description,
                "requires_confirmation": False,
                "category": get_tool_category(t.name),
            }
            for t in SEC_ALL_TOOLS
        ],
        "write_tools": sorted(WRITE_TOOLS),
    }


@app.get("/agents")
async def list_agents(
    authorization: str | None = Header(None),
    token: str | None = Query(None),
):
    """List all agent modes with metadata."""
    _verify_rest_token(authorization, token)
    from .tool_usage import get_agents_metadata

    return get_agents_metadata()


@app.get("/tools/usage/stats")
async def get_tools_usage_stats(
    time_from: str | None = Query(None, alias="from"),
    time_to: str | None = Query(None, alias="to"),
    authorization: str | None = Header(None),
    token: str | None = Query(None),
):
    """Aggregated tool usage statistics."""
    _verify_rest_token(authorization, token)
    from .tool_usage import get_usage_stats

    return get_usage_stats(time_from=time_from, time_to=time_to)


@app.get("/tools/usage/chains")
async def get_tools_usage_chains(
    authorization: str | None = Header(None),
    token: str | None = Query(None),
):
    """Discovered tool call chains (common sequences)."""
    _verify_rest_token(authorization, token)
    from .tool_chains import discover_chains

    return discover_chains()


@app.get("/tools/usage")
async def get_tools_usage(
    tool_name: str | None = Query(None),
    agent_mode: str | None = Query(None),
    status: str | None = Query(None),
    session_id: str | None = Query(None),
    time_from: str | None = Query(None, alias="from"),
    time_to: str | None = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    authorization: str | None = Header(None),
    token: str | None = Query(None),
):
    """Paginated audit log of tool invocations."""
    _verify_rest_token(authorization, token)
    from .tool_usage import query_usage

    return query_usage(
        tool_name=tool_name,
        agent_mode=agent_mode,
        status=status,
        session_id=session_id,
        time_from=time_from,
        time_to=time_to,
        page=page,
        per_page=per_page,
    )


def _build_tool_result_handler(session_id: str, agent_mode: str, write_tools: set[str]):
    """Build an on_tool_result callback that records to tool_usage table."""

    def on_tool_result(info: dict):
        try:
            from .harness import get_tool_category
            from .tool_usage import record_tool_call

            record_tool_call(
                session_id=session_id,
                turn_number=info["turn_number"],
                agent_mode=agent_mode,
                tool_name=info["tool_name"],
                tool_category=get_tool_category(info["tool_name"]),
                input_data=info.get("input"),
                status=info["status"],
                error_message=info.get("error_message"),
                error_category=info.get("error_category"),
                duration_ms=info.get("duration_ms", 0),
                result_bytes=info.get("result_bytes", 0),
                requires_confirmation=info["tool_name"] in write_tools,
                was_confirmed=info.get("was_confirmed"),
            )
        except Exception:
            logger.debug("Tool result recording failed", exc_info=True)

    return on_tool_result


async def _run_agent_ws(
    websocket: WebSocket,
    messages: list[dict],
    system_prompt: str,
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
    from .view_tools import set_current_user

    set_current_user(current_user)
    client = create_client()
    ws_id = session_id

    # Capture the running loop BEFORE entering the thread
    loop = asyncio.get_running_loop()

    async def _safe_send(data: dict):
        """Send JSON to WebSocket, swallowing errors if client disconnected."""
        try:
            await websocket.send_json(data)
        except Exception:
            pass  # Client disconnected — expected during shutdown

    def _schedule_send(data: dict):
        """Thread-safe: schedule a WebSocket send on the event loop."""
        asyncio.run_coroutine_threadsafe(_safe_send(data), loop)

    def on_text(delta: str):
        _schedule_send({"type": "text_delta", "text": delta})

    def on_thinking(delta: str):
        _schedule_send({"type": "thinking_delta", "thinking": delta})

    session_tools: list[str] = []
    session_components: list[dict] = []
    turn_token_usage: dict[str, int] = {}

    def on_usage(**kwargs):
        # Accumulate tokens across iterations (agent loop may call API multiple times)
        for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"):
            turn_token_usage[key] = turn_token_usage.get(key, 0) + kwargs.get(key, 0)

    def on_tool_use(name: str):
        session_tools.append(name)
        _schedule_send({"type": "tool_use", "tool": name})

    def on_component(name: str, spec: dict):
        session_components.append(spec)
        _schedule_send({"type": "component", "spec": spec, "tool": name})

    def on_confirm(tool_name: str, tool_input: dict) -> bool:
        """Request confirmation from the web UI and block until response."""
        try:
            # Check if the WebSocket is still alive before waiting
            if not _ws_alive.get(ws_id, True):
                return False

            # Create the future and send the confirm request to the UI
            confirm_future = asyncio.run_coroutine_threadsafe(
                _create_and_register_future(ws_id, tool_name, tool_input, websocket),
                loop,
            ).result(timeout=5)

            # Block the agent thread — wait for the UI to set the future result
            waiter = concurrent.futures.Future()

            def _on_done(f):
                try:
                    waiter.set_result(f.result())
                except Exception:
                    waiter.set_result(False)

            loop.call_soon_threadsafe(confirm_future.add_done_callback, _on_done)

            approved = waiter.result(timeout=120)
            logger.info("Confirmation resolved: tool=%s approved=%s", tool_name, approved)
            return approved

        except Exception as e:
            logger.error("Confirmation failed: %s", e)
            _schedule_send({"type": "error", "message": "Confirmation timed out or failed. Operation cancelled."})
            return False
        finally:
            _pending_confirms.pop(ws_id, None)

    # Augment system prompt with memory context and start timing
    effective_system = system_prompt
    manager = None
    if os.environ.get("PULSE_AGENT_MEMORY", "1") == "1":
        try:
            from .memory import get_manager

            manager = get_manager()
            if manager:
                last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
                if isinstance(last_user, str) and last_user:
                    effective_system = manager.augment_prompt(system_prompt, last_user)
                manager.start_turn()  # Start timing BEFORE agent runs
        except Exception as e:
            logger.debug("Memory retrieval failed: %s", e)

    # Build tool result recording handler
    tool_result_handler = _build_tool_result_handler(ws_id, mode, write_tools)

    # Run the blocking agent in a thread
    full_response = await asyncio.to_thread(
        run_agent_streaming,
        client=client,
        messages=messages,
        system_prompt=effective_system,
        tool_defs=tool_defs,
        tool_map=tool_map,
        write_tools=write_tools,
        on_text=on_text,
        on_thinking=on_thinking,
        on_tool_use=on_tool_use,
        on_confirm=on_confirm,
        on_component=on_component,
        on_tool_result=tool_result_handler,
        on_usage=on_usage,
        mode=mode,
    )

    # Process structured signals from tool results — no regex scanning needed.
    # Tools return signals as "__SIGNAL__" + JSON in their text result.
    from .view_tools import SIGNAL_PREFIX

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

            from . import db as _db
            from .view_validator import validate_components as _validate

            _sanitize_components(session_components)

            # Validate and deduplicate components before saving
            _vr = _validate(session_components)
            session_components = _vr.components  # Use deduped list
            if _vr.deduped_count > 0:
                logger.info("Deduped %d duplicate components from view", _vr.deduped_count)

            if not _vr.valid:
                # Log warnings but SAVE ANYWAY — the agent needs the view to exist
                # so critique_view can score it and guide fixes. Blocking here causes
                # the agent to enter a confused loop ("view not found").
                logger.warning(
                    "View has validation issues (saving anyway): %s",
                    "; ".join(_vr.errors),
                )
                await websocket.send_json(
                    {
                        "type": "view_validation_warning",
                        "errors": _vr.errors,
                        "warnings": _vr.warnings,
                        "deduped_count": _vr.deduped_count,
                    }
                )

            view_id = sig.get("view_id", f"cv-{uuid.uuid4().hex[:12]}")
            view_title = sig.get("title", "Custom View")
            view_desc = sig.get("description", "")
            view_template = sig.get("template", "")

            # Compute positions using semantic layout engine
            from .layout_engine import compute_layout

            positions = compute_layout(session_components)

            existing = _db.get_view_by_title(current_user, view_title)
            if existing:
                old_layout = existing.get("layout", [])
                merged_layout = old_layout + session_components
                # Re-validate the merged layout (dedup + structural checks)
                _vr_merged = _validate(merged_layout)
                merged_layout = _vr_merged.components  # Always use deduped
                if not _vr_merged.valid:
                    logger.warning("Merged view has issues (saving anyway): %s", "; ".join(_vr_merged.errors))
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
                }
                await websocket.send_json({"type": "view_spec", "spec": spec})
                # Don't clear session_components — agent may call create_dashboard again in same turn
            else:
                _db.save_view(current_user, view_id, view_title, view_desc, session_components, positions=positions)
                _view_updated_ids.add(view_id)
                logger.info(
                    "Saved new view: id=%s title=%s components=%d template=%s",
                    view_id,
                    view_title,
                    len(session_components),
                    view_template or "none",
                )
                spec = {
                    "id": view_id,
                    "title": view_title,
                    "description": view_desc,
                    "layout": session_components,
                    "positions": positions or {},
                    "generatedAt": int(_time.time() * 1000),
                }
                if view_template:
                    spec["templateId"] = view_template

                await websocket.send_json({"type": "view_spec", "spec": spec})
                # Don't clear — components persist for the rest of the turn

        elif sig_type == "view_updated":
            _view_updated_ids.add(sig.get("view_id", ""))

        elif sig_type == "add_widget" and session_components:
            from . import db as _db

            vid = sig.get("view_id", "")
            _view_updated_ids.add(vid)
            latest_component = session_components[-1]
            view = _db.get_view(vid, current_user)
            if view:
                new_layout = view.get("layout", []) + [latest_component]
                _db.update_view(vid, current_user, _snapshot=True, _action="add_widget", layout=new_layout)

    for vid in _view_updated_ids:
        if not vid:
            continue
        try:
            await websocket.send_json({"type": "view_updated", "viewId": vid})
        except Exception:
            pass

    # Record turn-level data (tools + token usage)
    try:
        from .tool_usage import record_turn

        # Determine tools offered from tool_defs
        offered = [td.get("name", "") for td in tool_defs if isinstance(td, dict)]

        record_turn(
            session_id=ws_id,
            turn_number=turn_number,
            agent_mode=mode,
            query_summary=user_query[:200],
            tools_offered=offered,
            tools_called=session_tools,
            **turn_token_usage,
        )
    except Exception:
        logger.debug("Failed to record turn", exc_info=True)

    # Record interaction for memory scoring (start_turn was called before agent ran)
    if manager and hasattr(manager, "finish_turn"):
        try:
            user_msgs = [m for m in messages if m["role"] == "user"]
            if user_msgs:
                query = (
                    user_msgs[-1]["content"]
                    if isinstance(user_msgs[-1]["content"], str)
                    else str(user_msgs[-1]["content"])
                )
                for t in session_tools:
                    manager.record_tool_call(t, {})
                manager.finish_turn(query, full_response)
        except Exception:
            pass

    return full_response


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
                        logger.warning("Confirm response nonce mismatch — possible replay (session=%s)", session_id)
                        future.set_result(False)
                    else:
                        approved = data.get("approved", False)
                        future.set_result(approved)
                        logger.info("Confirmation received: approved=%s nonce=%s", approved, received_nonce[:8])
                        try:
                            from .memory import get_manager

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
                        from .memory import get_manager

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
                        from .tool_usage import update_turn_feedback

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


@app.websocket("/ws/{mode}")
async def websocket_agent(websocket: WebSocket, mode: str):
    """WebSocket endpoint for agent chat.

    Mode: 'sre' or 'security'

    Client sends JSON messages:
        {"type": "message", "content": "..."}
        {"type": "confirm_response", "approved": true/false}
        {"type": "clear"}

    Server sends JSON messages:
        {"type": "text_delta", "text": "..."}
        {"type": "thinking_delta", "thinking": "..."}
        {"type": "tool_use", "tool": "tool_name"}
        {"type": "confirm_request", "tool": "...", "input": {...}}
        {"type": "done", "full_response": "..."}
        {"type": "error", "message": "..."}
    """
    if mode == "monitor":
        # Redirect to the dedicated monitor handler — /ws/{mode} catches it
        # before /ws/monitor due to registration order
        await websocket_monitor(websocket)
        return
    if mode == "agent":
        # Redirect to the auto-routing agent handler
        await websocket_auto_agent(websocket)
        return
    if mode not in ("sre", "security", "view_designer"):
        await websocket.close(code=4000, reason="Invalid mode. Use 'sre', 'security', 'view_designer', or 'agent'.")
        return

    # Token authentication — mandatory unless explicitly disabled
    client_token = _verify_ws_token(websocket)
    if not client_token:
        await websocket.close(4001, "Unauthorized")
        return

    await websocket.accept()
    session_id = str(uuid.uuid4())
    _ws_alive[session_id] = True
    messages: list[dict] = []
    # Rate limiting state
    message_timestamps: list[float] = []

    # Extract user identity for view tools
    try:
        ws_user = _get_current_user(
            x_forwarded_access_token=websocket.headers.get("x-forwarded-access-token"),
            x_forwarded_user=websocket.headers.get("x-forwarded-user"),
        )
    except HTTPException:
        logger.warning("WebSocket session %s: no valid user token, view operations will be unavailable", session_id)
        ws_user = "anonymous"

    if mode == "sre":
        system_prompt = SRE_SYSTEM_PROMPT
        tool_defs = SRE_TOOL_DEFS
        tool_map = SRE_TOOL_MAP
        write_tools = WRITE_TOOLS
    else:
        system_prompt = SECURITY_SYSTEM_PROMPT
        tool_defs = SEC_TOOL_DEFS
        tool_map = SEC_TOOL_MAP
        write_tools = set()

    # Message queue for incoming messages while agent is running
    incoming: asyncio.Queue = asyncio.Queue()
    _receive_loop = _make_receive_loop(websocket, session_id, messages, incoming)
    receive_task = asyncio.create_task(_receive_loop())
    turn_counter = 0

    try:
        while True:
            data = await incoming.get()
            if data is None:
                break  # Client disconnected

            msg_type = data.get("type")
            if msg_type != "message":
                continue

            turn_counter += 1

            # Rate limiting
            now = time.time()
            message_timestamps[:] = [t for t in message_timestamps if now - t < 60]
            if len(message_timestamps) >= MAX_MESSAGES_PER_MINUTE:
                await websocket.send_json({"type": "error", "message": "Rate limited. Max 10 messages per minute."})
                continue
            message_timestamps.append(now)

            content = data.get("content", "").strip()
            content = content[:8000]
            if not content:
                continue

            # Fleet mode — prefix content with fleet context
            fleet_mode = data.get("fleet", False)
            if fleet_mode:
                content = (
                    "[FLEET MODE: This query spans all managed clusters. "
                    "Use fleet_* tools (fleet_list_pods, fleet_list_deployments, fleet_compare_resource, etc.) "
                    "to query across clusters. Do NOT use single-cluster tools unless the user specifies a cluster.]\n\n"
                    + content
                )

            # Context from Pulse UI — sanitize and prefix
            ctx_prefix = _build_context_prefix(data)
            if ctx_prefix:
                content = ctx_prefix + content

            messages.append({"role": "user", "content": content})

            style_hint = _apply_style_hint(data)

            # Inject shared context from context bus
            from .context_bus import ContextEntry, get_context_bus

            namespace_from_context = ""
            ns_match = re.search(r"Namespace:\s*'?([a-zA-Z0-9\-._]+)'?", content)
            if ns_match:
                namespace_from_context = ns_match.group(1)
            bus = get_context_bus()
            shared_context = bus.build_context_prompt(namespace=namespace_from_context)
            effective_system = system_prompt + style_hint
            if shared_context:
                effective_system = effective_system + "\n\n" + shared_context

            # Inject relevant runbooks based on the user query
            if mode == "sre":
                try:
                    from .runbooks import select_runbooks

                    effective_system += "\n\n" + select_runbooks(content)
                except Exception:
                    pass

            try:
                full_response = await _run_agent_ws(
                    websocket,
                    messages,
                    effective_system,
                    tool_defs,
                    tool_map,
                    write_tools,
                    session_id,
                    current_user=ws_user,
                    mode=mode,
                    turn_number=turn_counter,
                    user_query=content,
                )
                messages.append({"role": "assistant", "content": full_response})

                # Publish agent response to shared context bus
                if full_response:
                    bus.publish(
                        ContextEntry(
                            source="sre_agent" if mode == "sre" else "security_agent",
                            category="user_resolution" if "resolved" in full_response.lower() else "diagnosis",
                            summary=full_response[:200],
                            details={"mode": mode, "full_length": len(full_response)},
                            namespace=namespace_from_context,
                        )
                    )

                try:
                    await websocket.send_json(
                        {
                            "type": "done",
                            "full_response": full_response,
                        }
                    )
                except Exception:
                    pass  # Client disconnected — expected during long queries
            except Exception as exc:
                logger.exception("Agent error")
                if messages:
                    messages.pop()
                # Build a descriptive error message
                err_type = type(exc).__name__
                err_msg = str(exc)[:200]
                if "DefaultCredentialsError" in err_type or "credentials" in err_msg.lower():
                    detail = (
                        "AI backend credentials not configured. Check ANTHROPIC_API_KEY or Vertex AI service account."
                    )
                    suggestions = [
                        "Verify the GCP service account key is mounted",
                        "Or set ANTHROPIC_API_KEY as an alternative",
                    ]
                elif "rate" in err_msg.lower() or "429" in err_msg:
                    detail = "AI API rate limit reached. Please wait a moment and try again."
                    suggestions = ["Wait 30 seconds before retrying"]
                else:
                    detail = f"Agent error: {err_type} — {err_msg}" if err_msg else f"Agent error: {err_type}"
                    suggestions = ["Try again", "Check agent logs for details"]
                try:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "message": detail,
                            "category": "server",
                            "suggestions": suggestions,
                            "operation": "",
                        }
                    )
                except Exception:
                    pass  # Client already disconnected

    except Exception:
        logger.exception("WebSocket error")
    finally:
        receive_task.cancel()
        try:
            await receive_task
        except asyncio.CancelledError:
            pass
        # Cancel any pending confirmation future so agent thread unblocks immediately
        future = _pending_confirms.pop(session_id, None)
        if future and not future.done():
            future.cancel()
        _pending_nonces.pop(session_id, None)
        _pending_timestamps.pop(session_id, None)
        _ws_alive.pop(session_id, None)


# ── /ws/agent: Auto-routing unified agent ─────────────────────────────────


@app.websocket("/ws/agent")
async def websocket_auto_agent(websocket: WebSocket):
    """Unified agent endpoint — auto-routes between SRE and Security based on query intent."""
    # Token authentication — same pattern as /ws/sre
    client_token = _verify_ws_token(websocket)
    if not client_token:
        await websocket.close(4001, "Unauthorized")
        return

    await websocket.accept()
    session_id = str(uuid.uuid4())
    _ws_alive[session_id] = True
    messages: list[dict] = []
    message_timestamps: list[float] = []
    last_mode: str = "sre"

    # Extract user identity for view tools
    try:
        ws_user = _get_current_user(
            x_forwarded_access_token=websocket.headers.get("x-forwarded-access-token"),
            x_forwarded_user=websocket.headers.get("x-forwarded-user"),
        )
    except HTTPException:
        logger.warning("WebSocket session %s: no valid user token, view operations will be unavailable", session_id)
        ws_user = "anonymous"

    # Message queue for incoming messages while agent is running
    incoming: asyncio.Queue = asyncio.Queue()
    _receive_loop = _make_receive_loop(websocket, session_id, messages, incoming)
    receive_task = asyncio.create_task(_receive_loop())
    turn_counter = 0

    try:
        while True:
            data = await incoming.get()
            if data is None:
                break  # Client disconnected

            msg_type = data.get("type")
            if msg_type != "message":
                continue

            turn_counter += 1

            # Rate limiting
            now = time.time()
            message_timestamps[:] = [t for t in message_timestamps if now - t < 60]
            if len(message_timestamps) >= MAX_MESSAGES_PER_MINUTE:
                await websocket.send_json({"type": "error", "message": "Rate limited. Max 10 messages per minute."})
                continue
            message_timestamps.append(now)

            content = data.get("content", "").strip()
            content = content[:8000]
            if not content:
                continue

            # Fleet mode — prefix content with fleet context
            fleet_mode = data.get("fleet", False)
            if fleet_mode:
                content = (
                    "[FLEET MODE: This query spans all managed clusters. "
                    "Use fleet_* tools (fleet_list_pods, fleet_list_deployments, fleet_compare_resource, etc.) "
                    "to query across clusters. Do NOT use single-cluster tools unless the user specifies a cluster.]\n\n"
                    + content
                )

            # --- Auto-classify intent with sticky mode ---
            intent, is_strong = classify_intent(content)

            # Sticky view_designer: once in dashboard mode, stay there unless
            # the user clearly asks about something SRE/security-specific.
            # This prevents "update the chart" from routing to SRE.
            _HARD_SWITCH_SRE = {
                "crash",
                "oom",
                "pending",
                "drain",
                "cordon",
                "crashloop",
                "node not ready",
                "why are",
                "what's wrong",
            }
            _HARD_SWITCH_SEC = {"rbac", "scc", "vulnerability", "compliance", "privilege", "security audit"}

            q_lower = content.lower()
            if last_mode == "view_designer" and intent != "view_designer":
                # Only break out of view_designer for unambiguous SRE/Security
                has_hard_sre = any(kw in q_lower for kw in _HARD_SWITCH_SRE)
                has_hard_sec = any(kw in q_lower for kw in _HARD_SWITCH_SEC)
                if not has_hard_sre and not has_hard_sec:
                    intent = "view_designer"
            elif last_mode != "sre" and not is_strong:
                # For other non-default modes, keep on weak signals
                intent = last_mode

            config = build_orchestrated_config(intent)
            last_mode = intent
            logger.info("Auto-agent classified intent=%s strong=%s for session=%s", intent, is_strong, session_id)

            system_prompt = config["system_prompt"]
            tool_defs = config["tool_defs"]
            tool_map = config["tool_map"]
            write_tools = config["write_tools"]

            # Context from Pulse UI — sanitize and prefix
            ctx_prefix = _build_context_prefix(data)
            if ctx_prefix:
                content = ctx_prefix + content

            messages.append({"role": "user", "content": content})

            style_hint = _apply_style_hint(data)

            # Inject shared context from context bus
            from .context_bus import ContextEntry, get_context_bus

            namespace_from_context = ""
            ns_match = re.search(r"Namespace:\s*'?([a-zA-Z0-9\-._]+)'?", content)
            if ns_match:
                namespace_from_context = ns_match.group(1)
            bus = get_context_bus()
            shared_context = bus.build_context_prompt(namespace=namespace_from_context)
            effective_system = system_prompt + style_hint
            if shared_context:
                effective_system = effective_system + "\n\n" + shared_context

            # Inject relevant runbooks based on the user query
            if intent in ("sre", "both"):
                try:
                    from .runbooks import select_runbooks

                    effective_system += "\n\n" + select_runbooks(content)
                except Exception:
                    pass

            try:
                full_response = await _run_agent_ws(
                    websocket,
                    messages,
                    effective_system,
                    tool_defs,
                    tool_map,
                    write_tools,
                    session_id,
                    current_user=ws_user,
                    mode=intent,
                    turn_number=turn_counter,
                    user_query=content,
                )
                messages.append({"role": "assistant", "content": full_response})

                # Publish agent response to shared context bus
                if full_response:
                    source = "sre_agent" if last_mode == "sre" else "security_agent"
                    bus.publish(
                        ContextEntry(
                            source=source,
                            category="user_resolution" if "resolved" in full_response.lower() else "diagnosis",
                            summary=full_response[:200],
                            details={"mode": last_mode, "full_length": len(full_response)},
                            namespace=namespace_from_context,
                        )
                    )

                try:
                    await websocket.send_json(
                        {
                            "type": "done",
                            "full_response": full_response,
                        }
                    )
                except Exception:
                    pass  # Client disconnected — expected during long queries
            except Exception as exc:
                logger.exception("Agent error")
                if messages:
                    messages.pop()
                # Build a descriptive error message
                err_type = type(exc).__name__
                err_msg = str(exc)[:200]
                if "DefaultCredentialsError" in err_type or "credentials" in err_msg.lower():
                    detail = (
                        "AI backend credentials not configured. Check ANTHROPIC_API_KEY or Vertex AI service account."
                    )
                    suggestions = [
                        "Verify the GCP service account key is mounted",
                        "Or set ANTHROPIC_API_KEY as an alternative",
                    ]
                elif "rate" in err_msg.lower() or "429" in err_msg:
                    detail = "AI API rate limit reached. Please wait a moment and try again."
                    suggestions = ["Wait 30 seconds before retrying"]
                else:
                    detail = f"Agent error: {err_type} — {err_msg}" if err_msg else f"Agent error: {err_type}"
                    suggestions = ["Try again", "Check agent logs for details"]
                try:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "message": detail,
                            "category": "server",
                            "suggestions": suggestions,
                            "operation": "",
                        }
                    )
                except Exception:
                    pass  # Client already disconnected

    except Exception:
        logger.exception("WebSocket error")
    finally:
        receive_task.cancel()
        try:
            await receive_task
        except asyncio.CancelledError:
            pass
        # Cancel any pending confirmation future so agent thread unblocks immediately
        future = _pending_confirms.pop(session_id, None)
        if future and not future.done():
            future.cancel()
        _pending_nonces.pop(session_id, None)
        _pending_timestamps.pop(session_id, None)
        _ws_alive.pop(session_id, None)


# ── Protocol v2: /ws/monitor ──────────────────────────────────────────────

from .monitor import (
    MonitorSession,
    execute_rollback,
    get_action_detail,
    get_fix_history,
    get_investigation_stats,
    is_autofix_paused,
)


@app.websocket("/ws/monitor")
async def websocket_monitor(websocket: WebSocket):
    """WebSocket endpoint for autonomous cluster monitoring (Protocol v2).

    Server pushes: finding, prediction, action_report, monitor_status
    Client sends: subscribe_monitor, action_response, get_fix_history
    """
    # Token authentication
    client_token = _verify_ws_token(websocket)
    if not client_token:
        await websocket.close(4001, "Unauthorized")
        return

    await websocket.accept()
    logger.info("Monitor client connected")

    # Wait for subscribe_monitor message to get config
    # Server-side trust level cap: client cannot escalate beyond this
    max_trust_level = int(os.environ.get("PULSE_AGENT_MAX_TRUST_LEVEL", "3"))
    trust_level = 1
    auto_fix_categories: list[str] = []

    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=10)
        data = json.loads(raw)
        if data.get("type") == "subscribe_monitor":
            requested_trust = data.get("trustLevel", 1)
            # Clamp to server-configured maximum — client cannot escalate
            try:
                trust_level = max(0, min(int(requested_trust), max_trust_level))
            except (ValueError, TypeError):
                logger.warning("Invalid trust level %r, defaulting to 1", requested_trust)
                trust_level = 1
            auto_fix_categories = [
                str(c) for c in (data.get("autoFixCategories") or []) if isinstance(c, str) and len(c) < 64
            ]
            logger.info(
                "Monitor subscribed: trust=%d (requested=%s, max=%d) categories=%s",
                trust_level,
                requested_trust,
                max_trust_level,
                auto_fix_categories,
            )
    except (TimeoutError, Exception):
        pass  # Use defaults

    session = MonitorSession(websocket, trust_level, auto_fix_categories)
    ws_id = str(uuid.uuid4())
    _ws_alive[ws_id] = True

    # Start scan loop as background task
    scan_task = asyncio.create_task(session.run_loop())

    # Listen for client messages (with rate limiting)
    message_timestamps: list[float] = []

    try:
        while True:
            raw = await websocket.receive_text()

            # Opportunistic cleanup of stale pending confirms
            _cleanup_stale_pending()

            # H6: message size check (matching the agent WS pattern)
            if len(raw) > MAX_MESSAGE_SIZE:
                await websocket.send_json({"type": "error", "message": "Message too large"})
                continue

            # Rate limiting (same as /ws/sre)
            now = time.time()
            message_timestamps[:] = [t for t in message_timestamps if now - t < 60]
            if len(message_timestamps) >= MAX_MESSAGES_PER_MINUTE:
                await websocket.send_json({"type": "error", "message": "Rate limited. Max 10 messages per minute."})
                continue
            message_timestamps.append(now)

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "Invalid JSON"})
                continue

            msg_type = data.get("type")

            if msg_type == "trigger_scan":
                # H1: check scan lock before creating a new task to prevent overlapping scans
                if session._scan_lock.locked():
                    logger.info("Manual scan skipped — scan already in progress")
                    await websocket.send_json({"type": "error", "message": "Scan already in progress"})
                else:
                    logger.info("Manual scan triggered by client")
                    asyncio.create_task(session.run_scan())

            elif msg_type == "action_response":
                action_id = data.get("actionId", "")
                if not isinstance(action_id, str) or len(action_id) > 200:
                    continue
                approved = data.get("approved", False)
                handled = session.resolve_action_response(action_id, approved)
                logger.info("Action response: id=%s approved=%s handled=%s", action_id, approved, handled)

            elif msg_type == "get_fix_history":
                filters = data.get("filters")
                try:
                    page = int(data.get("page", 1))
                except (TypeError, ValueError):
                    page = 1
                result = get_fix_history(page=page, filters=filters)
                await websocket.send_json({"type": "fix_history", **result})

    except WebSocketDisconnect:
        logger.info("Monitor client disconnected")
    except Exception as e:
        logger.error("Monitor WebSocket error: %s", e)
    finally:
        session.running = False
        scan_task.cancel()
        try:
            await scan_task
        except asyncio.CancelledError:
            pass
        _ws_alive.pop(ws_id, None)


# ── Protocol v2: REST endpoints ───────────────────────────────────────────


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


_DOUBLE_BRACE_RE = re.compile(r"\}(\s*)\{")


def _fix_promql(query: str) -> str:
    """Fix common PromQL syntax errors.

    Merges double label blocks: metric{a="1"}{b="2"} -> metric{a="1",b="2"}
    """
    if not query or "{" not in query:
        return query
    return _DOUBLE_BRACE_RE.sub(r",", query)


def _sanitize_components(components: list[dict]) -> list[dict]:
    """Sanitize component specs before saving to views.

    Fixes invalid PromQL in metric_card queries.
    """
    for comp in components:
        if comp.get("kind") == "metric_card" and comp.get("query"):
            comp["query"] = _fix_promql(comp["query"])
        # Handle grids/tabs/sections that contain nested components
        for container_key in ("items", "components"):
            nested = comp.get(container_key)
            if isinstance(nested, list):
                _sanitize_components(nested)
        # Handle tabs with nested components
        tabs = comp.get("tabs")
        if isinstance(tabs, list):
            for tab in tabs:
                tab_comps = tab.get("components")
                if isinstance(tab_comps, list):
                    _sanitize_components(tab_comps)
    return components


def _verify_rest_token(authorization: str | None = Header(None), token: str | None = Query(None)):
    """Verify token for REST endpoints — accepts Bearer header or query param."""
    expected = os.environ.get("PULSE_AGENT_WS_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Server not configured")
    client_token = ""
    if authorization and authorization.startswith("Bearer "):
        client_token = authorization[7:]
    elif token:
        client_token = token
    if not client_token or not hmac.compare_digest(client_token, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/fix-history")
async def rest_fix_history(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: str | None = Query(None),
    category: str | None = Query(None),
    since: int | None = Query(None),
    search: str | None = Query(None),
    authorization: str | None = Header(None),
    _token: str | None = Query(None, alias="token"),
):
    """Paginated fix history (Protocol v2). Requires token auth."""
    _verify_rest_token(authorization, _token)
    filters = {}
    if status:
        filters["status"] = status
    if category:
        filters["category"] = category
    if since:
        filters["since"] = since
    if search:
        filters["search"] = search
    return get_fix_history(page=page, page_size=page_size, filters=filters or None)


@app.get("/fix-history/{action_id}")
async def rest_action_detail(
    action_id: str,
    authorization: str | None = Header(None),
    token: str | None = Query(None),
):
    """Single action detail with before/after state (Protocol v2). Requires token auth."""
    _verify_rest_token(authorization, token)
    result = get_action_detail(action_id)
    if result is None:
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=404, content={"error": "Action not found"})
    return result


@app.post("/fix-history/{action_id}/rollback")
async def rollback_action(
    action_id: str,
    authorization: str | None = Header(None),
    token: str | None = Query(None),
):
    """Rollback a completed action (Protocol v2). Requires token auth."""
    _verify_rest_token(authorization, token)
    result = execute_rollback(action_id)
    if "error" in result:
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=400, content=result)
    return result


@app.get("/briefing")
async def rest_briefing(
    hours: int = Query(12, ge=1, le=72),
    authorization: str | None = Header(None),
    token: str | None = Query(None),
):
    """Cluster activity briefing for the last N hours. Requires token auth."""
    _verify_rest_token(authorization, token)
    from .monitor import get_briefing

    return get_briefing(hours)


@app.get("/predictions")
async def rest_predictions(
    authorization: str | None = Header(None),
    token: str | None = Query(None),
):
    """Active predictions — currently only available via /ws/monitor WebSocket stream."""
    _verify_rest_token(authorization, token)
    raise HTTPException(status_code=501, detail="Predictions are only available via the /ws/monitor WebSocket stream.")


@app.post("/simulate")
async def rest_simulate(
    request: Request,
    authorization: str | None = Header(None),
    token: str | None = Query(None),
):
    """Predict the impact of a tool action without executing it. Requires token auth."""
    _verify_rest_token(authorization, token)
    body = await request.json()
    tool = body.get("tool", "")
    inp = body.get("input", {})
    from .monitor import simulate_action

    result = simulate_action(tool, inp)
    return result


@app.get("/monitor/capabilities")
async def monitor_capabilities(
    authorization: str | None = Header(None),
    token: str | None = Query(None),
):
    """Expose monitor trust/capability limits so UI can align controls."""
    _verify_rest_token(authorization, token)
    from .monitor import AUTO_FIX_HANDLERS

    max_trust_level = int(os.environ.get("PULSE_AGENT_MAX_TRUST_LEVEL", "3"))
    return {
        "max_trust_level": max(0, min(max_trust_level, 4)),
        "supported_auto_fix_categories": sorted(AUTO_FIX_HANDLERS.keys()),
    }


@app.post("/monitor/pause")
async def pause_autofix(
    authorization: str | None = Header(None),
    token: str | None = Query(None),
):
    """Emergency kill switch — pause all auto-fix actions."""
    _verify_rest_token(authorization, token)
    from .monitor import set_autofix_paused

    set_autofix_paused(True)
    logger.warning("Auto-fix PAUSED via /monitor/pause")
    return {"autofix_paused": True}


@app.post("/monitor/resume")
async def resume_autofix(
    authorization: str | None = Header(None),
    token: str | None = Query(None),
):
    """Resume auto-fix actions after a pause."""
    _verify_rest_token(authorization, token)
    from .monitor import set_autofix_paused

    set_autofix_paused(False)
    logger.info("Auto-fix RESUMED via /monitor/resume")
    return {"autofix_paused": False}


@app.get("/memory/export")
async def export_memory(
    authorization: str | None = Header(None),
    token: str | None = Query(None),
):
    """Export learned runbooks and patterns for cross-pod sharing."""
    _verify_rest_token(authorization, token)
    from .memory import get_manager

    manager = get_manager()
    if not manager:
        return {"runbooks": [], "patterns": []}
    return {
        "runbooks": manager.store.export_runbooks(),
        "patterns": manager.store.export_patterns(),
    }


@app.post("/memory/import")
async def import_memory(
    body: dict,
    authorization: str | None = Header(None),
    token: str | None = Query(None),
):
    """Import runbooks and patterns from another pod's export."""
    _verify_rest_token(authorization, token)
    from .memory import get_manager

    manager = get_manager()
    if not manager:
        return {"imported_runbooks": 0, "imported_patterns": 0, "error": "Memory system not enabled"}
    runbooks = body.get("runbooks", [])
    patterns = body.get("patterns", [])
    imported_rb = manager.store.import_runbooks(runbooks) if runbooks else 0
    imported_pat = manager.store.import_patterns(patterns) if patterns else 0
    return {"imported_runbooks": imported_rb, "imported_patterns": imported_pat}


@app.get("/memory/stats")
async def memory_stats(
    authorization: str | None = Header(None),
    token: str | None = Query(None),
):
    """Memory system stats: incident count, runbook count, pattern count, top metrics."""
    _verify_rest_token(authorization, token)
    from .memory import get_manager

    manager = get_manager()
    if not manager:
        return {"enabled": False, "incidents": 0, "runbooks": 0, "patterns": 0, "metrics": {}}
    return {
        "enabled": True,
        "incidents": manager.store.get_incident_count(),
        "runbooks": len(manager.store.list_runbooks()),
        "patterns": len(manager.store.list_patterns()),
        "metrics": manager.store.get_metrics_summary(),
    }


@app.get("/memory/runbooks")
async def memory_runbooks(
    limit: int = Query(20, ge=1, le=100),
    authorization: str | None = Header(None),
    token: str | None = Query(None),
):
    """List learned runbooks sorted by success rate."""
    _verify_rest_token(authorization, token)
    from .memory import get_manager

    manager = get_manager()
    if not manager:
        return {"runbooks": []}
    runbooks = manager.store.list_runbooks()[:limit]
    return {"runbooks": runbooks}


@app.get("/memory/incidents")
async def memory_incidents(
    search: str = Query("", max_length=200),
    limit: int = Query(10, ge=1, le=50),
    authorization: str | None = Header(None),
    token: str | None = Query(None),
):
    """Search past incidents by query similarity."""
    _verify_rest_token(authorization, token)
    from .memory import get_manager

    manager = get_manager()
    if not manager:
        return {"incidents": []}
    if search:
        incidents = manager.store.search_incidents(search, limit=limit)
    else:
        # No search query — return most recent incidents
        rows = manager.store.db.fetchall("SELECT * FROM incidents ORDER BY timestamp DESC LIMIT ?", (limit,))
        incidents = [dict(r) for r in rows] if rows else []
    return {"incidents": incidents}


@app.get("/memory/patterns")
async def memory_patterns(
    authorization: str | None = Header(None),
    token: str | None = Query(None),
):
    """List detected recurring patterns."""
    _verify_rest_token(authorization, token)
    from .memory import get_manager

    manager = get_manager()
    if not manager:
        return {"patterns": []}
    return {"patterns": manager.store.list_patterns()}


@app.get("/context")
async def get_shared_context(
    authorization: str | None = Header(None),
    token: str | None = Query(None),
):
    """View recent shared context entries across all agents."""
    _verify_rest_token(authorization, token)
    from .context_bus import get_context_bus

    bus = get_context_bus()
    entries = bus.get_context_for(limit=20)
    return {
        "entries": [
            {
                "source": e.source,
                "category": e.category,
                "summary": e.summary,
                "namespace": e.namespace,
                "timestamp": e.timestamp,
                "age_seconds": int(time.time() - e.timestamp),
            }
            for e in entries
        ]
    }


@app.get("/eval/status")
async def eval_status(
    authorization: str | None = Header(None),
    token: str | None = Query(None),
):
    """Current eval gate status snapshot for UI surfaces."""
    _verify_rest_token(authorization, token)
    global _EVAL_STATUS_CACHE, _EVAL_STATUS_CACHE_TS_MS
    from .evals.outcomes import analyze_windows
    from .evals.runner import evaluate_suite
    from .evals.scenarios import load_suite

    now_ms = int(time.time() * 1000)
    if _EVAL_STATUS_CACHE and (now_ms - _EVAL_STATUS_CACHE_TS_MS) < _EVAL_STATUS_CACHE_TTL_MS:
        return _EVAL_STATUS_CACHE

    async with _EVAL_STATUS_LOCK:
        # Re-check after acquiring lock (another request may have populated the cache)
        now_ms = int(time.time() * 1000)
        if _EVAL_STATUS_CACHE and (now_ms - _EVAL_STATUS_CACHE_TS_MS) < _EVAL_STATUS_CACHE_TTL_MS:
            return _EVAL_STATUS_CACHE

        release = evaluate_suite("release", load_suite("release"))
        safety = evaluate_suite("safety", load_suite("safety"))
        integration = evaluate_suite("integration", load_suite("integration"))
        outcomes = analyze_windows(current_days=7, baseline_days=7)

        payload = {
            "note": "Release gate scores static fixtures. Use 'pulse-eval replay' for live agent testing.",
            "quality_gate_passed": bool(release.gate_passed) and bool(outcomes["gate_passed"]),
            "generated_at_ms": outcomes.get("generated_at_ms"),
            "release": {
                "gate_passed": release.gate_passed,
                "scenario_count": release.scenario_count,
                "average_overall": release.average_overall,
                "blocker_counts": release.blocker_counts,
            },
            "safety": {
                "gate_passed": safety.gate_passed,
                "scenario_count": safety.scenario_count,
                "average_overall": safety.average_overall,
            },
            "integration": {
                "gate_passed": integration.gate_passed,
                "scenario_count": integration.scenario_count,
                "average_overall": integration.average_overall,
            },
            "outcomes": {
                "gate_passed": outcomes.get("gate_passed", False),
                "current_actions": outcomes.get("current", {}).get("total_actions", 0),
                "baseline_actions": outcomes.get("baseline", {}).get("total_actions", 0),
                "regressions": outcomes.get("regressions", {}),
                "policy": outcomes.get("policy", {}),
            },
        }
        _EVAL_STATUS_CACHE = payload
        _EVAL_STATUS_CACHE_TS_MS = now_ms
        return payload


@app.get("/eval/score")
async def eval_tool_selection_score(
    authorization: str | None = Header(None),
    token: str | None = Query(None),
):
    """Score tool selection accuracy for static + learned eval prompts."""
    _verify_rest_token(authorization, token)

    from .harness import score_eval_prompts
    from .tool_usage import get_learned_eval_prompts

    # Import static prompts
    try:
        from tests.eval_prompts import EVAL_PROMPTS
    except ImportError:
        EVAL_PROMPTS = []

    # Score static
    static_result = (
        score_eval_prompts(EVAL_PROMPTS)
        if EVAL_PROMPTS
        else {"total": 0, "passed": 0, "failed": 0, "accuracy": 0, "failures": []}
    )

    # Score learned
    learned = get_learned_eval_prompts(days=30)
    clean_learned = [p for p in learned if not p[0].startswith("[{")]
    learned_result = (
        score_eval_prompts(clean_learned)
        if clean_learned
        else {"total": 0, "passed": 0, "failed": 0, "accuracy": 0, "failures": []}
    )

    # Combined
    combined_total = static_result["total"] + learned_result["total"]
    combined_passed = static_result["passed"] + learned_result["passed"]

    return {
        "static": {
            "accuracy": static_result["accuracy"],
            "passed": static_result["passed"],
            "total": static_result["total"],
        },
        "learned": {
            "accuracy": learned_result["accuracy"],
            "passed": learned_result["passed"],
            "total": learned_result["total"],
        },
        "combined": {
            "accuracy": combined_passed / combined_total if combined_total else 0,
            "passed": combined_passed,
            "total": combined_total,
        },
        "failures": [
            {"query": f["query"][:80], "expected": f["expected"], "mode": f["mode"]}
            for f in static_result["failures"][:10]
        ],
    }


# ---------------------------------------------------------------------------
# View Management (user-scoped custom dashboards)
# ---------------------------------------------------------------------------


_user_cache: collections.OrderedDict[str, tuple[str, float]] = collections.OrderedDict()
_USER_CACHE_TTL = 60  # seconds
_USER_CACHE_MAX = 500  # evict oldest entries beyond this


def _get_current_user(
    x_forwarded_access_token: str | None = None,
    x_forwarded_user: str | None = None,
) -> str:
    """Extract username from OAuth proxy headers.

    Priority: PULSE_AGENT_DEV_USER > X-Forwarded-User > TokenReview > JWT decode > token hash.
    The OAuth proxy sets X-Forwarded-User with the authenticated username — this is
    the most reliable source since OpenShift tokens are opaque (sha256~...), not JWTs.
    """
    dev_user = os.environ.get("PULSE_AGENT_DEV_USER", "")
    if dev_user:
        return dev_user

    # Best source: OAuth proxy sets X-Forwarded-User directly
    if x_forwarded_user and isinstance(x_forwarded_user, str) and x_forwarded_user.strip():
        username = x_forwarded_user.strip()
        # One-time migration: move hash-based views to real username
        if not _user_cache.get(f"_migrated_{username}"):
            try:
                from . import db

                migrated = db.migrate_view_ownership(username)
                if migrated:
                    logger.info("Migrated %d views to user '%s'", migrated, username)
            except Exception:
                pass
            _user_cache[f"_migrated_{username}"] = (username, time.time())
        return username

    token = x_forwarded_access_token or ""

    if not token:
        raise HTTPException(
            status_code=401,
            detail="User identity required. X-Forwarded-Access-Token or X-Forwarded-User header is missing.",
        )

    # Use full hash to prevent collision attacks (was [:16])
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    # Check cache (evict if expired)
    cached = _user_cache.get(token_hash)
    if cached:
        if (time.time() - cached[1]) < _USER_CACHE_TTL:
            return cached[0]
        # Don't evict yet — keep stale entry in case TokenReview fails

    # Resolve via Kubernetes TokenReview
    try:
        from kubernetes import client as k8s_client

        from .k8s_client import _load_k8s

        _load_k8s()
        auth_api = k8s_client.AuthenticationV1Api()
        review = k8s_client.TokenReview(spec=k8s_client.TokenReviewSpec(token=token))
        result = auth_api.create_token_review(review)
        if result.status.authenticated:
            username = result.status.user.username
            _cache_user(token_hash, username)
            return username
    except Exception:
        # If we have a cached identity (even stale), keep using it during API outage
        if cached:
            logger.warning("TokenReview API unavailable, extending cached identity '%s'", cached[0])
            _cache_user(token_hash, cached[0])  # refresh timestamp
            return cached[0]
        logger.warning("TokenReview API unavailable, using token-derived identity")

    # Final fallback: stable identity derived from token hash.
    # OpenShift tokens are sha256~ format (not JWTs), so we can't decode them.
    fallback_user = f"user-{token_hash[:16]}"
    _cache_user(token_hash, fallback_user)
    return fallback_user


def _cache_user(token_hash: str, username: str) -> None:
    """Cache a user identity with O(1) LRU eviction."""
    _user_cache[token_hash] = (username, time.time())
    _user_cache.move_to_end(token_hash)
    while len(_user_cache) > _USER_CACHE_MAX:
        _user_cache.popitem(last=False)


@app.get("/views")
async def rest_list_views(
    authorization: str | None = Header(None),
    token: str | None = Query(None),
    x_forwarded_access_token: str | None = Header(None, alias="X-Forwarded-Access-Token"),
    x_forwarded_user: str | None = Header(None, alias="X-Forwarded-User"),
):
    """List all views for the current user."""
    _verify_rest_token(authorization, token)
    from . import db

    owner = _get_current_user(x_forwarded_access_token, x_forwarded_user)
    views = db.list_views(owner)
    return {"views": views or [], "owner": owner}


@app.get("/views/{view_id}")
async def rest_get_view(
    view_id: str,
    authorization: str | None = Header(None),
    token: str | None = Query(None),
    x_forwarded_access_token: str | None = Header(None, alias="X-Forwarded-Access-Token"),
    x_forwarded_user: str | None = Header(None, alias="X-Forwarded-User"),
):
    """Get a single view by ID."""
    _verify_rest_token(authorization, token)
    from fastapi.responses import JSONResponse

    from . import db

    owner = _get_current_user(x_forwarded_access_token, x_forwarded_user)
    view = db.get_view(view_id, owner)
    if view is None:
        return JSONResponse(status_code=404, content={"error": "View not found"})
    return view


@app.post("/views")
async def rest_create_view(
    request: Request,
    authorization: str | None = Header(None),
    token: str | None = Query(None),
    x_forwarded_access_token: str | None = Header(None, alias="X-Forwarded-Access-Token"),
    x_forwarded_user: str | None = Header(None, alias="X-Forwarded-User"),
):
    """Save a new view for the current user."""
    _verify_rest_token(authorization, token)
    from fastapi.responses import JSONResponse

    from . import db

    owner = _get_current_user(x_forwarded_access_token, x_forwarded_user)
    body = await request.json()

    view_id = body.get("id", f"cv-{uuid.uuid4().hex[:12]}")
    if not re.match(r"^[a-zA-Z0-9_-]{1,64}$", view_id):
        return JSONResponse(status_code=400, content={"error": "view id must be alphanumeric/hyphens, max 64 chars"})
    title = str(body.get("title", "Untitled View"))[:200]
    description = str(body.get("description", ""))[:1000]
    layout = body.get("layout", [])
    positions = body.get("positions", {})
    icon = str(body.get("icon", ""))[:50]

    if not layout:
        return JSONResponse(status_code=400, content={"error": "layout is required"})
    if not isinstance(layout, list) or len(layout) > 50:
        return JSONResponse(status_code=400, content={"error": "layout must be a list with at most 50 widgets"})
    # Reject payloads over 1MB
    import json as _json

    if len(_json.dumps(layout)) > 1_000_000:
        return JSONResponse(status_code=400, content={"error": "layout payload too large (max 1MB)"})

    result = db.save_view(owner, view_id, title, description, layout, positions, icon)
    if result is None:
        return JSONResponse(status_code=500, content={"error": "Failed to save view"})
    return {"id": result, "owner": owner}


@app.put("/views/{view_id}")
async def rest_update_view(
    view_id: str,
    request: Request,
    authorization: str | None = Header(None),
    token: str | None = Query(None),
    x_forwarded_access_token: str | None = Header(None, alias="X-Forwarded-Access-Token"),
    x_forwarded_user: str | None = Header(None, alias="X-Forwarded-User"),
):
    """Update a view (title, description, layout, positions). Owner only."""
    _verify_rest_token(authorization, token)
    from fastapi.responses import JSONResponse

    from . import db

    owner = _get_current_user(x_forwarded_access_token, x_forwarded_user)
    body = await request.json()

    # Extract only allowed fields — never pass raw body as **kwargs
    updates = {}
    for key in ("title", "description", "icon", "layout", "positions"):
        if key in body:
            updates[key] = body[key]

    # Create version snapshot only when explicitly requested (save=true in body)
    if body.get("save"):
        updates["_snapshot"] = True
        updates["_action"] = body.get("action", "save")

    result = db.update_view(view_id, owner, **updates)
    if not result:
        return JSONResponse(status_code=404, content={"error": "View not found or not owned by you"})
    return {"updated": True}


@app.delete("/views/{view_id}")
async def rest_delete_view(
    view_id: str,
    authorization: str | None = Header(None),
    token: str | None = Query(None),
    x_forwarded_access_token: str | None = Header(None, alias="X-Forwarded-Access-Token"),
    x_forwarded_user: str | None = Header(None, alias="X-Forwarded-User"),
):
    """Delete a view. Owner only."""
    _verify_rest_token(authorization, token)
    from fastapi.responses import JSONResponse

    from . import db

    owner = _get_current_user(x_forwarded_access_token, x_forwarded_user)
    deleted = db.delete_view(view_id, owner)
    if not deleted:
        return JSONResponse(status_code=404, content={"error": "View not found or not owned by you"})
    return {"deleted": True}


@app.post("/views/{view_id}/clone")
async def rest_clone_view(
    view_id: str,
    request: Request,
    authorization: str | None = Header(None),
    token: str | None = Query(None),
    x_forwarded_access_token: str | None = Header(None, alias="X-Forwarded-Access-Token"),
    x_forwarded_user: str | None = Header(None, alias="X-Forwarded-User"),
):
    """Clone a view to the current user's account. Only the owner can clone their own views."""
    _verify_rest_token(authorization, token)
    from fastapi.responses import JSONResponse

    from . import db

    owner = _get_current_user(x_forwarded_access_token, x_forwarded_user)
    # Verify the caller owns the source view
    source = db.get_view(view_id, owner)
    if source is None:
        return JSONResponse(status_code=404, content={"error": "View not found or not owned by you"})
    new_id = db.clone_view(view_id, owner)
    if new_id is None:
        return JSONResponse(status_code=500, content={"error": "Clone failed"})
    return {"id": new_id, "owner": owner}


@app.post("/views/{view_id}/share")
async def rest_share_view(
    view_id: str,
    authorization: str | None = Header(None),
    token: str | None = Query(None),
    x_forwarded_access_token: str | None = Header(None, alias="X-Forwarded-Access-Token"),
    x_forwarded_user: str | None = Header(None, alias="X-Forwarded-User"),
):
    """Generate a share link for a view. The link allows others to clone it."""
    _verify_rest_token(authorization, token)
    from fastapi.responses import JSONResponse

    from . import db

    owner = _get_current_user(x_forwarded_access_token, x_forwarded_user)
    view = db.get_view(view_id, owner)
    if view is None:
        return JSONResponse(status_code=404, content={"error": "View not found or not owned by you"})

    secret = os.environ.get("PULSE_SHARE_TOKEN_KEY", "") or os.environ.get("PULSE_AGENT_WS_TOKEN", "")
    if not secret:
        return JSONResponse(status_code=503, content={"error": "Server not configured for sharing"})
    expires = int(time.time()) + 86400  # 24 hours
    payload = f"{view_id}:{expires}"
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    share_token = f"{payload}:{signature}"

    return {"share_token": share_token, "view_id": view_id, "expires_in": 86400}


@app.post("/views/claim/{share_token:path}")
async def rest_claim_shared_view(
    share_token: str,
    authorization: str | None = Header(None),
    token: str | None = Query(None),
    x_forwarded_access_token: str | None = Header(None, alias="X-Forwarded-Access-Token"),
    x_forwarded_user: str | None = Header(None, alias="X-Forwarded-User"),
):
    """Claim a shared view using a share token. Clones the view to your account."""
    _verify_rest_token(authorization, token)
    from fastapi.responses import JSONResponse

    from . import db

    # Verify share token — format is view_id:expires:full_hmac_sha256
    # The signature covers view_id:expires using the server's WS token as secret
    parts = share_token.split(":")
    if len(parts) != 3:
        return JSONResponse(status_code=400, content={"error": "Invalid share token"})

    view_id, expires_str, signature = parts
    try:
        expires = int(expires_str)
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "Invalid share token"})

    if int(time.time()) > expires:
        return JSONResponse(status_code=410, content={"error": "Share link has expired"})

    secret = os.environ.get("PULSE_AGENT_WS_TOKEN", "")
    if not secret:
        return JSONResponse(status_code=503, content={"error": "Server not configured"})
    expected_sig = hmac.new(secret.encode(), f"{view_id}:{expires_str}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected_sig):
        return JSONResponse(status_code=400, content={"error": "Invalid share token"})

    owner = _get_current_user(x_forwarded_access_token, x_forwarded_user)
    new_id = db.clone_view(view_id, owner)
    if new_id is None:
        return JSONResponse(status_code=404, content={"error": "Source view not found"})
    return {"id": new_id, "owner": owner}


# ---------------------------------------------------------------------------
# View Version History
# ---------------------------------------------------------------------------


@app.get("/views/{view_id}/versions")
async def rest_view_versions(
    view_id: str,
    authorization: str | None = Header(None),
    token: str | None = Query(None),
    x_forwarded_access_token: str | None = Header(None, alias="X-Forwarded-Access-Token"),
    x_forwarded_user: str | None = Header(None, alias="X-Forwarded-User"),
):
    """List version history for a view."""
    _verify_rest_token(authorization, token)
    from . import db

    owner = _get_current_user(x_forwarded_access_token, x_forwarded_user)
    # Verify ownership
    view = db.get_view(view_id, owner)
    if not view:
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=404, content={"error": "View not found"})
    versions = db.list_view_versions(view_id) or []
    return {"versions": versions, "view_id": view_id}


@app.post("/views/{view_id}/undo")
async def rest_undo_view(
    view_id: str,
    request: Request,
    authorization: str | None = Header(None),
    token: str | None = Query(None),
    x_forwarded_access_token: str | None = Header(None, alias="X-Forwarded-Access-Token"),
    x_forwarded_user: str | None = Header(None, alias="X-Forwarded-User"),
):
    """Undo the last change to a view (restore previous version)."""
    _verify_rest_token(authorization, token)
    from fastapi.responses import JSONResponse

    from . import db

    owner = _get_current_user(x_forwarded_access_token, x_forwarded_user)
    body = await request.json()
    version = body.get("version")

    if version is not None:
        # Restore specific version
        result = db.restore_view_version(view_id, owner, int(version))
    else:
        # Undo last change — find the latest version and restore it
        versions = db.list_view_versions(view_id, limit=1)
        if not versions:
            return JSONResponse(status_code=404, content={"error": "No version history available"})
        result = db.restore_view_version(view_id, owner, versions[0]["version"])

    if not result:
        return JSONResponse(status_code=404, content={"error": "Version not found or access denied"})
    return {"undone": True, "view_id": view_id}


# ---------------------------------------------------------------------------
# Live Query Refresh — lightweight Prometheus proxy for view widgets
# ---------------------------------------------------------------------------


@app.get("/query")
async def rest_query(
    q: str = Query(..., description="PromQL query string"),
    time_range: str = Query("", alias="range", description="Time range, e.g. '1h', '24h'"),
    authorization: str | None = Header(None),
    _token: str | None = Query(None, alias="token"),
):
    """Execute a PromQL query and return a ComponentSpec for live widget refresh.

    No Claude/LLM involved — direct Prometheus proxy.
    """
    _verify_rest_token(authorization, _token)

    from .k8s_tools import get_prometheus_query

    result = get_prometheus_query.call({"query": q, "time_range": time_range})

    if isinstance(result, tuple) and len(result) == 2:
        _text_result, component = result
        if component:
            return {"component": component}
        return {"component": None, "text": _text_result}
    return {"component": None, "text": str(result)}
