"""A proposal outlives the condition it was raised for, unless something says so.

``approve_fix`` already refuses a proposal whose finding has cleared on its
own -- but only at the moment a person clicks Approve. Until then it keeps
counting toward "N fixes waiting on you" in the inbox banner, asking for a
decision about a fix that no longer applies, with no way to answer it besides
clicking Approve and being told no.

Observed live: a crashloop proposal sat in `fix-history` for the better part
of an hour after the pod stopped restarting, approving it returned 409 every
time, and the banner never dropped to zero. ``expire_orphaned_proposals``
answers these the same way ``approve_fix`` would, once per scan, so nobody has
to find out the hard way that there is nothing left to fix.
"""

from __future__ import annotations

from typing import ClassVar
from unittest.mock import MagicMock, patch

from sre_agent.monitor.approvals import STALE_PROPOSAL_MESSAGE, expire_orphaned_proposals

MODULE = "sre_agent.monitor.approvals"


def _repo(proposals: list[dict]) -> MagicMock:
    repo = MagicMock()
    repo.fetch_proposed_actions.return_value = proposals
    repo.expire_proposal.return_value = True
    return repo


def _live(*finding_ids: str):
    """Stand in for ``_current_finding``: these ids are still being reported."""
    live = set(finding_ids)
    return lambda finding_id: {"id": finding_id} if finding_id in live else None


def test_a_proposal_whose_finding_is_gone_gets_expired():
    """The whole bug in one assertion: nobody has to click Approve to hear no."""
    repo = _repo([{"id": "a-1", "finding_id": "f-1"}])
    with (
        patch("sre_agent.repositories.get_monitor_repo", return_value=repo),
        patch(f"{MODULE}._current_finding", side_effect=_live()),
    ):
        assert expire_orphaned_proposals() == 1
    repo.expire_proposal.assert_called_once_with("a-1", STALE_PROPOSAL_MESSAGE)


def test_a_proposal_whose_finding_is_still_live_is_left_alone():
    """The condition is still happening -- there is still something to approve."""
    repo = _repo([{"id": "a-1", "finding_id": "f-1"}])
    with (
        patch("sre_agent.repositories.get_monitor_repo", return_value=repo),
        patch(f"{MODULE}._current_finding", side_effect=_live("f-1")),
    ):
        assert expire_orphaned_proposals() == 0
    repo.expire_proposal.assert_not_called()


def test_only_the_gone_ones_are_answered():
    """A mix of live and cleared conditions -- only the cleared one moves."""
    repo = _repo([{"id": "a-live", "finding_id": "f-live"}, {"id": "a-gone", "finding_id": "f-gone"}])
    with (
        patch("sre_agent.repositories.get_monitor_repo", return_value=repo),
        patch(f"{MODULE}._current_finding", side_effect=_live("f-live")),
    ):
        assert expire_orphaned_proposals() == 1
    repo.expire_proposal.assert_called_once_with("a-gone", STALE_PROPOSAL_MESSAGE)


def test_a_proposal_with_no_finding_id_is_skipped_rather_than_expired():
    """Nothing to look up means nothing to conclude -- refuse to guess."""
    repo = _repo([{"id": "a-1", "finding_id": ""}])
    with (
        patch("sre_agent.repositories.get_monitor_repo", return_value=repo),
        patch(f"{MODULE}._current_finding", side_effect=_live()),
    ):
        assert expire_orphaned_proposals() == 0
    repo.expire_proposal.assert_not_called()


def test_losing_the_race_to_a_click_does_not_double_count():
    """If a person approved it in the same instant, the UPDATE finds nothing
    left to change -- the count only reflects rows this sweep actually moved."""
    repo = _repo([{"id": "a-1", "finding_id": "f-1"}])
    repo.expire_proposal.return_value = False
    with (
        patch("sre_agent.repositories.get_monitor_repo", return_value=repo),
        patch(f"{MODULE}._current_finding", side_effect=_live()),
    ):
        assert expire_orphaned_proposals() == 0


# ── against the real database, because a mock cannot see a status column ──


def test_the_row_actually_flips_to_expired_in_postgres():
    """Mocks would answer whatever they are told; only a real row proves the
    UPDATE runs, the WHERE clause matches, and status ends up 'expired'.

    The action id is unique per run: save_action's ON CONFLICT upsert does not
    refresh finding_id, so reusing a fixed id across repeated runs against a
    persistent test database would silently pin it to whichever finding_id
    the very first run ever inserted.
    """
    import uuid

    from sre_agent.db import get_database
    from sre_agent.monitor.actions import save_action
    from sre_agent.monitor.findings import _make_finding

    action_id = f"a-expiry-test-{uuid.uuid4().hex[:8]}"
    resources = [{"kind": "Pod", "name": "klusterlet-expiry-test", "namespace": "open-cluster-management-agent"}]
    finding = _make_finding("warning", "crashloop", "Pod restarting (3x)", "", resources)
    save_action(
        {
            "id": action_id,
            "findingId": finding["id"],
            "status": "proposed",
            "tool": "",
            "reasoning": "proposed while nobody was connected to approve it",
        },
        category="crashloop",
        resources=resources,
        finding=finding,
    )

    class _FakeMonitor:
        _last_findings: ClassVar[dict] = {}  # the condition has cleared -- nothing is live

    with patch("sre_agent.monitor.cluster_monitor._cluster_monitor", _FakeMonitor()):
        assert expire_orphaned_proposals() >= 1

    db = get_database()
    row = db.fetchone("SELECT status, error FROM actions WHERE id = ?", (action_id,))
    assert row["status"] == "expired"
    assert row["error"] == STALE_PROPOSAL_MESSAGE


def test_a_still_live_finding_is_not_touched_by_the_real_sweep():
    import uuid

    from sre_agent.db import get_database
    from sre_agent.monitor.actions import save_action
    from sre_agent.monitor.findings import _make_finding

    action_id = f"a-expiry-test-{uuid.uuid4().hex[:8]}"
    resources = [{"kind": "Pod", "name": "klusterlet-still-live", "namespace": "open-cluster-management-agent"}]
    finding = _make_finding("warning", "crashloop", "Pod restarting (5x)", "", resources)
    save_action(
        {
            "id": action_id,
            "findingId": finding["id"],
            "status": "proposed",
            "tool": "",
            "reasoning": "proposed while nobody was connected to approve it",
        },
        category="crashloop",
        resources=resources,
        finding=finding,
    )

    class _FakeMonitor:
        _last_findings: ClassVar[dict] = {"crashloop:key": finding}  # the condition is still firing

    with patch("sre_agent.monitor.cluster_monitor._cluster_monitor", _FakeMonitor()):
        expire_orphaned_proposals()

    db = get_database()
    row = db.fetchone("SELECT status FROM actions WHERE id = ?", (action_id,))
    assert row["status"] == "proposed", "still happening -- there is still something to approve"
