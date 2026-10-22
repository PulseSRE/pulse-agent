"""Tools that change the agent itself, and who may call them.

``require_admin`` guarded the REST endpoints that edit skills, but the same
operations were reachable as *tools*: from a view's action button (which ran
whatever tool name the request named) and from chat, where a non-admin could
approve the confirmation themselves. A skill is part of the system prompt, so
editing one is the most powerful thing the agent exposes. These tools now
follow the same rule as the endpoints.
"""

from __future__ import annotations

#: Tools that rewrite the agent's own behaviour or act outside the cluster on
#: its behalf. Never offered to a non-admin, never runnable from a view button.
ADMIN_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "create_skill",
        "edit_skill",
        "delete_skill",
        "create_skill_from_template",
        "propose_git_change",
    }
)


def is_admin(user: str | None) -> bool:
    """The same answer ``require_admin`` gives for this user.

    With ``PULSE_AGENT_ADMIN_USERS`` set, only those users. Unset, any
    identified user (the documented pre-existing behaviour; the operator can
    require the list). Never an anonymous session.
    """
    from .config import get_settings

    if not user or user == "anonymous":
        return False
    configured = get_settings().server.admin_users
    allowed = {u.strip() for u in configured.split(",") if u.strip()}
    return not allowed or user in allowed


def without_admin_tools(config: dict, user: str | None) -> dict:
    """``config`` with admin-only tools removed unless ``user`` is an admin."""
    if is_admin(user):
        return config
    tool_map = config.get("tool_map") or {}
    if not any(name in tool_map for name in ADMIN_ONLY_TOOLS):
        return config
    out = dict(config)
    out["tool_map"] = {k: v for k, v in tool_map.items() if k not in ADMIN_ONLY_TOOLS}
    out["tool_defs"] = [d for d in config.get("tool_defs") or [] if _tool_name(d) not in ADMIN_ONLY_TOOLS]
    return out


def _tool_name(tool_def) -> str:
    if isinstance(tool_def, dict):
        return str(tool_def.get("name", ""))
    return str(getattr(tool_def, "name", ""))
