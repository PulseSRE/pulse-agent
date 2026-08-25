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
                {"name": "list_resources", "timestamp": 1},
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
                {"name": "list_resources", "timestamp": 0},
                # describe_resource, not describe_node: the per-kind describers
                # were consolidated and the fixture expectations migrated with
                # them (the agent can no longer call describe_node at all).
                {"name": "describe_resource", "timestamp": 1},
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


# ---------------------------------------------------------------------------
# Real-configuration replay
# ---------------------------------------------------------------------------
#
# The gate runs the real model, so whatever config the harness hands the agent
# loop is what the judge scores. These tests pin the two properties that make
# that measurement honest: the agent sees Pulse's real prompt and real tool
# schemas, and nothing in the tool map can reach a cluster.


_EXECUTED: list[str] = []


class _RealTool:
    """Stand-in for a registered tool — records if it is ever executed."""

    def __init__(self, name: str):
        self.name = name

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": f"Real description for {self.name}",
            "input_schema": {
                "type": "object",
                "properties": {"namespace": {"type": "string"}},
                "required": ["namespace"],
            },
        }

    def call(self, input_data: dict) -> str:
        _EXECUTED.append(self.name)
        return "LIVE CLUSTER DATA"


def _orchestrated_config(tool_names: list[str], write_tools: set[str] | None = None) -> dict:
    tools = {name: _RealTool(name) for name in tool_names}
    return {
        "system_prompt": "REAL SKILL PROMPT",
        "tool_defs": [t.to_dict() for t in tools.values()],
        "tool_map": tools,
        "write_tools": set(write_tools or set()),
    }


def _stream_kwargs(client) -> list[dict]:
    return [c.kwargs for c in client.messages.stream.call_args_list]


def _system_text(system) -> str:
    if isinstance(system, str):
        return system
    return "\n".join(block.get("text", "") for block in system)


class TestShadowToolMap:
    """shadow_tool_map is the single choke point that keeps replay offline."""

    def setup_method(self):
        _EXECUTED.clear()

    def test_every_real_tool_is_replaced(self):
        from sre_agent.evals.replay_config import RecordedTool, shadow_tool_map

        real = {"list_pods": _RealTool("list_pods"), "drain_node": _RealTool("drain_node")}
        shadowed = shadow_tool_map(real, {"list_pods": "pod-1 Running"})

        assert set(shadowed) == {"list_pods", "drain_node"}
        assert all(isinstance(t, RecordedTool) for t in shadowed.values())
        for tool in shadowed.values():
            tool.call({})
        assert _EXECUTED == []

    def test_recorded_value_is_returned(self):
        from sre_agent.evals.replay_config import shadow_tool_map

        shadowed = shadow_tool_map({"list_pods": _RealTool("list_pods")}, {"list_pods": "pod-1 Running"})
        assert shadowed["list_pods"].call({"namespace": "prod"}) == "pod-1 Running"
        assert shadowed["list_pods"].calls == [{"namespace": "prod"}]

    def test_unrecorded_tool_returns_sentinel(self):
        from sre_agent.evals.replay_config import shadow_tool_map

        shadowed = shadow_tool_map({"drain_node": _RealTool("drain_node")}, {})
        result = shadowed["drain_node"].call({})
        assert shadowed["drain_node"].missing is True
        assert "no recorded response for 'drain_node'" in result
        assert "LIVE CLUSTER DATA" not in result

    def test_real_schema_is_preserved(self):
        """A rebuilt tool_def must still carry the real description and params."""
        from sre_agent.evals.replay_config import shadow_tool_map

        shadowed = shadow_tool_map({"list_pods": _RealTool("list_pods")}, {"list_pods": "x"})
        schema = shadowed["list_pods"].to_dict()
        assert schema["description"] == "Real description for list_pods"
        assert schema["input_schema"]["properties"] == {"namespace": {"type": "string"}}

    def test_recorded_tools_missing_from_the_map_are_added(self):
        from sre_agent.evals.replay_config import shadow_tool_map

        shadowed = shadow_tool_map({}, {"correlate_incident": "recorded"})
        assert shadowed["correlate_incident"].call({}) == "recorded"

    def test_unreadable_schema_falls_back_to_an_empty_object(self):
        from sre_agent.evals.replay_config import shadow_tool_map

        class _Broken:
            name = "broken"

            def to_dict(self):
                raise RuntimeError("schema unavailable")

            def call(self, input_data):
                _EXECUTED.append("broken")
                return "LIVE"

        shadowed = shadow_tool_map({"broken": _Broken()}, {"broken": "recorded"})
        assert shadowed["broken"].to_dict()["input_schema"] == {"type": "object", "properties": {}, "required": []}
        assert shadowed["broken"].call({}) == "recorded"
        assert _EXECUTED == []


