"""Central tool registry — all @beta_tool functions register here."""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ToolLike(Protocol):
    """Protocol for tool objects (both @beta_tool and MCPTool)."""

    name: str

    def to_dict(self) -> dict: ...

    def call(self, input_data: dict) -> str | tuple[str, dict]: ...


TOOL_REGISTRY: dict[str, Any] = {}
WRITE_TOOL_NAMES: set[str] = set()
TOOL_CATEGORIES: dict[str, set[str]] = {}


def register_tool(tool: Any, *, is_write: bool, category: str = "general") -> Any:
    """Register a tool in the central registry.

    is_write is keyword-only and has NO default, deliberately. It decides which
    branch of the agent loop a tool takes: write tools go through the
    confirmation gate, read tools execute in parallel unattended. When it
    defaulted to False, four tools that rewrite the agent's own system prompt
    (create_skill, edit_skill, delete_skill, create_skill_from_template) were
    registered as reads — not by a bad decision, but by no decision at all.

    Requiring it means a tool cannot be added without someone answering "does
    this need a human to approve it?". Getting the answer wrong is a bug;
    never being asked is a class of bug.
    """
    TOOL_REGISTRY[tool.name] = tool
    if is_write:
        WRITE_TOOL_NAMES.add(tool.name)
    TOOL_CATEGORIES.setdefault(category, set()).add(tool.name)
    return tool


def get_all_tools() -> list[Any]:
    return list(TOOL_REGISTRY.values())


def get_tool_map() -> dict[str, Any]:
    return dict(TOOL_REGISTRY)


def unregister_tool(name: str) -> None:
    """Remove a tool from the registry."""
    TOOL_REGISTRY.pop(name, None)
    WRITE_TOOL_NAMES.discard(name)
    for cat_set in TOOL_CATEGORIES.values():
        cat_set.discard(name)


def get_write_tools() -> set[str]:
    return set(WRITE_TOOL_NAMES)


def get_tools_by_category(category: str) -> list[Any]:
    """Return all tools in a given category."""
    names = TOOL_CATEGORIES.get(category, set())
    return [TOOL_REGISTRY[n] for n in names if n in TOOL_REGISTRY]
