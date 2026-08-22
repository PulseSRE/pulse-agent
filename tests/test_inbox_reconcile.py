"""A queue that never forgets what recovered.

Resolution rides on ``ClusterMonitor._last_findings``: present last scan, gone
this scan, raise a resolution event. But that dict is in-memory and starts
empty in every process, so anything that recovered while the agent was
restarting was never in it and can never become stale. The item stays open,
critical and wrong until it is archived 48 hours later.

Measured on the reference cluster: of the open items that could be checked
against live cluster state, **seven of seven were already resolved** — a node
listed critical/NotReady had been Ready for six and a half hours, and four
Deployments listed "degraded (0/1)" were all 1/1. Two thirds of the queue had
not been re-examined in over six hours. An SRE working that queue top-down
investigates healthy infrastructure.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sre_agent.inbox import reconcile_open_items


def _row(item_id: str, key: str, title: str = "t", severity: str = "critical") -> dict:
    return {"id": item_id, "correlation_key": key, "title": title, "severity": severity}


def _repo_with(rows: list[dict]) -> MagicMock:
    repo = MagicMock()
    repo.fetch_open_machine_items.return_value = rows
    return repo


def test_an_item_the_cluster_no_longer_reports_is_resolved():
    repo = _repo_with([_row("i-1", "nodes::Node/ip-10-0-64-50")])
    with (
        patch("sre_agent.inbox.get_inbox_repo", return_value=repo),
        patch("sre_agent.inbox._publish_event"),
    ):
        assert reconcile_open_items({"crashloop:prod:Pod/api"}) == 1
    repo.resolve_item.assert_called_once()
    assert repo.resolve_item.call_args[0][0] == "i-1"


def test_an_item_the_cluster_still_reports_is_left_alone():
    key = "workloads:open-cluster-management:Deployment/klusterlet"
    repo = _repo_with([_row("i-1", key)])
    with (
        patch("sre_agent.inbox.get_inbox_repo", return_value=repo),
        patch("sre_agent.inbox._publish_event"),
    ):
        assert reconcile_open_items({key}) == 0
    repo.resolve_item.assert_not_called()


def test_a_scan_that_found_nothing_resolves_nothing():
    """The guard that matters most.

    A scan returning zero findings is far more likely to be broken — a lost
    API connection, a scanner that threw — than a cluster that became perfect
    between two cycles. Emptying the operator's queue on that reading is the
    worse failure by a distance.
    """
    repo = _repo_with([_row("i-1", "a"), _row("i-2", "b"), _row("i-3", "c")])
    with (
        patch("sre_agent.inbox.get_inbox_repo", return_value=repo),
        patch("sre_agent.inbox._publish_event"),
    ):
        assert reconcile_open_items(set()) == 0
    repo.resolve_item.assert_not_called()


def test_it_says_why_the_item_was_resolved():
    """ "Resolved" with no reason is indistinguishable from somebody closing it."""
    repo = _repo_with([_row("i-1", "gone")])
    with (
        patch("sre_agent.inbox.get_inbox_repo", return_value=repo),
        patch("sre_agent.inbox._publish_event"),
    ):
        reconcile_open_items({"still-here"})
    metadata = repo.resolve_item.call_args[0][2]
    assert "no longer reported" in metadata["resolved_reason"]


def test_it_only_asks_for_items_nobody_took_ownership_of():
    """The narrowing lives in the query, so assert the query is the one used."""
    repo = _repo_with([])
    with (
        patch("sre_agent.inbox.get_inbox_repo", return_value=repo),
        patch("sre_agent.inbox._publish_event"),
    ):
        reconcile_open_items({"x"})
    repo.fetch_open_machine_items.assert_called_once()


def test_the_query_excludes_claimed_pinned_and_human_created_items():
    """Reading the SQL, because that is where the restraint actually is.

    An item a person claimed or pinned is theirs; one somebody created by hand
    was never tied to a scanner finding and has no correlation key to check.
    """
    import inspect

    from sre_agent.repositories.inbox_repo import InboxRepository

    sql = inspect.getsource(InboxRepository.fetch_open_machine_items)
    assert "claimed_by IS NULL" in sql
    assert "pinned_by IS NULL" in sql
    assert "created_by = 'system:monitor'" in sql
    assert "correlation_key IS NOT NULL" in sql