class TestOfflineContext:
    """Cluster reads at prompt-build time must be off during replay."""

    def test_cluster_context_is_blanked_and_restored(self):
        import sre_agent.harness as harness_mod
        from sre_agent.evals.replay_config import offline_context

        original = harness_mod.get_cluster_context
        with patch.object(harness_mod, "get_cluster_context", lambda **kw: "LIVE CLUSTER STATE"):
            with offline_context():
                assert harness_mod.get_cluster_context(mode="sre") == ""
            assert harness_mod.get_cluster_context(mode="sre") == "LIVE CLUSTER STATE"
        assert harness_mod.get_cluster_context is original

    def test_agent_module_alias_is_patched_too(self):
        """agent.py imports get_cluster_context by value, so it needs its own patch."""
        import sre_agent.agent as agent_mod
        from sre_agent.evals.replay_config import offline_context

        with offline_context():
            assert agent_mod.get_cluster_context(mode="sre") == ""

    def test_slo_prometheus_query_is_disabled(self):
        """Skill routing asks the SLO registry for burn rates, which hits Prometheus."""
        from sre_agent.evals.replay_config import offline_context
        from sre_agent.slo_registry import get_slo_registry

        registry = get_slo_registry()
        assert registry._slos, "defaults should be registered, otherwise this proves nothing"
        with offline_context():
            assert registry.query_prometheus_values() == {}

    def test_llm_tool_picker_is_suppressed_by_default(self):
        import sre_agent.tool_predictor as tp
        from sre_agent.evals.replay_config import offline_context

        with offline_context():
            assert tp.llm_pick_tools(query="pods are crashing", tool_names=["list_pods"]) == []

    def test_llm_tool_picker_can_be_re_enabled(self):
        import sre_agent.tool_predictor as tp
        from sre_agent.evals.replay_config import offline_context

        original = tp.llm_pick_tools
        with offline_context(allow_llm_tool_picker=True):
            assert tp.llm_pick_tools is original

    def test_missing_required_patch_target_raises(self):
        """Losing isolation silently is the one failure this must never allow."""
        from sre_agent.evals import replay_config

        bogus = [("sre_agent.does_not_exist", "get_cluster_context", lambda **kw: "", True)]
        with patch.object(replay_config, "_ISOLATION_TARGETS", bogus):
            with pytest.raises(RuntimeError, match="Replay isolation failed"):
                with replay_config.offline_context():
                    pass


