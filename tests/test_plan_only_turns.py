"""A turn that asks for a plan does not change the cluster.

Under the release replay, "Diagnose the quota issue and propose a recovery
plan" sometimes ended with the model deleting three pods and sometimes with
the plan it was asked for. Confirmation is consent to a requested step; here
no step was requested, so writes are refused before anyone is asked.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sre_agent.action_policy import PLAN_ONLY_REFUSAL, latest_user_text, requests_plan_only
from sre_agent.agent import run_agent_streaming
from tests.test_agent import _MockAsyncStream

PLAN_ONLY = [
    "Namespace team-alpha cannot create new pods. Diagnose the quota issue and propose a recovery plan.",
    "Propose a fix for the crashlooping cache",
    "What should we do about worker-0's memory pressure?",
    "Give me my options for the failing rollout, don't change anything",
    "Recommend a rollback target for checkout",
    "Should we scale checkout-api up?",
]

MAY_ACT = [
    # Every replay fixture and SRE-Bench task that expects a write is phrased like these.
    "Pods in the payments namespace keep crashing with permission denied errors. Figure out what's going on and fix it.",
    "The checkout service deployment broke after the last rollout. Roll it back to the last working version.",
    "Roll it back to the previous version.",
    "The pod api-server in namespace production is crash-looping. Diagnose the root cause and resolve it.",
    "Delete the pod payments-api in namespace production immediately.",
    # A plan *and* an instruction to act.
    "Propose a plan, then go ahead and apply it",
    "Plan the upgrade to 4.18 and fix any blockers you find",
    "Can you restart the cache deployment? I'd suggest doing it now.",
    # Approving a plan proposed earlier.
    "yes, do it",
    "go ahead",
    # Questions with no request for a plan stay as they were.
    "The checkout service is broken after the last deploy. What's wrong?",
    "Find out why web-frontend cannot be scheduled.",
]


@pytest.mark.parametrize("text", PLAN_ONLY)
def test_plan_requests_are_plan_only(text):
    assert requests_plan_only(text)


@pytest.mark.parametrize("text", MAY_ACT)
def test_requests_to_act_are_not(text):
    assert not requests_plan_only(text)


def test_latest_user_text_skips_tool_results():
    messages = [
        {"role": "user", "content": "propose a plan"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "list_pods", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "pods"}]},
    ]
    assert latest_user_text(messages) == "propose a plan"
    blocks = [
        {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "image"}, {"type": "text", "text": "b"}]}
    ]
    assert latest_user_text(blocks) == "a\nb"
    assert latest_user_text([]) == ""


def test_ui_context_header_is_not_the_persons_words():
    # api/context.py prefixes what the person is viewing; its titles are not a request.
    text = (
        "[UI Context: Dashboard 'Upgrade plan' (ID: cv-1, 3 widgets)]\n"
        "Widgets: chart: Recommendations, data_table: Rollback plan\n"
        "IMPORTANT: The user is viewing this dashboard. Use view_id='cv-1' "
        "for any update_view_widgets or get_view_details calls.\n\n"
        "add a chart of memory by pod"
    )
    assert latest_user_text([{"role": "user", "content": text}]) == "add a chart of memory by pod"
    assert not requests_plan_only(latest_user_text([{"role": "user", "content": text}]))


def _client(*responses):
    client = MagicMock()
    client.messages.stream = MagicMock(side_effect=[_MockAsyncStream(r) for r in responses])
    return client


def _delete_then_stop():
    tool_use = SimpleNamespace(
        stop_reason="tool_use",
        content=[
            SimpleNamespace(
                type="tool_use",
                id="t1",
                name="delete_pod",
                input={"namespace": "team-alpha", "pod_name": "job-old-001"},
            )
        ],
    )
    final = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="Plan: ...")])
    return tool_use, final


async def _run(prompt: str, **kwargs):
    tool = MagicMock()
    tool.call.return_value = "deleted"
    confirm = AsyncMock(return_value=True)
    results: list[dict] = []
    client = _client(*_delete_then_stop())
    await run_agent_streaming(
        client=client,
        messages=[{"role": "user", "content": prompt}],
        system_prompt="test",
        tool_defs=[],
        tool_map={"delete_pod": tool},
        write_tools={"delete_pod"},
        on_confirm=confirm,
        on_tool_result=lambda r: results.append(r),
        **kwargs,
    )
    return tool, confirm, results, client


@pytest.mark.asyncio
@patch.dict("os.environ", {"PULSE_AGENT_HARNESS": "0"})
async def test_plan_turn_refuses_writes_before_confirmation():
    tool, confirm, results, client = await _run(PLAN_ONLY[0])

    tool.call.assert_not_called()
    confirm.assert_not_called()  # nobody is asked to approve a step they did not request
    assert results[0]["status"] == "denied" and results[0]["error_category"] == "plan_only"
    # The model is told to put the step in its plan, not just that it failed.
    second_call_messages = client.messages.stream.call_args_list[1].kwargs["messages"]
    tool_result = second_call_messages[-1]["content"][0]
    assert tool_result["type"] == "tool_result" and PLAN_ONLY_REFUSAL in str(tool_result["content"])


@pytest.mark.asyncio
@patch.dict("os.environ", {"PULSE_AGENT_HARNESS": "0"})
async def test_request_to_act_still_goes_through_confirmation(mock_k8s):
    tool, confirm, _, _ = await _run("Clean up the completed job pods in team-alpha and fix the quota.")

    confirm.assert_awaited_once()
    tool.call.assert_called_once()


@pytest.mark.asyncio
@patch.dict("os.environ", {"PULSE_AGENT_HARNESS": "0"})
async def test_generated_prompts_can_opt_out(mock_k8s):
    # Plan phases build their own prompts; their words are not a person's request.
    tool, confirm, _, _ = await _run(PLAN_ONLY[0], honor_plan_requests=False)

    confirm.assert_awaited_once()
    tool.call.assert_called_once()


def test_real_resource_context_prefix_is_stripped():
    from sre_agent.api.context import _build_context_prefix

    prefix = _build_context_prefix(
        {"context": {"kind": "Deployment", "namespace": "team-alpha", "name": "plan-runner"}}
    )
    assert prefix.startswith("[UI Context:")
    assert latest_user_text([{"role": "user", "content": prefix + "restart it"}]) == "restart it"
