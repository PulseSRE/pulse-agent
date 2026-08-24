"""A verification verdict has a time horizon.

Observed live on dev05 (v2.24.0): auto-fix deleted a crashlooping pod, the
health gate verified the fix on the next scan, the runbook was learned and the
trajectory promoted — then the same pod crashlooped again eight minutes later.
Every learning layer had already recorded the fix as a cure.

These tests hold the recurrence path to its contract: when a condition returns
inside the window after a verified fix, the verdict is downgraded on the action
row, the promotion is demoted, and memory walks its confirmed lesson back.
Outside the window, a returning condition is a new incident, not a revocation.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sre_agent.monitor.recurrence import RECURRED, process_recurrences

MODULE = "sre_agent.monitor.recurrence"

FINDING = {
    "id": "f-new",
    "category": "crashloop",
    "title": "Pod cluster-proxy-addon-manager crash-looping (5 restarts)",
    "resources": [{"kind": "Pod", "namespace": "multicluster-engine", "name": "cluster-proxy-addon-manager-abc"}],
}


def _verified_row(**overrides) -> dict:
    """A row verified 8 minutes before *now* — built per test, not at import,
    so a slow full-suite run cannot age it into a different minute count."""
    row = {
        "id": "a-1",
        "tool": "delete_pod",
        "category": "crashloop",
        "verification_evidence": "No active crashloop findings, and health check passed: ReplicaSet 2/2 ready",
        "verification_timestamp": int(time.time() * 1000) - 8 * 60_000,
    }
    row.update(overrides)
    return row


@pytest.fixture
def wired():
    """Patch every collaborator; individual tests override what they care about."""
    repo = MagicMock()
    repo.find_recent_verified_actions.return_value = [_verified_row()]
    monitor = MagicMock()
    monitor._broadcast_raw = AsyncMock()
    learner = MagicMock()
    manager = MagicMock()
    with (
        patch(f"{MODULE}.get_monitor_repo", return_value=repo),
        patch("sre_agent.trajectory.get_learner", return_value=learner),
        patch("sre_agent.memory.get_manager", return_value=manager),
        patch("sre_agent.skill_lifecycle.note_recurrence") as note,
    ):
        yield MagicMock(repo=repo, monitor=monitor, learner=learner, manager=manager, note=note)


# ── the verdict is downgraded ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recurrence_downgrades_the_verified_verdict(wired):
    await process_recurrences(wired.monitor, [dict(FINDING)])

    status_call = wired.repo.update_action_verification.call_args
    assert status_call[0][0] == "a-1"
    assert status_call[0][1] == RECURRED
    assert "RECURRED" in status_call[0][2]
    assert "8 min" in status_call[0][2], "the evidence must say how long the verdict held"


@pytest.mark.asyncio
async def test_the_outcome_stops_counting_as_a_success(wired):
    await process_recurrences(wired.monitor, [dict(FINDING)])
    wired.repo.update_action_outcome.assert_called_once_with("a-1", "recurred")


@pytest.mark.asyncio
async def test_the_downgrade_is_broadcast_with_the_original_action_id(wired):
    await process_recurrences(wired.monitor, [dict(FINDING)])

    events = [c.args[0] for c in wired.monitor._broadcast_raw.call_args_list]
    reports = [e for e in events if e.get("type") == "verification_report"]
    assert len(reports) == 1
    assert reports[0]["status"] == RECURRED
    assert reports[0]["actionId"] == "a-1", "the verdict being revoked is the original action's"
    assert reports[0]["findingId"] == "f-new", "the evidence for revoking it is the new sighting"


@pytest.mark.asyncio
async def test_original_evidence_is_kept_not_overwritten(wired):
    """The verdict was true on the scan that issued it; the downgrade appends."""
    await process_recurrences(wired.monitor, [dict(FINDING)])
    evidence = wired.repo.update_action_verification.call_args[0][2]
    assert "ReplicaSet 2/2 ready" in evidence


# ── the learning layers are walked back ───────────────────────────────────


@pytest.mark.asyncio
async def test_the_promoted_trajectory_is_demoted(wired):
    await process_recurrences(wired.monitor, [dict(FINDING)])
    key, reason = wired.learner.mark_recurred.call_args[0]
    assert key.startswith("crashloop:")
    assert "returned" in reason


@pytest.mark.asyncio
async def test_memory_retracts_the_confirmed_lesson(wired):
    await process_recurrences(wired.monitor, [dict(FINDING)])
    kwargs = wired.manager.record_fix_regression.call_args.kwargs
    assert kwargs["category"] == "crashloop"
    assert kwargs["tool"] == "delete_pod"
    assert kwargs["namespace"] == "multicluster-engine"
    assert kwargs["recurrence_minutes"] == 8


@pytest.mark.asyncio
async def test_the_scaffolded_skill_is_flagged_for_review(wired):
    await process_recurrences(wired.monitor, [dict(FINDING)])
    wired.note.assert_called_once()
    assert wired.note.call_args[0][0] == "crashloop"


# ── the horizon ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_verified_action_means_no_recurrence(wired):
    """A brand-new condition is an incident, not a revocation."""
    wired.repo.find_recent_verified_actions.return_value = []
    await process_recurrences(wired.monitor, [dict(FINDING)])

    wired.repo.update_action_verification.assert_not_called()
    wired.learner.mark_recurred.assert_not_called()
    wired.manager.record_fix_regression.assert_not_called()


@pytest.mark.asyncio
async def test_the_lookup_is_bounded_by_the_recurrence_window(wired):
    await process_recurrences(wired.monitor, [dict(FINDING)])
    corr_key, since_ms = wired.repo.find_recent_verified_actions.call_args[0]
    # Pod-hash stripped, so a recreated pod still matches its predecessor's fix.
    assert corr_key == "crashloop:multicluster-engine:Pod/cluster-proxy-addon"
    from sre_agent.config import get_settings

    window_ms = get_settings().monitor.recurrence_window * 1000
    now_ms = int(time.time() * 1000)
    assert abs((now_ms - window_ms) - since_ms) < 60_000, "since must be now minus the configured window"


@pytest.mark.asyncio
async def test_learning_is_retracted_once_per_condition_not_per_action(wired):
    wired.repo.find_recent_verified_actions.return_value = [
        _verified_row(),
        _verified_row(id="a-2", verification_timestamp=int(time.time() * 1000) - 20 * 60_000),
    ]
    await process_recurrences(wired.monitor, [dict(FINDING)])

    assert wired.repo.update_action_verification.call_count == 2
    assert wired.learner.mark_recurred.call_count == 1
    assert wired.manager.record_fix_regression.call_count == 1


@pytest.mark.asyncio
async def test_a_failed_downgrade_does_not_kill_the_scan(wired):
    wired.repo.update_action_verification.side_effect = RuntimeError("db down")
    await process_recurrences(wired.monitor, [dict(FINDING)])  # must not raise


# ── trajectory demotion ───────────────────────────────────────────────────


class _RecurDB:
    """Just enough of Database for mark_recurred: promoted rows with ids."""

    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, params=()):
        q = " ".join(sql.split())
        assert "status = 'recurred'" in q
        reason, _resolved, key = params
        promoted = [r for r in self.rows if r["candidate_key"] == key and r["status"] == "promoted"]
        cur = MagicMock()
        if promoted:
            newest = max(promoted, key=lambda r: r["id"])
            newest["status"], newest["reason"] = "recurred", reason
            cur.rowcount = 1
        else:
            cur.rowcount = 0
        return cur

    def commit(self):
        pass

    def fetchall(self, sql, params=()):
        counts: dict[str, int] = {}
        for r in self.rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        return [{"status": k, "c": v} for k, v in counts.items()]

    def fetchone(self, sql, params=()):
        return {"c": 0}


class TestTrajectoryDemotion:
    def test_only_the_most_recent_promotion_is_demoted(self):
        from sre_agent.trajectory import TrajectoryLearner

        rows = [
            {"id": 1, "candidate_key": "crashloop:Pod:ns:x", "status": "promoted", "reason": ""},
            {"id": 2, "candidate_key": "crashloop:Pod:ns:x", "status": "promoted", "reason": ""},
        ]
        learner = TrajectoryLearner(db=_RecurDB(rows))
        assert learner.mark_recurred("crashloop:Pod:ns:x", "came back in 8 min") is True
        assert rows[1]["status"] == "recurred", "the newest promotion is the dubious one"
        assert rows[0]["status"] == "promoted", "an older promotion was a separate incident"
        assert learner.recurred == 1

    def test_nothing_promoted_means_nothing_to_demote(self):
        from sre_agent.trajectory import TrajectoryLearner

        learner = TrajectoryLearner(db=_RecurDB([]))
        assert learner.mark_recurred("never-promoted", "reason") is False
        assert learner.recurred == 0

    def test_stats_report_recurred(self):
        from sre_agent.trajectory import TrajectoryLearner

        rows = [{"id": 1, "candidate_key": "k", "status": "promoted", "reason": ""}]
        learner = TrajectoryLearner(db=_RecurDB(rows))
        learner.mark_recurred("k", "recurred")
        assert learner.stats()["recurred"] == 1


# ── memory retraction ─────────────────────────────────────────────────────


class TestMemoryRetraction:
    def _manager(self):
        from sre_agent.memory import MemoryManager

        manager = MemoryManager.__new__(MemoryManager)
        manager.store = MagicMock()
        return manager

    def test_the_confirmed_incident_is_demoted_and_its_runbook_penalized(self):
        manager = self._manager()
        manager.store.mark_recent_autofix_regressed.return_value = 42

        result = manager.record_fix_regression(
            category="crashloop", tool="delete_pod", namespace="mce", resource_type="pod", recurrence_minutes=8
        )

        manager.store.mark_recent_autofix_regressed.assert_called_once_with("crashloop", "mce")
        manager.store.record_runbook_failure.assert_called_once_with(42)
        assert result["demoted_incident_id"] == 42

    def test_an_anti_pattern_incident_is_recorded(self):
        manager = self._manager()
        manager.store.mark_recent_autofix_regressed.return_value = None

        manager.record_fix_regression(category="crashloop", tool="delete_pod", recurrence_minutes=8)

        kwargs = manager.store.record_incident.call_args.kwargs
        assert kwargs["outcome"] == "regressed"
        assert kwargs["score"] < 0.4, "must land below the anti-pattern surface threshold"
        assert "does not hold" in kwargs["resolution"]

    def test_no_prior_confirmed_incident_penalizes_no_runbook(self):
        manager = self._manager()
        manager.store.mark_recent_autofix_regressed.return_value = None
        manager.record_fix_regression(category="crashloop", tool="delete_pod")
        manager.store.record_runbook_failure.assert_not_called()


# ── outcome accounting ────────────────────────────────────────────────────


def test_recurred_is_a_valid_outcome():
    from sre_agent.monitor.actions import _VALID_OUTCOMES

    assert "recurred" in _VALID_OUTCOMES


def test_success_rate_reports_recurred_without_counting_it_as_success():
    from sre_agent.monitor.actions import get_fix_success_rate

    rows = [{"outcome": "resolved", "cnt": 3}, {"outcome": "recurred", "cnt": 1}]
    with patch("sre_agent.monitor.actions.get_monitor_repo") as repo:
        repo.return_value.get_fix_success_rate_rows.return_value = rows
        stats = get_fix_success_rate(30)

    assert stats["recurred"] == 1
    assert stats["resolved"] == 3
    assert stats["success_rate"] == 0.75, "a fix that did not hold is not a success"


# ── durable attempt cap ───────────────────────────────────────────────────


def test_attempt_count_guard_returns_zero_without_a_key():
    from sre_agent.repositories.monitor_repo import MonitorRepository

    repo = MonitorRepository.__new__(MonitorRepository)
    assert repo.count_recent_fix_attempts("", 0) == 0
