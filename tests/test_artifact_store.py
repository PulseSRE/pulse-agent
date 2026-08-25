"""Runtime-written documents must outlive the pod that wrote them.

Skills, plan templates, scaffolded evals and version history were all written
to the container's overlay filesystem and erased by every restart and deploy.
"""

from unittest.mock import MagicMock, patch

import pytest

from sre_agent import artifact_store, plan_store, skill_store
from sre_agent.artifact_store import KIND_PLAN, KIND_SKILL


@pytest.fixture
def fake_db():
    db = MagicMock()
    db.fetchone.return_value = None  # no prior revision by default
    with patch("sre_agent.db.get_database", return_value=db):
        yield db


def _sql_calls(db):
    return [c[0][0] for c in db.execute.call_args_list]


class TestPersist:
    def test_upserts_and_bumps_version(self, fake_db):
        assert artifact_store.persist(KIND_SKILL, "s", "body", rel_path="s/skill.md") is True
        sql = _sql_calls(fake_db)[-1]
        assert "INSERT INTO runtime_artifacts" in sql
        assert "ON CONFLICT (kind, name) DO UPDATE" in sql
        assert "version = runtime_artifacts.version + 1" in sql

    def test_archives_the_prior_body_before_overwriting(self, fake_db):
        """An edit must stay reversible — this replaces the ephemeral .versions/ dir."""
        fake_db.fetchone.return_value = {"version": 3, "content": "old body", "created_by": "ali"}
        artifact_store.persist(KIND_SKILL, "s", "new body", rel_path="s/skill.md")

        calls = fake_db.execute.call_args_list
        archive_sql, archive_params = calls[0][0]
        assert "INSERT INTO runtime_artifact_versions" in archive_sql
        assert archive_params[2] == 3
        assert archive_params[3] == "old body"

    def test_first_write_archives_nothing(self, fake_db):
        artifact_store.persist(KIND_SKILL, "s", "body", rel_path="s/skill.md")
        assert not any("runtime_artifact_versions" in s for s in _sql_calls(fake_db))

    def test_a_dead_database_does_not_break_the_write_path(self):
        """Losing durability is bad; refusing to work at all is worse."""
        with patch("sre_agent.db.get_database", side_effect=RuntimeError("no db")):
            assert artifact_store.persist(KIND_SKILL, "s", "b", rel_path="s/skill.md") is False

    def test_retire_keeps_history(self, fake_db):
        assert artifact_store.forget(KIND_SKILL, "s") is True
        sql, params = fake_db.execute.call_args[0]
        assert "DELETE FROM runtime_artifacts" in sql
        assert "runtime_artifact_versions" not in sql
        assert params == (KIND_SKILL, "s")


class TestHydrate:
    def _row(self, name="learned", rel="learned/skill.md", content="body"):
        return {"name": name, "rel_path": rel, "content": content}

    def test_writes_persisted_artifacts_to_disk(self, tmp_path):
        with patch.object(artifact_store, "list_artifacts", return_value=[self._row()]):
            assert artifact_store.hydrate(KIND_SKILL, tmp_path) == 1
        assert (tmp_path / "learned" / "skill.md").read_text() == "body"

    def test_does_not_clobber_a_file_already_on_disk(self, tmp_path):
        target = tmp_path / "learned" / "skill.md"
        target.parent.mkdir(parents=True)
        target.write_text("newer on-disk content")
        with patch.object(artifact_store, "list_artifacts", return_value=[self._row()]):
            assert artifact_store.hydrate(KIND_SKILL, tmp_path) == 0
        assert target.read_text() == "newer on-disk content"

    def test_refuses_a_path_that_escapes_the_root(self, tmp_path):
        escape = self._row(name="evil", rel="../../etc/pwned")
        with patch.object(artifact_store, "list_artifacts", return_value=[escape]):
            assert artifact_store.hydrate(KIND_SKILL, tmp_path) == 0

    def test_one_bad_row_does_not_stop_the_rest(self, tmp_path):
        bad = {"name": "bad", "rel_path": None, "content": "x"}
        with patch.object(artifact_store, "list_artifacts", return_value=[bad, self._row()]):
            assert artifact_store.hydrate(KIND_SKILL, tmp_path) == 1


