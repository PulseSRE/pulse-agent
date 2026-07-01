"""Tests for MCP client."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from sre_agent.mcp_client import (
    MCPConnection,
    _connections,
    connect_mcp_server,
    disconnect_all,
    get_skill_mcp_tool_names,
    list_mcp_connections,
    list_mcp_tools,
    load_mcp_config,
    register_mcp_tools,
)


class TestLoadMCPConfig:
    def test_loads_sre_mcp_yaml(self):
        path = Path(__file__).parent.parent / "sre_agent" / "skills" / "sre" / "mcp.yaml"
        config = load_mcp_config(path)
        assert config is not None
        assert "server" in config
        assert "toolsets" in config
        assert "tool_renderers" in config

    def test_sre_mcp_has_helm(self):
        path = Path(__file__).parent.parent / "sre_agent" / "skills" / "sre" / "mcp.yaml"
        config = load_mcp_config(path)
        assert "helm" in config["toolsets"]

    def test_sre_mcp_has_observability(self):
        path = Path(__file__).parent.parent / "sre_agent" / "skills" / "sre" / "mcp.yaml"
        config = load_mcp_config(path)
        assert "observability" in config["toolsets"]

    def test_sre_mcp_has_renderers(self):
        path = Path(__file__).parent.parent / "sre_agent" / "skills" / "sre" / "mcp.yaml"
        config = load_mcp_config(path)
        assert "helm_list" in config["tool_renderers"]
        assert config["tool_renderers"]["helm_list"]["kind"] == "data_table"

    def test_sre_mcp_declares_write_tools(self):
        """helm_install/helm_uninstall must be declared write-capable so they
        get the same confirmation gate as native write tools."""
        path = Path(__file__).parent.parent / "sre_agent" / "skills" / "sre" / "mcp.yaml"
        config = load_mcp_config(path)
        assert "write_tools" in config
        assert "helm_install" in config["write_tools"]
        assert "helm_uninstall" in config["write_tools"]
        assert "helm_list" not in config["write_tools"]

    def test_missing_file_returns_none(self, tmp_path):
        config = load_mcp_config(tmp_path / "nonexistent.yaml")
        assert config is None

    def test_invalid_yaml_returns_none(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("{{invalid yaml}}")
        config = load_mcp_config(bad)
        assert config is None


class TestConnectMCPServer:
    def test_empty_url_returns_error(self):
        config = {"server": {"url": ""}, "toolsets": []}
        conn = connect_mcp_server("test", config)
        assert not conn.connected
        assert "No server URL" in conn.error

    def test_unknown_transport_returns_error(self):
        config = {"server": {"url": "test", "transport": "unknown"}, "toolsets": []}
        conn = connect_mcp_server("test", config)
        assert not conn.connected
        assert "Unknown transport" in conn.error

    def test_sse_connection_failure(self):
        config = {"server": {"url": "http://localhost:99999", "transport": "sse"}, "toolsets": []}
        conn = connect_mcp_server("test", config)
        assert not conn.connected
        assert conn.error  # Should have an error (connection refused)

    def test_missing_command_returns_error(self):
        config = {"server": {"url": "nonexistent_binary_xyz_12345", "transport": "stdio"}, "toolsets": []}
        conn = connect_mcp_server("test", config)
        assert not conn.connected
        assert "not found" in conn.error.lower() or "Failed" in conn.error

    def test_write_tools_parsed_from_config(self):
        config = {
            "server": {"url": "test", "transport": "unknown"},
            "toolsets": [],
            "write_tools": ["helm_install", "helm_uninstall"],
        }
        conn = connect_mcp_server("test", config)
        assert conn.write_tools == ["helm_install", "helm_uninstall"]

    def test_write_tools_defaults_to_empty(self):
        config = {"server": {"url": "test", "transport": "unknown"}, "toolsets": []}
        conn = connect_mcp_server("test", config)
        assert conn.write_tools == []

    def test_write_tools_ignores_non_string_entries(self):
        config = {
            "server": {"url": "test", "transport": "unknown"},
            "toolsets": [],
            "write_tools": ["helm_install", 123, None],
        }
        conn = connect_mcp_server("test", config)
        assert conn.write_tools == ["helm_install"]


class TestMCPConnection:
    def test_dataclass(self):
        conn = MCPConnection(
            name="test",
            url="npx @openshift/openshift-mcp-server",
            transport="stdio",
            toolsets=["helm"],
        )
        assert conn.name == "test"
        assert not conn.connected
        assert conn.tools == []


class TestRegisterMCPTools:
    def test_registers_tools(self):
        conn = MCPConnection(
            name="test-server",
            url="test",
            transport="stdio",
            toolsets=["helm"],
            connected=True,
            tools=["helm_list", "helm_install"],
            tool_renderers={"helm_list": {"kind": "data_table", "parser": "json"}},
        )
        conn.process = MagicMock()  # Fake process

        with patch("sre_agent.tool_registry.register_tool") as mock_register:
            count = register_mcp_tools(conn)
            assert count == 2
            assert mock_register.call_count == 2

    def test_declared_write_tool_registers_as_write(self):
        """A tool listed in conn.write_tools must be registered with is_write=True."""
        conn = MCPConnection(
            name="test-server",
            url="test",
            transport="stdio",
            toolsets=["helm"],
            connected=True,
            tools=["helm_list", "helm_install"],
            write_tools=["helm_install"],
        )
        conn.process = MagicMock()

        calls: dict[str, bool] = {}

        def capture(tool, is_write=False):
            calls[tool.name] = is_write

        with patch("sre_agent.tool_registry.register_tool", side_effect=capture):
            register_mcp_tools(conn)

        assert calls["helm_install"] is True
        assert calls["helm_list"] is False

    def test_no_write_tools_declared_registers_all_as_read(self):
        conn = MCPConnection(
            name="test-server",
            url="test",
            transport="stdio",
            toolsets=["helm"],
            connected=True,
            tools=["helm_list", "helm_install"],
        )
        conn.process = MagicMock()

        calls: dict[str, bool] = {}

        def capture(tool, is_write=False):
            calls[tool.name] = is_write

        with patch("sre_agent.tool_registry.register_tool", side_effect=capture):
            register_mcp_tools(conn)

        assert calls == {"helm_list": False, "helm_install": False}

    def test_tool_has_correct_attributes(self):
        conn = MCPConnection(
            name="test-server",
            url="test",
            transport="stdio",
            toolsets=[],
            connected=True,
            tools=["my_tool"],
        )
        conn.process = MagicMock()

        registered_tools = []

        def capture(tool, **kwargs):
            registered_tools.append(tool)

        with patch("sre_agent.tool_registry.register_tool", side_effect=capture):
            register_mcp_tools(conn)

        assert len(registered_tools) == 1
        tool = registered_tools[0]
        assert tool.name == "my_tool"
        assert "MCP" in tool.description
        d = tool.to_dict()
        assert d["name"] == "my_tool"
        assert "input_schema" in d


class TestListFunctions:
    def test_list_connections_empty(self):
        disconnect_all()
        result = list_mcp_connections()
        assert result == []

    def test_list_tools_empty(self):
        disconnect_all()
        result = list_mcp_tools()
        assert result == []


class TestGetSkillMcpToolNames:
    """Regression coverage for the skill_loader <-> mcp_client wiring.

    Category-scoped skills (e.g. 'sre') look up their own MCP connection by
    skill name to include those tools in their tool_map -- this must keep
    working, and must return [] safely when there's no connection.
    """

    def test_no_connection_returns_empty(self, monkeypatch):
        monkeypatch.setattr("sre_agent.mcp_client._connections", {})
        assert get_skill_mcp_tool_names("sre") == []

    def test_returns_tools_for_connected_skill(self, monkeypatch):
        conn = MCPConnection(
            name="test-mcp",
            url="http://x",
            transport="sse",
            toolsets=["helm"],
            connected=True,
            tools=["helm_list", "helm_install"],
        )
        monkeypatch.setitem(_connections, "sre", conn)
        assert get_skill_mcp_tool_names("sre") == ["helm_list", "helm_install"]

    def test_returns_empty_for_disconnected_server(self, monkeypatch):
        conn = MCPConnection(
            name="test-mcp",
            url="http://x",
            transport="sse",
            toolsets=["helm"],
            connected=False,
            tools=["helm_list"],
            error="connection refused",
        )
        monkeypatch.setitem(_connections, "sre", conn)
        assert get_skill_mcp_tool_names("sre") == []
