"""Replay failures retain scoped synthetic requests without leaking payloads."""

from unittest.mock import patch

import pytest

from sre_agent.evals.replay import MultiTurnReplayHarness, ReplayHarness, _tool_diagnostic


def test_diagnostic_preserves_defaults_and_redacts_sensitive_payloads():
    data = _tool_diagnostic(
        {
            "tool_name": "list_resources",
            "input": {
                "resource": "daemonsets",
                "group": "apps",
                "namespace": None,
                "show_wide": True,
                "sort_by": "name",
                "version": "v1",
                "query": 'memory{token="do-not-export"}',
                "authorization": "do-not-export",
                "yaml_content": "do-not-export",
                "title": "Bearer do-not-export password=do-not-export",
                "description": "token:'do-not-export'",
            },
            "status": "success",
            "turn_number": 2,
        }
    )
    assert data["input"]["namespace"] is None
    assert data["input"]["show_wide"] is True
    assert data["input"]["group"] == "apps"
    assert "do-not-export" not in str(data)
    assert data["iteration"] == 2


async def fake_agent(**kwargs):
    await kwargs["on_tool_use"]("list_resources")
    await kwargs["on_tool_result"](
        {
            "tool_name": "list_resources",
            "input": {"resource": "ds", "namespace": "ALL", "show_wide": True},
            "status": "denied",
            "error_category": "forbidden",
            "was_confirmed": False,
            "turn_number": 1,
        }
    )
    return "synthetic response"


@pytest.mark.asyncio
async def test_single_replay_keeps_attempt_inputs_including_denied_calls():
    with patch("sre_agent.evals.replay.run_agent_streaming", side_effect=fake_agent):
        result = await ReplayHarness({}, stub_config=True).run_async(None, "synthetic")
    attempt = result["tool_attempts"][0]
    assert attempt["input"] == {"resource": "ds", "namespace": "ALL", "show_wide": True}
    assert attempt["status"] == "denied"
    assert attempt["was_confirmed"] is False


@pytest.mark.asyncio
async def test_multiturn_replay_keeps_separate_attempt_inputs():
    with patch("sre_agent.evals.replay.run_agent_streaming", side_effect=fake_agent):
        result = await MultiTurnReplayHarness(
            [
                {"prompt": "first", "recorded_responses": {}},
                {"prompt": "second", "recorded_responses": {}},
            ],
            stub_config=True,
        ).run_async(None)
    assert [len(turn["tool_attempts"]) for turn in result["turns"]] == [1, 1]
    assert all(turn["tool_attempts"][0]["input"]["show_wide"] is True for turn in result["turns"])


@pytest.mark.asyncio
@pytest.mark.parametrize("multi", [False, True])
async def test_cli_artifacts_propagate_request_diagnostics(monkeypatch, multi):
    from unittest.mock import AsyncMock, MagicMock

    from sre_agent.evals import replay_cli

    client = MagicMock()
    client.close = AsyncMock()
    attempt = {"name": "list_resources", "input": {"resource": "ds", "show_wide": True}}
    result = {"response": "synthetic", "tool_calls": [], "duration_ms": 0, "tool_attempts": [attempt]}
    fixture = {"prompt": "synthetic", "recorded_responses": {}, "expected": {}}
    if multi:
        fixture.update(multi_turn=True, turns=[{"prompt": "synthetic", "recorded_responses": {}}])
        result = {"turns": [result], "total_duration_ms": 0, "modes": ["sre"]}
    monkeypatch.setattr(replay_cli, "load_fixture", lambda _: fixture)
    monkeypatch.setattr(replay_cli, "_setup_model", lambda *_: (client, None))
    target = (
        "sre_agent.evals.replay.MultiTurnReplayHarness.run_async"
        if multi
        else "sre_agent.evals.replay.ReplayHarness.run_async"
    )
    with patch(target, new=AsyncMock(return_value=result)):
        output = await replay_cli._run_fixture_async("synthetic")
    assert output["turn_tool_attempts" if multi else "tool_attempts"] == ([[attempt]] if multi else [attempt])
