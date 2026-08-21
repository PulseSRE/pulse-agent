"""Tests for the recorded replay evaluation harness.

These tests mock the Claude API so no real API key is needed.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sre_agent.evals.replay import (
    ReplayHarness,
    list_fixtures,
    load_fixture,
    score_replay,
)
from sre_agent.evals.replay_cli import _apply_judge_gate, _expected_for

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
        responses.append(SimpleNamespace(stop_reason="tool_use", content=tool_blocks))
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
        stream.__aenter__ = AsyncMock(return_value=stream)
        stream.__aexit__ = AsyncMock(return_value=False)

        async def _aiter(evts=events):
            for e in evts:
                yield e

        stream.__aiter__ = MagicMock(return_value=_aiter())
        stream.get_final_message = AsyncMock(return_value=resp)
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
            if fixture.get("multi_turn"):
                # Multi-turn fixtures have turns instead of prompt/recorded_responses
                assert "turns" in fixture, f"{name} missing 'turns'"
                for i, turn in enumerate(fixture["turns"]):
                    assert "prompt" in turn, f"{name} turn {i} missing 'prompt'"
                    assert "recorded_responses" in turn, f"{name} turn {i} missing 'recorded_responses'"
            else:
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
        harness = ReplayHarness(
            {
                "describe_pod": "CrashLoopBackOff",
                "get_pod_logs": "connection refused to db-service:5432",
            }
        )
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
            "response": "The root cause is a database connection failure. The pod cannot connect to db-service:5432.",
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
        from sre_agent.evals.judge import JUDGE_PROMPT_TEMPLATE, judge_response

        assert callable(judge_response)
        assert "Correctness" in JUDGE_PROMPT_TEMPLATE

    @pytest.mark.asyncio
    async def test_judge_returns_none_without_client(self):
        """judge_response should return None gracefully when no API key."""
        from sre_agent.evals.judge import judge_response

        with patch("sre_agent.evals.judge.logger"):
            result = await judge_response(
                prompt="test",
                response="test response",
                tool_calls=["list_pods"],
                client=None,
            )
        # Should be None (no real API key in test)
        # It either returns None from create_async_client failure or from the call
        assert result is None or isinstance(result, dict)


# ---------------------------------------------------------------------------
# Dry-run expectation trimming and judge gating
# ---------------------------------------------------------------------------


class TestDryRunExpectations:
    """In dry-run the mock decides content and ordering, so those cannot gate."""

    def test_live_expectations_pass_through_untouched(self):
        expected = {"should_mention": ["database"], "should_use_tools": ["describe_pod"]}
        assert _expected_for(expected, dry_run=False) == expected

    def test_dry_run_drops_content_and_ordering_checks(self):
        expected = {
            "should_mention": ["database"],
            "overall_should_mention": ["connection"],
            "should_use_tools_in_order": ["a", "b"],
            "should_use_tools": ["describe_pod"],
            "should_not_use_tools": ["delete_pod"],
            "max_tool_calls": 10,
        }
        trimmed = _expected_for(expected, dry_run=True)
        assert trimmed == {
            "should_use_tools": ["describe_pod"],
            "should_not_use_tools": ["delete_pod"],
            "max_tool_calls": 10,
        }

    def test_dry_run_trims_per_turn_content(self):
        expected = {"per_turn": [{"should_mention": ["db"], "should_use_tools": ["list_pods"]}]}
        trimmed = _expected_for(expected, dry_run=True)
        assert trimmed["per_turn"] == [{"should_use_tools": ["list_pods"]}]


class TestJudgeGate:
    """The judge decides correctness; keyword matching drops to advisory."""

    def _score_with_missing_keyword(self):
        result = {
            "response": "The workload cannot reach its datastore.",
            "tool_calls": [{"name": "describe_pod", "timestamp": 0}],
            "duration_ms": 500,
        }
        expected = {"should_mention": ["database"], "should_use_tools": ["describe_pod"]}
        return score_replay(result, expected)

    def test_no_threshold_leaves_score_untouched(self):
        score = self._score_with_missing_keyword()
        assert _apply_judge_gate(score, {"total": 95}, None) == score

    def test_no_judge_result_leaves_score_untouched(self):
        score = self._score_with_missing_keyword()
        assert _apply_judge_gate(score, None, 70) == score

    def test_right_answer_phrased_differently_passes(self):
        score = self._score_with_missing_keyword()
        assert score["passed"] is False  # keyword matching alone rejects it
        gated = _apply_judge_gate(score, {"total": 88}, 70)
        assert gated["passed"] is True
        advisory = [c for c in gated["checks"] if c.get("advisory")]
        assert advisory and all(c["kind"] == "content" for c in advisory)

    def test_low_judge_score_fails_even_with_every_keyword(self):
        result = {
            "response": "Something about the database and the connection, but no real diagnosis.",
            "tool_calls": [{"name": "describe_pod", "timestamp": 0}],
            "duration_ms": 500,
        }
        expected = {"should_mention": ["database", "connection"], "should_use_tools": ["describe_pod"]}
        score = score_replay(result, expected)
        assert score["passed"] is True  # every keyword present
        gated = _apply_judge_gate(score, {"total": 41}, 70)
        assert gated["passed"] is False

    def test_structure_checks_still_gate(self):
        result = {
            "response": "Deleted the pod.",
            "tool_calls": [{"name": "delete_pod", "timestamp": 0}],
            "duration_ms": 500,
        }
        expected = {"should_not_use_tools": ["delete_pod"]}
        score = score_replay(result, expected)
        gated = _apply_judge_gate(score, {"total": 99}, 70)
        assert gated["passed"] is False

    def test_non_numeric_judge_total_is_ignored(self):
        score = self._score_with_missing_keyword()
        assert _apply_judge_gate(score, {"total": "n/a"}, 70) == score