class TestBuildReplayConfig:
    def test_stub_flag_reproduces_the_old_configuration(self):
        from sre_agent.evals.replay_config import STUB_SYSTEM_PROMPT, build_replay_config

        cfg = build_replay_config("pods are crashing", {"list_pods": "x", "get_events": "y"}, stub=True)
        assert cfg["system_prompt"] == STUB_SYSTEM_PROMPT
        assert cfg["stub"] is True
        assert [d["name"] for d in cfg["tool_defs"]] == ["get_events", "list_pods"]
        assert all(d["input_schema"]["properties"] == {} for d in cfg["tool_defs"])
        assert cfg["write_tools"] == set()

    def test_real_config_keeps_real_defs_and_stubs_the_map(self):
        from sre_agent.evals.replay_config import RecordedTool, build_replay_config

        config = _orchestrated_config(["list_pods", "drain_node"])
        with patch("sre_agent.orchestrator.build_orchestrated_config", return_value=config):
            cfg = build_replay_config("pods are crashing", {"list_pods": "recorded"}, mode="sre")

        assert cfg["stub"] is False
        assert cfg["mode"] == "sre"
        # Real definitions reach the model...
        names = {d["name"] for d in cfg["tool_defs"]}
        assert names == {"list_pods", "drain_node"}
        assert any(d["input_schema"]["properties"] for d in cfg["tool_defs"])
        # ...but nothing executable does.
        assert all(isinstance(t, RecordedTool) for t in cfg["tool_map"].values())

    def test_real_config_assembles_the_product_system_prompt(self):
        from sre_agent.evals.replay_config import build_replay_config

        config = _orchestrated_config(["list_pods"])
        with patch("sre_agent.orchestrator.build_orchestrated_config", return_value=config):
            cfg = build_replay_config("a pod is crash-looping", {"list_pods": "recorded"}, mode="sre")

        text = _system_text(cfg["system_prompt"])
        assert isinstance(cfg["system_prompt"], list)
        assert "Intent Analysis" in text  # prompt_builder.INTENT_PREFIX
        assert len(text) > len("You are an SRE agent. Diagnose the issue.") * 10

    def test_recorded_tools_the_config_did_not_offer_are_reported(self):
        from sre_agent.evals.replay_config import build_replay_config

        config = _orchestrated_config(["list_pods"])
        recorded = {"list_pods": "a", "correlate_incident": "b"}
        with patch("sre_agent.orchestrator.build_orchestrated_config", return_value=config):
            cfg = build_replay_config("pods are crashing", recorded, mode="sre")

        assert cfg["unoffered_recorded_tools"] == ["correlate_incident"]
        assert {d["name"] for d in cfg["tool_defs"]} == {"list_pods"}
        assert "correlate_incident" in cfg["tool_map"]


class TestResolveMode:
    """Multi-turn follow-ups must not swap the agent's toolset mid-conversation."""

    def test_first_turn_uses_the_classifier(self):
        from sre_agent.evals import replay_config

        with patch.object(replay_config, "classify_mode", return_value="view_designer"):
            assert replay_config.resolve_mode("build me a dashboard") == "view_designer"

    def test_dashboard_follow_up_stays_in_view_designer(self):
        from sre_agent.evals import replay_config

        with patch.object(replay_config, "classify_mode", return_value="sre"):
            mode = replay_config.resolve_mode("Add a memory chart to it", last_mode="view_designer")
        assert mode == "view_designer"

    def test_hard_sre_keyword_breaks_out_of_view_designer(self):
        from sre_agent.evals import replay_config

        with patch.object(replay_config, "classify_mode", return_value="sre"):
            mode = replay_config.resolve_mode("the api-server pod is crashlooping", last_mode="view_designer")
        assert mode == "sre"

    def test_sre_turns_are_not_made_sticky(self):
        from sre_agent.evals import replay_config

        with patch.object(replay_config, "classify_mode", return_value="security"):
            assert replay_config.resolve_mode("scan for rbac risks", last_mode="sre") == "security"


