"""A timeout is not a rejection — and waiting for one must not stall the scan.

The monitor waited (originally 120s, then 900s) for a human to approve a
proposed fix. Two problems shipped in sequence: first, a timeout was recorded
as "Rejected by user or approval timed out" — two different facts written down
as one; then the fix for that raised the wait to 900s while leaving it inline
in the scan loop, so a dashboard tab left open (the common case: 281 of 368
proposals expired unanswered) stalled scanning, fix verification, and inbox
bridging for 15 minutes per proposal. The advertised escape hatch,
PULSE_AGENT_APPROVAL_TIMEOUT, was also a no-op because the flat->nested config
sync never learned the new field.

These tests exercise the real code paths — the earlier version of this file
tested a hand-copied mirror of the branch, which stayed green when the
production code regressed.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sre_agent.config import _reset_settings, get_settings
from sre_agent.monitor.cluster_monitor import ClusterMonitor, _proposal_outcome


class TestApprovalTimeoutConfig:
    def test_default_is_longer_than_a_scan_interval(self):
        """A window shorter than the scan cycle guarantees expiry by design."""
        s = get_settings().monitor
        assert s.approval_timeout > s.scan_interval

    def test_default_gives_an_operator_real_time_to_respond(self):
        assert get_settings().monitor.approval_timeout >= 600

    def test_env_var_actually_configures_the_timeout(self, monkeypatch):
        """PULSE_AGENT_APPROVAL_TIMEOUT must reach the nested config.

        Regression: the field existed only on the nested MonitorConfig, and
        model_post_init rebuilt that model from the flat fields without it —
        so the env var the commit message advertised did nothing.
        """
        monkeypatch.setenv("PULSE_AGENT_APPROVAL_TIMEOUT", "123")
        _reset_settings()
        try:
            assert get_settings().monitor.approval_timeout == 123
        finally:
            monkeypatch.delenv("PULSE_AGENT_APPROVAL_TIMEOUT")
            _reset_settings()


class TestOutcomeSeparation:
    """The production decision function, not a mirror of it."""

    def test_timeout_is_expired_not_failed(self):
        status, error = _proposal_outcome(timed_out=True, timeout_s=900)
        assert status == "expired"
        assert "never attempted" in error
        assert "900" in error

    def test_explicit_rejection_stays_failed(self):
        status, error = _proposal_outcome(timed_out=False, timeout_s=900)
        assert status == "failed"
        assert error == "Rejected by user"

    def test_rejection_message_no_longer_hedges(self):
        """The old text named both causes because it could not tell them apart."""
        _, error = _proposal_outcome(timed_out=False, timeout_s=900)
        assert "timed out" not in error


_PLAN = MagicMock(
    strategy="restart_pod",
    cause_category="crashloop",
    description="Delete the pod so its controller recreates it",
    confidence=0.9,
)

FINDING = {
    "id": "f-1",
    "category": "crashloop",
    "title": "Pod api-7f9 restarting (12x)",
    "autoFixable": True,
    "resources": [{"kind": "Pod", "name": "api-7f9", "namespace": "prod"}],
}


def _ask_first_monitor() -> tuple[ClusterMonitor, MagicMock, list[dict]]:
    """A trust-2 monitor with one connected (but silent) subscriber."""
    monitor = ClusterMonitor()
    subscriber = MagicMock()
    subscriber.trust_level = 2
    subscriber._pending_action_approvals = {}
    monitor._subscribers = [subscriber]
    monitor._broadcast_raw = AsyncMock()
    monitor.broadcast = AsyncMock()
    saved: list[dict] = []
    return monitor, subscriber, saved


def _autofix_patches(monitor, saved):
    repo = MagicMock()
    repo.check_pending_proposal.return_value = None
    repo.check_existing_human_review.return_value = None
    core = MagicMock()
    core.read_namespaced_pod.return_value = MagicMock(
        metadata=MagicMock(owner_references=[MagicMock(kind="ReplicaSet", name="api")])
    )
    settings = MagicMock()
    settings.monitor.autofix_enabled = True
    settings.monitor.max_trust_level = 2
    settings.monitor.approval_timeout = 900
    return (
        patch("sre_agent.monitor.cluster_monitor.is_autofix_paused", return_value=False),
        patch("sre_agent.monitor.cluster_monitor.get_settings", return_value=settings),
        patch("sre_agent.monitor.cluster_monitor.get_monitor_repo", return_value=repo),
        patch("sre_agent.monitor.cluster_monitor.save_action", side_effect=lambda r, **kw: saved.append(dict(r))),
        patch("sre_agent.monitor.cluster_monitor.get_core_client", return_value=core),
        patch("sre_agent.monitor.cluster_monitor._estimate_auto_fix_confidence", return_value=0.9),
        patch("sre_agent.monitor.cluster_monitor.notify_fix_proposed", new=AsyncMock()),
        patch("sre_agent.monitor.fix_planner.get_investigation_for_finding", return_value=None),
        patch("sre_agent.monitor.fix_planner.default_fix_plan", return_value=_PLAN),
    )


class TestApprovalWaitDoesNotBlockTheScan:
    """The stall regression: auto_fix must return while the operator thinks."""

    @pytest.mark.asyncio
    async def test_auto_fix_returns_before_the_operator_answers(self):
        import contextlib

        monitor, subscriber, saved = _ask_first_monitor()
        patches = _autofix_patches(monitor, saved)
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            # If the 900s wait were still inline, this would time out.
            await asyncio.wait_for(monitor.auto_fix([dict(FINDING)]), timeout=5)

            # The proposal is live: broadcast, registered for approval, and
            # a background wait exists — but nothing saved or executed yet.
            assert subscriber._pending_action_approvals, "approval future must be registered"
            assert monitor._proposals_awaiting_approval, "in-flight proposal must be tracked"
            assert len(monitor._approval_tasks) == 1
            assert saved == [], "no outcome may be recorded before the operator answers"

            # Cancel the pending wait so the test loop shuts down cleanly.
            for task in list(monitor._approval_tasks):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    @pytest.mark.asyncio
    async def test_pending_proposal_is_not_reproposed_next_scan(self):
        """With the wait off-loop, the next scan sees the same finding still
        firing; without the in-flight guard it would broadcast a duplicate."""
        import contextlib

        monitor, _subscriber, saved = _ask_first_monitor()
        patches = _autofix_patches(monitor, saved)
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            await asyncio.wait_for(monitor.auto_fix([dict(FINDING)]), timeout=5)
            first_broadcasts = monitor._broadcast_raw.await_count
            assert len(monitor._approval_tasks) == 1

            await asyncio.wait_for(monitor.auto_fix([dict(FINDING)]), timeout=5)
            assert len(monitor._approval_tasks) == 1, "second scan must not spawn a second wait"
            assert monitor._broadcast_raw.await_count == first_broadcasts, "no duplicate proposal broadcast"

            for task in list(monitor._approval_tasks):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    @pytest.mark.asyncio
    async def test_rejection_records_failed_and_frees_the_slot(self):
        import contextlib

        monitor, subscriber, saved = _ask_first_monitor()
        patches = _autofix_patches(monitor, saved)
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            await asyncio.wait_for(monitor.auto_fix([dict(FINDING)]), timeout=5)

            # The operator declines.
            future = next(iter(subscriber._pending_action_approvals.values()))
            future.set_result(False)
            for task in list(monitor._approval_tasks):
                await asyncio.wait_for(task, timeout=5)

            assert [r["status"] for r in saved] == ["failed"]
            assert saved[0]["error"] == "Rejected by user"
            assert monitor._proposals_awaiting_approval == set(), "slot must free for a future re-proposal"

    @pytest.mark.asyncio
    async def test_timeout_records_expired_via_the_real_path(self):
        """Drive the wait's timeout branch directly with a zero-second window."""
        monitor, _subscriber, saved = _ask_first_monitor()
        monitor._proposals_awaiting_approval.add("crashloop:prod:api")
        never = asyncio.get_running_loop().create_future()
        settings = MagicMock()
        settings.monitor.approval_timeout = 0  # immediate timeout in wait_for
        with (
            patch("sre_agent.monitor.cluster_monitor.get_settings", return_value=settings),
            patch("sre_agent.monitor.cluster_monitor.save_action", side_effect=lambda r, **kw: saved.append(dict(r))),
        ):
            await monitor._await_approval_then_execute(
                approval_future=never,
                action_report={"id": "a-1", "status": "proposed"},
                targeted_plan=_PLAN,
                category="crashloop",
                resources=FINDING["resources"],
                resource_key="Pod:prod:api",
                finding=dict(FINDING),
                verify_resources=None,
                corr_key="crashloop:prod:api",
            )

        assert [r["status"] for r in saved] == ["expired"]
        assert "never attempted" in saved[0]["error"]
        assert monitor._proposals_awaiting_approval == set()
