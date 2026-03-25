"""FastAPI WebSocket server for the Pulse Agent.

Exposes the SRE and Security agents over WebSocket for integration
with the OpenShift Pulse web UI.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .agent import create_client, run_agent_streaming
from .k8s_tools import ALL_TOOLS as SRE_ALL_TOOLS, WRITE_TOOLS
from .security_agent import (
    ALL_TOOLS as SEC_ALL_TOOLS,
    SECURITY_SYSTEM_PROMPT,
    TOOL_DEFS as SEC_TOOL_DEFS,
    TOOL_MAP as SEC_TOOL_MAP,
)
from .agent import SYSTEM_PROMPT as SRE_SYSTEM_PROMPT

logger = logging.getLogger("pulse_agent.api")

SRE_TOOL_DEFS = [t.to_dict() for t in SRE_ALL_TOOLS]
SRE_TOOL_MAP = {t.name: t for t in SRE_ALL_TOOLS}

# Pending confirmation requests keyed by connection
_pending_confirms: dict[int, asyncio.Future] = {}

# Max WebSocket message size (1MB)
MAX_MESSAGE_SIZE = 1_048_576

# Allowed characters in context fields (K8s name rules + slashes/dots)
_SAFE_CONTEXT = re.compile(r'^[a-zA-Z0-9\-._/: ]{0,253}$')


def _sanitize_context_field(value: str) -> str:
    """Sanitize a context field to prevent prompt injection."""
    if not isinstance(value, str):
        return ""
    if not _SAFE_CONTEXT.match(value):
        return value[:253].replace('\n', ' ').replace('\r', ' ')
    return value


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Verify k8s connectivity on startup."""
    try:
        from .k8s_client import get_core_client
        get_core_client().list_namespace(limit=1)
        logger.info("Connected to cluster")
    except Exception:
        logger.warning("Cannot connect to cluster — tools may fail")
    yield


app = FastAPI(title="Pulse Agent API", version="0.3.0", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/tools")
async def list_tools():
    """List all available tools grouped by mode."""
    return {
        "sre": [{"name": t.name, "description": t.description} for t in SRE_ALL_TOOLS],
        "security": [{"name": t.name, "description": t.description} for t in SEC_ALL_TOOLS],
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

    def _schedule_send(data: dict):
        """Thread-safe: schedule a WebSocket send on the event loop."""
        asyncio.run_coroutine_threadsafe(websocket.send_json(data), loop)

    def on_text(delta: str):
        _schedule_send({"type": "text_delta", "text": delta})

    def on_thinking(delta: str):
        _schedule_send({"type": "thinking_delta", "thinking": delta})

    def on_tool_use(name: str):
        _schedule_send({"type": "tool_use", "tool": name})

    def on_confirm(tool_name: str, tool_input: dict) -> bool:
        """Request confirmation from the web UI and block until response."""
        future = loop.call_soon_threadsafe(loop.create_future)
        # create_future via call_soon_threadsafe returns None; create it properly
        confirm_future = asyncio.run_coroutine_threadsafe(
            _create_and_register_future(ws_id, tool_name, tool_input, websocket),
            loop,
        ).result(timeout=5)

        # Block the agent thread until the UI responds
        event = threading.Event()
        result_holder = [False]
        timed_out = [False]

        def resolve(f):
            try:
                result_holder[0] = f.result()
            except Exception:
                result_holder[0] = False
            event.set()

        confirm_future.add_done_callback(resolve)
        event.wait(timeout=120)  # 2 minute timeout

        _pending_confirms.pop(ws_id, None)

        if not event.is_set():
            timed_out[0] = True
            _schedule_send({"type": "error", "message": "Confirmation timed out (2 minutes). Operation cancelled."})
            return False

        return result_holder[0]

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
    )

    return full_response


async def _create_and_register_future(ws_id: int, tool_name: str, tool_input: dict, websocket: WebSocket):
    """Create a Future on the event loop and send the confirm request."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    _pending_confirms[ws_id] = future
    await websocket.send_json({
        "type": "confirm_request",
        "tool": tool_name,
        "input": tool_input,
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

    await websocket.accept()
    ws_id = id(websocket)
    messages: list[dict] = []

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

    try:
        while True:
            raw = await websocket.receive_text()

            # Guard against oversized messages
            if len(raw) > MAX_MESSAGE_SIZE:
                await websocket.send_json({"type": "error", "message": "Message too large"})
                continue

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "Invalid JSON"})
                continue

            msg_type = data.get("type")

            if msg_type == "clear":
                messages.clear()
                await websocket.send_json({"type": "cleared"})
                continue

            if msg_type == "confirm_response":
                future = _pending_confirms.get(ws_id)
                if future and not future.done():
                    future.get_loop().call_soon_threadsafe(
                        future.set_result, data.get("approved", False)
                    )
                continue

            if msg_type == "message":
                content = data.get("content", "").strip()
                if not content:
                    continue

                # Optional context from Pulse UI — sanitized to prevent injection
                context = data.get("context")
                if context and isinstance(context, dict):
                    kind = _sanitize_context_field(context.get("kind", ""))
                    ns = _sanitize_context_field(context.get("namespace", ""))
                    name = _sanitize_context_field(context.get("name", ""))
                    if kind or name:
                        content = f"[Context: viewing {kind} {ns}/{name}]\n\n{content}"

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
                    messages.pop()  # Remove failed user message
                    await websocket.send_json({
                        "type": "error",
                        "message": "Agent encountered an error. Please try again.",
                    })
                continue

    except WebSocketDisconnect:
        logger.info("Client disconnected")
    except Exception:
        logger.exception("WebSocket error")
    finally:
        _pending_confirms.pop(ws_id, None)