class TestReplayHarnessRealConfig:
    """End-to-end: the loop runs with the real config and no real tool fires."""

    def setup_method(self):
        _EXECUTED.clear()

    @patch.dict("os.environ", {"PULSE_AGENT_HARNESS": "0"})
    def test_real_tools_are_never_executed(self):
        client = _make_mock_client(
            tool_names_to_call=["list_pods", "drain_node"],
            final_text="Diagnosis complete.",
        )
        config = _orchestrated_config(["list_pods", "drain_node"])
        with patch("sre_agent.orchestrator.build_orchestrated_config", return_value=config):
            harness = ReplayHarness({"list_pods": "api-server CrashLoopBackOff"}, mode="sre")
            result = harness.run(client=client, prompt="pods are crashing")

        assert _EXECUTED == []
        # drain_node is stopped by the harness deny policy (sre_agent/policy.py)
        # before the replay recording check can classify it as unrecorded — the
        # policy refusal is itself the correct "never executed" behavior.
        assert result["unrecorded_tool_calls"] == []
        messages = _stream_kwargs(client)[-1]["messages"]
        blob = str(messages)
        assert "api-server CrashLoopBackOff" in blob
        assert "Policy:" in blob  # replaces the unrecorded-tool sentinel for policy-denied tools
        assert "LIVE CLUSTER DATA" not in blob

    @patch.dict("os.environ", {"PULSE_AGENT_HARNESS": "0"})
    def test_a_real_tool_passed_in_by_a_caller_is_still_shadowed(self):
        client = _make_mock_client(tool_names_to_call=["get_events"], final_text="Done.")
        config = _orchestrated_config(["list_pods"])
        with patch("sre_agent.orchestrator.build_orchestrated_config", return_value=config):
            harness = ReplayHarness({"list_pods": "x"}, mode="sre")
            harness.run(
                client=client,
                prompt="pods are crashing",
                tool_map={"get_events": _RealTool("get_events")},
            )

        assert _EXECUTED == []

    @patch.dict("os.environ", {"PULSE_AGENT_HARNESS": "0"})
    def test_real_prompt_and_schemas_reach_the_model(self):
        client = _make_mock_client(final_text="Done.")
        config = _orchestrated_config(["list_pods"])
        with patch("sre_agent.orchestrator.build_orchestrated_config", return_value=config):
            harness = ReplayHarness({"list_pods": "x"}, mode="sre")
            harness.run(client=client, prompt="a pod is crash-looping")

        kwargs = _stream_kwargs(client)[0]
        assert kwargs["tools"][0]["description"] == "Real description for list_pods"
        assert kwargs["tools"][0]["input_schema"]["required"] == ["namespace"]
        assert "Intent Analysis" in _system_text(kwargs["system"])

    @patch.dict("os.environ", {"PULSE_AGENT_HARNESS": "0"})
    def test_stub_config_flag_restores_the_old_behaviour(self):
        client = _make_mock_client(final_text="Done.")
        harness = ReplayHarness({"list_pods": "x"}, stub_config=True)
        harness.run(client=client, prompt="a pod is crash-looping")

        kwargs = _stream_kwargs(client)[0]
        assert kwargs["tools"] == [
            {
                "name": "list_pods",
                "description": "Recorded stub for list_pods",
                "input_schema": {"type": "object", "properties": {}, "required": []},
            }
        ]
        assert _system_text(kwargs["system"]) == "You are an SRE agent. Diagnose the issue."

    @patch.dict("os.environ", {"PULSE_AGENT_HARNESS": "0"})
    def test_write_tools_are_confirmed_and_return_their_recording(self):
        """Without a confirm callback the loop denies writes, measuring a refusal."""
        client = _make_mock_client(tool_names_to_call=["scale_deployment"], final_text="Scaled.")
        config = _orchestrated_config(["scale_deployment"], write_tools={"scale_deployment"})
        with patch("sre_agent.orchestrator.build_orchestrated_config", return_value=config):
            harness = ReplayHarness({"scale_deployment": "scaled checkout to 5"}, mode="sre")
            result = harness.run(client=client, prompt="scale checkout to 5")

        assert [tc["name"] for tc in result["tool_calls"]] == ["scale_deployment"]
        blob = str(_stream_kwargs(client)[-1]["messages"])
        assert "scaled checkout to 5" in blob
        assert "Operation denied" not in blob
        assert _EXECUTED == []


