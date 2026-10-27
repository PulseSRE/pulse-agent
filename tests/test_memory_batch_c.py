"""Memory and context that never reached the model, pinned (functionality review, batch C)."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sre_agent.agent import run_agent_streaming
from tests.test_plan_only_turns import _client

MEMORY_MARKER = "Past Similar Incidents: api crashed after DB_HOST was removed"


def _end_turn():
    return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="ok")])


async def _system_sent(system_prompt, mode="sre", tool_map=None):
    client = _client(_end_turn())
    tool_map = tool_map or {"list_pods": MagicMock()}
    await run_agent_streaming(
        client=client,
        messages=[{"role": "user", "content": "why is api crashing"}],
        system_prompt=system_prompt,
        tool_defs=[{"name": n, "input_schema": {}} for n in tool_map],
        tool_map=tool_map,
        write_tools=set(),
        mode=mode,
    )
    return str(client.messages.stream.call_args_list[0].kwargs["system"])


# ── C1: a caller's assembled prompt reaches the model ─────────────────────


@pytest.mark.asyncio
async def test_memory_in_an_assembled_prompt_reaches_the_model():
    """Chat and monitor investigations pass cache blocks with memory appended."""
    from sre_agent.harness import build_cached_system_prompt

    blocks = build_cached_system_prompt("You are the SRE skill.", "cluster: 3 nodes")
    blocks.append({"type": "text", "text": MEMORY_MARKER})
    with patch(
        "sre_agent.tool_predictor.select_tools_adaptive",
        side_effect=lambda q, all_tool_map, **k: ([], all_tool_map, list(all_tool_map)),
    ):
        system = await _system_sent(blocks)
    assert MEMORY_MARKER in system


# ── C2: memory tools are offered on the server ────────────────────────────


def test_skill_configs_offer_the_memory_tools():
    from sre_agent.skill_loader import build_config_from_skill, get_skill

    config = build_config_from_skill(get_skill("sre"), "why is api crashing again")
    for name in ("search_past_incidents", "remember_environment_fact", "search_conversations"):
        assert name in config["tool_map"]
    assert "search_past_incidents" not in config["write_tools"]


def test_memory_tools_can_be_turned_off(monkeypatch):
    from sre_agent.config import _reset_settings
    from sre_agent.skill_loader import build_config_from_skill, get_skill

    monkeypatch.setenv("PULSE_AGENT_MEMORY", "0")
    _reset_settings()
    try:
        config = build_config_from_skill(get_skill("sre"), "why is api crashing")
        assert "search_past_incidents" not in config["tool_map"]
    finally:
        _reset_settings()


# ── C5: the second selection pass keeps what the skill guarantees ─────────


@pytest.mark.asyncio
async def test_second_selection_pass_keeps_guaranteed_tools():
    def _tool(name):
        t = MagicMock()
        t.name = name
        t.to_dict.return_value = {"name": name, "input_schema": {}}
        return t

    tool_map = {n: _tool(n) for n in ("list_pods", "get_prometheus_query", "search_past_incidents")}
    from sre_agent.skill_loader import get_skill

    skill = get_skill("sre")

    def predict_almost_nothing(query, all_tool_map, **kwargs):
        kept = {"list_pods": all_tool_map["list_pods"]}
        return [], kept, list(kept)

    with (
        patch("sre_agent.tool_predictor.select_tools_adaptive", side_effect=predict_almost_nothing),
        patch.object(skill, "requires_tools", ["get_prometheus_query"]),
    ):
        client = _client(_end_turn())
        await run_agent_streaming(
            client=client,
            messages=[{"role": "user", "content": "show burn rate"}],
            system_prompt="x",
            tool_defs=[{"name": n, "input_schema": {}} for n in tool_map],
            tool_map=tool_map,
            write_tools=set(),
            mode="sre",
        )
    offered = {d["name"] for d in client.messages.stream.call_args_list[0].kwargs["tools"]}
    assert {"get_prometheus_query", "search_past_incidents"} <= offered


# ── C4: feedback scores the session it was given in ───────────────────────


def _manager():
    from sre_agent.memory import MemoryManager

    mgr = MemoryManager.__new__(MemoryManager)
    mgr.store = MagicMock()
    mgr.store.record_incident.side_effect = [101, 202]
    mgr.store.get_incident_count.return_value = 1
    mgr._sessions = {}
    return mgr


def test_feedback_scores_the_turn_of_its_own_session():
    mgr = _manager()
    with patch("sre_agent.memory.extract_runbook"), patch("sre_agent.memory.is_duplicate_runbook", return_value=True):
        mgr.start_turn("alice-session")
        mgr.finish_turn("q1", "a1", session_id="alice-session")
        mgr.start_turn("bob-session")
        mgr.finish_turn("q2", "a2", session_id="bob-session")
        out = mgr.update_last_outcome(True, session_id="alice-session")
    assert out["incident_id"] == 101, "Alice's thumbs-up must not score Bob's turn"
    mgr.store.update_incident_outcome.assert_called_once()
    assert mgr.store.update_incident_outcome.call_args.args[0] == 101


def test_feedback_for_an_unknown_session_does_nothing():
    mgr = _manager()
    assert mgr.update_last_outcome(True, session_id="nobody") is None
    mgr.store.update_incident_outcome.assert_not_called()


# ── C6: the cost budget counts investigations, priced for the model ───────


def test_investigation_usage_is_recorded_as_a_turn():
    from sre_agent.monitor.investigations import _record_investigation_usage

    with patch("sre_agent.tool_usage.record_turn") as record:
        _record_investigation_usage(
            {"id": "f-123", "title": "api crashloop"},
            {"input_tokens": 900, "output_tokens": 100, "cache_read_tokens": 0, "cache_creation_tokens": 0},
            ["list_pods"],
            ["list_pods"],
        )
    kwargs = record.call_args.kwargs
    assert kwargs["session_id"].startswith("pipeline-") and kwargs["input_tokens"] == 900


def test_cost_estimate_uses_the_configured_model():
    from sre_agent.observability import estimate_cost_usd

    with patch(
        "sre_agent.observability.token_prices",
        return_value={"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
    ):
        assert estimate_cost_usd(1_000_000, 1_000_000, 1_000_000, 0) == pytest.approx(18.3)


# ── C7: budgets and the circuit breaker ───────────────────────────────────


def test_half_open_breaker_lets_one_probe_through():
    from sre_agent.agent import CircuitBreaker

    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=60)
    cb.record_failure()
    cb.last_failure_time = time.time() - 61
    assert cb.allow_request() is True, "the probe"
    assert cb.allow_request() is False, "everyone else waits for its verdict"
    cb.record_success()
    assert cb.allow_request() is True


@pytest.mark.asyncio
async def test_token_ceiling_ends_the_turn(monkeypatch):
    from sre_agent.config import _reset_settings

    monkeypatch.setenv("PULSE_AGENT_LOOP_MAX_INPUT_TOKENS", "1000")
    _reset_settings()
    try:
        tool = MagicMock()
        tool.call.return_value = "pods"
        heavy = SimpleNamespace(
            input_tokens=5000, output_tokens=10, cache_read_input_tokens=0, cache_creation_input_tokens=0
        )
        call = SimpleNamespace(
            stop_reason="tool_use",
            content=[SimpleNamespace(type="tool_use", id="t1", name="list_pods", input={})],
            usage=heavy,
        )
        client = _client(call, call, call, _end_turn())
        out = await run_agent_streaming(
            client=client,
            messages=[{"role": "user", "content": "list pods"}],
            system_prompt="x",
            tool_defs=[{"name": "list_pods", "input_schema": {}}],
            tool_map={"list_pods": tool},
            write_tools=set(),
        )
        assert "token budget" in out
        assert client.messages.stream.call_count < 4
    finally:
        _reset_settings()


# ── C8: handoffs and synthesis ────────────────────────────────────────────


def test_only_processed_handoffs_are_deleted():
    from sre_agent.monitor.handoff_processor import process_handoffs

    rows = [{"id": i, "details": "{}", "namespace": ""} for i in (1, 2, 3, 4, 5)]
    repo = MagicMock()
    repo.async_get_pending_handoffs = AsyncMock(return_value=rows)
    repo.async_delete_processed_handoffs = AsyncMock()
    with patch("sre_agent.monitor.handoff_processor.get_monitor_repo", return_value=repo):
        asyncio.run(process_handoffs(MagicMock()))
    repo.async_delete_processed_handoffs.assert_awaited_once_with([1, 2, 3, 4, 5])


def test_synthesis_does_not_stream_the_conflicts_block():
    from sre_agent.synthesis import synthesize_parallel_outputs

    chunks = ["Both agree the pod", " is OOMKilled.\n```js", 'on\n{"conflicts": []}\n```']

    class _Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        @property
        def text_stream(self):
            async def gen():
                for c in chunks:
                    yield c

            return gen()

    client = MagicMock()
    client.messages.stream = MagicMock(return_value=_Stream())
    seen: list[str] = []

    async def on_delta(text):
        seen.append(text)

    result = SimpleNamespace(primary_skill="sre", secondary_skill="security", primary_output="a", secondary_output="b")
    with patch("sre_agent.synthesis._synthesis_model", return_value="claude-test"):
        out = asyncio.run(synthesize_parallel_outputs(result, "q", client, on_text_delta=on_delta))
    streamed = "".join(seen)
    assert "conflicts" not in streamed and "```" not in streamed
    assert streamed.startswith("Both agree the pod is OOMKilled.")
    assert client.messages.stream.call_args.kwargs["model"] == "claude-test"
    assert "conflicts" not in out.unified_response


# ── C9: MCP toolsets go through the operator ──────────────────────────────


def test_mcp_toolsets_are_set_on_the_operator_resource():
    from sre_agent.api import skill_rest

    api = MagicMock()
    api.list_namespaced_custom_object.return_value = {"items": [{"metadata": {"name": "pulse"}}]}
    with (
        patch("kubernetes.client.CustomObjectsApi", return_value=api),
        patch("sre_agent.k8s_client._load_k8s"),
        patch.object(skill_rest, "_detect_namespace", return_value="openshiftpulse"),
    ):
        out = skill_rest._set_toolsets_on_operator_cr(["core", "helm"])
    assert out["managedBy"] == "pulse-operator"
    body = api.patch_namespaced_custom_object.call_args.args[-1]
    assert body == {"spec": {"agent": {"mcp": {"toolsets": "core,helm"}}}}


def test_mcp_toolsets_explain_when_the_agent_may_not_change_the_operator():
    from fastapi import HTTPException
    from kubernetes.client.rest import ApiException

    from sre_agent.api import skill_rest

    api = MagicMock()
    api.list_namespaced_custom_object.return_value = {"items": [{"metadata": {"name": "pulse"}}]}
    api.patch_namespaced_custom_object.side_effect = ApiException(status=403)
    with (
        patch("kubernetes.client.CustomObjectsApi", return_value=api),
        patch("sre_agent.k8s_client._load_k8s"),
        patch.object(skill_rest, "_detect_namespace", return_value="openshiftpulse"),
    ):
        with pytest.raises(HTTPException) as err:
            skill_rest._set_toolsets_on_operator_cr(["core"])
    assert err.value.status_code == 409 and "oc patch openshiftpulse pulse" in err.value.detail
