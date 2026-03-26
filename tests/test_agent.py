"""Tests for the agent loop, sanitization, and safety mechanisms."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

import pytest

from sre_agent.agent import (
    _sanitize_content,
    _execute_tool,
    run_agent_streaming,
    MAX_ITERATIONS,
    WRITE_TOOLS,
)


class TestSanitizeContent:
    def test_text_block(self):
        blocks = [SimpleNamespace(type="text", text="hello")]
        result = _sanitize_content(blocks)
        assert result == [{"type": "text", "text": "hello"}]

    def test_tool_use_strips_caller(self):
        blocks = [SimpleNamespace(
            type="tool_use", id="t1", name="list_pods",
            input={"namespace": "default"}, caller="some_caller"
        )]
        result = _sanitize_content(blocks)
        assert result == [{"type": "tool_use", "id": "t1", "name": "list_pods", "input": {"namespace": "default"}}]
        assert "caller" not in result[0]

    def test_thinking_block(self):
        blocks = [SimpleNamespace(type="thinking", thinking="hmm", signature="sig123")]
        result = _sanitize_content(blocks)
        assert result == [{"type": "thinking", "thinking": "hmm", "signature": "sig123"}]

    def test_redacted_thinking(self):
        blocks = [SimpleNamespace(type="redacted_thinking", data="redacted_data")]
        result = _sanitize_content(blocks)
        assert result == [{"type": "redacted_thinking", "data": "redacted_data"}]

    def test_unknown_block_skipped(self):
        blocks = [SimpleNamespace(type="unknown_type", foo="bar")]
        result = _sanitize_content(blocks)
        assert result == []

    def test_mixed_blocks(self):
        blocks = [
            SimpleNamespace(type="thinking", thinking="step 1", signature="s1"),
            SimpleNamespace(type="text", text="The answer is 42"),
            SimpleNamespace(type="tool_use", id="t1", name="list_pods", input={}),
        ]
        result = _sanitize_content(blocks)
        assert len(result) == 3
        assert result[0]["type"] == "thinking"
        assert result[1]["type"] == "text"
        assert result[2]["type"] == "tool_use"


class TestExecuteTool:
    def test_success(self):
        tool = MagicMock()
        tool.call.return_value = "result data"
        tool_map = {"my_tool": tool}
        text, component = _execute_tool("my_tool", {"arg": "val"}, tool_map)
        assert text == "result data"
        assert component is None
        tool.call.assert_called_once_with({"arg": "val"})

    def test_success_with_component(self):
        tool = MagicMock()
        tool.call.return_value = ("result data", {"kind": "data_table"})
        tool_map = {"my_tool": tool}
        text, component = _execute_tool("my_tool", {}, tool_map)
        assert text == "result data"
        assert component == {"kind": "data_table"}

    def test_unknown_tool(self):
        text, component = _execute_tool("nonexistent", {}, {})
        assert "unknown tool" in text
        assert component is None

    def test_exception_returns_type_only(self):
        tool = MagicMock()
        tool.call.side_effect = ValueError("secret details here")
        tool_map = {"bad_tool": tool}
        text, component = _execute_tool("bad_tool", {}, tool_map)
        assert "ValueError" in text
        assert "secret details" not in text
        assert component is None


class TestConfirmationGate:
    def _make_stream_context(self, responses):
        """Build a mock client that returns responses in sequence."""
        client = MagicMock()
        streams = []
        for resp in responses:
            stream = MagicMock()
            stream.__enter__ = MagicMock(return_value=stream)
            stream.__exit__ = MagicMock(return_value=False)
            stream.__iter__ = MagicMock(return_value=iter([]))
            stream.get_final_message.return_value = resp
            streams.append(stream)
        client.messages.stream = MagicMock(side_effect=streams)
        return client

    def test_write_tool_blocked_without_confirm(self):
        """Write tool should be blocked if on_confirm returns False."""
        tool_use_response = SimpleNamespace(
            stop_reason="tool_use",
            content=[
                SimpleNamespace(type="tool_use", id="t1", name="delete_pod",
                                input={"namespace": "default", "pod_name": "victim"}),
            ],
        )
        final_response = SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="Cancelled.")],
        )
        client = self._make_stream_context([tool_use_response, final_response])

        mock_tool = MagicMock()
        mock_tool.call.return_value = "deleted"

        result = run_agent_streaming(
            client=client,
            messages=[{"role": "user", "content": "delete pod"}],
            system_prompt="test",
            tool_defs=[],
            tool_map={"delete_pod": mock_tool},
            write_tools={"delete_pod"},
            on_confirm=lambda name, inp: False,  # User says no
        )

        # Tool should NOT have been called
        mock_tool.call.assert_not_called()

    def test_write_tool_allowed_with_confirm(self):
        """Write tool should execute if on_confirm returns True."""
        tool_use_response = SimpleNamespace(
            stop_reason="tool_use",
            content=[
                SimpleNamespace(type="tool_use", id="t1", name="scale_deployment",
                                input={"namespace": "default", "name": "nginx", "replicas": 5}),
            ],
        )
        final_response = SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="Scaled.")],
        )
        client = self._make_stream_context([tool_use_response, final_response])

        mock_tool = MagicMock()
        mock_tool.call.return_value = "Scaled default/nginx to 5 replicas."

        result = run_agent_streaming(
            client=client,
            messages=[{"role": "user", "content": "scale nginx to 5"}],
            system_prompt="test",
            tool_defs=[],
            tool_map={"scale_deployment": mock_tool},
            write_tools={"scale_deployment"},
            on_confirm=lambda name, inp: True,  # User says yes
        )

        mock_tool.call.assert_called_once()

    def test_read_tool_no_confirm_needed(self):
        """Read tools should execute without confirmation."""
        tool_use_response = SimpleNamespace(
            stop_reason="tool_use",
            content=[
                SimpleNamespace(type="tool_use", id="t1", name="list_pods", input={"namespace": "default"}),
            ],
        )
        final_response = SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="Here are the pods.")],
        )
        client = self._make_stream_context([tool_use_response, final_response])

        mock_tool = MagicMock()
        mock_tool.call.return_value = "default/web-1  Running"

        confirm_mock = MagicMock()

        run_agent_streaming(
            client=client,
            messages=[{"role": "user", "content": "list pods"}],
            system_prompt="test",
            tool_defs=[],
            tool_map={"list_pods": mock_tool},
            write_tools={"delete_pod"},  # list_pods not in write_tools
            on_confirm=confirm_mock,
        )

        mock_tool.call.assert_called_once()
        confirm_mock.assert_not_called()


class TestIterationGuard:
    def test_max_iterations_stops_loop(self):
        """Agent should stop after MAX_ITERATIONS even if model keeps calling tools."""
        # Create a response that always asks for another tool
        tool_response = SimpleNamespace(
            stop_reason="tool_use",
            content=[
                SimpleNamespace(type="tool_use", id="t1", name="list_pods", input={}),
            ],
        )

        client = MagicMock()
        stream = MagicMock()
        stream.__enter__ = MagicMock(return_value=stream)
        stream.__exit__ = MagicMock(return_value=False)
        stream.__iter__ = MagicMock(return_value=iter([]))
        stream.get_final_message.return_value = tool_response
        client.messages.stream = MagicMock(return_value=stream)

        mock_tool = MagicMock()
        mock_tool.call.return_value = "pods"

        run_agent_streaming(
            client=client,
            messages=[{"role": "user", "content": "loop forever"}],
            system_prompt="test",
            tool_defs=[],
            tool_map={"list_pods": mock_tool},
        )

        # Should have been called exactly MAX_ITERATIONS times
        assert mock_tool.call.call_count == MAX_ITERATIONS


class TestWriteToolSet:
    def test_all_write_tools_accounted_for(self):
        expected = {"scale_deployment", "restart_deployment", "cordon_node", "uncordon_node",
                    "delete_pod", "apply_yaml", "create_network_policy",
                    "rollback_deployment", "drain_node", "propose_git_change",
                    "install_gitops_operator", "create_argo_application"}
        assert WRITE_TOOLS == expected

    def test_read_tools_not_in_write_set(self):
        read_tools = {"list_pods", "list_nodes", "get_events", "describe_pod", "list_namespaces"}
        assert WRITE_TOOLS & read_tools == set()
