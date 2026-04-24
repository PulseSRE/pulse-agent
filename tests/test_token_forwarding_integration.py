"""Integration tests for user token forwarding end-to-end."""

import concurrent.futures
from unittest.mock import MagicMock, patch

from sre_agent.k8s_client import _user_api_client_var, _user_token_var, user_token_context


class TestEndToEndTokenFlow:
    def test_execute_tool_with_token_creates_user_client(self):
        """Full flow: _execute_tool sets contextvar, get_core_client returns user client."""
        from sre_agent.agent import _execute_tool

        captured_clients = []

        def fake_tool(inp):
            from sre_agent.k8s_client import get_core_client

            with patch("sre_agent.k8s_client._load_k8s"):
                c = get_core_client()
                captured_clients.append(c)
            return "ok"

        mock_tool = MagicMock()
        mock_tool.call.side_effect = fake_tool
        tool_map = {"test": mock_tool}

        _execute_tool("test", {}, tool_map, user_token="integration-token")

        assert len(captured_clients) == 1
        api_client = captured_clients[0].api_client
        assert api_client.configuration.api_key["authorization"] == "Bearer integration-token"

    def test_execute_tool_without_token_uses_sa(self):
        """Without user token, get_core_client returns SA singleton."""
        from sre_agent.agent import _execute_tool

        captured_tokens = []

        def fake_tool(inp):
            captured_tokens.append(_user_token_var.get())
            return "ok"

        mock_tool = MagicMock()
        mock_tool.call.side_effect = fake_tool
        tool_map = {"test": mock_tool}

        _execute_tool("test", {}, tool_map, user_token=None)

        assert captured_tokens == [None]

    def test_monitor_path_never_gets_token(self):
        """Simulate monitor calling get_core_client — no contextvar set."""
        assert _user_token_var.get() is None
        with patch("sre_agent.k8s_client._load_k8s"):
            from sre_agent.k8s_client import get_core_client

            c1 = get_core_client()
            c2 = get_core_client()
            assert c1 is c2  # SA singleton

    def test_concurrent_users_isolated(self):
        """Two concurrent tool calls with different tokens don't cross-contaminate."""
        from sre_agent.agent import _execute_tool

        results = {}

        def capture_token_a(inp):
            results["a"] = _user_token_var.get()
            return "ok"

        def capture_token_b(inp):
            results["b"] = _user_token_var.get()
            return "ok"

        tool_a = MagicMock()
        tool_a.call.side_effect = capture_token_a
        tool_b = MagicMock()
        tool_b.call.side_effect = capture_token_b

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(_execute_tool, "a", {}, {"a": tool_a}, "token-alice")
            f2 = pool.submit(_execute_tool, "b", {}, {"b": tool_b}, "token-bob")
            f1.result()
            f2.result()

        assert results["a"] == "token-alice"
        assert results["b"] == "token-bob"
        assert _user_token_var.get() is None

    def test_user_token_context_with_k8s_client(self):
        """user_token_context wraps non-tool K8s calls (topology path)."""
        captured = []

        with patch("sre_agent.k8s_client._load_k8s"):
            with user_token_context("topo-token"):
                from sre_agent.k8s_client import get_core_client

                c = get_core_client()
                captured.append(c.api_client.configuration.api_key.get("authorization"))

        assert captured == ["Bearer topo-token"]
        assert _user_token_var.get() is None

    def test_mcp_post_uses_token_from_contextvar(self):
        """MCP _mcp_post reads the contextvar and adds Authorization header."""
        reset_tok = _user_token_var.set("mcp-test-token")
        reset_cli = _user_api_client_var.set(None)
        try:
            import urllib.request

            with patch.object(urllib.request, "urlopen") as mock_urlopen:
                mock_ctx = MagicMock()
                mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
                mock_ctx.__exit__ = MagicMock(return_value=False)
                mock_ctx.headers = {}
                mock_ctx.read.return_value = b'{"result": {}}'
                mock_urlopen.return_value = mock_ctx

                from sre_agent.mcp_client import _mcp_post

                _mcp_post("http://localhost:8081", {"jsonrpc": "2.0", "id": 999})
                req = mock_urlopen.call_args[0][0]
                assert req.get_header("Authorization") == "Bearer mcp-test-token"
        finally:
            _user_token_var.reset(reset_tok)
            _user_api_client_var.reset(reset_cli)

    def test_view_executor_forwards_token(self):
        """View executor sets contextvar when executing widget tools."""
        from sre_agent.k8s_client import _user_token_var

        captured = []

        def mock_call(args):
            captured.append(_user_token_var.get())
            return ("result", {"kind": "info_card_grid", "props": {}})

        mock_tool = MagicMock()
        mock_tool.call = mock_call

        with patch("sre_agent.view_executor._resolve_tool", return_value=mock_tool):
            with patch("sre_agent.view_executor.WRITE_TOOL_NAMES", set()):
                from sre_agent.view_executor import _execute_tool_widget

                _execute_tool_widget(
                    {"tool": "t", "args": {}, "kind": "info_card_grid", "title": "X"},
                    item_id="i",
                    user_token="view-user-token",
                )

        assert captured == ["view-user-token"]
        assert _user_token_var.get() is None

    def test_config_toggle_disables_forwarding(self):
        """When token_forwarding=False, user_token_context is a noop."""
        with patch("sre_agent.config.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(token_forwarding=False)
            with user_token_context("should-be-ignored"):
                assert _user_token_var.get() is None
