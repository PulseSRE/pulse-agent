"""Claude-powered SRE agent with Kubernetes tool use."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import anthropic

from .k8s_tools import ALL_TOOLS, WRITE_TOOLS
from .runbooks import RUNBOOKS, ALERT_TRIAGE_CONTEXT

logger = logging.getLogger("pulse_agent")

MAX_ITERATIONS = 25

SYSTEM_PROMPT = """\
You are an expert OpenShift/Kubernetes Site Reliability Engineer (SRE) agent.
You have direct access to a live cluster through the tools provided.

## Your Responsibilities

1. **Cluster Diagnostics** — Investigate pod failures, node issues, crash loops, \
OOM kills, image pull errors, scheduling problems, and networking issues.

2. **Incident Triage** — When asked about problems, systematically gather data: \
check events, pod status, logs, and node conditions. Correlate symptoms and \
identify root causes before suggesting fixes.

3. **Resource Management** — Analyze resource quotas, capacity, and utilization. \
Identify over/under-provisioned workloads.

4. **Runbook Execution** — Execute common SRE operations like scaling deployments, \
restarting pods, and cordoning nodes. ALWAYS confirm destructive actions with the \
user before executing them.

## Guidelines

- Start diagnostics by gathering broad context (events, pod list), then drill down.
- When you find unhealthy pods, check their logs and describe output.
- For Warning events, explain what they mean and suggest remediation.
- When presenting findings, be concise but thorough. Use structured output.
- For write operations (scale, restart, delete, cordon), ALWAYS explain what you \
are about to do and ASK for confirmation before executing.
- If you don't have enough information, use the available tools to gather it — \
don't guess.
- When checking cluster health, look at: nodes, cluster operators (on OpenShift), \
warning events, and pods not in Running state.
- Use `get_prometheus_query` to check real-time metrics (CPU, memory, latency).
- Use `get_firing_alerts` to check for active alerts before diagnosing issues.
- After performing write operations, use `record_audit_entry` to log what you did.
""" + RUNBOOKS + ALERT_TRIAGE_CONTEXT

# Build raw tool definitions from @beta_tool decorated functions
TOOL_DEFS = [t.to_dict() for t in ALL_TOOLS]
TOOL_MAP = {t.name: t for t in ALL_TOOLS}


def create_client():
    """Create an Anthropic client.

    Uses Vertex AI if GCP project is configured,
    otherwise falls back to direct Anthropic API.
    """
    project = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", "")
    region = os.environ.get("CLOUD_ML_REGION", "")

    if project and region:
        return anthropic.AnthropicVertex(region=region, project_id=project)

    return anthropic.Anthropic()


def _sanitize_content(content) -> list[dict]:
    """Convert response content blocks to plain dicts safe for round-tripping.

    The API rejects extra fields (e.g. 'caller' on tool_use blocks) when they
    are echoed back in the assistant message, so we strip to the essentials.
    """
    result = []
    for block in content:
        if block.type == "text":
            result.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            result.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
        elif block.type == "thinking":
            result.append({"type": "thinking", "thinking": block.thinking, "signature": block.signature})
        elif block.type == "redacted_thinking":
            result.append({"type": "redacted_thinking", "data": block.data})
    return result


def _execute_tool(name: str, input_data: dict, tool_map: dict) -> str:
    """Execute a tool by name and return the result string."""
    tool = tool_map.get(name)
    if not tool:
        return f"Error: unknown tool '{name}'"
    try:
        result = tool.call(input_data)
        logger.info(json.dumps({
            "event": "tool_executed",
            "tool": name,
            "input": input_data,
            "result_length": len(result),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }))
        return result
    except Exception as e:
        logger.error(json.dumps({
            "event": "tool_error",
            "tool": name,
            "input": input_data,
            "error": str(type(e).__name__),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }))
        return f"Error executing {name}: {type(e).__name__}"


def run_agent_streaming(
    client,
    messages: list[dict],
    system_prompt: str,
    tool_defs: list,
    tool_map: dict,
    write_tools: set[str] | None = None,
    on_text=None,
    on_thinking=None,
    on_tool_use=None,
    on_confirm=None,
) -> str:
    """Run an agent turn with streaming, handling the tool loop manually.

    This is the shared agent loop used by both SRE and Security agents.

    Args:
        client: Anthropic or AnthropicVertex client.
        messages: Conversation history.
        system_prompt: System prompt for the agent.
        tool_defs: List of tool definition dicts.
        tool_map: Dict mapping tool name to callable.
        write_tools: Set of tool names that require user confirmation.
        on_text: Callback for text deltas.
        on_thinking: Callback for thinking deltas.
        on_tool_use: Callback when a tool is invoked (name, input).
        on_confirm: Callback to confirm write operations. Returns True to proceed.

    Returns the full final text response.
    """
    if write_tools is None:
        write_tools = set()

    full_text_parts = []
    iterations = 0

    model = os.environ.get("PULSE_AGENT_MODEL", "claude-opus-4-6")
    max_tokens = int(os.environ.get("PULSE_AGENT_MAX_TOKENS", "16000"))

    while iterations < MAX_ITERATIONS:
        iterations += 1

        with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            thinking={"type": "adaptive"},
            tools=tool_defs,
            messages=messages,
        ) as stream:
            for event in stream:
                if event.type == "content_block_start":
                    if hasattr(event.content_block, "name"):
                        if on_tool_use:
                            on_tool_use(event.content_block.name)
                elif event.type == "content_block_delta":
                    if event.delta.type == "text_delta":
                        if on_text:
                            on_text(event.delta.text)
                        full_text_parts.append(event.delta.text)
                    elif event.delta.type == "thinking_delta":
                        if on_thinking:
                            on_thinking(event.delta.thinking)

            response = stream.get_final_message()

        if response.stop_reason == "end_turn":
            break

        messages.append({"role": "assistant", "content": _sanitize_content(response.content)})

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    # Confirmation gate for write operations
                    if block.name in write_tools and on_confirm:
                        if not on_confirm(block.name, block.input):
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": "Operation cancelled by user.",
                                "is_error": True,
                            })
                            continue

                    result = _execute_tool(block.name, block.input, tool_map)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": tool_results})
        elif response.stop_reason == "pause_turn":
            continue
        else:
            break

    if iterations >= MAX_ITERATIONS:
        logger.warning("Agent hit max iteration limit (%d)", MAX_ITERATIONS)

    return "".join(full_text_parts)


def run_agent_turn_streaming(
    client,
    messages: list[dict],
    system_prompt: str | None = None,
    extra_tool_defs: list | None = None,
    extra_tool_map: dict | None = None,
    on_text=None,
    on_thinking=None,
    on_tool_use=None,
    on_confirm=None,
) -> str:
    """Run the SRE agent. Delegates to the shared agent loop."""
    effective_defs = TOOL_DEFS + (extra_tool_defs or [])
    effective_map = {**TOOL_MAP, **(extra_tool_map or {})}

    return run_agent_streaming(
        client=client,
        messages=messages,
        system_prompt=system_prompt or SYSTEM_PROMPT,
        tool_defs=effective_defs,
        tool_map=effective_map,
        write_tools=WRITE_TOOLS,
        on_text=on_text,
        on_thinking=on_thinking,
        on_tool_use=on_tool_use,
        on_confirm=on_confirm,
    )
