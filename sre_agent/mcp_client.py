"""MCP Client — connects to MCP servers and registers tools.

Loads mcp.yaml from skill packages, connects to MCP servers,
discovers available tools, and registers them in the tool registry
with UI rendering via mcp_renderer.
"""

from __future__ import annotations

import itertools
import json
import logging
import select
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("pulse_agent.mcp_client")


@dataclass
class MCPConnection:
    """A connection to an MCP server."""

    name: str
    url: str
    transport: str  # stdio or sse
    toolsets: list[str]
    tool_renderers: dict[str, dict] = field(default_factory=dict)
    connected: bool = False
    tools: list[str] = field(default_factory=list)
    tool_schemas: dict[str, dict] = field(default_factory=dict)  # name → {description, inputSchema}
    prompts: list[str] = field(default_factory=list)
    prompt_schemas: dict[str, dict] = field(default_factory=dict)  # name → {description, arguments}
    process: Any = None  # subprocess for stdio transport
    error: str = ""


# Global MCP connections
_connections: dict[str, MCPConnection] = {}
_request_id_counter = itertools.count(1)


class MCPTool:
    """Tool wrapper that calls an MCP server and renders the output."""

    def __init__(self, name: str, fn: Any, description: str, input_schema: dict | None = None):
        self.name = name
        self._fn = fn
        self.description = description
        self._input_schema = input_schema or {"type": "object", "properties": {}, "required": []}

    def call(self, input_data: dict) -> tuple[str, dict]:
        return self._fn(**input_data)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self._input_schema,
        }


def _resolve_env_vars(text: str) -> str:
    """Resolve ${VAR:-default} patterns in config strings."""
    import os
    import re

    def _replace(match):
        var = match.group(1)
        default = match.group(3) or ""
        return os.environ.get(var, default)

    return re.sub(r"\$\{(\w+)(:-([^}]*))?\}", _replace, text)


def load_mcp_config(mcp_yaml_path: Path) -> dict | None:
    """Load and parse an mcp.yaml file. Resolves ${ENV:-default} in values."""
    if not mcp_yaml_path.exists():
        return None
    try:
        raw = mcp_yaml_path.read_text(encoding="utf-8")
        resolved = _resolve_env_vars(raw)
        return yaml.safe_load(resolved)
    except Exception as e:
        logger.warning("Failed to load mcp.yaml at %s: %s", mcp_yaml_path, e)
        return None


def connect_mcp_server(name: str, config: dict) -> MCPConnection:
    """Connect to an MCP server from config.

    For stdio transport, spawns the server process.
    For sse transport, connects via HTTP.
    """
    server = config.get("server", {})
    url = server.get("url", "")
    transport = server.get("transport", "stdio")
    toolsets = config.get("toolsets", [])
    tool_renderers = config.get("tool_renderers", {})
    display_name = config.get("name", f"OpenShift MCP ({name})")

    conn = MCPConnection(
        name=display_name,
        url=url,
        transport=transport,
        toolsets=toolsets,
        tool_renderers=tool_renderers,
    )

    if not url:
        conn.error = "No server URL configured"
        return conn

    try:
        if transport == "stdio":
            conn = _connect_stdio(conn)
        elif transport == "sse":
            conn = _connect_sse(conn)
        else:
            conn.error = f"Unknown transport: {transport}"
    except Exception as e:
        conn.error = str(e)
        logger.warning("Failed to connect MCP server '%s': %s", name, e)

    return conn