class TestMultiTurnRealConfig:
    def setup_method(self):
        _EXECUTED.clear()

    @patch.dict("os.environ", {"PULSE_AGENT_HARNESS": "0"})
    def test_each_turn_is_configured_and_shadowed(self):
        from sre_agent.evals.replay import MultiTurnReplayHarness

        turns = [
            {"prompt": "list pods in staging", "recorded_responses": {"list_pods": "frontend-1 Running"}},
            {
                "prompt": "show me the logs for the first one",
                "recorded_responses": {"get_pod_logs": "connection refused"},
            },
        ]
        client = _make_mock_client(tool_names_to_call=["list_pods"], final_text="Pods listed.")
        client2 = _make_mock_client(tool_names_to_call=["get_pod_logs"], final_text="Logs read.")
        client.messages.stream.side_effect = list(client.messages.stream.side_effect) + list(
            client2.messages.stream.side_effect
        )

        config = _orchestrated_config(["list_pods", "get_pod_logs", "drain_node"])
        with patch("sre_agent.orchestrator.build_orchestrated_config", return_value=config):
            harness = MultiTurnReplayHarness(turns, mode="sre")
            result = harness.run(client=client)

        assert _EXECUTED == []
        assert len(result["turns"]) == 2
        assert result["modes"] == ["sre", "sre"]
        assert len(harness.configs) == 2
        # Turn 2 must not still be serving turn 1's recording.
        assert harness.configs[1]["tool_map"]["list_pods"].missing is True
        assert harness.configs[1]["tool_map"]["get_pod_logs"].value == "connection refused"

    @patch.dict("os.environ", {"PULSE_AGENT_HARNESS": "0"})
    def test_sticky_mode_is_applied_across_turns(self):
        from sre_agent.evals import replay_config
        from sre_agent.evals.replay import MultiTurnReplayHarness

        turns = [
            {"prompt": "Create a dashboard for production", "recorded_responses": {"create_dashboard": "made"}},
            {"prompt": "Add a memory chart to it", "recorded_responses": {"add_widget_to_view": "added"}},
        ]
        client = _make_mock_client(final_text="One.")
        client2 = _make_mock_client(final_text="Two.")
        client.messages.stream.side_effect = list(client.messages.stream.side_effect) + list(
            client2.messages.stream.side_effect
        )

        modes = iter(["view_designer", "sre"])
        config = _orchestrated_config(["create_dashboard", "add_widget_to_view"])
        with (
            patch.object(replay_config, "classify_mode", side_effect=lambda q: next(modes)),
            patch("sre_agent.orchestrator.build_orchestrated_config", return_value=config),
        ):
            harness = MultiTurnReplayHarness(turns)
            result = harness.run(client=client)

        assert result["modes"] == ["view_designer", "view_designer"]

    @patch.dict("os.environ", {"PULSE_AGENT_HARNESS": "0"})
    def test_stub_config_flag_restores_the_old_multi_turn_behaviour(self):
        from sre_agent.evals.replay import MultiTurnReplayHarness

        turns = [{"prompt": "list pods", "recorded_responses": {"list_pods": "frontend-1 Running"}}]
        client = _make_mock_client(final_text="Done.")
        harness = MultiTurnReplayHarness(turns, stub_config=True)
        harness.run(client=client)

        kwargs = _stream_kwargs(client)[0]
        assert _system_text(kwargs["system"]) == "You are an SRE agent. Diagnose the issue."
        assert kwargs["tools"][0]["description"] == "Recorded stub for list_pods"


