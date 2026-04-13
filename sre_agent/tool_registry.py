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


def register_tool(tool: Any, is_write: bool = False) -> Any:
    """Register a tool in the central registry."""
    TOOL_REGISTRY[tool.name] = tool
    if is_write:
        WRITE_TOOL_NAMES.add(tool.name)
    return tool


def get_all_tools() -> list[Any]:
    return list(TOOL_REGISTRY.values())


def get_tool_map() -> dict[str, Any]:
    return dict(TOOL_REGISTRY)


def unregister_tool(name: str) -> None:
    """Remove a tool from the registry."""
    TOOL_REGISTRY.pop(name, None)
    WRITE_TOOL_NAMES.discard(name)


def get_write_tools() -> set[str]:
    return set(WRITE_TOOL_NAMES)
