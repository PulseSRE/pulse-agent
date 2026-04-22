"""WebSocket endpoint handlers for SRE, Security, Auto-agent, and Monitor."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid

from fastapi import HTTPException, WebSocket, WebSocketDisconnect

from ..config import get_settings
from ..monitor import MonitorSession, get_fix_history
from ..orchestrator import build_orchestrated_config, classify_intent, fix_typos
from .agent_ws import (
    MAX_MESSAGE_SIZE,
    MAX_MESSAGES_PER_MINUTE,
    _cleanup_stale_pending,
    _make_receive_loop,
    _pending_confirms,
    _pending_nonces,
    _pending_timestamps,
    _run_agent_ws,
    _ws_alive,
)
from .auth import _get_current_user, _verify_ws_token
from .context import _apply_style_hint, _build_context_prefix

# Active monitor sessions — keyed by ws_id, used by /debug/memory
_active_monitor_sessions: dict[str, MonitorSession] = {}

logger = logging.getLogger("pulse_agent.api")

# Keywords that force a mode switch out of view_designer.
# Defined at module level to avoid set construction per message.
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
        # Redirect to the dedicated monitor handler -- /ws/{mode} catches it
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

    # Token authentication -- mandatory unless explicitly disabled
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

    # Persist chat session
    try:
        from ..chat_history import create_session

        create_session(session_id, ws_user, mode)
    except Exception:
        logger.debug("Failed to create chat session record", exc_info=True)

    # Use skill-based config (delegates to build_orchestrated_config which tries skills first)
    # Note: no query available at connection time — tool selection uses categories.
    # Adaptive selection happens per-turn in the auto-agent handler.
    config = build_orchestrated_config(mode)
    system_prompt = config["system_prompt"]
    tool_defs = config["tool_defs"]
    tool_map = config["tool_map"]
    write_tools = config["write_tools"]

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

            # Fix common typos before tool selection
            content = fix_typos(content)

            # Fleet mode -- prefix content with fleet context
            fleet_mode = data.get("fleet", False)
            if fleet_mode:
                from ..prompt_builder import FLEET_PREFIX

                content = FLEET_PREFIX + "\n\n" + content

            # Context from Pulse UI -- sanitize and prefix
            ctx_prefix = _build_context_prefix(data)
            if ctx_prefix:
                content = ctx_prefix + content

            messages.append({"role": "user", "content": content})

            style_hint = _apply_style_hint(data)

            # Inject shared context from context bus
            from ..context_bus import ContextEntry, get_context_bus

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
                    from ..runbooks import select_runbooks

                    effective_system += "\n\n" + select_runbooks(content)
                except Exception:
                    pass

            try:
                _result = await _run_agent_ws(
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
                full_response = _result[0] if isinstance(_result, tuple) else _result
                messages.append({"role": "assistant", "content": full_response})

                # Persist messages to chat history (single commit)
                try:
                    from ..chat_history import save_turn

                    save_turn(session_id, content, full_response, is_first_turn=(turn_counter == 1))
                except Exception:
                    logger.debug("Failed to persist chat messages", exc_info=True)

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
                    pass  # Client disconnected -- expected during long queries
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


# -- /ws/agent: Auto-routing unified agent ---------------------------------


async def websocket_auto_agent(websocket: WebSocket):
    """Unified agent endpoint -- auto-routes between SRE and Security based on query intent."""
    # Token authentication -- same pattern as /ws/agent
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

    # Persist chat session
    try:
        from ..chat_history import create_session

        create_session(session_id, ws_user, "auto")
    except Exception:
        logger.debug("Failed to create chat session record", exc_info=True)

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

            # Fix common typos before classification and tool selection
            content = fix_typos(content)

            # Fleet mode -- prefix content with fleet context
            fleet_mode = data.get("fleet", False)
            if fleet_mode:
                from ..prompt_builder import FLEET_PREFIX

                content = FLEET_PREFIX + "\n\n" + content

            # --- Auto-classify intent with sticky mode ---
            # Try multi-skill routing first, fall back to single-skill
            secondary_skill = None
            try:
                from ..skill_loader import classify_query, classify_query_multi

                skill, secondary_skill = classify_query_multi(content)
                intent = skill.name
                is_strong = True
            except Exception:
                try:
                    from ..skill_loader import classify_query

                    skill = classify_query(content)
                    intent = skill.name
                    is_strong = True
                except Exception:
                    intent, is_strong = classify_intent(content)

            q_lower = content.lower()
            if last_mode == "view_designer" and intent != "view_designer":
                # Break out of view_designer for:
                # 1. Unambiguous SRE/Security keywords
                # 2. Non-conflicting custom skills (postmortem, capacity_planner)
                # Never switch to a conflicting skill (plan_builder) mid-session.
                has_hard_sre = any(kw in q_lower for kw in _HARD_SWITCH_SRE)
                has_hard_sec = any(kw in q_lower for kw in _HARD_SWITCH_SEC)
                try:
                    from ..skill_loader import get_skill

                    current = get_skill(last_mode)
                    is_conflicting = current and intent in (current.conflicts_with or [])
                except Exception:
                    is_conflicting = False
                is_custom_skill = intent not in ("sre", "security", "view_designer", "both") and not is_conflicting
                if not has_hard_sre and not has_hard_sec and not is_custom_skill:
                    intent = "view_designer"
            elif last_mode == "security" and intent == "sre" and not is_strong:
                pass  # Let it switch to SRE
            elif last_mode and last_mode == intent:
                pass  # Same skill — no switch needed
            elif last_mode and last_mode not in ("sre", "security", "view_designer", "both"):
                # Custom skill sticky mode — check if skill declares handoff
                try:
                    from ..skill_loader import check_handoff, get_skill

                    current = get_skill(last_mode)
                    if current and not check_handoff(current, content):
                        # No handoff triggered — stay in current skill
                        intent = last_mode
                except Exception:
                    pass

            config = build_orchestrated_config(intent, query=content)
            last_mode = intent
            logger.info("Auto-agent classified intent=%s strong=%s for session=%s", intent, is_strong, session_id)

            system_prompt = config["system_prompt"]
            tool_defs = config["tool_defs"]
            tool_map = config["tool_map"]
            write_tools = config["write_tools"]

            # Context from Pulse UI -- sanitize and prefix
            ctx_prefix = _build_context_prefix(data)
            if ctx_prefix:
                content = ctx_prefix + content

            messages.append({"role": "user", "content": content})

            # Gather context inputs for prompt builder
            style_hint = _apply_style_hint(data)

            from ..context_bus import ContextEntry, get_context_bus

            namespace_from_context = ""
            ns_match = re.search(r"Namespace:\s*'?([a-zA-Z0-9\-._]+)'?", content)
            if ns_match:
                namespace_from_context = ns_match.group(1)
            bus = get_context_bus()
            shared_context = bus.build_context_prompt(namespace=namespace_from_context)

            # Use prompt builder for unified assembly
            try:
                from ..prompt_builder import assemble_prompt as _assemble
                from ..skill_loader import get_skill as _get_skill_for_ws

                _ws_skill = _get_skill_for_ws(intent)
                if _ws_skill:
                    from ..harness import build_cached_system_prompt

                    static, dynamic = _assemble(
                        _ws_skill,
                        content,
                        intent,
                        list(tool_map.keys()),
                        fleet_mode=fleet_mode,
                        style_hint=style_hint,
                        shared_context=shared_context,
                    )
                    effective_system = build_cached_system_prompt(static, dynamic)
                else:
                    # Fallback: manual assembly for legacy modes
                    from ..harness import build_cached_system_prompt

                    _static = system_prompt + style_hint
                    _dynamic = shared_context or ""
                    if intent in ("sre", "both"):
                        try:
                            from ..runbooks import select_runbooks

                            _dynamic += "\n\n" + select_runbooks(content)
                        except Exception:
                            pass
                    effective_system = build_cached_system_prompt(_static, _dynamic)
            except Exception:
                # Safe fallback
                from ..harness import build_cached_system_prompt

                _static = system_prompt + style_hint
                effective_system = build_cached_system_prompt(_static, shared_context or "")

            # --- Multi-skill parallel execution ---
            if secondary_skill and get_settings().multi_skill:
                try:
                    from ..agent import _circuit_breaker

                    if _circuit_breaker and not _circuit_breaker.allow_request():
                        logger.info("Circuit breaker OPEN — degrading to single-skill")
                        secondary_skill = None
                except Exception:
                    pass

                if secondary_skill:
                    try:
                        await websocket.send_json(
                            {"type": "multi_skill_start", "skills": [intent, secondary_skill.name]}
                        )

                        from ..plan_runtime import run_parallel_skills
                        from ..synthesis import synthesize_parallel_outputs

                        parallel_result = await run_parallel_skills(
                            primary=skill,
                            secondary=secondary_skill,
                            query=content,
                            messages=messages,
                            client=None,
                            websocket=websocket,
                            session_id=session_id,
                        )

                        from ..skill_selector import get_last_selection_result

                        _sel = get_last_selection_result()
                        if _sel:
                            parallel_result.primary_confidence = _sel.fused_scores.get(intent, 0.0)
                            parallel_result.secondary_confidence = _sel.fused_scores.get(secondary_skill.name, 0.0)

                        _p_out = parallel_result.primary_output.strip()
                        _s_out = parallel_result.secondary_output.strip()
                        if not _p_out and not _s_out:
                            logger.warning("Both skill outputs empty — skipping synthesis")
                            full_response = "Both skills returned empty results. Please try rephrasing your query."
                            messages.append({"role": "assistant", "content": full_response})
                            await websocket.send_json({"type": "text_delta", "text": full_response})
                            await websocket.send_json(
                                {"type": "done", "full_response": full_response, "skill_name": intent}
                            )
                            last_mode = intent
                            continue
                        elif not _p_out or not _s_out:
                            active_skill = intent if _p_out else secondary_skill.name
                            active_output = _p_out or _s_out
                            empty_skill = secondary_skill.name if _p_out else intent
                            logger.warning(
                                "Skill '%s' returned empty output — using '%s' only", empty_skill, active_skill
                            )
                            full_response = f"*Note: {empty_skill} skill did not return results.*\n\n{active_output}"
                            messages.append({"role": "assistant", "content": full_response})
                            await websocket.send_json({"type": "text_delta", "text": full_response})
                            await websocket.send_json(
                                {
                                    "type": "done",
                                    "full_response": full_response,
                                    "skill_name": active_skill,
                                    "multi_skill": {
                                        "skills": [intent, secondary_skill.name],
                                        "empty_skill": empty_skill,
                                    },
                                }
                            )
                            last_mode = intent
                            continue

                        await websocket.send_json({"type": "skill_progress", "skill": "synthesis", "status": "running"})

                        from ..agent import create_client as _create_synth_client

                        synth_client = _create_synth_client()

                        async def _synth_text_delta(text: str):
                            try:
                                await websocket.send_json({"type": "text_delta", "text": text})
                            except Exception:
                                pass

                        synthesis = await synthesize_parallel_outputs(
                            parallel_result,
                            content,
                            synth_client,
                            on_text_delta=_synth_text_delta,
                        )
                        full_response = synthesis.unified_response
                        messages.append({"role": "assistant", "content": full_response})

                        # Forward buffered components from both skills
                        for comp in parallel_result.primary_components + parallel_result.secondary_components:
                            try:
                                await websocket.send_json({"type": "component", "spec": comp, "tool": "parallel"})
                            except Exception:
                                pass

                        if parallel_result.primary_output:
                            bus.publish(
                                ContextEntry(
                                    source="sre_agent",
                                    category="diagnosis",
                                    summary=parallel_result.primary_output[:200],
                                    details={"mode": intent},
                                    namespace=namespace_from_context,
                                )
                            )
                        if parallel_result.secondary_output:
                            bus.publish(
                                ContextEntry(
                                    source="security_agent" if secondary_skill.name == "security" else "sre_agent",
                                    category="diagnosis",
                                    summary=parallel_result.secondary_output[:200],
                                    details={"mode": secondary_skill.name},
                                    namespace=namespace_from_context,
                                )
                            )

                        from dataclasses import asdict

                        combined_tokens = {
                            k: parallel_result.primary_tokens.get(k, 0) + parallel_result.secondary_tokens.get(k, 0)
                            for k in set(list(parallel_result.primary_tokens) + list(parallel_result.secondary_tokens))
                        }

                        await websocket.send_json(
                            {
                                "type": "done",
                                "full_response": full_response,
                                "skill_name": intent,
                                "duration_ms": parallel_result.duration_ms,
                                "input_tokens": combined_tokens.get("input_tokens", 0),
                                "output_tokens": combined_tokens.get("output_tokens", 0),
                                "multi_skill": {
                                    "skills": [intent, secondary_skill.name],
                                    "conflicts": [asdict(c) for c in synthesis.conflicts],
                                },
                            }
                        )

                        last_mode = intent
                        continue

                    except Exception:
                        logger.warning("Multi-skill execution failed, falling back to single-skill", exc_info=True)
                        secondary_skill = None

            try:
                result = await _run_agent_ws(
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
                # Unpack response + metadata tuple
                if isinstance(result, tuple):
                    full_response, turn_meta = result
                else:
                    full_response, turn_meta = result, {}
                messages.append({"role": "assistant", "content": full_response})

                # Persist messages to chat history (single commit)
                try:
                    from ..chat_history import save_turn

                    save_turn(session_id, content, full_response, is_first_turn=(turn_counter == 1))
                except Exception:
                    logger.debug("Failed to persist chat messages", exc_info=True)

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
                            "skill_name": last_mode,
                            "tool_count": turn_meta.get("tool_count", 0),
                            "duration_ms": turn_meta.get("duration_ms", 0),
                            "input_tokens": turn_meta.get("input_tokens", 0),
                            "output_tokens": turn_meta.get("output_tokens", 0),
                        }
                    )
                except Exception:
                    pass  # Client disconnected -- expected during long queries

                # Record skill invocation for analytics (with tool/token data)
                try:
                    from ..skill_analytics import record_skill_invocation
                    from ..skill_loader import get_skill as _get_skill_for_analytics

                    _sk = _get_skill_for_analytics(last_mode)
                    record_skill_invocation(
                        session_id=session_id,
                        user_id=ws_user or "anonymous",
                        skill_name=last_mode,
                        skill_version=_sk.version if _sk else 0,
                        query_summary=content[:200],
                        tools_called=turn_meta.get("tools_called"),
                        duration_ms=turn_meta.get("duration_ms", 0),
                        input_tokens=turn_meta.get("input_tokens", 0),
                        output_tokens=turn_meta.get("output_tokens", 0),
                    )
                except Exception:
                    logger.debug("Failed to record skill invocation", exc_info=True)

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


# -- Protocol v2: /ws/monitor -----------------------------------------------


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
    max_trust_level = get_settings().max_trust_level
    trust_level = 1
    auto_fix_categories: list[str] = []

    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=10)
        data = json.loads(raw)
        if data.get("type") == "subscribe_monitor":
            requested_trust = data.get("trustLevel", 1)
            # Clamp to server-configured maximum -- client cannot escalate
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
    _active_monitor_sessions[ws_id] = session

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

            # Rate limiting (same as /ws/agent)
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
                    logger.info("Manual scan skipped -- scan already in progress")
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

            elif msg_type == "set_disabled_scanners":
                scanner_ids = data.get("scannerIds", [])
                if isinstance(scanner_ids, list):
                    session.disabled_scanners = {str(s) for s in scanner_ids if isinstance(s, str) and len(s) < 64}
                    logger.info("Disabled scanners updated: %s", session.disabled_scanners)
                    await websocket.send_json(
                        {"type": "ack", "message": f"Disabled {len(session.disabled_scanners)} scanners"}
                    )

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
        await session.cancel_pending_investigations()
        scan_task.cancel()
        try:
            await scan_task
        except asyncio.CancelledError:
            pass
        _ws_alive.pop(ws_id, None)
        _active_monitor_sessions.pop(ws_id, None)
