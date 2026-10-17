"""create_dashboard checks the turn's draft before saving, and the model sees the result.

Before, the quality engine ran only after the agent's turn ended and its
findings went to the UI, while the model was told "Here is your dashboard".
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from sre_agent import view_draft
from sre_agent.view_tools import SIGNAL_PREFIX, create_dashboard


@pytest.fixture(autouse=True)
def _queries_return_data():
    """These tests cover the structural check; live queries are covered in test_view_widget_data."""
    with patch.object(view_draft, "_run_query", return_value=("ok", "")):
        yield


def _chart(title, points=3, query=""):
    series = [{"name": "a", "data": [[i, i] for i in range(points)]}] if points else []
    spec = {"kind": "chart", "title": title, "series": series}
    if query:
        spec["query"] = query
    return spec


def _table(title, rows=2):
    return {
        "kind": "data_table",
        "title": title,
        "columns": [{"id": "name", "header": "Name"}],
        "rows": [{"name": f"pod-{i}"} for i in range(rows)],
    }


def _card(title):
    return {"kind": "metric_card", "title": title, "value": "42", "query": "up"}


def _call(**kwargs):
    return create_dashboard.call({"title": "Prod", **kwargs})


def _signal(text):
    assert SIGNAL_PREFIX in text, text
    return json.loads(text.split(SIGNAL_PREFIX, 1)[1])


class TestPrecheck:
    def test_empty_widget_blocks(self):
        blocking, _ = view_draft.precheck([_card("Pods"), _chart("CPU", points=0), _table("Pods table")])
        assert any("Empty widget 'CPU'" in b for b in blocking)

    def test_widget_count_is_advisory_not_blocking(self):
        blocking, advisory = view_draft.precheck([_card("Pods")])
        assert blocking == []
        assert any("at least" in a for a in advisory)

    def test_draft_is_not_mutated(self):
        spec = {"kind": "chart", "title": "CPU", "series": [{"label": "a", "data": [[1, 2]]}]}
        view_draft.precheck([spec])
        assert spec["series"][0] == {"label": "a", "data": [[1, 2]]}

    def test_exclusions_are_case_insensitive(self):
        widgets = [_card("Pods"), _chart("CPU")]
        assert view_draft.apply_exclusions(widgets, view_draft.parse_titles(" cpu | ")) == [widgets[0]]


class TestCreateDashboardWithDraft:
    def test_without_draft_saves_as_before(self):
        assert _signal(_call())["type"] == "view_spec"

    def test_broken_draft_is_held_once_then_saved(self):
        token = view_draft.bind([_card("Pods"), _chart("CPU", points=0), _table("Pods table")])
        try:
            first = _call()
            assert SIGNAL_PREFIX not in first
            assert "Not saved yet" in first and "Empty widget 'CPU'" in first
            # A second call always saves: the check can hold a save back at most once.
            assert _signal(_call())["type"] == "view_spec"
        finally:
            view_draft.unbind(token)

    def test_exclude_fixes_the_draft_and_reaches_the_save_signal(self):
        token = view_draft.bind([_card("Pods"), _chart("CPU", points=0), _table("Pods table")])
        try:
            sig = _signal(_call(exclude="CPU"))
            assert sig["exclude"] == ["cpu"]
        finally:
            view_draft.unbind(token)

    def test_advisory_notes_reach_the_model_on_save(self):
        token = view_draft.bind([_card("Pods")])
        try:
            text = _call()
            assert "Quality notes" in text and "at least" in text
            assert _signal(text)["type"] == "view_spec"
        finally:
            view_draft.unbind(token)


@patch.dict("os.environ", {"PULSE_AGENT_HARNESS": "0"})
@pytest.mark.asyncio
async def test_create_dashboard_sees_the_draft_inside_a_real_agent_turn():
    """The tool runs in a pool thread; the draft must reach it through the caller's context."""
    from sre_agent.agent import run_agent_streaming
    from tests.test_agent import _MockAsyncStream

    draft = [_card("Pods"), _chart("CPU", points=0), _table("Pods table")]
    call = SimpleNamespace(
        stop_reason="tool_use",
        content=[SimpleNamespace(type="tool_use", id="c1", name="create_dashboard", input={"title": "Prod"})],
    )
    done = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="ok")])
    client = MagicMock()
    client.messages.stream = MagicMock(side_effect=[_MockAsyncStream(call), _MockAsyncStream(done)])
    messages = [{"role": "user", "content": "build it"}]
    token = view_draft.bind(draft)
    try:
        await run_agent_streaming(
            client=client,
            messages=messages,
            system_prompt="t",
            tool_defs=[],
            tool_map={"create_dashboard": create_dashboard},
        )
    finally:
        view_draft.unbind(token)
    result = next(m["content"] for m in messages if m["role"] == "user" and isinstance(m["content"], list))[0]
    assert "Not saved yet" in result["content"]