class TestToolRegistryInReplay:
    """Replay must select from the same tool universe production does."""

    def test_registry_is_populated(self):
        from sre_agent.evals.replay_config import ensure_tool_registry

        count = ensure_tool_registry()
        # The curated static fallback is far smaller than the full registry; if
        # discovery silently failed we would be measuring a different agent.
        assert count > 80, f"only {count} tools registered — discovery did not run"

    def test_node_investigation_is_offered_the_tools_it_needs(self):
        """The union of module maps and registry is what makes plain-decorated
        tools selectable — TOOL_REGISTRY only holds @beta_tool(category=...) ones."""
        from sre_agent.evals.replay_config import ensure_tool_registry
        from sre_agent.skill_loader import build_config_from_skill, get_skill

        ensure_tool_registry()
        skill = get_skill("sre")
        assert skill is not None
        config = build_config_from_skill(skill, query="a cluster node is NotReady, investigate it")
        offered = set(config["tool_map"])

        # list_resources supersedes the per-kind listing tools, so that is what a
        # node investigation should be offered — not list_nodes.
        assert "list_resources" in offered, "the universal listing tool must be offered"

    def test_superseded_tools_are_not_re_offered(self):
        """list_resources replaced the per-kind listers; they must stay retired."""
        from sre_agent.tool_categories import TOOL_CATEGORIES

        categorised: set[str] = set()
        for cat in TOOL_CATEGORIES.values():
            categorised.update(cat.get("tools", []))

        for tool in ("list_namespaces", "get_services", "list_daemonsets", "get_resource_quotas"):
            assert tool not in categorised, f"{tool} is superseded by list_resources and must not be categorised"

    def test_is_idempotent(self):
        from sre_agent.evals.replay_config import ensure_tool_registry

        assert ensure_tool_registry() == ensure_tool_registry()


class TestIsolationRefcounting:
    """Concurrent fixtures share one set of patches, so they must be refcounted."""

    def test_patches_survive_an_inner_exit(self):
        from sre_agent import harness
        from sre_agent.evals.replay_config import offline_context

        real = harness.get_cluster_context
        with offline_context():
            patched = harness.get_cluster_context
            assert patched is not real
            with offline_context():
                pass
            # the inner exit must NOT have restored the live reader
            assert harness.get_cluster_context is patched
        assert harness.get_cluster_context is real

    def test_conflicting_picker_settings_are_refused(self):
        import pytest

        from sre_agent.evals.replay_config import offline_context

        with offline_context(allow_llm_tool_picker=False):
            with pytest.raises(RuntimeError, match="already active"):
                with offline_context(allow_llm_tool_picker=True):
                    pass

    def test_depth_returns_to_zero_after_nesting(self):
        from sre_agent.evals import replay_config
        from sre_agent.evals.replay_config import offline_context

        with offline_context():
            with offline_context():
                assert replay_config._isolation_depth == 2
        assert replay_config._isolation_depth == 0
        assert replay_config._isolation_stack is None

    def test_concurrent_entries_never_leave_it_unpatched(self):
        import threading

        from sre_agent import harness
        from sre_agent.evals.replay_config import offline_context

        real = harness.get_cluster_context
        observed: list[bool] = []
        start = threading.Barrier(4)

        def worker() -> None:
            start.wait()
            for _ in range(25):
                with offline_context():
                    observed.append(harness.get_cluster_context is not real)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert observed, "workers recorded nothing"
        assert all(observed), "a thread saw the live cluster reader while inside isolation"
        assert harness.get_cluster_context is real


