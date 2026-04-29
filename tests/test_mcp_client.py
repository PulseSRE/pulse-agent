"""Tests for MCP client."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from sre_agent.mcp_client import (
    MCPConnection,
    _connections,
    _connections_lock,
    connect_mcp_server,
    connect_skill_mcp,
    disconnect_all,
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

    def test_sre_mcp_has_config(self):
        path = Path(__file__).parent.parent / "sre_agent" / "skills" / "sre" / "mcp.yaml"
        config = load_mcp_config(path)
        assert "config" in config["toolsets"]

    def test_sre_mcp_has_observability(self):
        path = Path(__file__).parent.parent / "sre_agent" / "skills" / "sre" / "mcp.yaml"
        config = load_mcp_config(path)
        assert "observability" in config["toolsets"]

    def test_sre_mcp_has_renderers(self):
        path = Path(__file__).parent.parent / "sre_agent" / "skills" / "sre" / "mcp.yaml"
        config = load_mcp_config(path)
        assert "helm_list" in config["tool_renderers"]
        assert config["tool_renderers"]["helm_list"]["kind"] == "data_table"

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
        assert "not allowed" in conn.error.lower() or "not found" in conn.error.lower()


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


class TestMcpTokenForwarding:
    def test_mcp_post_includes_auth_header_when_token_set(self):
        from unittest.mock import MagicMock, patch

        from sre_agent.k8s_client import _user_api_client_var, _user_token_var

        reset_tok = _user_token_var.set("user-oauth-token")
        reset_cli = _user_api_client_var.set(None)
        try:
            with patch("urllib.request.urlopen") as mock_urlopen:
                mock_ctx = MagicMock()
                mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
                mock_ctx.__exit__ = MagicMock(return_value=False)
                mock_ctx.headers = {}
                mock_ctx.read.return_value = b'{"result": {}}'
                mock_urlopen.return_value = mock_ctx

                from sre_agent.mcp_client import _mcp_post

                _mcp_post("http://localhost:8081", {"jsonrpc": "2.0", "id": 1})
                call_args = mock_urlopen.call_args
                req = call_args[0][0]
                assert req.get_header("Authorization") == "Bearer user-oauth-token"
        finally:
            _user_token_var.reset(reset_tok)
            _user_api_client_var.reset(reset_cli)

    def test_mcp_post_no_auth_header_when_no_token(self):
        from unittest.mock import MagicMock, patch

        from sre_agent.k8s_client import _user_token_var

        assert _user_token_var.get() is None
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_ctx.headers = {}
            mock_ctx.read.return_value = b'{"result": {}}'
            mock_urlopen.return_value = mock_ctx

            from sre_agent.mcp_client import _mcp_post

            _mcp_post("http://localhost:8081", {"jsonrpc": "2.0", "id": 1})
            call_args = mock_urlopen.call_args
            req = call_args[0][0]
            assert not req.has_header("Authorization")


class TestConcurrentConnectSkillMcp:
    """Verify _connections dict is safe under concurrent access."""

    def setup_method(self):
        disconnect_all()

    def teardown_method(self):
        disconnect_all()

    def test_concurrent_connect_skill_mcp_no_runtime_error(self, tmp_path):
        """3 concurrent threads calling connect_skill_mcp must not raise RuntimeError
        from dict mutation, and _connections must have correct entries afterward."""
        # Create 3 skill dirs with mcp.yaml
        skill_paths = []
        for i in range(3):
            skill_dir = tmp_path / f"skill_{i}"
            skill_dir.mkdir()
            mcp_yaml = skill_dir / "mcp.yaml"
            mcp_yaml.write_text(f"server:\n  url: http://localhost:999{i}\n  transport: sse\ntoolsets: []\n")
            skill_paths.append((f"skill_{i}", skill_dir))

        def slow_connect(name, config, **kwargs):
            """Simulate a slow MCP connection."""
            time.sleep(0.05)
            return MCPConnection(
                name=name,
                url=config["server"]["url"],
                transport="sse",
                toolsets=[],
                connected=True,
                tools=[f"{name}_tool"],
            )

        errors: list[Exception] = []

        def worker(skill_name, skill_path):
            try:
                connect_skill_mcp(skill_name, skill_path, builtin=True, max_retries=1)
            except Exception as e:
                errors.append(e)

        with (
            patch("sre_agent.mcp_client.connect_mcp_server", side_effect=slow_connect),
            patch("sre_agent.mcp_client.register_mcp_tools", return_value=1),
        ):
            threads = [threading.Thread(target=worker, args=(name, path)) for name, path in skill_paths]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        assert not errors, f"Threads raised: {errors}"
        with _connections_lock:
            assert len(_connections) == 3
            for name, _ in skill_paths:
                assert name in _connections
                assert _connections[name].connected


class TestStdioArgBlocking:
    """Verify dangerous stdio arguments are blocked."""

    def test_blocked_eval_flag(self):
        config = {"server": {"url": "node --eval console.log(1)", "transport": "stdio"}, "toolsets": []}
        conn = connect_mcp_server("test-eval", config)
        assert not conn.connected
        assert "Blocked" in conn.error
        assert "--eval" in conn.error

    def test_blocked_yes_flag(self):
        config = {"server": {"url": "npx --yes @malicious/pkg", "transport": "stdio"}, "toolsets": []}
        conn = connect_mcp_server("test-yes", config)
        assert not conn.connected
        assert "Blocked" in conn.error
        assert "--yes" in conn.error

    def test_blocked_python_c_flag(self):
        config = {"server": {"url": "python3 -c import os", "transport": "stdio"}, "toolsets": []}
        conn = connect_mcp_server("test-c", config)
        assert not conn.connected
        assert "Blocked" in conn.error

    def test_blocked_prefix_form(self):
        config = {"server": {"url": "node --eval=console.log(1)", "transport": "stdio"}, "toolsets": []}
        conn = connect_mcp_server("test-prefix", config)
        assert not conn.connected
        assert "Blocked" in conn.error

    def test_allowed_normal_args(self):
        """Normal args like package names should not be blocked."""
        config = {
            "server": {"url": "npx @openshift/openshift-mcp-server --port 3000", "transport": "stdio"},
            "toolsets": [],
        }
        conn = connect_mcp_server("test-normal", config)
        # Will fail on actual process spawn (no npx available in test), but should NOT fail on arg validation
        assert "Blocked" not in (conn.error or "")


class TestNonBuiltinStdioBlocked:
    """Verify user-created skills cannot use stdio transport."""

    def setup_method(self):
        disconnect_all()

    def teardown_method(self):
        disconnect_all()

    def test_non_builtin_stdio_returns_none(self, tmp_path):
        skill_dir = tmp_path / "user_skill"
        skill_dir.mkdir()
        (skill_dir / "mcp.yaml").write_text("server:\n  url: npx some-tool\n  transport: stdio\ntoolsets: []\n")
        result = connect_skill_mcp("user_skill", skill_dir, builtin=False)
        assert result is None

    def test_builtin_stdio_allowed(self, tmp_path):
        skill_dir = tmp_path / "builtin_skill"
        skill_dir.mkdir()
        (skill_dir / "mcp.yaml").write_text("server:\n  url: npx some-tool\n  transport: stdio\ntoolsets: []\n")
        result = connect_skill_mcp("builtin_skill", skill_dir, builtin=True)
        assert result is not None

    def test_non_builtin_sse_allowed(self, tmp_path):
        skill_dir = tmp_path / "user_skill_sse"
        skill_dir.mkdir()
        (skill_dir / "mcp.yaml").write_text("server:\n  url: http://localhost:9999\n  transport: sse\ntoolsets: []\n")
        result = connect_skill_mcp("user_skill_sse", skill_dir, builtin=False)
        assert result is not None
