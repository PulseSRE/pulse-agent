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


def register_tool(tool: Any, is_write: bool = False, category: str = "general") -> Any:
    """Register a tool in the central registry."""
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
