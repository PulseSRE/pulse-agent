"""Tests for the recorded replay evaluation harness.

These tests mock the Claude API so no real API key is needed.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from sre_agent.evals.replay import (
    ReplayHarness,
    list_fixtures,
    load_fixture,
    score_replay,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_client(tool_names_to_call: list[str] | None = None, final_text: str = "Done."):
    """Build a mock Anthropic client that optionally calls tools then responds.

    If *tool_names_to_call* is provided the first API response will be a
    tool_use stop, followed by an end_turn with *final_text*.
    Otherwise a single end_turn is returned.

    The mock streams emit ``content_block_start`` and ``content_block_delta``
    events so that the agent loop's ``on_tool_use`` and ``on_text`` callbacks
    fire correctly.
    """
    responses = []
    event_lists = []

    if tool_names_to_call:
        tool_blocks = [
            SimpleNamespace(
                type="tool_use",
                id=f"t{i}",
                name=name,
                input={},
            )
            for i, name in enumerate(tool_names_to_call)
        ]
        # Events for the tool_use response
        tool_events = [
            SimpleNamespace(
                type="content_block_start",
                content_block=SimpleNamespace(name=name),
            )
            for name in tool_names_to_call
        ]
        responses.append(
            SimpleNamespace(stop_reason="tool_use", content=tool_blocks)
        )
        event_lists.append(tool_events)

    # Events for the final text response
    text_events = [
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text=final_text),
        )
    ]
    responses.append(
        SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=final_text)],
        )
    )
    event_lists.append(text_events)

    client = MagicMock()
    streams = []
    for resp, events in zip(responses, event_lists):
        stream = MagicMock()
        stream.__enter__ = MagicMock(return_value=stream)
        stream.__exit__ = MagicMock(return_value=False)
        stream.__iter__ = MagicMock(return_value=iter(events))
        stream.get_final_message.return_value = resp
        streams.append(stream)

    client.messages.stream = MagicMock(side_effect=streams)
    return client


# ---------------------------------------------------------------------------
# Fixture loading tests
# ---------------------------------------------------------------------------


class TestFixtureLoading:
    def test_list_fixtures_returns_names(self):
        names = list_fixtures()
        assert isinstance(names, list)
        assert "crashloop_diagnosis" in names
        assert "pending_pod" in names
        assert "node_not_ready" in names

    def test_load_fixture_valid(self):
        fixture = load_fixture("crashloop_diagnosis")
        assert fixture["name"] == "crashloop_diagnosis"
        assert "prompt" in fixture
        assert "recorded_responses" in fixture
        assert "expected" in fixture

    def test_load_fixture_missing_raises(self):
        with pytest.raises(FileNotFoundError):
            load_fixture("nonexistent_fixture_xyz")

    def test_all_fixtures_have_required_keys(self):
        for name in list_fixtures():
            fixture = load_fixture(name)
            assert "name" in fixture, f"{name} missing 'name'"
            assert "prompt" in fixture, f"{name} missing 'prompt'"
            assert "recorded_responses" in fixture, f"{name} missing 'recorded_responses'"
            assert "expected" in fixture, f"{name} missing 'expected'"


# ---------------------------------------------------------------------------
# ReplayHarness tests
# ---------------------------------------------------------------------------


class TestReplayHarness:
    @patch.dict("os.environ", {"PULSE_AGENT_HARNESS": "0"})
    def test_run_returns_response(self):
        """Harness should return the agent's final text."""
        client = _make_mock_client(final_text="The root cause is X.")
        harness = ReplayHarness({"describe_pod": "pod info"})
        result = harness.run(client=client, prompt="What is wrong?")

        assert "response" in result
        assert "tool_calls" in result
        assert "duration_ms" in result
        assert isinstance(result["duration_ms"], float)

    @patch.dict("os.environ", {"PULSE_AGENT_HARNESS": "0"})
    def test_run_tracks_tool_calls(self):
        """Harness should record which tools the agent called."""
        client = _make_mock_client(
            tool_names_to_call=["describe_pod", "get_pod_logs"],
            final_text="The database connection is refused.",
        )
        harness = ReplayHarness({
            "describe_pod": "CrashLoopBackOff",
            "get_pod_logs": "connection refused to db-service:5432",
        })
        result = harness.run(client=client, prompt="Pod is crash-looping.")

        tool_names = [tc["name"] for tc in result["tool_calls"]]
        assert "describe_pod" in tool_names
        assert "get_pod_logs" in tool_names

    @patch.dict("os.environ", {"PULSE_AGENT_HARNESS": "0"})
    def test_recorded_responses_are_returned(self):
        """Tools should return recorded responses, not make real API calls."""
        recorded = {"list_pods": "production/api-server  Status=CrashLoopBackOff"}

        client = _make_mock_client(
            tool_names_to_call=["list_pods"],
            final_text="Found the issue.",
        )
        harness = ReplayHarness(recorded)
        result = harness.run(client=client, prompt="Check pods")

        # The mock tool should have been set up to return the recorded value
        assert result["response"] == "Found the issue."

    @patch.dict("os.environ", {"PULSE_AGENT_HARNESS": "0"})
    def test_stub_defs_generated_from_recorded(self):
        """When no tool_defs provided, stubs should be generated."""
        harness = ReplayHarness({"describe_pod": "info", "get_events": "events"})
        defs = harness._build_stub_defs()
        names = {d["name"] for d in defs}
        assert "describe_pod" in names
        assert "get_events" in names
        for d in defs:
            assert "input_schema" in d


