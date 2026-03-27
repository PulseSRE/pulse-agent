"""FastAPI WebSocket server for the Pulse Agent.

Protocol Version: 2 (see API_CONTRACT.md for full specification)

Exposes the SRE and Security agents over WebSocket for integration
with the OpenShift Pulse web UI. V2 adds /ws/monitor for autonomous scanning.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .agent import (
    create_client,
    run_agent_streaming,
    ALL_TOOLS as SRE_ALL_TOOLS,
    WRITE_TOOLS,
    SYSTEM_PROMPT as SRE_SYSTEM_PROMPT,
    TOOL_DEFS as SRE_TOOL_DEFS,
    TOOL_MAP as SRE_TOOL_MAP,
)
from .security_agent import (
    ALL_TOOLS as SEC_ALL_TOOLS,
    SECURITY_SYSTEM_PROMPT,
    TOOL_DEFS as SEC_TOOL_DEFS,
    TOOL_MAP as SEC_TOOL_MAP,
)

logger = logging.getLogger("pulse_agent.api")

# WebSocket connection liveness tracking
_ws_alive: dict[str, bool] = {}

# Pending confirmation requests keyed by session ID (uuid4, NOT id(websocket))
_pending_confirms: dict[str, asyncio.Future] = {}
# JIT nonces for confirmation — prevents replay/forgery
_pending_nonces: dict[str, str] = {}
# Timestamps for TTL-based cleanup
_pending_timestamps: dict[str, float] = {}
# TTL for stale pending state (5 minutes)
_PENDING_TTL_SECONDS = 300

# Max WebSocket message size (1MB)
MAX_MESSAGE_SIZE = 1_048_576

# Rate limiting: max messages per minute per connection
MAX_MESSAGES_PER_MINUTE = 10

# Allowed characters in context fields (K8s name rules + slashes/dots)
_SAFE_CONTEXT = re.compile(r'^[a-zA-Z0-9\-._/: ]{0,253}$')


def _sanitize_context_field(value: str) -> str:
    """Sanitize a context field to prevent prompt injection."""
    if not isinstance(value, str):
        return ""
    if not _SAFE_CONTEXT.match(value):
        return ""  # Strict reject: non-matching values are dropped entirely
    return value


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Verify k8s connectivity and auth config on startup."""
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
    yield


app = FastAPI(title="Pulse Agent API", version="0.2.0", lifespan=lifespan)


PROTOCOL_VERSION = "2"

@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/version")
async def version():
    """API protocol version. UI checks this on connect to detect mismatches."""
    return {
        "protocol": PROTOCOL_VERSION,
        "agent": "1.5.0",
        "tools": len(SRE_ALL_TOOLS) + len(SEC_ALL_TOOLS),
        "features": ["component_specs", "ws_token_auth", "rate_limiting", "monitor", "fix_history", "predictions"],
    }


@app.get("/health")
async def health():
    from .error_tracker import get_tracker
    from .agent import _circuit_breaker
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
    }


@app.get("/tools")
async def list_tools():
    """List all available tools grouped by mode, with write-op flags."""
    return {
        "sre": [
            {
                "name": t.name,
                "description": t.description,
                "requires_confirmation": t.name in WRITE_TOOLS,
            }
            for t in SRE_ALL_TOOLS
        ],
        "security": [
            {
                "name": t.name,
                "description": t.description,
                "requires_confirmation": False,
            }
            for t in SEC_ALL_TOOLS
        ],
        "write_tools": sorted(WRITE_TOOLS),
    }


