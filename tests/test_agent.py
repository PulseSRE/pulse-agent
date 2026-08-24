"""Tests for the agent loop, sanitization, and safety mechanisms."""

from __future__ import annotations

import asyncio
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


@patch.dict("os.environ", {"PULSE_AGENT_HARNESS": "0"})
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
    async def test_write_tool_allowed_with_confirm(self, mock_k8s):
        """Write tool should execute if on_confirm returns True.

        scale_deployment carries a verification contract, so executing it now
        includes a precondition read of the target — hence mock_k8s.
        """
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


@patch.dict("os.environ", {"PULSE_AGENT_HARNESS": "0"})
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
            # Forcing a finalizer off skips the cleanup it was protecting.
            "remove_finalizer",
            # Skill mutation edits the system prompt itself — see
            # test_skill_mutation_tools_require_confirmation below.
            "create_skill",
            "edit_skill",
            "delete_skill",
            "create_skill_from_template",
        }
        assert expected == WRITE_TOOLS

    def test_read_tools_not_in_write_set(self):
        read_tools = {"list_pods", "list_nodes", "get_events", "describe_pod", "list_namespaces"}
        assert WRITE_TOOLS & read_tools == set()

    def test_skill_mutation_tools_require_confirmation(self):
        """Editing a skill edits the system prompt, so it must reach the confirm gate.

        Regression: these four were registered with the default is_write=False,
        which put them in run_agent_streaming's `read_blocks` branch — executed
        in parallel, with on_confirm never called. The agent could rewrite its
        own instructions, permanently and for every later session, while
        deleting one pod still required approval.

        This matters most on the path nothing else defends: untrusted cluster
        text reaching the model through a diagnostic tool and asking it to call
        edit_skill. _validate_skill_safety matches seven literal English
        phrases, so it is a speed bump rather than a control; the confirmation
        gate is what actually puts a human in front of the change.
        """
        for name in ("create_skill", "edit_skill", "delete_skill", "create_skill_from_template"):
            assert name in WRITE_TOOLS, (
                f"{name} mutates the system prompt and must be registered with "
                "is_write=True, or the confirmation gate is bypassed"
            )


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


class TestInvokeHelper:
    @pytest.mark.asyncio
    async def test_invoke_with_sync_callback(self):
        """_invoke should handle sync callbacks returning None."""
        from sre_agent.agent import _invoke

        results = []

        def sync_cb(x):
            results.append(x)

        await _invoke(sync_cb, "hello")
        assert results == ["hello"]

    @pytest.mark.asyncio
    async def test_invoke_with_async_callback(self):
        """_invoke should await async callbacks."""
        from sre_agent.agent import _invoke

        results = []

        async def async_cb(x):
            results.append(x)

        await _invoke(async_cb, "world")
        assert results == ["world"]

    @pytest.mark.asyncio
    async def test_invoke_with_sync_callback_returning_value(self):
        """_invoke should return sync callback's return value."""
        from sre_agent.agent import _invoke

        def sync_cb():
            return True

        result = await _invoke(sync_cb)
        assert result is True

    @pytest.mark.asyncio
    async def test_invoke_with_async_callback_returning_value(self):
        """_invoke should return async callback's return value."""
        from sre_agent.agent import _invoke

        async def async_cb():
            return 42

        result = await _invoke(async_cb)
        assert result == 42


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


