"""Dynamic Prompt Builder — assembles the complete system prompt from parts.

Centralizes prompt assembly that was previously scattered across
ws_endpoints.py, agent.py, harness.py, and runbooks.py.

Assembly order (static → dynamic):
1. Base skill prompt (from skill.md)
2. Intent analysis prefix (think-before-acting guidance)
3. Component hint (UI rendering schemas, tailored to skill's tools)
4. Style hint (UI-driven preferences)
5. Fleet mode prefix (if multi-cluster)
6. Runbooks (matched to query, SRE/both only)
7. Shared context (from context bus)
8. Cluster context + intelligence + chain hints (from harness)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .skill_loader import Skill

logger = logging.getLogger("pulse_agent.prompt_builder")


# ---------------------------------------------------------------------------
# Intent Analysis Prefix — injected into every prompt (~100 tokens)
# Makes the agent classify the query before acting, leading to better
# tool selection and more focused responses.
# ---------------------------------------------------------------------------

INTENT_PREFIX = """
## Intent Analysis

Before responding, silently determine:
1. **Intent**: diagnose | monitor | build | scan | fix | explain | compare | plan | self-describe
2. **Entities**: namespace, resource type, resource name, time range
3. **Scope**: resource | namespace | cluster | fleet
4. **Complexity**: simple (1-2 tools) | moderate (3-5 tools) | complex (multi-step)

For complex queries, outline your plan before executing.
For simple queries, act directly.
Use extracted entities in tool calls — don't ask the user for information already in their query.

## Self-Awareness

IMPORTANT: When asked about your capabilities ("what can you do?", "help", "what tools?"),
ALWAYS call the self-description tools — do NOT answer from memory. Your capabilities
change dynamically (skills are added, MCP tools connect). Only the tools have the current list.

Required tool calls for capability questions:
- "What can you do?" / "help" → MUST call `list_my_skills`
- "What tools?" → MUST call `list_my_tools`
- "What components/widgets?" → MUST call `list_ui_components`
- "What PromQL recipes?" → MUST call `list_promql_recipes`
- "What runbooks?" → MUST call `list_runbooks`
- "Explain <resource>" → MUST call `explain_resource`
- "What APIs are deprecated?" → MUST call `list_deprecated_apis`
- "Create a skill" → MUST call `create_skill` (you CAN create, edit, and delete skills)
- "Clone a skill" → MUST call `create_skill_from_template`

NEVER say you can't do something if you have a tool for it. Check your available tools first.
"""

FLEET_PREFIX = (
    "[FLEET MODE: This query spans all managed clusters. "
    "Use fleet_* tools (fleet_list_pods, fleet_list_deployments, "
    "fleet_compare_resource, etc.) to query across clusters. "
    "Do NOT use single-cluster tools unless the user specifies a cluster.]"
)


def assemble_prompt(
    skill: Skill,
    query: str,
    mode: str,
    tool_names: list[str],
    *,
    fleet_mode: bool = False,
    ui_context: str = "",
    style_hint: str = "",
    shared_context: str = "",
) -> tuple[str, str]:
    """Assemble the complete system prompt from all sources.

    Returns (static_prompt, dynamic_context) for build_cached_system_prompt().
    Static parts are cached by the Anthropic API (5-min ephemeral TTL).
    Dynamic parts change per-turn (cluster state, runbooks, context bus).

    Args:
        skill: The active skill (provides base prompt + component config)
        query: The user's current query (for runbook matching)
        mode: Agent mode name (sre, security, view_designer, etc.)
        tool_names: List of tool names being offered this turn
        fleet_mode: Whether multi-cluster fleet mode is active
        ui_context: Sanitized context from the Pulse UI (namespace, resource)
        style_hint: UI-driven style preferences
        shared_context: Cross-agent shared context from context bus
    """
    # --- Static parts (cached by Anthropic API) ---
    static_parts: list[str] = []

    # 1. Base skill prompt
    static_parts.append(skill.system_prompt)

    # 2. Intent analysis prefix
    static_parts.append(INTENT_PREFIX)

    # 3. Component hint (tailored to skill's tools)
    from .skill_loader import _build_component_hint

    hint = _build_component_hint(skill, tool_names)
    if hint:
        static_parts.append(hint)

    # 4. Style hint
    if style_hint:
        static_parts.append(style_hint)

    # 5. Fleet mode prefix
    if fleet_mode:
        static_parts.append(FLEET_PREFIX)

    static_prompt = "\n\n".join(p for p in static_parts if p)

    # --- Dynamic parts (refreshed per-turn, not cached) ---
    dynamic_parts: list[str] = []

    # 6. Runbooks (matched to query keywords)
    if mode in ("sre", "both"):
        try:
            from .runbooks import select_runbooks

            runbook_text = select_runbooks(query)
            if runbook_text:
                dynamic_parts.append(runbook_text)
        except Exception:
            logger.debug("Runbook selection failed", exc_info=True)

    # 7. Shared context from context bus
    if shared_context:
        dynamic_parts.append(shared_context)

    # 8. UI context
    if ui_context:
        dynamic_parts.append(ui_context)

    # 9. Cluster context + intelligence + chain hints
    try:
        from .harness import get_cluster_context

        cluster_ctx = get_cluster_context(mode=mode)
        if cluster_ctx:
            dynamic_parts.append(cluster_ctx)
    except Exception:
        logger.debug("Cluster context gathering failed", exc_info=True)

    dynamic_context = "\n\n".join(p for p in dynamic_parts if p)

    logger.debug(
        "Prompt assembled: static=%d chars, dynamic=%d chars, skill=%s, tools=%d",
        len(static_prompt),
        len(dynamic_context),
        skill.name,
        len(tool_names),
    )

    return static_prompt, dynamic_context
