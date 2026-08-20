"""The inbox has to be a queue people finish, not a wall they scroll past.

Both behaviours here were measured on a live cluster: three items stuck in
agent_reviewing for 73 minutes against a five-minute threshold, and 40 of 76
open items more than 40 hours old.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from sre_agent.inbox import _UNTOUCHED_EXPIRY_HOURS, expire_untouched_items

REPO = "sre_agent.inbox.get_inbox_repo"


def _row(**over):
    base = {
        "id": "inb-1",
        "item_type": "task",
        "status": "triaged",
        "title": "Pod restarting",
        "summary": "",
        "severity": "warning",
        "priority_score": 5.0,
        "confidence": 0.9,
        "noise_score": 0.0,
        "namespace": "demo",
        "resources": "[]",
        "correlation_key": "crashloop:demo:Pod/web",
        "claimed_by": None,
        "pinned_by": "[]",
        "created_by": "system:monitor",
        "metadata": "{}",
        "created_at": 1,
        "updated_at": 1,
    }
    base.update(over)
    return base


@pytest.fixture
def repo():
    r = MagicMock()
    r.fetch_untouched_open_items.return_value = []
    with patch(REPO, return_value=r):
        yield r


def test_an_untouched_machine_raised_item_is_archived(repo):
    repo.fetch_untouched_open_items.return_value = [_row()]
    with patch("sre_agent.inbox._publish_event"):
        assert expire_untouched_items() == 1
    assert repo.update_status.call_args.args[1] == "archived"


def test_the_cutoff_is_48_hours(repo):
    """Measured: 40 of 76 open items were older than 40 hours."""
    assert _UNTOUCHED_EXPIRY_HOURS == 48
    with patch("sre_agent.inbox._publish_event"):
        expire_untouched_items()
    cutoff = repo.fetch_untouched_open_items.call_args.args[0]
    assert abs((int(time.time()) - cutoff) - 48 * 3600) < 5


def test_an_item_somebody_claimed_is_never_expired(repo):
    """Clearing noise is the point; tidying away somebody's work is not."""
    repo.fetch_untouched_open_items.return_value = [_row(claimed_by="alice")]
    with patch("sre_agent.inbox._publish_event"):
        assert expire_untouched_items() == 0
    assert not repo.update_status.called


def test_a_pinned_item_is_never_expired(repo):
    """pinned_by is a JSON list of users, not a name."""
    repo.fetch_untouched_open_items.return_value = [_row(pinned_by='["alice"]')]
    with patch("sre_agent.inbox._publish_event"):
        assert expire_untouched_items() == 0
    assert not repo.update_status.called


def test_a_task_a_person_created_is_never_expired(repo):
    """A human wrote it down on purpose. Age is not evidence it stopped mattering."""
    repo.fetch_untouched_open_items.return_value = [_row(created_by="alice")]
    with patch("sre_agent.inbox._publish_event"):
        assert expire_untouched_items() == 0
    assert not repo.update_status.called


def test_a_database_error_expires_nothing(repo):
    """Failing open: never archive work because a query broke."""
    repo.fetch_untouched_open_items.side_effect = RuntimeError("db down")
    assert expire_untouched_items() == 0
    assert not repo.update_status.called


def test_the_sweep_runs_on_the_scan_cycle_not_only_at_startup():
    """A guard that only runs at boot does not guard anything while running."""
    import inspect

    from sre_agent.inbox import run_generator_cycle

    source = inspect.getsource(run_generator_cycle)
    assert "sweep_stale_items()" in source
    assert "expire_untouched_items()" in source