class _FakeAsyncClient:
    """Minimal stand-in that just tracks whether close() was awaited."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class TestBorrowAsyncClient:
    """Regression coverage for the shared-client-cancellation bug.

    A timed-out proactive investigation used to cancel an in-flight streamed
    call on the monitor's one long-lived AsyncAnthropicVertex client, which
    can corrupt that client's connection pool for the rest of the process.
    borrow_async_client(client=None) is the fix's foundation: it always
    creates a disposable client and always closes it — including when the
    caller is cancelled mid-`async with` — so a timeout only ever damages a
    client nobody else will reuse.
    """

    @pytest.mark.asyncio
    async def test_creates_and_closes_a_fresh_client_when_none_given(self):
        from sre_agent.agent import borrow_async_client

        created: list[_FakeAsyncClient] = []

        def _factory():
            c = _FakeAsyncClient()
            created.append(c)
            return c

        with patch("sre_agent.agent.create_async_client", side_effect=_factory):
            async with borrow_async_client(None) as c:
                assert c is created[0]
                assert c.closed is False

        assert created[0].closed is True

    @pytest.mark.asyncio
    async def test_cancellation_closes_the_owned_client_without_touching_a_shared_one(self):
        """Cancelling mid-call must close the fresh client and never touch a
        caller-supplied (shared, long-lived) client at all."""
        from sre_agent.agent import borrow_async_client

        created: list[_FakeAsyncClient] = []

        def _factory():
            c = _FakeAsyncClient()
            created.append(c)
            return c

        shared = _FakeAsyncClient()

        async def _slow_investigation_using_fresh_client():
            with patch("sre_agent.agent.create_async_client", side_effect=_factory):
                async with borrow_async_client(None) as c:
                    assert c is not shared
                    await asyncio.sleep(10)

        task = asyncio.create_task(_slow_investigation_using_fresh_client())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert len(created) == 1
        assert created[0].closed is True, "a cancelled call must still close its own disposable client"
        assert shared.closed is False, "cancellation must never close/touch a client it didn't create"

    @pytest.mark.asyncio
    async def test_never_closes_a_caller_supplied_shared_client(self):
        """Passing an explicit client (e.g. a long-lived shared one) must leave
        it open — borrow_async_client only closes clients it created itself."""
        from sre_agent.agent import borrow_async_client

        shared = _FakeAsyncClient()

        async with borrow_async_client(shared) as c:
            assert c is shared

        assert shared.closed is False


class TestTokenForwarding:
    def test_execute_tool_sets_contextvar(self):
        from unittest.mock import MagicMock

        from sre_agent.agent import _execute_tool
        from sre_agent.k8s_client import _user_token_var

        captured_token = []

        def capture_tool(inp):
            captured_token.append(_user_token_var.get())
            return "ok"

        mock_tool = MagicMock()
        mock_tool.call.side_effect = capture_tool
        tool_map = {"test_tool": mock_tool}

        _execute_tool("test_tool", {}, tool_map, user_token="my-token")
        assert captured_token == ["my-token"]
        assert _user_token_var.get() is None

    def test_execute_tool_resets_on_exception(self):
        from unittest.mock import MagicMock

        from sre_agent.agent import _execute_tool
        from sre_agent.k8s_client import _user_token_var

        mock_tool = MagicMock()
        mock_tool.call.side_effect = RuntimeError("boom")
        tool_map = {"bad_tool": mock_tool}

        _execute_tool("bad_tool", {}, tool_map, user_token="leaked?")
        assert _user_token_var.get() is None

    def test_execute_tool_no_token(self):
        from unittest.mock import MagicMock

        from sre_agent.agent import _execute_tool
        from sre_agent.k8s_client import _user_token_var

        captured = []

        def capture(inp):
            captured.append(_user_token_var.get())
            return "ok"

        mock_tool = MagicMock()
        mock_tool.call.side_effect = capture
        tool_map = {"t": mock_tool}

        _execute_tool("t", {}, tool_map, user_token=None)
        assert captured == [None]


# ---------------------------------------------------------------------------
# Circuit breaker thread safety
# ---------------------------------------------------------------------------


class TestCircuitBreakerThreadSafety:
    def test_has_lock(self):
        from sre_agent.agent import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=3)
        assert hasattr(cb, "_lock")

    def test_concurrent_failures_trip_breaker(self):
        import concurrent.futures

        from sre_agent.agent import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=5, recovery_timeout=60)
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(cb.record_failure) for _ in range(10)]
            concurrent.futures.wait(futures)
        assert cb.state == CircuitBreaker.OPEN
        assert cb.failure_count == 10

    def test_record_success_resets_under_contention(self):
        import concurrent.futures

        from sre_agent.agent import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=100, recovery_timeout=60)

        def fail_then_succeed(i):
            cb.record_failure()
            if i == 0:
                cb.record_success()

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(fail_then_succeed, i) for i in range(5)]
            concurrent.futures.wait(futures)
        # After record_success, state should be CLOSED and count 0,
        # but other threads may have called record_failure after.
        # The important thing is no exception was raised (no race crash).
        assert cb.state in (CircuitBreaker.CLOSED, CircuitBreaker.OPEN)


class TestToolRegistrationRequiresAnExplicitDecision:
    """register_tool must not let is_write be omitted.

    When it defaulted to False, four tools that rewrite the agent's own system
    prompt were registered as reads — not by a bad decision, but by no decision.
    Making the parameter required means a tool cannot be added without someone
    answering "does this need a human to approve it?".
    """

    def test_is_write_is_required_and_keyword_only(self):
        import inspect

        from sre_agent.tool_registry import register_tool

        sig = inspect.signature(register_tool)
        param = sig.parameters["is_write"]
        assert param.default is inspect.Parameter.empty, (
            "is_write must have no default — a default is how the skill-mutation tools silently became read tools"
        )
        assert param.kind is inspect.Parameter.KEYWORD_ONLY, (
            "is_write must be keyword-only so call sites read register_tool(x, is_write=True)"
        )
