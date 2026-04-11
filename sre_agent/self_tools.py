"""Self-description tools — let the agent tell users what it can do."""

from __future__ import annotations

from anthropic import beta_tool


@beta_tool
def list_my_skills() -> str:
    """List all available agent skills with descriptions. Call when the user asks what you can do or what skills you have."""
    from .skill_loader import list_skills

    skills = list_skills()
    lines = [f"I have {len(skills)} skills:\n"]
    for skill in sorted(skills, key=lambda s: -s.priority):
        status = " (degraded)" if skill.degraded else ""
        lines.append(f"**{skill.name}** v{skill.version}{status} — {skill.description}")
        if skill.categories:
            lines.append(f"  Categories: {', '.join(skill.categories)}")
        if skill.handoff_to:
            targets = ", ".join(skill.handoff_to.keys())
            lines.append(f"  Can hand off to: {targets}")

    return "\n".join(lines)


@beta_tool
def list_my_tools() -> str:
    """List all tools available to the agent grouped by source. Call when the user asks what tools you have."""
    from .mcp_client import list_mcp_tools
    from .tool_registry import TOOL_REGISTRY

    try:
        mcp_names = {t["name"] for t in list_mcp_tools()}
    except Exception:
        mcp_names = set()

    native = []
    mcp = []
    for name in sorted(TOOL_REGISTRY):
        tool = TOOL_REGISTRY[name]
        desc = getattr(tool, "description", "")[:80]
        if name in mcp_names:
            mcp.append(f"- `{name}` — {desc}")
        else:
            native.append(f"- `{name}` — {desc}")

    lines = [f"I have {len(TOOL_REGISTRY)} tools:\n"]
    lines.append(f"**Native ({len(native)}):**")
    lines.extend(native[:30])  # cap to avoid massive output
    if len(native) > 30:
        lines.append(f"  ... and {len(native) - 30} more")
    if mcp:
        lines.append(f"\n**MCP ({len(mcp)}):**")
        lines.extend(mcp)

    return "\n".join(lines)
