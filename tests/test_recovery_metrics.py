"""Recovery timing uses observed verification, not proposal/execution time."""

import time

import pytest

from sre_agent.db import Database, reset_database, set_database
from sre_agent.repositories.monitor_repo import MonitorRepository
from tests.conftest import _TEST_DB_URL, truncate_core_tables


@pytest.fixture
def repo():
    db = Database(_TEST_DB_URL)
    set_database(db)
    truncate_core_tables(db)
    try:
        yield MonitorRepository(db)
    finally:
        truncate_core_tables(db)
        reset_database()


def save_case(repo, name, detected, action_at, verified_at, verdict="verified"):
    repo.db.execute(
        "INSERT INTO findings (id, cluster, message, timestamp) VALUES (?, ?, ?, ?)",
        (name, "local", "Pod crashing", detected),
    )
    repo.db.commit()
    repo.save_action(
        {
            "id": f"a-{name}",
            "findingId": name,
            "status": "completed",
            "verificationStatus": verdict,
            "verificationTimestamp": verified_at,
        },
        "crashloop",
        "[]",
        0,
        "{}",
        action_at,
    )


def test_time_includes_approval_and_recovery_wait(repo):
    now = int(time.time() * 1000)
    save_case(repo, "slow-recovery", now - 900_000, now - 890_000, now - 10_000)
    result = repo.fetch_time_to_resolution(7)
    assert result["sample_count"] == 1
    assert result["avg_seconds"] == 890


def test_window_follows_verification_not_old_proposal(repo):
    now = int(time.time() * 1000)
    save_case(repo, "old-proposal", now - 9 * 86400000, now - 8 * 86400000, now - 1000)
    assert repo.fetch_time_to_resolution(7)["sample_count"] == 1


@pytest.mark.parametrize(
    "verdict,offset",
    [
        ("verified", None),
        ("unverifiable", -1000),
        ("still_failing", -1000),
        ("verified_then_recurred", -1000),
        ("verified", 60000),
        ("verified", -300000),
    ],
)
def test_unproven_or_inconsistent_recovery_is_not_a_sample(repo, verdict, offset):
    now = int(time.time() * 1000)
    save_case(repo, "invalid", now - 200000, now - 100000, None if offset is None else now + offset, verdict)
    result = repo.fetch_time_to_resolution(7)
    assert result["sample_count"] == 0
    assert result["avg_seconds"] is None


def test_empty_recovery_dashboard_has_no_green_verdict(repo, monkeypatch):
    from sre_agent.api.monitor_rest import _compute_kpi_dashboard_sync

    monkeypatch.setattr("sre_agent.repositories.get_monitor_repo", lambda: repo)
    dashboard = _compute_kpi_dashboard_sync(7)["kpis"]
    assert len(dashboard) == 11
    assert dashboard["false_positive_rate"]["status"] == "info"
    result = dashboard["time_to_resolution"]
    assert result["sample_count"] == 0
    assert result["status"] == "info"


def test_success_rate_requires_recovery_and_excludes_unanswered_proposals(repo):
    now = int(time.time() * 1000)
    for name, verdict in [("good", "verified"), ("pending", None), ("recurred", "verified_then_recurred")]:
        save_case(repo, name, now - 10000, now - 5000, now - 1000, verdict)
    repo.save_action({"id": "proposal", "status": "proposed"}, "crashloop", "[]", 0, "{}", now)
    result = repo.fetch_fix_rate(7)
    assert result["total"] == 3
    assert result["good"] == 1