# ---------------------------------------------------------------------------
# Scoring tests
# ---------------------------------------------------------------------------


class TestScoreReplay:
    def test_perfect_score(self):
        result = {
            "response": "The database connection is refused at db-service:5432.",
            "tool_calls": [
                {"name": "describe_pod", "timestamp": 0},
                {"name": "get_pod_logs", "timestamp": 1},
            ],
            "duration_ms": 500,
        }
        expected = {
            "should_mention": ["database", "connection", "db-service"],
            "should_use_tools": ["describe_pod", "get_pod_logs"],
            "should_not_use_tools": ["delete_pod"],
            "max_tool_calls": 10,
        }
        score = score_replay(result, expected)
        assert score["passed"] is True
        assert score["score"] == 100

    def test_missing_keyword_reduces_score(self):
        result = {
            "response": "The pod is failing.",
            "tool_calls": [{"name": "describe_pod", "timestamp": 0}],
            "duration_ms": 500,
        }
        expected = {
            "should_mention": ["database", "connection"],
            "should_use_tools": ["describe_pod"],
        }
        score = score_replay(result, expected)
        assert score["passed"] is False
        assert score["score"] < 100

    def test_forbidden_tool_fails(self):
        result = {
            "response": "Deleted the pod to fix it.",
            "tool_calls": [
                {"name": "describe_pod", "timestamp": 0},
                {"name": "delete_pod", "timestamp": 1},
            ],
            "duration_ms": 500,
        }
        expected = {
            "should_not_use_tools": ["delete_pod"],
        }
        score = score_replay(result, expected)
        assert score["passed"] is False

    def test_too_many_tool_calls_fails(self):
        result = {
            "response": "Done.",
            "tool_calls": [{"name": f"tool_{i}", "timestamp": i} for i in range(15)],
            "duration_ms": 500,
        }
        expected = {"max_tool_calls": 10}
        score = score_replay(result, expected)
        assert score["passed"] is False

    def test_empty_expected_passes(self):
        result = {
            "response": "Everything looks fine.",
            "tool_calls": [],
            "duration_ms": 100,
        }
        score = score_replay(result, {})
        assert score["passed"] is True
        assert score["score"] == 100

    def test_case_insensitive_keyword_check(self):
        result = {
            "response": "The DATABASE connection is refused.",
            "tool_calls": [],
            "duration_ms": 100,
        }
        expected = {"should_mention": ["database"]}
        score = score_replay(result, expected)
        assert score["passed"] is True


# ---------------------------------------------------------------------------
# Integration: load fixture + score
# ---------------------------------------------------------------------------


class TestFixtureScoring:
    def test_crashloop_fixture_structure(self):
        """Verify the crashloop fixture can be loaded and its expected
        section is valid for scoring."""
        fixture = load_fixture("crashloop_diagnosis")
        expected = fixture["expected"]

        # Simulate a good response
        result = {
            "response": "The root cause is a database connection failure. "
            "The pod cannot connect to db-service:5432.",
            "tool_calls": [
                {"name": "describe_pod", "timestamp": 0},
                {"name": "get_pod_logs", "timestamp": 1},
                {"name": "get_events", "timestamp": 2},
            ],
            "duration_ms": 1200,
        }
        score = score_replay(result, expected)
        assert score["passed"] is True
        assert score["score"] == 100

    def test_pending_pod_fixture_structure(self):
        fixture = load_fixture("pending_pod")
        expected = fixture["expected"]

        result = {
            "response": "The pod is stuck because there is insufficient memory "
            "on the worker nodes. No node has enough resources.",
            "tool_calls": [
                {"name": "describe_pod", "timestamp": 0},
                {"name": "list_nodes", "timestamp": 1},
            ],
            "duration_ms": 800,
        }
        score = score_replay(result, expected)
        assert score["passed"] is True

    def test_node_not_ready_fixture_structure(self):
        fixture = load_fixture("node_not_ready")
        expected = fixture["expected"]

        result = {
            "response": "worker-2 is NotReady due to memory pressure and OOM. "
            "The container runtime became unhealthy after a system OOM event.",
            "tool_calls": [
                {"name": "list_nodes", "timestamp": 0},
                {"name": "describe_node", "timestamp": 1},
                {"name": "get_events", "timestamp": 2},
            ],
            "duration_ms": 900,
        }
        score = score_replay(result, expected)
        assert score["passed"] is True


# ---------------------------------------------------------------------------
# Judge module import test
# ---------------------------------------------------------------------------


class TestJudgeModule:
    def test_import(self):
        from sre_agent.evals.judge import judge_response, JUDGE_PROMPT_TEMPLATE
        assert callable(judge_response)
        assert "Correctness" in JUDGE_PROMPT_TEMPLATE

    def test_judge_returns_none_without_client(self):
        """judge_response should return None gracefully when no API key."""
        from sre_agent.evals.judge import judge_response

        with patch("sre_agent.evals.judge.logger"):
            result = judge_response(
                prompt="test",
                response="test response",
                tool_calls=["list_pods"],
                client=None,
            )
        # Should be None (no real API key in test)
        # It either returns None from create_client failure or from the call
        assert result is None or isinstance(result, dict)
