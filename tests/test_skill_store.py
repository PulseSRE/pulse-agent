"""Skills created at runtime must outlive the pod that made them."""

from unittest.mock import MagicMock, patch

import pytest

from sre_agent import skill_store


@pytest.fixture
def fake_db():
    db = MagicMock()
    with patch("sre_agent.db.get_database", return_value=db):
        yield db


class TestPersist:
    def test_persist_upserts_and_bumps_version(self, fake_db):
        assert skill_store.persist_skill("my_skill", "---\nname: my_skill\n---\nbody") is True
        sql, params = fake_db.execute.call_args[0]
        assert "INSERT INTO user_skills" in sql
        assert "ON CONFLICT (name) DO UPDATE" in sql
        assert "version = user_skills.version + 1" in sql
        # Directory name is the on-disk form, which uses hyphens.
        assert params[0] == "my_skill"
        assert params[1] == "my-skill"

    def test_persist_records_the_source(self, fake_db):
        skill_store.persist_skill("s", "body", source="scaffolded")
        assert fake_db.execute.call_args[0][1][3] == "scaffolded"

    def test_a_dead_database_does_not_break_skill_creation(self):
        """Losing durability is bad; refusing to work at all is worse."""
        with patch("sre_agent.db.get_database", side_effect=RuntimeError("no db")):
            assert skill_store.persist_skill("s", "body") is False

    def test_a_failing_write_is_swallowed(self, fake_db):
        fake_db.execute.side_effect = RuntimeError("connection reset")
        assert skill_store.persist_skill("s", "body") is False

    def test_forget_removes_the_row_so_a_delete_is_not_undone_on_boot(self, fake_db):
        assert skill_store.forget_skill("s") is True
        sql, params = fake_db.execute.call_args[0]
        assert "DELETE FROM user_skills" in sql
        assert params == ("s",)


class TestHydrate:
    def _row(self, name="learned_skill", content="---\nname: learned\n---\nbody"):
        return {"name": name, "dir_name": name.replace("_", "-"), "content": content}

    def test_writes_persisted_skills_to_disk(self, tmp_path):
        with patch.object(skill_store, "list_stored_skills", return_value=[self._row()]):
            written = skill_store.hydrate_skills_dir(tmp_path)
        assert written == 1
        assert (tmp_path / "learned-skill" / "skill.md").read_text().startswith("---")

    def test_does_not_clobber_a_file_already_on_disk(self, tmp_path):
        """A file present is either the image's or this pod's own newer write."""
        target = tmp_path / "learned-skill" / "skill.md"
        target.parent.mkdir(parents=True)
        target.write_text("newer on-disk content")
        with patch.object(skill_store, "list_stored_skills", return_value=[self._row()]):
            written = skill_store.hydrate_skills_dir(tmp_path)
        assert written == 0
        assert target.read_text() == "newer on-disk content"

    def test_empty_store_is_a_no_op(self, tmp_path):
        with patch.object(skill_store, "list_stored_skills", return_value=[]):
            assert skill_store.hydrate_skills_dir(tmp_path) == 0

    def test_one_bad_row_does_not_stop_the_rest(self, tmp_path):
        bad = {"name": "bad", "dir_name": None, "content": "x"}
        with patch.object(skill_store, "list_stored_skills", return_value=[bad, self._row()]):
            written = skill_store.hydrate_skills_dir(tmp_path)
        assert written == 1
