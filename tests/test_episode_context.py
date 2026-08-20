"""Recurrence and "what changed" — the two questions after "what is broken".

Both are drawn from a real outage. The control plane degraded six times in one
day, every two hours at the same minute past, each worse than the last. "etcd
lost its leader" is a page; "sixth time today, every two hours, escalating" is
a diagnosis — and a human found that by reading graphs after the fact.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sre_agent.monitor import episodes as ep
from sre_agent.monitor.episodes import _CHANGE_LOOKBACK_SECONDS, changes_around, recurrence_summary

MODULE = "sre_agent.monitor.episodes"
HOUR = 3600


def _episode(eid, started_at, recurrence_of=None):
    return {"id": eid, "started_at": started_at, "recurrence_of": recurrence_of, "status": "open"}


@pytest.fixture
def repo():
    r = MagicMock()
    with patch(f"{MODULE}._repo", return_value=r):
        yield r


# ── recurrence ────────────────────────────────────────────────────────────


def test_a_first_occurrence_is_not_recurring(repo):
    repo.recurrence_chain.return_value = []
    assert recurrence_summary("ep-1") == {"occurrences": 1, "recurring": False}


def test_occurrences_are_counted_including_this_one(repo):
    now = 1_786_000_000
    repo.recurrence_chain.return_value = [_episode("ep-0", now - 2 * HOUR), _episode("ep-x", now - 4 * HOUR)]
    repo.get.return_value = _episode("ep-1", now)
    assert recurrence_summary("ep-1")["occurrences"] == 3


def test_a_regular_cadence_is_named(repo):
    """Every two hours is the clue. Reporting it is the point of the feature."""
    now = 1_786_000_000
    repo.recurrence_chain.return_value = [
        _episode("e5", now - 2 * HOUR),
        _episode("e4", now - 4 * HOUR),
        _episode("e3", now - 6 * HOUR),
    ]
    repo.get.return_value = _episode("e6", now)
    assert recurrence_summary("e6")["interval_seconds"] == 2 * HOUR


def test_an_irregular_return_reports_no_cadence(repo):
    """Random recurrence is a different problem; do not invent a pattern."""
    now = 1_786_000_000
    repo.recurrence_chain.return_value = [
        _episode("e3", now - 1 * HOUR),
        _episode("e2", now - 9 * HOUR),
        _episode("e1", now - 10 * HOUR),
    ]
    repo.get.return_value = _episode("e4", now)
    assert "interval_seconds" not in recurrence_summary("e4")


def test_the_window_spans_first_to_latest(repo):
    now = 1_786_000_000
    repo.recurrence_chain.return_value = [_episode("e1", now - 10 * HOUR)]
    repo.get.return_value = _episode("e2", now)
    assert recurrence_summary("e2")["window_seconds"] == 10 * HOUR


def test_prior_episodes_are_linked_so_they_can_be_opened(repo):
    now = 1_786_000_000
    repo.recurrence_chain.return_value = [_episode("e1", now - HOUR)]
    repo.get.return_value = _episode("e2", now)
    assert recurrence_summary("e2")["prior_episode_ids"] == ["e1"]


# ── what changed ──────────────────────────────────────────────────────────


def _change(key, title, created_at, namespace="demo"):
    return {
        "id": "inb-x",
        "title": title,
        "namespace": namespace,
        "correlation_key": key,
        "created_at": created_at,
    }


def test_changes_before_the_episode_are_reported_oldest_first(repo):
    started = 1_786_000_000
    repo.get.return_value = _episode("ep-1", started)
    rows = [
        _change("audit_deployment:mce:Deployment/ocm", "ocm-controller rolled out", started - 300),
        _change("audit_config:mce:ConfigMap/settings", "settings changed", started - 900),
    ]
    with patch("sre_agent.repositories.inbox_repo.get_inbox_repo") as g:
        g.return_value.fetch_items_by_category_window.return_value = rows
        changes = changes_around("ep-1")
    assert [c["title"] for c in changes] == ["settings changed", "ocm-controller rolled out"]


def test_each_change_says_how_long_before_it_happened(repo):
    started = 1_786_000_000
    repo.get.return_value = _episode("ep-1", started)
    with patch("sre_agent.repositories.inbox_repo.get_inbox_repo") as g:
        g.return_value.fetch_items_by_category_window.return_value = [
            _change("audit_rbac:demo:ClusterRoleBinding/x", "cluster-admin granted", started - 120)
        ]
        change = changes_around("ep-1")[0]
    assert change["seconds_before"] == 120
    assert change["category"] == "audit_rbac"


def test_only_the_window_before_the_episode_is_searched(repo):
    """Widen it and 'what changed' becomes 'everything that ever changed'."""
    started = 1_786_000_000
    repo.get.return_value = _episode("ep-1", started)
    with patch("sre_agent.repositories.inbox_repo.get_inbox_repo") as g:
        g.return_value.fetch_items_by_category_window.return_value = []
        changes_around("ep-1")
        args = g.return_value.fetch_items_by_category_window.call_args.args
    assert args[1] == started - _CHANGE_LOOKBACK_SECONDS
    assert args[2] == started
    assert set(args[0]) == {"audit_config", "audit_rbac", "audit_deployment"}


def test_an_unknown_episode_reports_no_changes(repo):
    repo.get.return_value = None
    assert changes_around("ep-missing") == []


def test_a_database_error_reports_no_changes_rather_than_raising(repo):
    """The episode is still worth showing without its change history."""
    repo.get.return_value = _episode("ep-1", 1_786_000_000)
    with patch("sre_agent.repositories.inbox_repo.get_inbox_repo", side_effect=RuntimeError("db down")):
        assert changes_around("ep-1") == []


# ── the investigation that already ran ────────────────────────────────────
# Causes are eligible for automatic investigation, so by the time an operator
# opens the card the work has usually been attempted — 22 attempts on a live
# cluster, all failed. Offering a fresh "ask the AI" without showing that
# gives two routes to the same call and implies nothing was tried.


def _investigation(**over):
    base = {
        "id": "inv-1",
        "status": "completed",
        "summary": "etcd lost quorum briefly",
        "suspected_cause": "peer latency",
        "recommended_fix": "check the network path",
        "confidence": 0.8,
        "error": None,
        "timestamp": 1786000000000,
    }
    base.update(over)
    return base


def _db_returning(row):
    db = MagicMock()
    db.fetchone.return_value = row
    return patch("sre_agent.db.get_database", return_value=db)


def test_the_investigation_already_run_is_returned(repo):
    repo.get.return_value = {"id": "ep-1", "cause_finding_id": "f-1"}
    with _db_returning(_investigation()):
        found = ep.investigation_for("ep-1")
    assert found["suspected_cause"] == "peer latency"
    assert found["failed"] is False


def test_a_failed_investigation_is_shown_not_hidden(repo):
    """An empty panel reads as 'nothing worth investigating' — the wrong conclusion."""
    repo.get.return_value = {"id": "ep-1", "cause_finding_id": "f-1"}
    with _db_returning(_investigation(status="failed", error="Connection error.", summary=None)):
        found = ep.investigation_for("ep-1")
    assert found["failed"] is True
    assert found["error"] == "Connection error."


def test_an_episode_with_no_investigation_returns_none(repo):
    repo.get.return_value = {"id": "ep-1", "cause_finding_id": "f-1"}
    with _db_returning(None):
        assert ep.investigation_for("ep-1") is None


def test_an_episode_with_no_cause_finding_returns_none(repo):
    repo.get.return_value = {"id": "ep-1", "cause_finding_id": ""}
    assert ep.investigation_for("ep-1") is None


def test_a_database_error_does_not_take_the_episode_down(repo):
    repo.get.return_value = {"id": "ep-1", "cause_finding_id": "f-1"}
    with patch("sre_agent.db.get_database", side_effect=RuntimeError("db down")):
        assert ep.investigation_for("ep-1") is None


def test_the_newest_investigation_wins(repo):
    """A cause re-investigated later should show the latest attempt."""
    import inspect

    source = inspect.getsource(ep.investigation_for)
    assert "ORDER BY timestamp DESC" in source
    assert "LIMIT 1" in source
