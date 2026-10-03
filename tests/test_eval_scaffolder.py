"""Automatic skill scaffolds are redacted review drafts, never eval assertions."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sre_agent import eval_scaffolder, eval_store
from sre_agent.artifact_store import KIND_EVAL_DRAFT


@pytest.fixture
def draft_root(tmp_path, monkeypatch):
    from sre_agent.config import _reset_settings

    monkeypatch.setenv("PULSE_AGENT_USER_EVALS_DIR", str(tmp_path))
    _reset_settings()
    yield tmp_path
    _reset_settings()


def scaffold():
    return eval_scaffolder.scaffold_eval_from_plan(
        skill_name="oom-api",
        finding={"id": "private-resource-name", "title": "SECRET_TOKEN", "category": "oom"},
        plan_result=SimpleNamespace(
            phase_outputs={"verify": SimpleNamespace(status="completed", evidence_summary="SECRET_TOKEN")}
        ),
        tools_called=["describe_pod", "get_pod_logs", "patch_resource", "SECRET_TOKEN"],
        confidence=0.99,
        duration_seconds=45,
    )


def test_plan_generates_nonrunnable_private_durable_draft_only(draft_root):
    with patch("sre_agent.artifact_store.persist", return_value=True) as persist:
        assert scaffold()
    path = next((draft_root / "drafts").glob("*.json"))
    rendered = path.read_text()
    draft = json.loads(rendered)
    assert "SECRET_TOKEN" not in rendered
    assert "private-resource-name" not in rendered
    assert draft["review"]["status"] == "needs_review"
    assert draft["runnable"] is False
    assert draft["recorded_responses"] == {}
    assert draft["expected"] == {"should_block_release": False}
    assert "verification_passed" not in draft
    assert "duration_seconds" not in draft
    assert draft["observations"]["stored_action_verification"] == "not_available"
    assert draft["tool_selection"]["requested_tools"] == ["describe_pod", "get_pod_logs", "patch_resource"]
    assert draft["tool_selection"]["omitted_count"] == 1
    assert not (draft_root / "fixtures").exists()
    assert not (draft_root / "scenarios_data").exists()
    assert persist.call_args.args[0] == KIND_EVAL_DRAFT
    assert path.stat().st_mode & 0o777 == 0o600


def test_existing_review_and_reviewed_fixture_are_never_overwritten(draft_root):
    with patch("sre_agent.artifact_store.persist", return_value=True):
        assert scaffold()
        path = next((draft_root / "drafts").glob("*.json"))
        path.write_text("review-in-progress")
        fixture = eval_store.fixtures_dir() / "reviewed.json"
        fixture.write_text("reviewed fixture")
        assert not scaffold()
    assert path.read_text() == "review-in-progress"
    assert fixture.read_text() == "reviewed fixture"


def test_investigation_does_not_invent_resolution_metrics_or_tools(draft_root):
    with patch("sre_agent.artifact_store.persist", return_value=True):
        assert eval_scaffolder.scaffold_eval_from_investigation(
            skill_name="node-pressure",
            finding={"id": "f1", "title": "SECRET_TOKEN"},
            investigation_result={"summary": "SECRET_TOKEN", "confidence": 0.99},
        )
    draft = json.loads(next((draft_root / "drafts").glob("*.json")).read_text())
    assert draft["tool_selection"]["requested_tools"] == []
    assert draft["observations"]["stored_action_verification"] == "not_available"
    assert "SECRET_TOKEN" not in json.dumps(draft)


def test_empty_skill_name_refused(draft_root):
    assert not eval_scaffolder.scaffold_eval_from_investigation(skill_name="///", finding={}, investigation_result={})
    assert not (draft_root / "drafts").exists()


def test_missing_durability_is_not_reported_as_persisted_success(draft_root):
    with patch("sre_agent.artifact_store.persist", return_value=False):
        assert not scaffold()
