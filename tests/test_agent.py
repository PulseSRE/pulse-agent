"""Tests for the agent loop, sanitization, and safety mechanisms."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import pytest

from sre_agent.agent import (
    MAX_ITERATIONS,
    WRITE_TOOLS,
    _execute_tool,
    _sanitize_content,
    run_agent_streaming,
)


class _MockAsyncStream:
    """Mock async stream supporting both async context manager and async iteration."""

    def __init__(self, final_message):
        self._final_message = final_message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def get_final_message(self):
        return self._final_message


class TestSanitizeContent:
    def test_text_block(self):
        blocks = [SimpleNamespace(type="text", text="hello")]
        result = _sanitize_content(blocks)
        assert result == [{"type": "text", "text": "hello"}]

    def test_tool_use_strips_caller(self):
        blocks = [
            SimpleNamespace(
                type="tool_use", id="t1", name="list_pods", input={"namespace": "default"}, caller="some_caller"
            )
        ]
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
        text, component, meta = _execute_tool("my_tool", {"arg": "val"}, tool_map)
        assert text == "result data"
        assert component is None
        assert meta["status"] == "success"
        assert meta["result_bytes"] == len("result data")
        tool.call.assert_called_once_with({"arg": "val"})

    def test_success_with_component(self):
        tool = MagicMock()
        tool.call.return_value = ("result data", {"kind": "data_table"})
        tool_map = {"my_tool": tool}
        text, component, meta = _execute_tool("my_tool", {}, tool_map)
        assert text == "result data"
        assert component == {"kind": "data_table"}
        assert meta["status"] == "success"
        assert meta["result_bytes"] == len("result data")

    def test_unknown_tool(self):
        text, component, meta = _execute_tool("nonexistent", {}, {})
        assert "unknown tool" in text
        assert component is None
        assert meta["status"] == "error"
        assert meta["error_category"] == "not_found"
        assert meta["result_bytes"] == 0

    def test_exception_returns_type_only(self):
        tool = MagicMock()
        tool.call.side_effect = ValueError("secret details here")
        tool_map = {"bad_tool": tool}
        text, component, meta = _execute_tool("bad_tool", {}, tool_map)
        assert "ValueError" in text
        assert "secret details" not in text
        assert component is None
        assert meta["status"] == "error"
        assert "ValueError" in meta["error_message"]
        assert meta["result_bytes"] == 0


class TestConfirmationGate:
    def _make_stream_context(self, responses):
        """Build a mock client that returns responses in sequence."""
        client = MagicMock()
        streams = [_MockAsyncStream(resp) for resp in responses]
        client.messages.stream = MagicMock(side_effect=streams)
        return client

    @pytest.mark.asyncio
    async def test_write_tool_blocked_without_confirm(self):
        """Write tool should be blocked if on_confirm returns False."""
        tool_use_response = SimpleNamespace(
            stop_reason="tool_use",
            content=[
                SimpleNamespace(
                    type="tool_use", id="t1", name="delete_pod", input={"namespace": "default", "pod_name": "victim"}
                ),
            ],
        )
        final_response = SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="Cancelled.")],
        )
        client = self._make_stream_context([tool_use_response, final_response])

        mock_tool = MagicMock()
        mock_tool.call.return_value = "deleted"

        await run_agent_streaming(
            client=client,
            messages=[{"role": "user", "content": "delete pod"}],
            system_prompt="test",
            tool_defs=[],
            tool_map={"delete_pod": mock_tool},
            write_tools={"delete_pod"},
            on_confirm=AsyncMock(return_value=False),
        )

        # Tool should NOT have been called
        mock_tool.call.assert_not_called()

    @pytest.mark.asyncio
    async def test_write_tool_allowed_with_confirm(self):
        """Write tool should execute if on_confirm returns True."""
        tool_use_response = SimpleNamespace(
            stop_reason="tool_use",
            content=[
                SimpleNamespace(
                    type="tool_use",
                    id="t1",
                    name="scale_deployment",
                    input={"namespace": "default", "name": "nginx", "replicas": 5},
                ),
            ],
        )
        final_response = SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="Scaled.")],
        )
        client = self._make_stream_context([tool_use_response, final_response])

        mock_tool = MagicMock()
        mock_tool.call.return_value = "Scaled default/nginx to 5 replicas."

        await run_agent_streaming(
            client=client,
            messages=[{"role": "user", "content": "scale nginx to 5"}],
            system_prompt="test",
            tool_defs=[],
            tool_map={"scale_deployment": mock_tool},
            write_tools={"scale_deployment"},
            on_confirm=AsyncMock(return_value=True),
        )

        mock_tool.call.assert_called_once()

    @pytest.mark.asyncio
    async def test_read_tool_no_confirm_needed(self):
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

        confirm_mock = AsyncMock()

        await run_agent_streaming(
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
    @pytest.mark.asyncio
    async def test_max_iterations_stops_loop(self):
        """Agent should stop after MAX_ITERATIONS even if model keeps calling tools."""
        # Create a response that always asks for another tool
        tool_response = SimpleNamespace(
            stop_reason="tool_use",
            content=[
                SimpleNamespace(type="tool_use", id="t1", name="list_pods", input={}),
            ],
        )

        client = MagicMock()
        stream = _MockAsyncStream(tool_response)
        client.messages.stream = MagicMock(return_value=stream)

        mock_tool = MagicMock()
        mock_tool.call.return_value = "pods"

        await run_agent_streaming(
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
        expected = {
            "scale_deployment",
            "restart_deployment",
            "cordon_node",
            "uncordon_node",
            "delete_pod",
            "apply_yaml",
            "create_network_policy",
            "rollback_deployment",
            "drain_node",
            "propose_git_change",
            "install_gitops_operator",
            "create_argo_application",
            "exec_command",
            "test_connectivity",
        }
        assert expected == WRITE_TOOLS

    def test_read_tools_not_in_write_set(self):
        read_tools = {"list_pods", "list_nodes", "get_events", "describe_pod", "list_namespaces"}
        assert WRITE_TOOLS & read_tools == set()


@patch.dict("os.environ", {"PULSE_AGENT_HARNESS": "0"})
class TestOnToolResult:
    def _make_stream_context(self, responses):
        client = MagicMock()
        streams = [_MockAsyncStream(resp) for resp in responses]
        client.messages.stream = MagicMock(side_effect=streams)
        return client

    @pytest.mark.asyncio
    async def test_on_tool_result_called_for_read_tool(self):
        tool_use_response = SimpleNamespace(
            stop_reason="tool_use",
            content=[SimpleNamespace(type="tool_use", id="t1", name="list_pods", input={"namespace": "default"})],
        )
        final_response = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="Done.")])
        client = self._make_stream_context([tool_use_response, final_response])
        mock_tool = MagicMock()
        mock_tool.call.return_value = "pod-1 Running"
        results = []
        await run_agent_streaming(
            client=client,
            messages=[{"role": "user", "content": "list pods"}],
            system_prompt="test",
            tool_defs=[],
            tool_map={"list_pods": mock_tool},
            on_tool_result=AsyncMock(side_effect=lambda info: results.append(info)),
        )
        assert len(results) == 1
        r = results[0]
        assert r["tool_name"] == "list_pods"
        assert r["input"] == {"namespace": "default"}
        assert r["status"] == "success"
        assert r["error_message"] is None
        assert r["duration_ms"] >= 0
        assert r["result_bytes"] > 0
        assert r["was_confirmed"] is None

    @pytest.mark.asyncio
    async def test_on_tool_result_called_for_write_tool_confirmed(self):
        tool_use_response = SimpleNamespace(
            stop_reason="tool_use",
            content=[SimpleNamespace(type="tool_use", id="t1", name="delete_pod", input={"pod_name": "x"})],
        )
        final_response = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="Done.")])
        client = self._make_stream_context([tool_use_response, final_response])
        mock_tool = MagicMock()
        mock_tool.call.return_value = "deleted"
        results = []
        await run_agent_streaming(
            client=client,
            messages=[{"role": "user", "content": "delete pod"}],
            system_prompt="test",
            tool_defs=[],
            tool_map={"delete_pod": mock_tool},
            write_tools={"delete_pod"},
            on_confirm=AsyncMock(return_value=True),
            on_tool_result=AsyncMock(side_effect=lambda info: results.append(info)),
        )
        assert len(results) == 1
        assert results[0]["was_confirmed"] is True
        assert results[0]["status"] == "success"

    @pytest.mark.asyncio
    async def test_on_tool_result_called_for_write_tool_denied(self):
        tool_use_response = SimpleNamespace(
            stop_reason="tool_use",
            content=[SimpleNamespace(type="tool_use", id="t1", name="delete_pod", input={"pod_name": "x"})],
        )
        final_response = SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text="Cancelled.")]
        )
        client = self._make_stream_context([tool_use_response, final_response])
        mock_tool = MagicMock()
        results = []
        await run_agent_streaming(
            client=client,
            messages=[{"role": "user", "content": "delete pod"}],
            system_prompt="test",
            tool_defs=[],
            tool_map={"delete_pod": mock_tool},
            write_tools={"delete_pod"},
            on_confirm=AsyncMock(return_value=False),
            on_tool_result=AsyncMock(side_effect=lambda info: results.append(info)),
        )
        assert len(results) == 1
        assert results[0]["was_confirmed"] is False
        assert results[0]["status"] == "denied"

    @pytest.mark.asyncio
    async def test_on_tool_result_captures_error(self):
        tool_use_response = SimpleNamespace(
            stop_reason="tool_use",
            content=[SimpleNamespace(type="tool_use", id="t1", name="bad_tool", input={})],
        )
        final_response = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="Error.")])
        client = self._make_stream_context([tool_use_response, final_response])
        mock_tool = MagicMock()
        mock_tool.call.side_effect = RuntimeError("k8s unreachable")
        results = []
        await run_agent_streaming(
            client=client,
            messages=[{"role": "user", "content": "do thing"}],
            system_prompt="test",
            tool_defs=[],
            tool_map={"bad_tool": mock_tool},
            on_tool_result=AsyncMock(side_effect=lambda info: results.append(info)),
        )
        assert len(results) == 1
        assert results[0]["status"] == "error"
        assert "RuntimeError" in results[0]["error_message"]

    @pytest.mark.asyncio
    async def test_on_tool_result_includes_iteration(self):
        tool_use_response = SimpleNamespace(
            stop_reason="tool_use",
            content=[SimpleNamespace(type="tool_use", id="t1", name="list_pods", input={})],
        )
        final_response = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="Done.")])
        client = self._make_stream_context([tool_use_response, final_response])
        mock_tool = MagicMock()
        mock_tool.call.return_value = "pods"
        results = []
        await run_agent_streaming(
            client=client,
            messages=[{"role": "user", "content": "list"}],
            system_prompt="test",
            tool_defs=[],
            tool_map={"list_pods": mock_tool},
            on_tool_result=AsyncMock(side_effect=lambda info: results.append(info)),
        )
        assert results[0]["turn_number"] == 1


class TestAsyncConfirmation:
    @pytest.mark.asyncio
    async def test_cancelled_future_returns_false(self):
        """CancelledError during confirmation await should return False (deny)."""
        import asyncio

        future = asyncio.get_running_loop().create_future()
        future.cancel()

        async def on_confirm(name, inp):
            try:
                return await asyncio.wait_for(future, timeout=5)
            except (asyncio.CancelledError, TimeoutError):
                return False

        result = await on_confirm("delete_pod", {"pod_name": "test"})
        assert result is False

    @pytest.mark.asyncio
    async def test_timeout_returns_false(self):
        """TimeoutError during confirmation await should return False (deny)."""
        import asyncio

        future = asyncio.get_running_loop().create_future()

        async def on_confirm(name, inp):
            try:
                return await asyncio.wait_for(future, timeout=0.01)
            except (asyncio.CancelledError, TimeoutError):
                return False

        result = await on_confirm("delete_pod", {"pod_name": "test"})
        assert result is False


class TestAsyncToolExecution:
    @pytest.mark.asyncio
    async def test_tool_timeout_via_asyncio_wait(self):
        """Tools exceeding timeout should be in the pending set."""
        import asyncio
        import time

        from sre_agent.agent import _tool_pool

        def slow_tool(name, input_data, tool_map):
            time.sleep(10)
            return "done", None, {"status": "success", "error_message": None, "error_category": None, "result_bytes": 4}

        loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(loop.run_in_executor(_tool_pool, slow_tool, "slow", {}, {}))
        _done, pending = await asyncio.wait({task}, timeout=0.05)

        assert len(pending) == 1
        for p in pending:
            p.cancel()


class TestCreateAsyncClient:
    @patch.dict(os.environ, {"ANTHROPIC_VERTEX_PROJECT_ID": "test-proj", "CLOUD_ML_REGION": "us-east5"})
    def test_returns_async_vertex_when_configured(self):
        from sre_agent.agent import create_async_client

        client = create_async_client()
        assert isinstance(client, anthropic.AsyncAnthropicVertex)

    @patch.dict(os.environ, {"ANTHROPIC_VERTEX_PROJECT_ID": "", "CLOUD_ML_REGION": ""})
    def test_returns_async_anthropic_when_no_vertex(self):
        from sre_agent.agent import create_async_client

        client = create_async_client()
        assert isinstance(client, anthropic.AsyncAnthropic)
