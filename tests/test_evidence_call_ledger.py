"""Evidence cites the tool call it came from, and grounding checks the citation
against a ledger recorded as each call ran.

Before this, grounding matched an evidence item's free-text ``source`` against
the names of tools the run had called. A call that errored still counted as
read, and two calls to the same tool could not be told apart.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from sre_agent.evidence import CallLedger, Evidence, derive_confidence, ground_evidence, parse_evidence


def _ledger(*calls: tuple[str, str]) -> CallLedger:
    ledger = CallLedger()
    for i, (tool, status) in enumerate(calls, start=1):
        ledger.record({"tool_name": tool, "status": status, "call_ref": f"c{i}", "input": {"namespace": "shop"}})
    return ledger


def test_a_cited_call_that_returned_data_sources_the_item():
    ledger = _ledger(("get_pod_logs", "success"))
    [item] = ground_evidence([Evidence(observation="DB_HOST unset", call_ref="c1", source="logs")], ledger)
    assert item.source == "get_pod_logs"
    assert item.call_ref == "c1"
    assert item.call_input == "namespace=shop"
    assert item.unverified_source == ""


def test_a_cited_call_that_failed_counts_as_unread():
    ledger = _ledger(("get_prometheus_query", "error"))
    [item] = ground_evidence([Evidence(observation="error rate 40%", call_ref="c1", source="prometheus")], ledger)
    assert item.source == ""
    assert "c1 error" in item.unverified_source


def test_a_citation_to_a_call_that_never_ran_is_unsourced():
    ledger = _ledger(("get_events", "success"))
    [item] = ground_evidence([Evidence(observation="x", call_ref="c9", source="get_events")], ledger)
    assert item.source == ""
    assert "no call c9" in item.unverified_source


def test_an_uncited_source_only_matches_tools_that_returned_data():
    """Name matching still works without a citation, but an errored tool no longer counts."""
    ledger = _ledger(("get_events", "success"), ("get_prometheus_query", "error"))
    events, prom = ground_evidence(
        [Evidence(observation="a", source="events"), Evidence(observation="b", source="prometheus")], ledger
    )
    assert events.source == "events"
    assert prom.source == "" and prom.unverified_source == "prometheus"


def test_citations_are_read_loosely():
    ledger = _ledger(("get_events", "success"), ("get_pod_logs", "success"), ("describe_pod", "success"))
    assert ledger.get("C3").tool == "describe_pod"
    assert ledger.get("call c2").tool == "get_pod_logs"
    assert ledger.get("diagnose.c1").tool == "get_events"
    assert ledger.get("") is None


def test_prompted_call_key_is_parsed():
    [item] = parse_evidence([{"observation": "x", "call": "c2"}])
    assert item.call_ref == "c2"


def test_a_failed_query_no_longer_raises_confidence():
    claimed = [
        Evidence(observation=f"s{i}", source="get_prometheus_query", call_ref=f"c{i + 1}", confidence=0.9)
        for i in range(3)
    ]
    all_ok = _ledger(*[("get_prometheus_query", "success")] * 3)
    all_failed = _ledger(*[("get_prometheus_query", "error")] * 3)
    assert derive_confidence(ground_evidence(claimed, all_failed), 0.95) < derive_confidence(
        ground_evidence(claimed, all_ok), 0.95
    )


def test_an_empty_ledger_changes_nothing():
    evidence = [Evidence(observation="x", source="prometheus", call_ref="c1")]
    assert ground_evidence(evidence, CallLedger()) == evidence


# ---------------------------------------------------------------------------
# The agent loop labels calls only when asked
# ---------------------------------------------------------------------------


class _Stream:
    def __init__(self, blocks, stop):
        self._msg = SimpleNamespace(content=blocks, stop_reason=stop, usage=None)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        async def gen():
            if False:
                yield None

        return gen()

    async def get_final_message(self):
        return self._msg


class _Client:
    def __init__(self):
        self.requests: list[dict] = []
        self.messages = SimpleNamespace(stream=self._stream)

    def _stream(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs.get("messages"), default=str)))
        if len(self.requests) == 1:
            return _Stream(
                [
                    SimpleNamespace(type="tool_use", id="tu_a", name="get_events", input={"namespace": "shop"}),
                    SimpleNamespace(type="tool_use", id="tu_b", name="broken_tool", input={}),
                ],
                "tool_use",
            )
        return _Stream([SimpleNamespace(type="text", text="done")], "end_turn")


def _run(label: bool):
    from unittest.mock import MagicMock

    from sre_agent.agent import run_agent_streaming

    get_events = MagicMock()
    get_events.call.return_value = "Warning BackOff pod/api"
    broken_tool = MagicMock()
    broken_tool.call.side_effect = RuntimeError("boom")

    client = _Client()
    seen: list[dict] = []
    asyncio.run(
        run_agent_streaming(
            client=client,
            messages=[{"role": "user", "content": "investigate"}],
            system_prompt="s",
            tool_defs=[],
            tool_map={"get_events": get_events, "broken_tool": broken_tool},
            write_tools=set(),
            on_tool_result=seen.append,
            honor_plan_requests=False,
            label_tool_calls=label,
        )
    )
    results = [b for b in client.requests[1][-1]["content"] if b.get("type") == "tool_result"]
    return results, seen


def test_labelled_results_carry_their_ref_and_metadata_matches(monkeypatch):
    results, seen = _run(label=True)
    by_id = {r["tool_use_id"]: str(r["content"]) for r in results}
    assert by_id["tu_a"].startswith("[call c1: get_events]")
    assert by_id["tu_b"].startswith("[call c2: broken_tool]")
    meta = {m["tool_use_id"]: m for m in seen}
    assert meta["tu_a"]["call_ref"] == "c1" and meta["tu_a"]["status"] == "success"
    assert meta["tu_b"]["call_ref"] == "c2" and meta["tu_b"]["status"] != "success"

    ledger = CallLedger()
    for m in seen:
        ledger.record(m)
    assert ledger.tools_read == ["get_events"]
    assert ledger.get("c1").input_summary == "namespace=shop"


def test_results_are_unlabelled_by_default():
    results, seen = _run(label=False)
    assert not any("[call " in str(r["content"]) for r in results)
    # The metadata still says which call is which.
    assert {m["call_ref"] for m in seen} == {"c1", "c2"}


def test_evidence_view_names_the_call_and_what_was_not_read():
    from sre_agent.view_executor import _evidence_items

    items = _evidence_items(
        {
            "evidence_detail": [
                {"observation": "a", "source": "get_events", "call_ref": "c1", "call_input": "namespace=shop"},
                {"observation": "b", "source": "", "unverified_source": "get_prometheus_query (c2 error)"},
            ]
        }
    )
    assert items[0]["detail"].endswith("source: get_events(namespace=shop)")
    assert items[1]["detail"].endswith("cites get_prometheus_query (c2 error), not read")