class TestParallelExecution:
    def test_order_is_preserved_regardless_of_completion(self):
        import time

        from sre_agent.evals.replay_cli import _execute

        def slow_first(name: str) -> dict:
            # invert the natural completion order
            time.sleep(0.05 if name == "a" else 0.0)
            return {"fixture": name}

        out = _execute(["a", "b", "c"], slow_first, concurrency=3)
        assert [r["fixture"] for r in out] == ["a", "b", "c"]

    def test_serial_path_used_for_a_single_fixture(self):
        from sre_agent.evals.replay_cli import _execute

        seen: list[str] = []
        out = _execute(["only"], lambda n: (seen.append(n), {"fixture": n})[1], concurrency=8)
        assert [r["fixture"] for r in out] == ["only"]
        assert seen == ["only"]

    def test_every_fixture_runs_exactly_once(self):
        import threading

        from sre_agent.evals.replay_cli import _execute

        lock = threading.Lock()
        calls: list[str] = []

        def record(name: str) -> dict:
            with lock:
                calls.append(name)
            return {"fixture": name}

        names = [f"f{i}" for i in range(12)]
        out = _execute(names, record, concurrency=4)
        assert sorted(calls) == sorted(names)
        assert [r["fixture"] for r in out] == names


class TestMultiTurnHistory:
    """A follow-up turn must be able to see what the previous turn found."""

    def test_compaction_preserves_the_callers_list_identity(self):
        from sre_agent.loop_budget import compact_tool_results

        big = "x" * 60_000
        block = {"type": "tool_result", "tool_use_id": "t", "content": big}
        original = [{"role": "user", "content": [dict(block)]} for _ in range(9)]
        held = original  # the caller keeps this reference across the agent turn
        compacted, reclaimed = compact_tool_results(original)
        assert reclaimed > 0
        held[:] = compacted
        # appends made after compaction must still be visible to the caller
        held.append({"role": "assistant", "content": "done"})
        assert original[-1]["content"] == "done"
        assert original is held

    def test_harness_passes_the_live_list_not_a_copy(self):
        """The agent appends tool_use/tool_result blocks to the list it is given.

        Passing a copy discarded them, so turn 2 saw only turn 1's final prose and
        was asked follow-ups about data it could no longer see.
        """
        import inspect

        from sre_agent.evals import replay

        src = inspect.getsource(replay.MultiTurnReplayHarness.run)
        assert '"messages": messages,' in src, "multi-turn must pass the live list"
        assert '"messages": list(messages)' not in src, "copying discards the tool exchange"

    def test_final_text_is_not_duplicated_when_the_agent_already_appended(self):
        import inspect

        from sre_agent.evals import replay

        src = inspect.getsource(replay.MultiTurnReplayHarness.run)
        assert 'messages[-1].get("role") == "assistant"' in src


class TestRegressionGate:
    """The gate blocks work that makes things worse, not work that is not yet perfect."""

    @staticmethod
    def _result(name: str, passed: bool) -> dict:
        return {"fixture": name, "score": {"passed": passed}}

    def test_a_newly_failing_fixture_is_a_regression(self):
        from sre_agent.evals.replay_cli import _regressions

        baseline = {"a": True, "b": True}
        assert _regressions([self._result("a", True), self._result("b", False)], baseline) == ["b"]

    def test_a_fixture_that_was_already_failing_is_not(self):
        from sre_agent.evals.replay_cli import _regressions

        # 17 fixtures fail today; the gate must not block every merge on them
        baseline = {"a": True, "b": False}
        assert _regressions([self._result("a", True), self._result("b", False)], baseline) == []

    def test_fixing_one_while_breaking_another_still_fails(self):
        from sre_agent.evals.replay_cli import _regressions

        # same total, not the same thing — which is why this is per-fixture
        baseline = {"a": True, "b": False}
        regressed = _regressions([self._result("a", False), self._result("b", True)], baseline)
        assert regressed == ["a"]

    def test_a_new_fixture_is_not_a_regression(self):
        from sre_agent.evals.replay_cli import _regressions

        assert _regressions([self._result("brand_new", False)], {"a": True}) == []

    def test_baseline_file_matches_the_suite(self):
        import json
        from pathlib import Path

        from sre_agent.evals.replay import list_fixtures

        data = json.loads(Path("sre_agent/evals/baselines/replay.json").read_text())
        assert set(data["fixtures"]) == set(list_fixtures()), (
            "baseline is out of step with the fixture suite — refresh with --save-baseline"
        )