def _connect_stdio(conn: MCPConnection) -> MCPConnection:
    """Connect via stdio transport (spawn process)."""
    # Parse command: "npx @openshift/openshift-mcp-server" → ["npx", "@openshift/openshift-mcp-server"]
    parts = conn.url.split()
    cmd = parts[0]
    args = parts[1:] if len(parts) > 1 else []

    # Add toolset flags
    for ts in conn.toolsets:
        args.extend(["--enable-toolset", ts])

    try:
        process = subprocess.Popen(
            [cmd, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        conn.process = process

        # Discover tools via MCP initialize handshake
        try:
            tool_defs = _discover_tools_stdio(process, conn.toolsets)
        except Exception:
            process.terminate()
            raise
        conn.tools = [t["name"] for t in tool_defs]
        conn.tool_schemas = {t["name"]: t for t in tool_defs}
        conn.connected = True
        logger.info("MCP '%s' connected: %d tools from %s", conn.name, len(conn.tools), conn.toolsets)
    except FileNotFoundError:
        conn.error = f"Command not found: {cmd}. Install with: npm install -g {args[0] if args else cmd}"
    except Exception as e:
        conn.error = f"Failed to start: {e}"

    return conn


def _parse_sse_response(raw: str) -> dict:
    """Parse SSE response format: 'event: message\\ndata: {json}'."""
    for line in raw.split("\n"):
        line = line.strip()
        if line.startswith("data: "):
            return json.loads(line[6:])
    # Try raw JSON parse as fallback
    return json.loads(raw)


def _mcp_post(base_url: str, payload: dict, session_id: str = "") -> tuple[dict, str]:
    """POST to MCP /mcp endpoint with SSE headers. Returns (response_dict, session_id)."""
    import urllib.request

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    req = urllib.request.Request(
        f"{base_url}/mcp",
        data=json.dumps(payload).encode(),
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        sid = resp.headers.get("Mcp-Session-Id", session_id)
        raw = resp.read().decode()
    return _parse_sse_response(raw), sid


def _connect_sse(conn: MCPConnection) -> MCPConnection:
    """Connect via SSE/streamable HTTP transport to MCP server."""
    import urllib.error

    base_url = conn.url.rstrip("/")

    try:
        # Initialize
        init_request = {
            "jsonrpc": "2.0",
            "id": next(_request_id_counter),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pulse-agent", "version": "1.0"},
            },
        }
        _init_resp, session_id = _mcp_post(base_url, init_request)

        # Discover tools
        tools_request = {
            "jsonrpc": "2.0",
            "id": next(_request_id_counter),
            "method": "tools/list",
            "params": {},
        }
        tools_resp, session_id = _mcp_post(base_url, tools_request, session_id)

        tools_raw = tools_resp.get("result", {}).get("tools", [])
        tool_defs = [
            {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "inputSchema": t.get("inputSchema", {"type": "object", "properties": {}, "required": []}),
            }
            for t in tools_raw
            if t.get("name")
        ]

        conn.tools = [t["name"] for t in tool_defs]
        conn.tool_schemas = {t["name"]: t for t in tool_defs}

        # Discover prompts (MCP prompts/list)
        try:
            prompts_request = {
                "jsonrpc": "2.0",
                "id": next(_request_id_counter),
                "method": "prompts/list",
                "params": {},
            }
            prompts_resp, session_id = _mcp_post(base_url, prompts_request, session_id)
            prompts_raw = prompts_resp.get("result", {}).get("prompts", [])
            for p in prompts_raw:
                name = p.get("name", "")
                if name:
                    conn.prompts.append(name)
                    # Convert prompt arguments to tool-style inputSchema
                    props = {}
                    required = []
                    for arg in p.get("arguments", []):
                        arg_name = arg.get("name", "")
                        if arg_name:
                            props[arg_name] = {
                                "type": "string",
                                "description": arg.get("description", ""),
                            }
                            if arg.get("required"):
                                required.append(arg_name)
                    conn.prompt_schemas[name] = {
                        "name": name,
                        "description": p.get("description", f"MCP prompt: {name}"),
                        "inputSchema": {"type": "object", "properties": props, "required": required},
                    }
            logger.info("MCP SSE '%s' discovered %d prompts: %s", conn.name, len(conn.prompts), conn.prompts)
        except Exception as e:
            logger.debug("Prompt discovery failed for '%s': %s", conn.name, e)

        conn.connected = True
        conn._sse_base_url = base_url
        conn._sse_session_id = session_id
        logger.info("MCP SSE '%s' connected: %d tools, %d prompts", conn.name, len(conn.tools), len(conn.prompts))

    except urllib.error.URLError as e:
        conn.error = f"Cannot connect to MCP server at {base_url}: {e.reason}"
    except Exception as e:
        conn.error = f"SSE connection failed: {e}"

    return conn


def _discover_tools_stdio(process: subprocess.Popen, toolsets: list[str]) -> list[str]:
    """Send MCP initialize request and discover available tools.

    MCP protocol: send JSON-RPC initialize → receive tools/list response.
    """
    try:
        # Send initialize request
        init_request = {
            "jsonrpc": "2.0",
            "id": next(_request_id_counter),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pulse-agent", "version": "1.0"},
            },
        }
        process.stdin.write(json.dumps(init_request) + "\n")
        process.stdin.flush()

        ready, _, _ = select.select([process.stdout], [], [], 10)
        if not ready:
            return []

        line = process.stdout.readline()
        if not line:
            return []

        # Send tools/list request
        tools_request = {"jsonrpc": "2.0", "id": next(_request_id_counter), "method": "tools/list", "params": {}}
        process.stdin.write(json.dumps(tools_request) + "\n")
        process.stdin.flush()

        ready, _, _ = select.select([process.stdout], [], [], 10)
        if not ready:
            return []

        line = process.stdout.readline()
        if not line:
            return []

        response = json.loads(line)
        tools_raw = response.get("result", {}).get("tools", [])
        return [
            {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "inputSchema": t.get("inputSchema", {"type": "object", "properties": {}, "required": []}),
            }
            for t in tools_raw
            if t.get("name")
        ]
    except Exception as e:
        logger.debug("Tool discovery failed: %s", e)
        return []


def call_mcp_tool(conn: MCPConnection, tool_name: str, arguments: dict) -> str:
    """Call a tool on an MCP server and return the text result."""
    if not conn.connected:
        return f"Error: MCP server '{conn.name}' is not connected"

    # SSE transport — HTTP POST
    if conn.transport == "sse":
        return _call_mcp_tool_sse(conn, tool_name, arguments)

    # Stdio transport — pipe to process
    if not conn.process:
        return f"Error: MCP server '{conn.name}' has no process"

    try:
        request = {
            "jsonrpc": "2.0",
            "id": next(_request_id_counter),
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        conn.process.stdin.write(json.dumps(request) + "\n")
        conn.process.stdin.flush()

        ready, _, _ = select.select([conn.process.stdout], [], [], 30)
        if not ready:
            return "Error: MCP tool call timed out"

        line = conn.process.stdout.readline()
        if not line:
            return "Error: No response from MCP server"

        response = json.loads(line)
        if "error" in response:
            return f"Error: {response['error'].get('message', 'Unknown error')}"

        # Extract text content from MCP response
        result = response.get("result", {})
        content = result.get("content", [])
        texts = [c.get("text", "") for c in content if c.get("type") == "text"]
        return "\n".join(texts) if texts else json.dumps(result)
    except Exception as e:
        return f"Error calling MCP tool '{tool_name}': {e}"


def _call_mcp_tool_sse(conn: MCPConnection, tool_name: str, arguments: dict) -> str:
    """Call an MCP tool via SSE/streamable HTTP transport."""
    base_url = getattr(conn, "_sse_base_url", conn.url.rstrip("/"))
    session_id = getattr(conn, "_sse_session_id", "")
    try:
        request = {
            "jsonrpc": "2.0",
            "id": next(_request_id_counter),
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        response, _ = _mcp_post(base_url, request, session_id)

        if "error" in response:
            return f"Error: {response['error'].get('message', 'Unknown error')}"

        result = response.get("result", {})
        content = result.get("content", [])
        texts = [c.get("text", "") for c in content if c.get("type") == "text"]
        return "\n".join(texts) if texts else json.dumps(result)
    except Exception as e:
        return f"Error calling MCP tool '{tool_name}' via SSE: {e}"


def call_mcp_prompt(conn: MCPConnection, prompt_name: str, arguments: dict) -> str:
    """Call an MCP prompt and return the text result."""
    if not conn.connected:
        return f"Error: MCP server '{conn.name}' is not connected"

    base_url = getattr(conn, "_sse_base_url", conn.url.rstrip("/"))
    session_id = getattr(conn, "_sse_session_id", "")
    try:
        request = {
            "jsonrpc": "2.0",
            "id": next(_request_id_counter),
            "method": "prompts/get",
            "params": {"name": prompt_name, "arguments": arguments},
        }
        response, _ = _mcp_post(base_url, request, session_id)

        if "error" in response:
            return f"Error: {response['error'].get('message', 'Unknown error')}"

        result = response.get("result", {})
        messages = result.get("messages", [])
        texts = []
        for msg in messages:
            content = msg.get("content", {})
            if isinstance(content, dict) and content.get("type") == "text":
                texts.append(content.get("text", ""))
            elif isinstance(content, str):
                texts.append(content)
        return "\n\n".join(texts) if texts else json.dumps(result)
    except Exception as e:
        return f"Error calling MCP prompt '{prompt_name}': {e}"


def register_mcp_tools(conn: MCPConnection) -> int:
    """Register MCP tools in the Pulse tool registry with UI rendering.

    Returns the number of tools registered.
    """
    from .mcp_renderer import render_mcp_output
    from .tool_registry import register_tool

    count = 0
    for tool_name in conn.tools:
        renderer_config = conn.tool_renderers.get(tool_name)

        # Create a wrapper that calls MCP and renders the output
        def _make_tool_fn(tn, rc, cn):
            def tool_fn(**kwargs):
                raw_output = call_mcp_tool(cn, tn, kwargs)
                text, component = render_mcp_output(tn, raw_output, renderer_config=rc)
                return (text, component)

            tool_fn.__name__ = tn
            tool_fn.__doc__ = f"MCP tool: {tn} (from {cn.name})"
            return tool_fn

        fn = _make_tool_fn(tool_name, renderer_config, conn)
        schema_def = conn.tool_schemas.get(tool_name, {})
        description = schema_def.get("description", f"MCP tool from {conn.name}")
        input_schema = schema_def.get("inputSchema", {"type": "object", "properties": {}, "required": []})
        tool = MCPTool(tool_name, fn, description, input_schema=input_schema)
        register_tool(tool)
        count += 1

    # Register MCP prompts as tools (prompts are callable workflows)
    for prompt_name in conn.prompts:

        def _make_prompt_fn(pn, cn):
            def prompt_fn(**kwargs):
                raw_output = call_mcp_prompt(cn, pn, kwargs)
                return (raw_output, None)

            prompt_fn.__name__ = pn
            prompt_fn.__doc__ = f"MCP prompt: {pn} (from {cn.name})"
            return prompt_fn

        fn = _make_prompt_fn(prompt_name, conn)
        schema_def = conn.prompt_schemas.get(prompt_name, {})
        description = schema_def.get("description", f"MCP prompt from {conn.name}")
        input_schema = schema_def.get("inputSchema", {"type": "object", "properties": {}, "required": []})
        tool = MCPTool(prompt_name, fn, description, input_schema=input_schema)
        register_tool(tool)
        conn.tools.append(prompt_name)
        count += 1

    logger.info("Registered %d MCP tools+prompts from '%s'", count, conn.name)
    return count


def connect_skill_mcp(skill_name: str, skill_path: Path) -> MCPConnection | None:
    """Connect to the MCP server defined in a skill's mcp.yaml."""
    mcp_yaml = skill_path / "mcp.yaml"
    config = load_mcp_config(mcp_yaml)
    if not config:
        return None

    conn = connect_mcp_server(skill_name, config)
    _connections[skill_name] = conn

    if conn.connected:
        register_mcp_tools(conn)

    return conn


def disconnect_all() -> None:
    """Disconnect all MCP servers."""
    for _name, conn in _connections.items():
        if conn.process:
            try:
                conn.process.terminate()
                conn.process.wait(timeout=5)
            except Exception:
                try:
                    conn.process.kill()
                except Exception:
                    pass
            conn.connected = False
    _connections.clear()


def add_standalone_server(name: str, url: str, transport: str = "sse") -> MCPConnection:
    """Add and connect a standalone MCP server (not tied to a skill).

    Returns the MCPConnection (check .connected and .error for status).
    """
    key = f"standalone:{name}"
    if key in _connections:
        # Disconnect old connection first
        _disconnect_one(key)

    config = {
        "name": name,
        "server": {"url": url, "transport": transport},
        "toolsets": [],
    }
    conn = connect_mcp_server(key, config)
    _connections[key] = conn

    if conn.connected:
        register_mcp_tools(conn)

    return conn


def remove_standalone_server(name: str) -> bool:
    """Remove a standalone MCP server. Returns True if found and removed."""
    key = f"standalone:{name}"
    if key not in _connections:
        return False
    _disconnect_one(key)
    return True


def _disconnect_one(key: str) -> None:
    """Disconnect and remove a single MCP connection by key."""
    conn = _connections.pop(key, None)
    if conn is None:
        return

    # Unregister tools from tool registry
    from .tool_registry import unregister_tool

    for tool_name in conn.tools:
        unregister_tool(tool_name)

    # Terminate process if stdio
    if conn.process:
        try:
            conn.process.terminate()
            conn.process.wait(timeout=5)
        except Exception:
            try:
                conn.process.kill()
            except Exception:
                pass
    conn.connected = False


def test_mcp_connection(url: str, transport: str = "sse") -> dict:
    """Test connectivity to an MCP server without registering.

    Returns {"connected": bool, "tools_count": int, "error": str}.
    """
    config = {
        "server": {"url": url, "transport": transport},
        "toolsets": [],
    }
    conn = connect_mcp_server("__test__", config)

    result = {
        "connected": conn.connected,
        "tools_count": len(conn.tools),
        "tools": conn.tools[:20],  # preview first 20
        "error": conn.error,
    }

    # Clean up — terminate process if stdio
    if conn.process:
        try:
            conn.process.terminate()
            conn.process.wait(timeout=5)
        except Exception:
            try:
                conn.process.kill()
            except Exception:
                pass

    return result


def list_mcp_connections() -> list[dict]:
    """List all MCP connections with status."""
    result = []
    for key, c in _connections.items():
        entry = {
            "name": c.name,
            "url": c.url,
            "transport": c.transport,
            "connected": c.connected,
            "tools": c.tools,
            "prompts": c.prompts,
            "toolsets": c.toolsets,
            "error": c.error,
            "standalone": key.startswith("standalone:"),
        }
        result.append(entry)
    return result


def list_mcp_tools() -> list[dict]:
    """List all tools from all connected MCP servers."""
    tools = []
    for conn in _connections.values():
        for tool_name in conn.tools:
            tools.append(
                {
                    "name": tool_name,
                    "server": conn.name,
                    "has_renderer": tool_name in conn.tool_renderers,
                }
            )
    return tools