class TestKindHelpers:
    def test_skill_rel_path_uses_the_on_disk_hyphen_form(self, fake_db):
        skill_store.persist_skill("my_skill", "body")
        params = fake_db.execute.call_args[0][1]
        assert params[0] == KIND_SKILL
        assert params[1] == "my_skill"
        assert params[2] == "my-skill/skill.md"

    def test_plan_rel_path_is_the_incident_type_yaml(self, fake_db):
        plan_store.persist_plan("crashloop", "id: x")
        params = fake_db.execute.call_args[0][1]
        assert params[0] == KIND_PLAN
        assert params[2] == "crashloop.yaml"

    def test_source_is_recorded(self, fake_db):
        skill_store.persist_skill("s", "body", source="scaffolded")
        assert fake_db.execute.call_args[0][1][4] == "scaffolded"


class TestAgainstARealDatabase:
    """Exercises the store against Postgres rather than a mock.

    The mocked tests above assert the SQL, which is not the same as asserting
    the effect. Database.execute keeps its connection checked out until commit,
    so an uncommitted write is silently discarded — the artifact looks persisted
    and is gone on the next restart, which is precisely the bug this module
    exists to fix. Only a real round-trip catches that.
    """

    @pytest.fixture(autouse=True)
    def _clean(self):
        from sre_agent.db import get_database

        db = get_database()
        # The pattern goes in a parameter: Database.execute always passes a
        # params tuple, so a literal % in the SQL is read as a placeholder.
        db.execute("DELETE FROM runtime_artifacts WHERE name LIKE ?", ("itest\\_%",))
        db.execute("DELETE FROM runtime_artifact_versions WHERE name LIKE ?", ("itest\\_%",))
        db.commit()
        yield

    def test_a_write_is_actually_committed(self):
        assert artifact_store.persist(KIND_SKILL, "itest_skill", "body", rel_path="itest/skill.md") is True
        rows = {r["name"]: r for r in artifact_store.list_artifacts(KIND_SKILL)}
        assert "itest_skill" in rows
        assert rows["itest_skill"]["content"] == "body"
        assert rows["itest_skill"]["rel_path"] == "itest/skill.md"

    def test_rewriting_bumps_the_version_and_archives_the_old_body(self):
        artifact_store.persist(KIND_SKILL, "itest_skill", "v1", rel_path="itest/skill.md")
        artifact_store.persist(KIND_SKILL, "itest_skill", "v2", rel_path="itest/skill.md")
        artifact_store.persist(KIND_SKILL, "itest_skill", "v3", rel_path="itest/skill.md")

        current = {r["name"]: r for r in artifact_store.list_artifacts(KIND_SKILL)}["itest_skill"]
        assert current["version"] == 3
        assert current["content"] == "v3"

        history = artifact_store.list_versions(KIND_SKILL, "itest_skill")
        assert [(h["version"], h["content"]) for h in history] == [(2, "v2"), (1, "v1")]

    def test_kinds_do_not_collide_on_the_same_name(self):
        artifact_store.persist(KIND_SKILL, "itest_shared", "skill body", rel_path="itest/skill.md")
        artifact_store.persist(KIND_PLAN, "itest_shared", "plan body", rel_path="itest.yaml")
        skills = {r["name"]: r["content"] for r in artifact_store.list_artifacts(KIND_SKILL)}
        plans = {r["name"]: r["content"] for r in artifact_store.list_artifacts(KIND_PLAN)}
        assert skills["itest_shared"] == "skill body"
        assert plans["itest_shared"] == "plan body"

    def test_retiring_removes_the_current_row_but_keeps_history(self):
        artifact_store.persist(KIND_SKILL, "itest_gone", "v1", rel_path="itest/skill.md")
        artifact_store.persist(KIND_SKILL, "itest_gone", "v2", rel_path="itest/skill.md")
        assert artifact_store.forget(KIND_SKILL, "itest_gone") is True

        names = [r["name"] for r in artifact_store.list_artifacts(KIND_SKILL)]
        assert "itest_gone" not in names
        assert artifact_store.list_versions(KIND_SKILL, "itest_gone")

    def test_hydrate_round_trip_restores_what_was_written(self, tmp_path):
        """The whole point: a write survives into a fresh filesystem."""
        artifact_store.persist(KIND_SKILL, "itest_round", "restored body", rel_path="itest-round/skill.md")
        written = artifact_store.hydrate(KIND_SKILL, tmp_path)
        assert written >= 1
        assert (tmp_path / "itest-round" / "skill.md").read_text() == "restored body"
