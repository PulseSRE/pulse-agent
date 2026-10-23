"""Do skill routing and tool prediction actually learn from use?

Each test pins one break found by tracing the loops end to end: selection
outcomes logged with empty channel scores, no override ever recorded, weights
learned from the selector agreeing with itself, the tool predictor learning its
own LLM guesses, decay that never ran, and co-occurrence that looked one way.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

from sre_agent.db import Database
from sre_agent.db_migrations import run_migrations
from sre_agent.skill_selector import SelectionResult, _last_selection_result_var

_TEST_DB_URL = os.environ.get("PULSE_AGENT_TEST_DATABASE_URL", "postgresql://pulse:pulse@localhost:5433/pulse_test")

SCORES = {"keyword": {"sre": 0.9, "security": 0.1}, "semantic": {"security": 0.8, "sre": 0.2}}


def _selection(skill="sre", scores=SCORES) -> SelectionResult:
    return SelectionResult(
        skill_name=skill,
        fused_scores={"sre": 0.6, "security": 0.4},
        channel_scores=scores,
        threshold_used=0.45,
        source="orca",
    )


def _record(routing_skill="sre", **kw):
    from sre_agent.tool_usage import record_turn

    with (
        patch("sre_agent.repositories.tool_usage_repo.get_tool_usage_repo"),
        patch("sre_agent.tool_predictor.learn"),
        patch("sre_agent.skill_selector.record_selection_outcome") as outcome,
    ):
        record_turn(
            session_id="s",
            turn_number=1,
            agent_mode="sre",
            query_summary="pods crashing",
            tools_offered=["list_pods"],
            tools_called=["list_pods"],
            routing_decision={"skill_name": routing_skill},
            **kw,
        )
    return outcome


# ── what gets logged ──────────────────────────────────────────────────────


def test_a_turn_logs_the_real_channel_scores():
    def run():
        _last_selection_result_var.set(_selection())
        return _record()

    outcome = contextvars.copy_context().run(run)
    outcome.assert_called_once()
    assert outcome.call_args.kwargs["result"].channel_scores == SCORES
    assert outcome.call_args.kwargs["skill_overridden"] is None


def test_loading_another_skill_mid_turn_is_logged_as_an_override():
    """skill_load runs in an executor; what it notes must reach the recorder."""
    from sre_agent.request_context import begin_turn, current_turn

    def run():
        _last_selection_result_var.set(_selection())
        begin_turn()

        def tool_body():  # what skill_load does, on a worker thread
            current_turn().loaded_skills.append("security")

        ctx = contextvars.copy_context()
        with ThreadPoolExecutor(1) as pool:
            pool.submit(ctx.run, tool_body).result()
        return _record()

    outcome = contextvars.copy_context().run(run)
    assert outcome.call_args.kwargs["skill_overridden"] == "security"


def test_loading_the_routed_skill_is_not_an_override():
    from sre_agent.request_context import begin_turn, current_turn

    def run():
        _last_selection_result_var.set(_selection())
        begin_turn()
        current_turn().loaded_skills.append("sre")
        return _record()

    assert contextvars.copy_context().run(run).call_args.kwargs["skill_overridden"] is None


def test_skill_load_notes_the_skill_but_not_a_reference():
    from sre_agent.request_context import begin_turn, current_turn
    from sre_agent.skill_tools import skill_load

    def run():
        begin_turn()
        skill_load.call({"name": "sre"})
        skill_load.call({"name": "security", "reference": "does-not-exist"})
        return list(current_turn().loaded_skills)

    assert contextvars.copy_context().run(run) == ["sre"]


@pytest.mark.parametrize(
    ("selection", "routing_skill"),
    [
        (_selection(scores={}), "sre"),  # a hard pre-route: no channels voted
        (_selection(skill="security"), "sre"),  # left over from an earlier turn
    ],
)
def test_selections_without_evidence_are_not_logged(selection, routing_skill):
    def run():
        _last_selection_result_var.set(selection)
        return _record(routing_skill=routing_skill)

    contextvars.copy_context().run(run).assert_not_called()


def test_logged_weights_are_the_live_ones():
    from sre_agent.skill_selector import record_selection_outcome

    live = {"keyword": 0.5, "semantic": 0.5}
    repo = MagicMock()
    with (
        patch("sre_agent.repositories.get_skill_analytics_repo", return_value=repo),
        patch("sre_agent.skill_loader._get_selector", return_value=MagicMock(_weights=live)),
    ):
        record_selection_outcome(session_id="s", query_summary="q", result=_selection())
    assert json.loads(repo.record_selection_outcome.call_args.kwargs["channel_weights_json"]) == live


# ── learning from it ──────────────────────────────────────────────────────


def _row(overridden=None):
    return {
        "channel_scores": json.dumps(SCORES),
        "selected_skill": "sre",
        "skill_overridden": overridden,
        "tools_requested_missing": None,
    }


def test_the_channel_that_named_the_corrected_skill_gains_weight():
    from sre_agent.selector_learning import recompute_channel_weights
    from sre_agent.skill_selector import DEFAULT_WEIGHTS

    repo = MagicMock()
    # Routing chose sre on keyword evidence; the agent kept switching to
    # security, which only the semantic channel pointed at.
    repo.fetch_selection_log.return_value = [_row()] * 4 + [_row("security")] * 8
    with patch("sre_agent.repositories.selector_learning_repo.get_selector_learning_repo", return_value=repo):
        weights = recompute_channel_weights(7)

    base_total = sum(DEFAULT_WEIGHTS.values())
    base = {k: v / base_total for k, v in DEFAULT_WEIGHTS.items()}
    assert weights["semantic"] > base["semantic"]
    assert weights["keyword"] < base["keyword"]
    repo.persist_weights.assert_called_once_with(weights)


def test_no_corrections_leaves_weights_alone():
    from sre_agent.selector_learning import recompute_channel_weights

    repo = MagicMock()
    repo.fetch_selection_log.return_value = [_row()] * 100
    with patch("sre_agent.repositories.selector_learning_repo.get_selector_learning_repo", return_value=repo):
        assert recompute_channel_weights(7) == {}
    repo.persist_weights.assert_not_called()


def test_the_prompt_reports_live_weights_without_recomputing():
    from sre_agent.intelligence import _compute_selector_weights

    live = {"keyword": 0.3, "semantic": 0.7}
    with (
        patch("sre_agent.skill_loader._get_selector", return_value=MagicMock(_weights=live)),
        patch("sre_agent.selector_learning.recompute_channel_weights") as recompute,
    ):
        out = _compute_selector_weights()
    recompute.assert_not_called()
    assert "semantic: 0.700" in out


# ── the tool predictor ────────────────────────────────────────────────────


def test_llm_picks_are_not_learned_as_hits():
    from sre_agent.tool_predictor import PredictionResult, select_tools_adaptive

    tools = {n: MagicMock(to_dict=lambda n=n: {"name": n}) for n in ("list_pods", "get_pod_logs", "describe_pod")}
    with (
        patch("sre_agent.tool_predictor.predict_tools", return_value=PredictionResult()),
        patch("sre_agent.tool_predictor.llm_pick_tools", return_value=["get_pod_logs"]),
        patch("sre_agent.tool_predictor.learn") as learn,
    ):
        select_tools_adaptive("why is api crashing", all_tool_map=tools)
    learn.assert_not_called()


@pytest.fixture
def pdb():
    db = Database(_TEST_DB_URL)
    run_migrations(db)
    db.execute("DELETE FROM tool_predictions")
    db.execute("DELETE FROM tool_cooccurrence")
    db.commit()
    with patch("sre_agent.tool_predictor._get_db", return_value=db):
        yield db


def test_a_miss_does_not_keep_a_pairing_alive(pdb):
    from sre_agent.tool_predictor import learn

    learn(query="etcd slow", tools_called=["get_etcd_status"], tools_offered=["get_etcd_status", "list_pods"])
    pdb.execute("UPDATE tool_predictions SET last_seen = NOW() - INTERVAL '40 days'")
    pdb.commit()
    learn(query="etcd slow", tools_called=["get_etcd_status"], tools_offered=["get_etcd_status", "list_pods"])
    rows = {
        r["tool_name"]: r["fresh"]
        for r in pdb.fetchall(
            "SELECT tool_name, bool_and(last_seen > NOW() - INTERVAL '1 day') AS fresh "
            "FROM tool_predictions GROUP BY tool_name"
        )
    }
    assert rows == {"get_etcd_status": True, "list_pods": False}


def test_decay_fades_hits_and_misses_and_prunes_dead_pairings(pdb):
    from sre_agent.tool_predictor import decay_scores

    pdb.execute(
        "INSERT INTO tool_predictions (token, tool_name, score, hit_count, miss_count, last_seen) VALUES "
        "('etcd', 'get_etcd_status', 4.0, 4, 1, NOW()), "
        "('etcd', 'list_pods', 0.0, 0, 0.04, NOW()), "
        "('etcd', 'old_tool', 9.0, 9, 0, NOW() - INTERVAL '40 days')"
    )
    pdb.commit()
    decay_scores(factor=0.5, prune_days=30)
    rows = {r["tool_name"]: r for r in pdb.fetchall("SELECT * FROM tool_predictions")}
    assert set(rows) == {"get_etcd_status"}
    assert rows["get_etcd_status"]["hit_count"] == pytest.approx(2.0)
    assert rows["get_etcd_status"]["miss_count"] == pytest.approx(0.5), "fractional, not rounded away"


def test_cooccurrence_finds_partners_on_either_side(pdb):
    from sre_agent.tool_predictor import _expand_cooccurrence

    pdb.execute("INSERT INTO tool_cooccurrence (tool_a, tool_b, frequency) VALUES ('describe_pod', 'get_pod_logs', 7)")
    pdb.commit()
    assert _expand_cooccurrence(pdb, ["get_pod_logs"]) == ["describe_pod"]
    assert _expand_cooccurrence(pdb, ["describe_pod"]) == ["get_pod_logs"]


def test_the_daily_flywheel_runs_decay():
    from sre_agent.monitor.flywheel import run_daily_tasks

    with (
        patch("sre_agent.tool_predictor.decay_scores") as decay,
        patch("sre_agent.selector_learning.recompute_channel_weights", return_value={}),
        patch("sre_agent.trajectory.get_learner"),
    ):
        summary = asyncio.run(run_daily_tasks())
    decay.assert_called_once()
    assert summary["tool_predictions"] == "decayed"