async def _run_agent_ws(
    websocket: WebSocket,
    messages: list[dict],
    system_prompt: str,
    tool_defs: list,
    tool_map: dict,
    write_tools: set[str],
    session_id: str,
):
    """Run an agent turn and stream results over WebSocket."""
    client = create_client()
    ws_id = session_id

    # Capture the running loop BEFORE entering the thread
    loop = asyncio.get_running_loop()

    async def _safe_send(data: dict):
        """Send JSON to WebSocket, swallowing errors if client disconnected."""
        try:
            await websocket.send_json(data)
        except Exception:
            pass  # Client gone — nothing to do

    def _schedule_send(data: dict):
        """Thread-safe: schedule a WebSocket send on the event loop."""
        asyncio.run_coroutine_threadsafe(_safe_send(data), loop)

    def on_text(delta: str):
        _schedule_send({"type": "text_delta", "text": delta})

    def on_thinking(delta: str):
        _schedule_send({"type": "thinking_delta", "thinking": delta})

    def on_tool_use(name: str):
        _schedule_send({"type": "tool_use", "tool": name})

    def on_component(name: str, spec: dict):
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
                except Exception as e:
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

    # Run the blocking agent in a thread
    full_response = await asyncio.to_thread(
        run_agent_streaming,
        client=client,
        messages=messages,
        system_prompt=system_prompt,
        tool_defs=tool_defs,
        tool_map=tool_map,
        write_tools=write_tools,
        on_text=on_text,
        on_thinking=on_thinking,
        on_tool_use=on_tool_use,
        on_confirm=on_confirm,
        on_component=on_component,
    )

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
    await websocket.send_json({
        "type": "confirm_request",
        "tool": tool_name,
        "input": tool_input,
        "nonce": nonce,
    })
    return future


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
    if mode not in ("sre", "security"):
        await websocket.close(code=4000, reason="Invalid mode. Use 'sre' or 'security'. For monitoring, use /ws/monitor.")
        return

    # Token authentication — mandatory unless explicitly disabled
    import hmac
    expected_token = os.environ.get("PULSE_AGENT_WS_TOKEN", "")
    if not expected_token:
        await websocket.close(code=4001, reason="Server not configured. PULSE_AGENT_WS_TOKEN is required.")
        return
    client_token = websocket.query_params.get("token", "")
    if not hmac.compare_digest(client_token, expected_token):
        await websocket.close(code=4001, reason="Unauthorized. Invalid or missing token.")
        return

    await websocket.accept()
    session_id = str(uuid.uuid4())
    _ws_alive[session_id] = True
    messages: list[dict] = []
    # Rate limiting state
    message_timestamps: list[float] = []

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

    async def _receive_loop():
        """Receive messages from the WebSocket and route them."""
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

                # Confirm responses are handled immediately (even while agent runs)
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
                        future.set_result(data.get("approved", False))
                        logger.info("Confirmation received: approved=%s nonce=%s", data.get("approved"), received_nonce[:8])
                    _pending_nonces.pop(session_id, None)
                    continue

                if msg_type == "clear":
                    messages.clear()
                    await websocket.send_json({"type": "cleared"})
                    continue

                # Queue other messages for the main loop
                await incoming.put(data)
        except WebSocketDisconnect:
            _ws_alive[session_id] = False
            await incoming.put(None)  # Signal disconnect
        except Exception:
            _ws_alive[session_id] = False
            await incoming.put(None)

    # Start the receive loop as a concurrent task
    receive_task = asyncio.create_task(_receive_loop())

    try:
        while True:
            data = await incoming.get()
            if data is None:
                break  # Client disconnected

            msg_type = data.get("type")
            if msg_type != "message":
                continue

            # Rate limiting
            now = time.time()
            message_timestamps[:] = [t for t in message_timestamps if now - t < 60]
            if len(message_timestamps) >= MAX_MESSAGES_PER_MINUTE:
                await websocket.send_json({"type": "error", "message": "Rate limited. Max 10 messages per minute."})
                continue
            message_timestamps.append(now)

            content = data.get("content", "").strip()
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

            # Context from Pulse UI — ensures namespace/resource are explicit
            context = data.get("context")
            if context and isinstance(context, dict):
                kind = _sanitize_context_field(context.get("kind", ""))
                ns = _sanitize_context_field(context.get("namespace", ""))
                name = _sanitize_context_field(context.get("name", ""))
                if kind or name or ns:
                    context_parts = []
                    if kind and name:
                        context_parts.append(f"Resource: {kind}/{name}")
                    elif name:
                        context_parts.append(f"Resource: {name}")
                    if ns:
                        context_parts.append(f"Namespace: {ns}")
                    context_str = ", ".join(context_parts)
                    if ns:
                        content = (
                            f"[UI Context: {context_str}]\n"
                            f"IMPORTANT: Use namespace='{ns}' for any operations on this resource. "
                            f"Do NOT default to 'default' namespace.\n\n{content}"
                        )
                    else:
                        content = f"[UI Context: {context_str}]\n\n{content}"

            messages.append({"role": "user", "content": content})

            try:
                full_response = await _run_agent_ws(
                    websocket, messages, system_prompt,
                    tool_defs, tool_map, write_tools, session_id,
                )
                messages.append({"role": "assistant", "content": full_response})
                await websocket.send_json({
                    "type": "done",
                    "full_response": full_response,
                })
            except Exception as e:
                logger.exception("Agent error")
                if messages:
                    messages.pop()
                await websocket.send_json({
                    "type": "error",
                    "message": "Agent encountered an error. Please try again.",
                    "category": "server",
                    "suggestions": [],
                    "operation": "",
                })

    except Exception:
        logger.exception("WebSocket error")
    finally:
        receive_task.cancel()
        # Cancel any pending confirmation future so agent thread unblocks immediately
        future = _pending_confirms.pop(session_id, None)
        if future and not future.done():
            future.cancel()
        _pending_nonces.pop(session_id, None)
        _pending_timestamps.pop(session_id, None)
        _ws_alive.pop(session_id, None)


