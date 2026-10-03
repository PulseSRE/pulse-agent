"""A pooled async client must stay on one loop through replay and judging (#56)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from sre_agent.evals import replay, replay_cli


class LoopBoundClient:
    """Model the loop affinity of an async HTTP client's pooled connections."""

    def __init__(self):
        self.loop = None
        self.agent_calls = 0
        self.judge_calls = 0
        self.closed = False
        self.messages = SimpleNamespace(create=self.create)

    def check_loop(self):
        loop = asyncio.get_running_loop()
        if self.loop is None:
            self.loop = loop
        assert not self.loop.is_closed(), "Event loop is closed"
        assert loop is self.loop, "Client reused across event loops"

    async def create(self, **kwargs):
        self.check_loop()
        self.judge_calls += 1
        scores = {"correctness": 30, "completeness": 30, "actionability": 20, "safety": 20, "total": 100}
        return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(scores))])

    async def close(self):
        self.check_loop()
        self.closed = True


@pytest.fixture
def loop_client(monkeypatch):
    client = LoopBoundClient()

    async def agent(*, client, **kwargs):
        client.check_loop()
        client.agent_calls += 1
        return "Diagnosis complete"

    monkeypatch.setattr(replay, "run_agent_streaming", agent)
    monkeypatch.setattr(replay_cli, "_setup_model", lambda *args: (client, None))
    return client


def fixture_data(multi_turn):
    turn = {"prompt": "Diagnose this incident", "recorded_responses": {}}
    if multi_turn:
        return {"multi_turn": True, "turns": [turn, dict(turn)], "expected": {}}
    return {**turn, "expected": {}}


@pytest.mark.parametrize("multi_turn", [False, True])
def test_agent_turns_judge_samples_and_cleanup_share_loop(monkeypatch, loop_client, multi_turn):
    monkeypatch.setattr(replay_cli, "load_fixture", lambda name: fixture_data(multi_turn))
    result = replay_cli._run_fixture("loop", use_judge=True, judge_min=60, judge_samples=3, stub_config=True)
    assert result["score"]["passed"]
    assert loop_client.agent_calls == (2 if multi_turn else 1)
    assert loop_client.judge_calls == 3
    assert loop_client.closed
    assert loop_client.loop.is_closed()


@pytest.mark.parametrize("multi_turn", [False, True])
def test_client_closed_on_agent_failure(monkeypatch, loop_client, multi_turn):
    async def fail(*, client, **kwargs):
        client.check_loop()
        raise RuntimeError("agent failed")

    monkeypatch.setattr(replay, "run_agent_streaming", fail)
    monkeypatch.setattr(replay_cli, "load_fixture", lambda name: fixture_data(multi_turn))
    with pytest.raises(RuntimeError, match="agent failed"):
        replay_cli._run_fixture("loop", stub_config=True)
    assert loop_client.closed
    assert loop_client.loop.is_closed()


def test_sync_multi_turn_harness_keeps_one_loop(loop_client):
    harness = replay.MultiTurnReplayHarness(fixture_data(True)["turns"], stub_config=True)
    result = harness.run(loop_client)
    assert len(result["turns"]) == 2
    assert loop_client.agent_calls == 2
    # The harness borrows the caller's client; only the CLI owns its cleanup.
    assert not loop_client.closed


def test_correlation_timeline_does_not_require_duplicate_event_fetch():
    fixture = replay.load_fixture("integration_incident_correlation")
    result = {
        "response": "The root cause was a ConfigMap configuration change at 14:02.",
        "tool_calls": [{"name": "correlate_incident"}],
    }
    assert replay.score_replay(result, fixture["expected"])["passed"]
    result["tool_calls"].append({"name": "delete_pod"})
    assert not replay.score_replay(result, fixture["expected"])["passed"]
