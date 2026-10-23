"""Which fix holds for which cause, learned from verification verdicts.

The planner mapped each cause to one strategy and kept applying it however
often it failed. These tests pin the ledger and the steering rules.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from sre_agent import fix_learning
from sre_agent.db import get_database
from sre_agent.fix_learning import HUMAN_REVIEW, mark_recurred, record_verdict, steer, track_record
from sre_agent.monitor.fix_planner import FixPlan, default_fix_plan, plan_fix

POD = [{"kind": "Pod", "name": "api-7f9c8d6b5-abcde", "namespace": "shop"}]


@pytest.fixture(autouse=True)
def _empty_ledger():
    db = get_database()
    db.execute("DELETE FROM fix_outcomes")
    db.commit()
    yield
    db.execute("DELETE FROM fix_outcomes")
    db.commit()


def _verdicts(cause: str, strategy: str, statuses: list[str]) -> None:
    for i, status in enumerate(statuses):
        record_verdict(f"a-{cause}-{strategy}-{i}", cause, strategy, status)


def _plan(strategy="restart_controller", cause="crashloop") -> FixPlan:
    return FixPlan(strategy=strategy, cause_category=cause, confidence=0.6, description="d", params={"resources": POD})


# ── the ledger ────────────────────────────────────────────────────────────


def test_only_definite_verdicts_are_recorded():
    _verdicts("crashloop", "restart_controller", ["verified", "still_failing", "unverifiable", "improved"])
    record_verdict("a-h", "crashloop", HUMAN_REVIEW, "verified")
    assert track_record("crashloop") == {"restart_controller": (1, 1)}


def test_a_recurrence_turns_a_hold_into_a_loss():
    record_verdict("a-1", "oom", "patch_resources", "verified")
    assert track_record("oom") == {"patch_resources": (1, 0)}
    mark_recurred("a-1")
    assert track_record("oom") == {"patch_resources": (0, 1)}


def test_old_verdicts_stop_counting():
    record_verdict("a-old", "crashloop", "restart_controller", "still_failing")
    old = int((time.time() - (fix_learning.WINDOW_DAYS + 1) * 86400) * 1000)
    get_database().execute("UPDATE fix_outcomes SET recorded_at = ?", (old,))
    get_database().commit()
    assert track_record("crashloop") == {}


# ── steering ──────────────────────────────────────────────────────────────


def test_no_history_changes_nothing():
    plan = steer(_plan())
    assert plan.strategy == "restart_controller"
    assert "track_record" not in plan.params


def test_a_fix_that_keeps_holding_is_kept_and_its_record_shown():
    _verdicts("crashloop", "restart_controller", ["verified"] * 4)
    plan = steer(_plan())
    assert plan.strategy == "restart_controller"
    assert "held 4 of 4" in plan.description


def test_two_failures_are_not_enough_to_escalate():
    _verdicts("crashloop", "restart_controller", ["still_failing"] * 2)
    assert steer(_plan()).strategy == "restart_controller"


def test_a_fix_that_never_holds_is_handed_to_a_person():
    _verdicts("crashloop", "restart_controller", ["still_failing"] * 3)
    plan = steer(_plan())
    assert plan.strategy == HUMAN_REVIEW
    assert "escalated to a person: restart_controller held 0 of 3" in plan.params["track_record"]


def test_a_failing_default_gives_way_to_an_untried_alternative():
    _verdicts("oom", "patch_resources", ["still_failing"] * 2)
    plan = steer(_plan("patch_resources", "oom"))
    assert plan.strategy == "restart_controller"
    assert "switched from patch_resources" in plan.description


def test_an_alternative_with_a_better_record_wins():
    _verdicts("oom", "patch_resources", ["still_failing", "verified", "still_failing"])
    _verdicts("oom", "restart_controller", ["verified"] * 3)
    assert steer(_plan("patch_resources", "oom")).strategy == "restart_controller"


def test_causes_without_safe_alternatives_never_switch():
    _verdicts("crashloop", "restart_controller", ["still_failing"] * 2)
    _verdicts("crashloop", "patch_resources", ["verified"] * 5)
    assert steer(_plan()).strategy == "restart_controller", "doubling memory blind is not a crashloop fix"


def test_human_review_plans_are_left_alone():
    plan = steer(_plan(HUMAN_REVIEW))
    assert plan.strategy == HUMAN_REVIEW and "track_record" not in plan.params


def test_a_broken_database_does_not_break_planning():
    with patch.object(fix_learning, "_db", side_effect=RuntimeError("down")):
        assert steer(_plan()).strategy == "restart_controller"


# ── wired into the planner ────────────────────────────────────────────────


def test_both_planner_paths_are_steered():
    _verdicts("crashloop", "restart_controller", ["still_failing"] * 3)
    _verdicts("oom", "patch_resources", ["still_failing"] * 3)
    _verdicts("oom", "restart_controller", ["still_failing"] * 3)
    finding = {"category": "crashloop", "resources": POD}
    assert default_fix_plan("crashloop", finding).strategy == HUMAN_REVIEW
    investigation = {"suspected_cause": "OOMKilled, exceeded memory limit", "confidence": 0.9}
    assert plan_fix(investigation, finding).strategy == HUMAN_REVIEW


def test_verification_records_the_verdict_for_the_strategy_used():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from sre_agent.monitor import health_gate
    from sre_agent.monitor.verification_pipeline import process_verifications

    monitor = SimpleNamespace(_scan_counter=2, _broadcast_raw=AsyncMock(), _pending_verifications={})
    monitor._pending_verifications["a-v"] = {
        "action_id": "a-v",
        "finding_id": "",
        "category": "crashloop",
        "resources": POD,
        "target_scan": 2,
        "strategy": "restart_controller",
        "cause": "crashloop",
    }
    repo = MagicMock(async_update_action_verification=AsyncMock())
    with (
        patch.object(health_gate, "check_resources", return_value=(health_gate.PASS, "ok")),
        patch("sre_agent.monitor.verification_pipeline.get_monitor_repo", return_value=repo),
    ):
        asyncio.run(process_verifications(monitor, []))
    assert track_record("crashloop") == {"restart_controller": (1, 0)}
