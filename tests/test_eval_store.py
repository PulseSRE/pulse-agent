"""Eval artifacts written at runtime must land somewhere a cluster pod can write.

Boot hydration used to target the installed package directory, which is
read-only under OpenShift's arbitrary UID — every restore failed with EACCES,
was swallowed by a catch-all, and DB-persisted eval scenarios and fixtures
were simply never seen again. These tests pin the fix: hydration targets the
settings-configured writable dir, the loaders read that dir alongside the
packaged data, and a write failure is loud, not silent.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

import sre_agent
from sre_agent import artifact_store, eval_store
from sre_agent.artifact_store import KIND_EVAL_FIXTURE, KIND_EVAL_SCENARIO


@pytest.fixture()
def evals_dir(tmp_path: Path, monkeypatch):
    from sre_agent.config import _reset_settings

    monkeypatch.setenv("PULSE_AGENT_USER_EVALS_DIR", str(tmp_path))
    _reset_settings()
    yield tmp_path
    _reset_settings()


def _suite_row(scenario_id: str = "scaffolded_db-only_oom") -> dict:
    suite = {
        "suite_name": "scaffolded",
        "description": "restored from db",
        "scenarios": [
            {
                "scenario_id": scenario_id,
                "category": "sre",
                "description": "Auto-generated: restored",
                "tool_calls": ["describe_pod"],
                "final_response": "resolved",
                "expected": {"should_block_release": False},
            }
        ],
    }
    return {"name": "scaffolded", "rel_path": "scaffolded.json", "content": json.dumps(suite)}


def _fixture_row(name: str = "scaffolded_db-only_oom") -> dict:
    fixture = {"name": name, "prompt": "p", "recorded_responses": {}, "expected": {}}
    return {"name": name, "rel_path": f"{name}.json", "content": json.dumps(fixture)}


class TestWritableTarget:
    def test_dirs_come_from_settings_not_the_package(self, evals_dir):
        package_root = Path(sre_agent.__file__).parent.resolve()
        for directory in (eval_store.scenarios_dir(), eval_store.fixtures_dir()):
            assert not str(directory.resolve()).startswith(str(package_root)), (
                f"{directory} is inside the installed package — read-only on the "
                "cluster, so hydration and scaffolding there fail with EACCES"
            )
        assert eval_store.scenarios_dir() == evals_dir / "scenarios_data"
        assert eval_store.fixtures_dir() == evals_dir / "fixtures"

    def test_boot_hydration_targets_the_settings_dirs(self, evals_dir):
        """The exact regression: hydrate() pointed at Path(__file__)/evals/..."""
        roots: list[Path] = []
        with patch.object(eval_store, "hydrate", side_effect=lambda kind, root: roots.append(root) or 0):
            eval_store.hydrate_evals_dirs()
        assert roots == [evals_dir / "scenarios_data", evals_dir / "fixtures"]

    def test_hydration_restores_db_rows_even_when_package_dir_is_read_only(self, evals_dir, tmp_path):
        """Simulates the cluster: site-packages locked down, settings dir writable."""
        read_only_pkg = tmp_path / "site-packages-evals"
        read_only_pkg.mkdir()
        read_only_pkg.chmod(0o555)
        try:

            def rows(kind: str) -> list[dict]:
                return [_suite_row()] if kind == KIND_EVAL_SCENARIO else [_fixture_row()]

            with patch.object(artifact_store, "list_artifacts", side_effect=rows):
                written = eval_store.hydrate_evals_dirs()

            assert written == 2
            assert (evals_dir / "scenarios_data" / "scaffolded.json").exists()
            assert (evals_dir / "fixtures" / "scaffolded_db-only_oom.json").exists()
        finally:
            read_only_pkg.chmod(0o755)

    def test_hydration_into_a_read_only_dir_is_not_silent(self, tmp_path, monkeypatch, caplog):
        """If hydration ever targets an unwritable dir again, it must say so."""
        from sre_agent.config import _reset_settings

        root = tmp_path / "ro-evals"
        (root / "scenarios_data").mkdir(parents=True)
        (root / "fixtures").mkdir()
        monkeypatch.setenv("PULSE_AGENT_USER_EVALS_DIR", str(root))
        _reset_settings()
        (root / "scenarios_data").chmod(0o555)
        (root / "fixtures").chmod(0o555)
        try:

            def rows(kind: str) -> list[dict]:
                return [_suite_row()] if kind == KIND_EVAL_SCENARIO else [_fixture_row()]

            with (
                patch.object(artifact_store, "list_artifacts", side_effect=rows),
                caplog.at_level(logging.WARNING, logger="pulse_agent.artifact_store"),
            ):
                written = eval_store.hydrate_evals_dirs()

            assert written == 0
            assert "Failed to restore" in caplog.text, (
                "hydration into a read-only directory restored nothing and logged "
                "nothing — the EACCES-swallowed-at-boot bug is back"
            )
        finally:
            (root / "scenarios_data").chmod(0o755)
            (root / "fixtures").chmod(0o755)
            _reset_settings()


class TestReadBack:
    """The audit gap: persisted evals must be read by the suite, not just written."""

    def test_hydrated_scenarios_appear_in_load_suite(self, evals_dir):
        from sre_agent.evals.scenarios import load_suite

        def rows(kind: str) -> list[dict]:
            return [_suite_row("scaffolded_db-only_oom")] if kind == KIND_EVAL_SCENARIO else []

        with patch.object(artifact_store, "list_artifacts", side_effect=rows):
            eval_store.hydrate_evals_dirs()

        ids = {s.scenario_id for s in load_suite("scaffolded")}
        assert "scaffolded_db-only_oom" in ids

    def test_runtime_suite_merges_with_packaged_scenarios(self, evals_dir):
        from sre_agent.evals.scenarios import load_suite

        packaged_ids = {s.scenario_id for s in load_suite("scaffolded")}
        assert packaged_ids, "packaged scaffolded suite should ship at least one scenario"

        (evals_dir / "scenarios_data").mkdir(parents=True, exist_ok=True)
        (evals_dir / "scenarios_data" / "scaffolded.json").write_text(_suite_row()["content"])

        merged_ids = {s.scenario_id for s in load_suite("scaffolded")}
        assert packaged_ids <= merged_ids
        assert "scaffolded_db-only_oom" in merged_ids

    def test_runtime_scenario_wins_on_id_collision(self, evals_dir):
        from sre_agent.evals.scenarios import load_suite

        packaged = load_suite("scaffolded")
        target = packaged[0].scenario_id
        row = _suite_row(target)
        (evals_dir / "scenarios_data").mkdir(parents=True, exist_ok=True)
        (evals_dir / "scenarios_data" / "scaffolded.json").write_text(row["content"])

        merged = {s.scenario_id: s for s in load_suite("scaffolded")}
        assert merged[target].description == "Auto-generated: restored"

    def test_packaged_only_suites_still_load(self, evals_dir):
        from sre_agent.evals.scenarios import load_suite

        assert load_suite("core")

    def test_missing_suite_still_raises(self, evals_dir):
        from sre_agent.evals.scenarios import load_suite

        with pytest.raises(FileNotFoundError):
            load_suite("does_not_exist")

    def test_hydrated_fixtures_are_listed_and_loadable(self, evals_dir):
        from sre_agent.evals.replay import list_fixtures, load_fixture

        def rows(kind: str) -> list[dict]:
            return [_fixture_row()] if kind == KIND_EVAL_FIXTURE else []

        with patch.object(artifact_store, "list_artifacts", side_effect=rows):
            eval_store.hydrate_evals_dirs()

        assert "scaffolded_db-only_oom" in list_fixtures()
        assert load_fixture("scaffolded_db-only_oom")["name"] == "scaffolded_db-only_oom"

    def test_bundled_fixture_wins_over_runtime_copy(self, evals_dir):
        """A fixture shipped in the image is the image's to define."""
        from sre_agent.evals.replay import load_fixture

        (evals_dir / "fixtures").mkdir(parents=True, exist_ok=True)
        (evals_dir / "fixtures" / "crashloop_diagnosis.json").write_text('{"name": "shadow"}')

        assert load_fixture("crashloop_diagnosis")["name"] != "shadow"
