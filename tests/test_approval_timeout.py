"""A timeout is not a rejection.

The monitor waited 120s for a human to approve a proposed fix and, on
timeout, recorded status="failed" with "Rejected by user or approval timed
out". On the dev cluster 281 of 368 actions ended that way without anyone
rejecting anything: the window is short, the proposal is unprompted, and
nobody was at the dashboard. Two different facts were being written down as
one, and the agent's own idea was filed as having failed when it never ran.
"""

from __future__ import annotations

from sre_agent.config import get_settings


class TestApprovalTimeoutConfig:
    def test_timeout_is_configurable(self):
        assert isinstance(get_settings().monitor.approval_timeout, int)

    def test_default_is_longer_than_a_scan_interval(self):
        """A window shorter than the scan cycle guarantees expiry by design."""
        s = get_settings().monitor
        assert s.approval_timeout > s.scan_interval

    def test_default_gives_an_operator_real_time_to_respond(self):
        assert get_settings().monitor.approval_timeout >= 600


class TestOutcomeSeparation:
    """The distinction has to survive into what gets written down."""

    def _report(self, timed_out: bool) -> dict:
        # Mirrors the branch in cluster_monitor._attempt_auto_fix.
        timeout = get_settings().monitor.approval_timeout
        return {
            "status": "expired" if timed_out else "failed",
            "error": (
                f"No response within {timeout}s — the fix was never attempted" if timed_out else "Rejected by user"
            ),
        }

    def test_timeout_is_expired_not_failed(self):
        r = self._report(timed_out=True)
        assert r["status"] == "expired"
        assert "never attempted" in r["error"]

    def test_explicit_rejection_stays_failed(self):
        r = self._report(timed_out=False)
        assert r["status"] == "failed"
        assert r["error"] == "Rejected by user"

    def test_the_two_are_not_the_same_record(self):
        assert self._report(True) != self._report(False)

    def test_rejection_message_no_longer_hedges(self):
        """The old text named both causes because it could not tell them apart."""
        assert "timed out" not in self._report(False)["error"]