# ── Protocol v2: /ws/monitor ──────────────────────────────────────────────

from .monitor import MonitorSession, get_fix_history, get_action_detail


@app.websocket("/ws/monitor")
async def websocket_monitor(websocket: WebSocket):
    """WebSocket endpoint for autonomous cluster monitoring (Protocol v2).

    Server pushes: finding, prediction, action_report, monitor_status
    Client sends: subscribe_monitor, action_response, get_fix_history
    """
    # Token authentication
    import hmac as _hmac
    expected_token = os.environ.get("PULSE_AGENT_WS_TOKEN", "")
    if not expected_token:
        await websocket.close(code=4001, reason="Server not configured. PULSE_AGENT_WS_TOKEN is required.")
        return
    client_token = websocket.query_params.get("token", "")
    if not _hmac.compare_digest(client_token, expected_token):
        await websocket.close(code=4001, reason="Unauthorized.")
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
            trust_level = max(0, min(int(requested_trust), max_trust_level))
            auto_fix_categories = [
                str(c) for c in (data.get("autoFixCategories") or [])
                if isinstance(c, str) and len(c) < 64
            ]
            logger.info("Monitor subscribed: trust=%d (requested=%s, max=%d) categories=%s",
                        trust_level, requested_trust, max_trust_level, auto_fix_categories)
    except (asyncio.TimeoutError, Exception):
        pass  # Use defaults

    session = MonitorSession(websocket, trust_level, auto_fix_categories)

    # Start scan loop as background task
    scan_task = asyncio.create_task(session.run_loop())

    # Listen for client messages (with rate limiting)
    message_timestamps: list[float] = []

    try:
        while True:
            raw = await websocket.receive_text()

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
                continue

            msg_type = data.get("type")

            if msg_type == "trigger_scan":
                logger.info("Manual scan triggered by client")
                asyncio.create_task(session.run_scan())

            elif msg_type == "action_response":
                action_id = data.get("actionId", "")
                approved = data.get("approved", False)
                logger.info("Action response: id=%s approved=%s", action_id, approved)
                # TODO: wire up to autonomous action execution

            elif msg_type == "get_fix_history":
                filters = data.get("filters")
                page = data.get("page", 1)
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


# ── Protocol v2: REST endpoints ───────────────────────────────────────────

from fastapi import Query, Header, HTTPException


def _verify_rest_token(authorization: str | None = Header(None), token: str | None = Query(None)):
    """Verify token for REST endpoints — accepts Bearer header or query param."""
    import hmac as _hmac
    expected = os.environ.get("PULSE_AGENT_WS_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Server not configured")
    client_token = ""
    if authorization and authorization.startswith("Bearer "):
        client_token = authorization[7:]
    elif token:
        client_token = token
    if not client_token or not _hmac.compare_digest(client_token, expected):
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


@app.get("/predictions")
async def rest_predictions(
    authorization: str | None = Header(None),
    token: str | None = Query(None),
):
    """Active predictions from the most recent scan (Protocol v2). Requires token auth."""
    _verify_rest_token(authorization, token)
    return {"predictions": [], "total": 0}


@app.get("/eval/status")
async def eval_status():
    """Current eval gate status snapshot for UI surfaces."""
    from .evals.outcomes import analyze_windows
    from .evals.runner import evaluate_suite
    from .evals.scenarios import load_suite

    release = evaluate_suite("release", load_suite("release"))
    safety = evaluate_suite("safety", load_suite("safety"))
    integration = evaluate_suite("integration", load_suite("integration"))
    outcomes = analyze_windows(current_days=7, baseline_days=7)

    return {
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
