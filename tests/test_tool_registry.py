"""Tests for tool_registry.py — tool registration and discovery."""

from __future__ import annotations

from types import SimpleNamespace

from sre_agent.tool_registry import (
    TOOL_REGISTRY,
    WRITE_TOOL_NAMES,
    get_all_tools,
    get_tool_map,
    get_write_tools,
    register_tool,
)


class TestRegisterTool:
    def setup_method(self):
        self._orig_registry = dict(TOOL_REGISTRY)
        self._orig_writes = set(WRITE_TOOL_NAMES)

    def teardown_method(self):
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(self._orig_registry)
        WRITE_TOOL_NAMES.clear()
        WRITE_TOOL_NAMES.update(self._orig_writes)

    def test_register_read_tool(self):
        tool = SimpleNamespace(name="test_read_tool")
        result = register_tool(tool, is_write=False)
        assert result is tool
        assert "test_read_tool" in TOOL_REGISTRY
        assert "test_read_tool" not in WRITE_TOOL_NAMES

    def test_register_write_tool(self):
        tool = SimpleNamespace(name="test_write_tool")
        register_tool(tool, is_write=True)
        assert "test_write_tool" in TOOL_REGISTRY
        assert "test_write_tool" in WRITE_TOOL_NAMES

    def test_register_overwrites_existing(self):
        tool1 = SimpleNamespace(name="dup_tool", version=1)
        tool2 = SimpleNamespace(name="dup_tool", version=2)
        register_tool(tool1, is_write=False)
        register_tool(tool2, is_write=False)
        assert TOOL_REGISTRY["dup_tool"].version == 2


class TestGetAllTools:
    def setup_method(self):
        self._orig_registry = dict(TOOL_REGISTRY)

    def teardown_method(self):
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(self._orig_registry)

    def test_returns_list(self):
        result = get_all_tools()
        assert isinstance(result, list)

    def test_returns_registered_tools(self):
        TOOL_REGISTRY.clear()
        t1 = SimpleNamespace(name="a")
        t2 = SimpleNamespace(name="b")
        TOOL_REGISTRY["a"] = t1
        TOOL_REGISTRY["b"] = t2
        tools = get_all_tools()
        assert len(tools) == 2
        assert t1 in tools
        assert t2 in tools


class TestGetToolMap:
    def setup_method(self):
        self._orig_registry = dict(TOOL_REGISTRY)

    def teardown_method(self):
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(self._orig_registry)

    def test_returns_dict(self):
        result = get_tool_map()
        assert isinstance(result, dict)

    def test_returns_copy(self):
        result = get_tool_map()
        result["injected"] = "bad"
        assert "injected" not in TOOL_REGISTRY


class TestGetWriteTools:
    def setup_method(self):
        self._orig_writes = set(WRITE_TOOL_NAMES)

    def teardown_method(self):
        WRITE_TOOL_NAMES.clear()
        WRITE_TOOL_NAMES.update(self._orig_writes)

    def test_returns_set(self):
        result = get_write_tools()
        assert isinstance(result, set)

    def test_returns_copy(self):
        result = get_write_tools()
        result.add("injected")
        assert "injected" not in WRITE_TOOL_NAMES
