"""Production metrics and learned weights use the migrated PostgreSQL schema."""

import json

import pytest

from sre_agent.db import get_database
from sre_agent.repositories.analytics_repo import AnalyticsRepository
from sre_agent.repositories.eval_repo import EvalRepository
from sre_agent.repositories.selector_learning_repo import SelectorLearningRepository
from sre_agent.repositories.skill_analytics_repo import SkillAnalyticsRepository
from tests.conftest import truncate_tables


@pytest.fixture
def database():
    db = get_database()
    truncate_tables(db, "eval_runs", "skill_selection_log")
    return db


@pytest.mark.asyncio
async def test_real_eval_records_reach_metrics_sparkline(database, monkeypatch):
    from sre_agent.api.metrics_rest import metrics_eval_trend

    repo = EvalRepository(database)
    for suite, score in [("release", 0.4), ("other", 0.99), ("release", 0.9)]:
        repo.record_run(
            suite_name=suite,
            source="synthetic",
            model="synthetic",
            scenario_count=3,
            passed_count=2,
            gate_passed=False,
            average_overall=score,
            dimensions_json="{}",
            blocker_counts_json="{}",
            scenarios_json="[]",
            prompt_audit_json="{}",
            judge_avg=None,
        )
    analytics = AnalyticsRepository(database)
    monkeypatch.setattr("sre_agent.repositories.get_analytics_repo", lambda: analytics)
    rows = analytics.fetch_eval_scores("release", 1)
    assert len(rows) == 1
    assert rows[0]["score"] == pytest.approx(0.9)
    assert rows[0]["pass"] is False
    assert rows[0]["scenarios"] == 3
    trend = await metrics_eval_trend(suite="release", releases=10)
    assert trend["sparkline"] == pytest.approx([0.4, 0.9])
    assert trend["current_score"] == pytest.approx(0.9)
    assert trend["runs_count"] == 2


def test_actual_weight_recomputation_persists_and_reloads(database, monkeypatch):
    from sre_agent.selector_learning import load_learned_weights, recompute_channel_weights

    analytics = SkillAnalyticsRepository(database)
    for index in range(10):
        analytics.record_selection_outcome(
            session_id=f"synthetic-{index}",
            query_summary="synthetic observation",
            channel_scores_json=json.dumps({"keyword": {"sre": 1.0}}),
            fused_scores_json=json.dumps({"sre": 1.0}),
            selected_skill="sre",
            threshold_used=0.5,
            conflicts_json="[]",
            skill_overridden=None,
            tools_requested_missing=[],
            selection_ms=2,
            channel_weights_json="{}",
        )
    repo = SelectorLearningRepository(database)
    monkeypatch.setattr("sre_agent.repositories.selector_learning_repo.get_selector_learning_repo", lambda: repo)
    learned = recompute_channel_weights()
    assert learned
    assert sum(learned.values()) == pytest.approx(1.0, abs=0.001)
    # A new repository instance observes the committed snapshot, rather than a
    # returned in-memory result that silently failed to persist.
    assert SelectorLearningRepository(database).load_learned_weights() == learned
    assert load_learned_weights() == learned
    snapshot = database.fetchone(
        "SELECT channel_scores, fused_scores FROM skill_selection_log WHERE session_id = ?", ("__weight_snapshot__",)
    )
    assert snapshot["channel_scores"] == {}
    assert snapshot["fused_scores"] == {}
