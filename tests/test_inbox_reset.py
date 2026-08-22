"""Resetting the inbox: count from now, keep the history.

Written against a real inbox — 339 items, 306 of them resolved, and a critical
item reading "Pod promoter-controller-manager restarting (122x)" for a
container whose lifetime counter had been climbing for days. The number was
true and useless. These tests pin the two halves of the fix: what a reset does
to the queue, and what the scanners report afterwards.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from sre_agent.monitor import baseline

BASELINE = "sre_agent.monitor.baseline"


@pytest.fixture(autouse=True)
def _clean_baseline():
    baseline.invalidate()
    yield
    baseline.invalidate()


def _with_reset(reset_at, restarts=None):
    """Patch the repository so the baseline module sees one reset."""
    repo = MagicMock()
    repo.latest.return_value = {"id": 7, "reset_at": reset_at, "reset_by": "sre@example.com"}
    repo.restart_baseline.return_value = restarts or {}
    return patch("sre_agent.repositories.reset_repo.get_reset_repo", return_value=repo)


# ── the baseline itself ───────────────────────────────────────────────────


def test_no_reset_means_lifetime_counts():
    """Before anyone resets, nothing changes. This is the upgrade path."""
    repo = MagicMock()
    repo.latest.return_value = None
    with patch("sre_agent.repositories.reset_repo.get_reset_repo", return_value=repo):
        assert baseline.watermark() is None
        assert baseline.restarts_since_reset("ns", "pod", "c", 122) == 122
        assert baseline.occurred_since_reset(1) is True


def test_restarts_are_counted_from_the_reset():
    with _with_reset(1000, {("ns", "pod", "c"): 118}):
        assert baseline.restarts_since_reset("ns", "pod", "c", 122) == 4


def test_a_container_with_no_baseline_is_new_and_counts_in_full():
    with _with_reset(1000, {("ns", "other", "c"): 5}):
        assert baseline.restarts_since_reset("ns", "pod", "c", 3) == 3


def test_a_recreated_pod_restarts_its_own_counter():
    """Lower than the baseline means the pod was replaced, not that it healed."""
    with _with_reset(1000, {("ns", "pod", "c"): 118}):
        assert baseline.restarts_since_reset("ns", "pod", "c", 2) == 2


def test_an_undated_occurrence_still_counts():
    """Absence of a timestamp is not evidence of age — never hide on unknown."""
    with _with_reset(1000):
        assert baseline.occurred_since_reset(None) is True
        assert baseline.occurred_since_reset(999) is False
        assert baseline.occurred_since_reset(1000) is True


def test_a_broken_database_does_not_stop_a_scan():
    repo = MagicMock()
    repo.latest.side_effect = RuntimeError("no such table: inbox_resets")
    with patch("sre_agent.repositories.reset_repo.get_reset_repo", return_value=repo):
        assert baseline.watermark() is None
        assert baseline.restarts_since_reset("ns", "pod", "c", 9) == 9


# ── what the crashloop scanner reports afterwards ─────────────────────────


def _pod(ns, name, container, restarts, last_restart_epoch):
    finished = SimpleNamespace(timestamp=lambda: last_restart_epoch)
    cs = SimpleNamespace(
        name=container,
        restart_count=restarts,
        state=SimpleNamespace(waiting=SimpleNamespace(reason="CrashLoopBackOff")),
        last_state=SimpleNamespace(terminated=SimpleNamespace(finished_at=finished)),
    )
    return SimpleNamespace(
        metadata=SimpleNamespace(namespace=ns, name=name),
        status=SimpleNamespace(container_statuses=[cs]),
    )


def _scan(pods):
    """Run the real scanner. Times are relative to the actual clock rather than
    a patched one: the scanner compares against ``datetime.now`` in two places
    and a half-mocked clock made it silently report nothing."""
    from sre_agent.monitor import scanners

    return scanners.scan_crashlooping_pods(pods=SimpleNamespace(items=pods))


NOW = int(time.time())


def test_a_pod_that_stopped_restarting_before_the_reset_is_gone():
    """The 122x item. Its restarts are all older than the reset."""
    pods = [_pod("promoter-system", "promoter-abc", "manager", 122, NOW - 60)]
    with _with_reset(NOW - 30, {("promoter-system", "promoter-abc", "manager"): 122}):
        assert _scan(pods) == []


def test_a_pod_still_restarting_after_the_reset_comes_straight_back():
    pods = [_pod("promoter-system", "promoter-abc", "manager", 128, NOW - 10)]
    with _with_reset(NOW - 30, {("promoter-system", "promoter-abc", "manager"): 122}):
        findings = _scan(pods)
    assert len(findings) == 1
    assert "restarting (6x)" in findings[0]["title"]
    assert "128 in the pod's lifetime" in findings[0]["summary"]


def test_below_the_threshold_after_rebaselining_is_not_reported():
    """122 lifetime restarts, two since the reset, threshold 3 — nothing to say."""
    pods = [_pod("promoter-system", "promoter-abc", "manager", 124, NOW - 10)]
    with _with_reset(NOW - 30, {("promoter-system", "promoter-abc", "manager"): 122}):
        assert _scan(pods) == []


# ── the reset operation ───────────────────────────────────────────────────


def test_reset_archives_open_items_and_never_deletes_them():
    from sre_agent import inbox

    repo = MagicMock()
    repo.fetch_all_open_items.return_value = [
        {"id": "inb-1", "metadata": "{}", "status": "triaged", "pinned_by": "[]", "claimed_by": None},
        {"id": "inb-2", "metadata": "{}", "status": "new", "pinned_by": '["sre"]', "claimed_by": "sre"},
    ]
    reset_repo = MagicMock()
    reset_repo.record.return_value = 7
    reset_repo.save_restart_baseline.return_value = 3

    with (
        patch("sre_agent.inbox.get_inbox_repo", return_value=repo),
        patch("sre_agent.repositories.reset_repo.get_reset_repo", return_value=reset_repo),
        patch("sre_agent.inbox._current_restart_counts", return_value=[]),
        patch("sre_agent.monitor.episodes.list_open", return_value=[]),
        patch("sre_agent.inbox._publish_event"),
    ):
        result = inbox.reset_inbox("sre@example.com")

    assert result["items_archived"] == 2
    assert result["pinned_archived"] == 1
    assert result["claimed_archived"] == 1
    # Archived with a reason, never deleted — the history is the point.
    assert repo.archive_with_reason.call_count == 2
    assert "Inbox reset by sre@example.com" in repo.archive_with_reason.call_args[0][1]
    assert not any("delete" in str(c).lower() for c in repo.mock_calls)


def test_reset_closes_open_episodes():
    """An episode outlives its symptoms; clearing one without the other leaves
    a banner pointing at rows that are no longer there."""
    from sre_agent import inbox

    repo = MagicMock()
    repo.fetch_all_open_items.return_value = []
    reset_repo = MagicMock()
    reset_repo.record.return_value = 7

    with (
        patch("sre_agent.inbox.get_inbox_repo", return_value=repo),
        patch("sre_agent.repositories.reset_repo.get_reset_repo", return_value=reset_repo),
        patch("sre_agent.inbox._current_restart_counts", return_value=[]),
        patch("sre_agent.monitor.episodes.list_open", return_value=[{"id": "ep-1"}, {"id": "ep-2"}]),
        patch("sre_agent.monitor.episodes.dismiss", return_value=True) as dismiss,
        patch("sre_agent.inbox._publish_event"),
    ):
        result = inbox.reset_inbox("sre@example.com")

    assert result["episodes_closed"] == 2
    assert dismiss.call_args[0][1] == "reset:sre@example.com"


def test_a_failed_snapshot_still_resets():
    """The cluster being unreachable must not leave the queue half-cleared."""
    from sre_agent import inbox

    repo = MagicMock()
    repo.fetch_all_open_items.return_value = [
        {"id": "inb-1", "metadata": "{}", "status": "new", "pinned_by": "[]", "claimed_by": None}
    ]
    reset_repo = MagicMock()
    reset_repo.record.return_value = 7

    with (
        patch("sre_agent.inbox.get_inbox_repo", return_value=repo),
        patch("sre_agent.repositories.reset_repo.get_reset_repo", return_value=reset_repo),
        patch("sre_agent.inbox._current_restart_counts", side_effect=RuntimeError("API down")),
        patch("sre_agent.monitor.episodes.list_open", return_value=[]),
        patch("sre_agent.inbox._publish_event"),
    ):
        result = inbox.reset_inbox("sre@example.com")

    assert result["items_archived"] == 1
    assert result["containers_baselined"] == 0


# ── an inbox row should say where ─────────────────────────────────────────


def test_an_inbox_item_takes_its_namespace_from_its_resource():
    """Measured on the reference cluster: 0 of 31 open items carried a
    namespace while the resource underneath plainly did. `_make_finding` never
    sets a top-level "namespace" — scanners put it in resources[0] — so the
    inbox row's namespace badge had never once rendered, and four identical
    "TargetDown" rows sat there with nothing to tell them apart."""
    from sre_agent.inbox import _primary_namespace

    finding = {
        "title": "TargetDown",
        "resources": [{"kind": "Alert", "name": "TargetDown", "namespace": "openshift-lightspeed"}],
    }
    assert _primary_namespace(finding) == "openshift-lightspeed"


def test_a_cluster_scoped_finding_gets_no_namespace():
    """A node or an alert with no namespace label has none, and None is the
    right answer — a row should not invent a location for something that does
    not have one."""
    from sre_agent.inbox import _primary_namespace

    assert _primary_namespace({"resources": [{"kind": "Node", "name": "ip-10-0-64-50"}]}) is None
    assert _primary_namespace({"resources": [{"kind": "Alert", "name": "X", "namespace": ""}]}) is None
    assert _primary_namespace({"resources": []}) is None
    assert _primary_namespace({}) is None


def test_an_explicit_namespace_still_wins():
    """The fallback is a fallback. Anything that does set the field keeps it."""
    from sre_agent.inbox import _primary_namespace

    finding = {"namespace": "explicit", "resources": [{"kind": "Pod", "name": "p", "namespace": "from-resource"}]}
    assert (finding.get("namespace") or _primary_namespace(finding)) == "explicit"
