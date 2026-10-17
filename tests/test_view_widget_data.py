"""create_dashboard runs each widget's live query before saving.

The structural check sees a widget's spec, not what it will show. Charts and
metric cards with a ``query`` are re-run by the UI on every refresh, so a
query that errors or matches nothing renders as an error box or "No data"
however good the spec looks.
"""

import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from sre_agent import view_draft
from sre_agent.prometheus import PrometheusConfigError
from sre_agent.view_tools import SIGNAL_PREFIX, create_dashboard


def _vector(*values):
    return {
        "status": "success",
        "data": {"resultType": "vector", "result": [{"metric": {}, "value": [0, v]} for v in values]},
    }


def _bad_query(message):
    return urllib.error.HTTPError(
        "http://prom/api/v1/query", 400, "Bad Request", {}, io.BytesIO(json.dumps({"error": message}).encode())
    )


class _Prometheus:
    """Answers by query text; records what was asked."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, endpoint, params=None, timeout=30):
        query = params["query"]
        self.calls.append(query)
        answer = self.answers[query]
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def prometheus():
    def install(answers):
        fake = _Prometheus(answers)
        patcher = patch("sre_agent.prometheus.prometheus_request", side_effect=fake)
        patcher.start()
        install.patchers.append(patcher)
        return fake

    install.patchers = []
    yield install
    for p in install.patchers:
        p.stop()


def _card(title, query):
    return {"kind": "metric_card", "title": title, "value": "", "query": query}


def test_finds_queries_in_nested_widgets():
    components = [
        {"kind": "grid", "title": "Overview", "items": [_card("CPU", "q_cpu"), _card("Mem", "q_mem")]},
        {
            "kind": "tabs",
            "tabs": [{"label": "Pods", "components": [{"kind": "chart", "title": "Restarts", "query": "q_r"}]}],
        },
        {"kind": "section", "title": "S", "components": [{"kind": "stat_card", "title": "Up", "query": "q_up"}]},
        {"kind": "data_table", "title": "No query", "rows": []},
    ]
    assert view_draft.widget_queries(components) == [
        ("CPU", "q_cpu"),
        ("Mem", "q_mem"),
        ("Restarts", "q_r"),
        ("Up", "q_up"),
    ]


def test_empty_failed_and_nan_queries_are_reported(prometheus):
    prometheus(
        {
            "good": _vector("4"),
            "typo": _vector(),
            "broken": _bad_query('parse error: unexpected ")"'),
            "ratio": _vector("NaN", "+Inf"),
        }
    )
    findings = view_draft.check_widget_data(
        [_card("Nodes", "good"), _card("Memory", "typo"), _card("Errors", "broken"), _card("Ratio", "ratio")]
    )
    assert len(findings) == 3
    assert any(f.startswith("No data for 'Memory'") for f in findings)
    assert any(f.startswith("Query failed for 'Errors'") and "parse error" in f for f in findings)
    assert any(f.startswith("No usable data for 'Ratio'") for f in findings)


def test_each_query_runs_once(prometheus):
    fake = prometheus({"up": _vector("1")})
    assert view_draft.check_widget_data([_card("A", "up"), _card("B", "up")]) == []
    assert fake.calls == ["up"]


@pytest.mark.parametrize(
    "failure",
    [PrometheusConfigError("no CA"), urllib.error.URLError("connection refused"), TimeoutError()],
)
def test_unreachable_prometheus_never_blocks(prometheus, failure):
    # A monitoring outage must not hold a save, even when another query came back empty.
    prometheus({"empty": _vector(), "down": failure})
    assert view_draft.check_widget_data([_card("A", "empty"), _card("B", "down")]) == []


def test_create_dashboard_holds_a_save_with_an_empty_query_once(prometheus):
    prometheus({"sum(rate(typo_total[5m]))": _vector(), "up": _vector("1")})
    components = [
        _card("Request rate", "sum(rate(typo_total[5m]))"),
        _card("Targets up", "up"),
        {"kind": "data_table", "title": "Pods", "columns": [{"id": "n", "header": "N"}], "rows": [{"n": "a"}]},
    ]
    token = view_draft.bind(components)
    try:
        first = create_dashboard.call({"title": "Prod"})
        assert first.startswith("Not saved yet") and "No data for 'Request rate'" in first
        assert SIGNAL_PREFIX not in first
        # The check holds a save back once; the second call always saves.
        second = create_dashboard.call({"title": "Prod", "exclude": "Request rate"})
        assert SIGNAL_PREFIX in second
    finally:
        view_draft.unbind(token)
