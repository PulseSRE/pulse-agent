"""Explicit selected-incident export is redacted, evidence-bound, review-only."""

import copy
import json
import stat
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from sre_agent.evals import incident_draft

SECRET = "token-super-private-credential"


def records():
    finding = {"id": "finding-private-name", "timestamp": 100, "resolved": 1, "category": "crashloop", "title": SECRET}
    action = {
        "id": "action-private-name",
        "finding_id": finding["id"],
        "timestamp": 200,
        "status": "completed",
        "verification_status": "verified",
        "verification_timestamp": 300,
        "verification_evidence": f"read readiness: {SECRET}",
        "tool": "restart_deployment",
        "input": {"token": SECRET},
        "before_state": SECRET,
        "after_state": SECRET,
        "resources": SECRET,
    }
    investigation = {
        "id": "investigation-private-name",
        "finding_id": finding["id"],
        "timestamp": 150,
        "status": "completed",
        "evidence": json.dumps([{"observation": SECRET, "source": SECRET}]),
        "summary": SECRET,
        "suspected_cause": SECRET,
    }
    return action, finding, investigation


def test_draft_redacts_every_payload_and_does_not_fabricate_replay_evidence():
    sources = records()
    original = copy.deepcopy(sources)
    draft = incident_draft.build_draft(*sources)
    rendered = json.dumps(draft)
    assert SECRET not in rendered
    assert all(source["id"] not in rendered for source in sources)
    assert sources == original
    assert draft["review"]["status"] == "needs_review"
    assert draft["runnable"] is False
    assert draft["recorded_responses"] == {}
    assert draft["expected"] == {"should_block_release": False}
    assert "verification_passed" not in draft
    assert "duration_seconds" not in draft
    assert draft["observations"]["stored_verification"] == "verified"


@pytest.mark.parametrize(
    "row,field,value",
    [
        (0, "status", "proposed"),
        (0, "verification_status", "pending"),
        (0, "verification_status", "still_failing"),
        (0, "verification_status", "unverifiable"),
        (0, "verification_evidence", ""),
        (0, "verification_timestamp", 200),
        (0, "verification_timestamp", True),
        (1, "resolved", 0),
        (1, "resolved", "1"),
        (1, "resolved", 1.0),
        (2, "finding_id", "other"),
        (2, "status", "failed"),
        (2, "timestamp", 201),
        (2, "evidence", "not json"),
        (2, "evidence", "[]"),
        (2, "evidence", [" "]),
        (2, "evidence", [{"source": "claim with no observation"}]),
    ],
)
def test_unverified_malformed_or_unlinked_incident_fails_closed(row, field, value):
    source = list(records())
    source[row][field] = value
    with pytest.raises(incident_draft.DraftError):
        incident_draft.build_draft(*source)


def test_arbitrary_labels_and_tool_names_cannot_export_secret_text():
    action, finding, investigation = records()
    action["tool"] = SECRET
    finding["category"] = SECRET
    draft = incident_draft.build_draft(action, finding, investigation)
    assert SECRET not in json.dumps(draft)
    assert draft["observations"]["incident_category"] == "other"


def test_loader_reads_only_selected_action_and_preceding_investigation(monkeypatch):
    from sre_agent.repositories import monitor_repo

    action, finding, investigation = records()
    repo = MagicMock()
    repo.get_action_by_id.return_value = action
    repo.fetch_finding_by_id.return_value = finding
    repo.db.fetchone.return_value = investigation
    monkeypatch.setattr(monitor_repo, "get_monitor_repo", lambda: repo)
    assert incident_draft.load_selected_records(action["id"]) == (action, finding, investigation)
    repo.get_action_by_id.assert_called_once_with(action["id"])
    repo.fetch_finding_by_id.assert_called_once_with(finding["id"])
    query, params = repo.db.fetchone.call_args.args
    assert "timestamp <= ?" in query
    assert "status = 'completed'" in query
    assert params == (finding["id"], action["timestamp"])


def test_private_output_and_overwrite_refusal(tmp_path, monkeypatch):
    from sre_agent import config

    monkeypatch.setattr(
        config, "get_settings", lambda: SimpleNamespace(server=SimpleNamespace(user_evals_dir=tmp_path / "evals"))
    )
    output = tmp_path / "incident.draft.json"
    draft = incident_draft.build_draft(*records())
    incident_draft.write_draft(draft, output)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        incident_draft.write_draft(draft, output)
    with pytest.raises(incident_draft.DraftError):
        incident_draft.write_draft(draft, tmp_path / "evals" / "fixtures" / "bad.json")


def test_cli_requires_explicit_selection_and_sanitizes_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        incident_draft, "load_selected_records", lambda _id: (_ for _ in ()).throw(RuntimeError(SECRET))
    )
    assert incident_draft.main(["--action-id", "selected", "--output", str(tmp_path / "draft.json")]) == 1
    assert SECRET not in capsys.readouterr().out
    assert not (tmp_path / "draft.json").exists()


def test_replay_loader_rejects_draft_even_if_copied_to_fixture_directory(tmp_path, monkeypatch):
    from sre_agent.evals import replay

    (tmp_path / "draft.json").write_text(json.dumps(incident_draft.build_draft(*records())))
    monkeypatch.setattr(replay, "_fixture_dirs", lambda: [tmp_path])
    with pytest.raises(ValueError, match="Unreviewed"):
        replay.load_fixture("draft")


@pytest.mark.parametrize("wrapped", [False, True])
def test_scenario_loader_rejects_copied_draft(monkeypatch, wrapped):
    from sre_agent.evals import scenarios

    draft = incident_draft.build_draft(*records())
    payload = {"suite_name": "bad", "scenarios": [draft]} if wrapped else draft
    monkeypatch.setattr(scenarios, "_packaged_payload", lambda _name: None)
    monkeypatch.setattr(scenarios, "_runtime_payload", lambda _name: payload)
    with pytest.raises(ValueError, match="Unreviewed"):
        scenarios.load_raw_suite("bad")


def test_cli_creates_only_selected_review_draft(tmp_path, monkeypatch, capsys):
    from sre_agent import config

    selected = []

    def load(action_id):
        selected.append(action_id)
        return records()

    monkeypatch.setattr(incident_draft, "load_selected_records", load)
    monkeypatch.setattr(
        config, "get_settings", lambda: SimpleNamespace(server=SimpleNamespace(user_evals_dir=tmp_path / "evals"))
    )
    output = tmp_path / "selected.draft.json"
    assert incident_draft.main(["--action-id", "chosen", "--output", str(output)]) == 0
    assert selected == ["chosen"]
    assert json.loads(output.read_text())["review"]["status"] == "needs_review"
    assert "needs_review" in capsys.readouterr().out
    assert not (tmp_path / "evals").exists()
