"""FastAPI WebSocket server for the Pulse Agent.

Exposes the SRE and Security agents over WebSocket for integration
with the OpenShift Pulse web UI.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import re
import time
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

# Pending confirmation requests keyed by connection
_pending_confirms: dict[int, asyncio.Future] = {}
# JIT nonces for confirmation — prevents replay/forgery
_pending_nonces: dict[int, str] = {}

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


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


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
):
    """Run an agent turn and stream results over WebSocket."""
    client = create_client()
    ws_id = id(websocket)

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


async def _create_and_register_future(ws_id: int, tool_name: str, tool_input: dict, websocket: WebSocket):
    """Create a Future on the event loop and send the confirm request with a JIT nonce."""
    import secrets
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    nonce = secrets.token_urlsafe(16)
    _pending_confirms[ws_id] = future
    _pending_nonces[ws_id] = nonce
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
    if mode not in ("sre", "security"):
        await websocket.close(code=4000, reason="Invalid mode. Use 'sre' or 'security'.")
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
    ws_id = id(websocket)
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
                    future = _pending_confirms.get(ws_id)
                    expected_nonce = _pending_nonces.get(ws_id)
                    received_nonce = data.get("nonce", "")

                    if not future or future.done():
                        logger.warning("Confirm response received but no pending future (ws_id=%s)", ws_id)
                    elif expected_nonce and received_nonce != expected_nonce:
                        logger.warning("Confirm response nonce mismatch — possible replay (ws_id=%s)", ws_id)
                        future.set_result(False)
                    else:
                        future.set_result(data.get("approved", False))
                        logger.info("Confirmation received: approved=%s nonce=%s", data.get("approved"), received_nonce[:8])
                    _pending_nonces.pop(ws_id, None)
                    continue

                if msg_type == "clear":
                    messages.clear()
                    await websocket.send_json({"type": "cleared"})
                    continue

                # Queue other messages for the main loop
                await incoming.put(data)
        except WebSocketDisconnect:
            await incoming.put(None)  # Signal disconnect
        except Exception:
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
                    tool_defs, tool_map, write_tools,
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
                })

    except Exception:
        logger.exception("WebSocket error")
    finally:
        receive_task.cancel()
        # Cancel any pending confirmation future so agent thread unblocks immediately
        future = _pending_confirms.pop(ws_id, None)
        if future and not future.done():
            future.cancel()
        _pending_nonces.pop(ws_id, None)
