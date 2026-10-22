"""Chat loop failures that used to break a session or mislead the person.

Each test pins one defect found in review:
- API errors surfaced when the stream was entered, outside the retry ``try``,
  so 429/529 retries and the circuit breaker never ran;
- a failed turn rolled back only its last message, leaving invalid history
  that failed every later request in the session;
- the history cap could cut between a tool_use and its tool_result;
- a max_tokens response was appended with a half-written tool_use;
- a turn that finished in its last iteration was labelled as cut off;
- a timed-out write was reported as if it had not happened;
- the model got the head of a pod's log tail, not the end where the crash is.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import pytest

import sre_agent.agent as agent_mod
from sre_agent.agent import CircuitBreaker, run_agent_streaming
from sre_agent.api.ws_endpoints import _cap_messages, _rollback_turn
from sre_agent.k8s_tools.pods import _log_tail_for_model

# Stand-ins rather than real HTTP objects: the SDK's HTTP library differs
# between versions (httpx, httpx2), and the errors only read these fields.
_REQ = SimpleNamespace(method="POST", url="https://api.anthropic.com/v1/messages")


def _status_error(code: int, message: str = "error") -> anthropic.APIStatusError:
    response = SimpleNamespace(status_code=code, headers={}, request=_REQ)
    return anthropic.APIStatusError(message, response=response, body=None)


def _text_event(text: str):
    return SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=text))


class _Stream:
    """A streamed response: optional events, then a final message or an error.

    ``fail_on_enter`` raises where the real SDK sends the request; ``fail_after``
    raises after the events have been delivered, i.e. once text reached the user.
    """

    def __init__(self, final=None, events=(), fail_on_enter=None, fail_after=None):
        self.final, self.events = final, list(events)
        self.fail_on_enter, self.fail_after = fail_on_enter, fail_after

    async def __aenter__(self):
        if self.fail_on_enter:
            raise self.fail_on_enter
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for e in self.events:
            yield e
        if self.fail_after:
            raise self.fail_after

    async def get_final_message(self):
        return self.final


def _done(text: str = "All good.", stop: str = "end_turn"):
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)], usage=None)


def _client(*streams):
    client = MagicMock()
    client.messages.stream = MagicMock(side_effect=list(streams))
    return client


@pytest.fixture
def breaker():
    fresh = CircuitBreaker(failure_threshold=2, recovery_timeout=60)
    with patch.object(agent_mod, "_circuit_breaker", fresh), patch.object(agent_mod.asyncio, "sleep", AsyncMock()):
        yield fresh


async def _run(client, messages=None, **kw):
    msgs = messages if messages is not None else [{"role": "user", "content": "how are my pods?"}]
    return await run_agent_streaming(
        client=client,
        messages=msgs,
        system_prompt="sys",
        tool_defs=[],
        tool_map={},
        honor_plan_requests=False,
        **kw,
    )


@patch.dict("os.environ", {"PULSE_AGENT_HARNESS": "0"})
class TestApiErrorsAreRetried:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "err", [_status_error(429), _status_error(529), anthropic.APIConnectionError(request=_REQ)]
    )
    async def test_error_when_the_request_is_sent_is_retried(self, breaker, err):
        client = _client(_Stream(fail_on_enter=err), _Stream(final=_done(), events=[_text_event("All good.")]))
        assert await _run(client) == "All good."
        assert client.messages.stream.call_count == 2
        assert breaker.failure_count == 0

    @pytest.mark.asyncio
    async def test_persistent_overload_opens_the_breaker(self, breaker):
        client = _client(*[_Stream(fail_on_enter=_status_error(529)) for _ in range(4)])
        with pytest.raises(anthropic.APIStatusError):
            await _run(client)
        assert client.messages.stream.call_count == 4  # first try + 3 retries
        assert breaker.failure_count == 1
        # The second persistent outage trips it (threshold 2).
        client = _client(*[_Stream(fail_on_enter=_status_error(529)) for _ in range(4)])
        assert "Silent Mode" in await _run(client)
        assert breaker.is_open

    @pytest.mark.asyncio
    async def test_failure_after_text_reached_the_user_is_not_retried(self, breaker):
        """A retry would stream the same opening twice."""
        client = _client(
            _Stream(events=[_text_event("Looking at ")], fail_after=_status_error(529)),
            _Stream(final=_done()),
        )
        with pytest.raises(anthropic.APIStatusError):
            await _run(client)
        assert client.messages.stream.call_count == 1
        assert breaker.failure_count == 1

    @pytest.mark.asyncio
    async def test_bad_request_is_raised_without_counting_as_an_outage(self, breaker):
        client = _client(_Stream(fail_on_enter=_status_error(400, "messages: invalid")))
        with pytest.raises(anthropic.APIStatusError):
            await _run(client)
        assert breaker.failure_count == 0


@patch.dict("os.environ", {"PULSE_AGENT_HARNESS": "0"})
class TestHowATurnEnds:
    @pytest.mark.asyncio
    async def test_max_tokens_says_the_answer_was_cut_off_and_keeps_history_valid(self, breaker):
        truncated = SimpleNamespace(
            stop_reason="max_tokens",
            content=[
                SimpleNamespace(type="text", text="The pod is"),
                SimpleNamespace(type="tool_use", id="t1", name="describe_pod", input={}),
            ],
            usage=None,
        )
        messages = [{"role": "user", "content": "why is it crashing?"}]
        out = await _run(_client(_Stream(final=truncated, events=[_text_event("The pod is")])), messages)
        assert "cut off at the output length limit" in out
        # The half-written tool_use was not added: it has no result, and the
        # next request in the session would be rejected.
        assert messages == [{"role": "user", "content": "why is it crashing?"}]

    @pytest.mark.asyncio
    async def test_finishing_in_the_last_iteration_is_not_reported_as_cut_off(self, breaker):
        with patch.object(agent_mod, "MAX_ITERATIONS", 1):
            out = await _run(_client(_Stream(final=_done(), events=[_text_event("All good.")])))
        assert out == "All good."


class TestTimedOutWrite:
    def test_write_timeout_says_the_outcome_is_unknown(self):
        def slow(*_args, **_kwargs):
            import time

            time.sleep(2)
            return "scaled"

        tool = MagicMock()
        tool.call.side_effect = slow
        # A write with no verification contract, so no precondition read runs.
        with patch.object(agent_mod, "get_write_tools", lambda: {"restart_widget"}):
            text, _, meta = agent_mod._execute_tool_with_timeout(
                "restart_widget", {"name": "web"}, {"restart_widget": tool}, timeout=1
            )
        assert meta["status"] == "error"
        assert "still running" in text and "outcome is unknown" in text
        assert "do not repeat" in text

    def test_read_timeout_is_still_a_plain_timeout(self):
        def slow(*_args, **_kwargs):
            import time

            time.sleep(2)
            return "pods"

        tool = MagicMock()
        tool.call.side_effect = slow
        text, _, _ = agent_mod._execute_tool_with_timeout("list_pods", {}, {"list_pods": tool}, timeout=1)
        assert text == "Error: list_pods timed out after 1s"


def _tool_turn(i: int) -> list[dict]:
    return [
        {"role": "user", "content": f"question {i}"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "list_pods", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "ok"}]},
        {"role": "assistant", "content": f"answer {i}"},
    ]


class TestSessionHistory:
    def test_cap_never_starts_on_a_tool_result_or_an_assistant_message(self):
        history = [m for i in range(5) for m in _tool_turn(i)]  # 20 messages
        for cap in range(1, 20):
            capped = list(history)
            _cap_messages(capped, cap)
            assert len(capped) <= cap
            if capped:
                first = capped[0]
                assert first["role"] == "user" and isinstance(first["content"], str), (cap, first)

    def test_cap_leaves_short_history_alone(self):
        history = _tool_turn(0)
        _cap_messages(history, 50)
        assert history == _tool_turn(0)

    def test_failed_turn_is_rolled_back_whole(self):
        messages = _tool_turn(0)
        turn_start = len(messages)
        # The failed turn got as far as a tool call and its result.
        messages += _tool_turn(1)[:3]
        _rollback_turn(messages, turn_start)
        assert messages == _tool_turn(0)


class TestPodLogsForTheModel:
    def test_short_log_is_unchanged(self):
        assert _log_tail_for_model("a\nb\n") == "a\nb\n"

    def test_long_log_keeps_the_end_where_the_crash_is(self):
        log = "\n".join(f"startup line {i}" for i in range(500)) + "\nFATAL: out of memory\n"
        out = _log_tail_for_model(log)
        assert out.rstrip().endswith("FATAL: out of memory")
        assert "startup line 0\n" not in out
        assert out.startswith("[... ") and "earlier characters omitted" in out
        body = out.split("\n", 1)[1]
        assert body.startswith("startup line "), "should start on a whole line"
        assert len(body) <= 2000
