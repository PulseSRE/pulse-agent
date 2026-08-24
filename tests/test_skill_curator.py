"""Skill curator: the forgetting half of learning.

Proposes (never performs) archiving of unused agent-created skills and
consolidation of near-duplicates. Archive is a recoverable move, pinning is
an exemption, and built-in / human-authored skills are untouchable.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from sre_agent.skill_curator import (
    archive_skill,
    find_overlapping_skills,
    find_stale_skills,
    list_archived,
    restore_skill,
)
from sre_agent.skill_loader import Skill


def _skill(name: str, tmp_path: Path, *, generated_by="auto", pinned=False, keywords=None, age_days=60) -> Skill:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    f = d / "skill.md"
    f.write_text(f"---\nname: {name}\n---\nbody")
    import os

    old = time.time() - age_days * 86400
    os.utime(f, (old, old))
    return Skill(
        name=name,
        version=1,
        description="",
        keywords=keywords or [],
        categories=[],
        write_tools=False,
        priority=10,
        system_prompt="",
        path=d,
        builtin=False,
        generated_by=generated_by,
        pinned=pinned,
    )


class TestStaleDetection:
    def test_unused_old_agent_skill_is_stale(self, tmp_path):
        s = _skill("auto-old", tmp_path)
        with (
            patch("sre_agent.skill_loader.list_skills", return_value=[s]),
            patch("sre_agent.skill_curator._recent_invocations", return_value={"sre": 40}),
        ):
            stale = find_stale_skills()
        assert [x["name"] for x in stale] == ["auto-old"]

    def test_recently_used_skill_is_not_stale(self, tmp_path):
        s = _skill("auto-busy", tmp_path)
        with (
            patch("sre_agent.skill_loader.list_skills", return_value=[s]),
            patch("sre_agent.skill_curator._recent_invocations", return_value={"auto-busy": 3}),
        ):
            assert find_stale_skills() == []

    def test_pinned_skill_is_exempt(self, tmp_path):
        s = _skill("auto-pinned", tmp_path, pinned=True)
        with (
            patch("sre_agent.skill_loader.list_skills", return_value=[s]),
            patch("sre_agent.skill_curator._recent_invocations", return_value={"sre": 40}),
        ):
            assert find_stale_skills() == []

    def test_young_skill_gets_time_to_earn_traffic(self, tmp_path):
        s = _skill("auto-new", tmp_path, age_days=5)
        with (
            patch("sre_agent.skill_loader.list_skills", return_value=[s]),
            patch("sre_agent.skill_curator._recent_invocations", return_value={"sre": 40}),
        ):
            assert find_stale_skills() == []

    def test_human_authored_skills_are_never_curated(self, tmp_path):
        s = _skill("hand-written", tmp_path, generated_by="")
        with (
            patch("sre_agent.skill_loader.list_skills", return_value=[s]),
            patch("sre_agent.skill_curator._recent_invocations", return_value={"sre": 40}),
        ):
            assert find_stale_skills() == []

    def test_no_usage_data_proposes_nothing(self, tmp_path):
        """Absence of analytics is not evidence of absence of use — a failed
        query must never manufacture an archive proposal."""
        s = _skill("auto-old", tmp_path)
        with (
            patch("sre_agent.skill_loader.list_skills", return_value=[s]),
            patch("sre_agent.skill_curator._recent_invocations", return_value={}),
        ):
            assert find_stale_skills() == []


class TestOverlapDetection:
    def test_near_duplicate_lessons_are_flagged(self, tmp_path):
        a = _skill("auto-a", tmp_path, keywords=["crashloop", "restart", "pod", "backoff"])
        b = _skill("auto-b", tmp_path, keywords=["crashloop", "restart", "pod", "probe"])
        with patch("sre_agent.skill_loader.list_skills", return_value=[a, b]):
            pairs = find_overlapping_skills()
        assert len(pairs) == 1
        assert sorted(pairs[0]["names"]) == ["auto-a", "auto-b"]

    def test_distinct_skills_are_not_flagged(self, tmp_path):
        a = _skill("auto-a", tmp_path, keywords=["crashloop", "restart"])
        b = _skill("auto-b", tmp_path, keywords=["certificate", "expiry"])
        with patch("sre_agent.skill_loader.list_skills", return_value=[a, b]):
            assert find_overlapping_skills() == []

    def test_pinned_skills_are_exempt_from_consolidation(self, tmp_path):
        a = _skill("auto-a", tmp_path, keywords=["crashloop", "restart"], pinned=True)
        b = _skill("auto-b", tmp_path, keywords=["crashloop", "restart"])
        with patch("sre_agent.skill_loader.list_skills", return_value=[a, b]):
            assert find_overlapping_skills() == []


class TestArchiveRestore:
    def test_archive_moves_and_restore_brings_back(self, tmp_path):
        s = _skill("auto-x", tmp_path)
        with (
            patch("sre_agent.skill_loader.get_skill", return_value=s),
            patch("sre_agent.skill_loader.reload_skills"),
        ):
            dest = archive_skill("auto-x")
        assert dest == tmp_path / ".archive" / "auto-x"
        assert (dest / "skill.md").exists()
        assert not (tmp_path / "auto-x").exists()

        with (
            patch("sre_agent.skill_curator._archive_roots", return_value=[tmp_path / ".archive"]),
            patch("sre_agent.skill_loader.reload_skills"),
        ):
            assert list_archived() == [
                {"name": "auto-x", "archived_at": pytest.approx(dest.stat().st_mtime * 1000, abs=2000)}
            ]
            back = restore_skill("auto-x")
        assert back == tmp_path / "auto-x"
        assert (back / "skill.md").exists()
        assert not dest.exists()

    def test_archive_refuses_non_agent_skills(self, tmp_path):
        s = _skill("hand-written", tmp_path, generated_by="")
        with patch("sre_agent.skill_loader.get_skill", return_value=s):
            with pytest.raises(ValueError, match="not agent-created"):
                archive_skill("hand-written")

    def test_archive_refuses_pinned_skills(self, tmp_path):
        s = _skill("auto-pinned", tmp_path, pinned=True)
        with patch("sre_agent.skill_loader.get_skill", return_value=s):
            with pytest.raises(ValueError, match="pinned"):
                archive_skill("auto-pinned")

    def test_restore_refuses_to_clobber_a_live_skill(self, tmp_path):
        s = _skill("auto-x", tmp_path)
        with (
            patch("sre_agent.skill_loader.get_skill", return_value=s),
            patch("sre_agent.skill_loader.reload_skills"),
        ):
            archive_skill("auto-x")
        _skill("auto-x", tmp_path)  # a new live skill takes the name
        with patch("sre_agent.skill_curator._archive_roots", return_value=[tmp_path / ".archive"]):
            with pytest.raises(ValueError, match="already exists"):
                restore_skill("auto-x")


class TestLoaderSkipsArchive:
    def test_archived_skills_do_not_load(self, tmp_path):
        from sre_agent.skill_loader import load_skills

        live = tmp_path / "live-skill"
        live.mkdir()
        (live / "skill.md").write_text("---\nname: live_skill\ndescription: d\nkeywords: [x]\n---\nbody")
        archived = tmp_path / ".archive" / "dead-skill"
        archived.mkdir(parents=True)
        (archived / "skill.md").write_text("---\nname: dead_skill\ndescription: d\nkeywords: [x]\n---\nbody")

        with patch("sre_agent.skill_loader._get_user_skills_dir", return_value=tmp_path / "nouser"):
            loaded = load_skills(skills_dir=tmp_path)
        assert "live_skill" in loaded
        assert "dead_skill" not in loaded


class TestCurationGenerator:
    def test_generator_emits_proposals_not_actions(self, tmp_path):
        from sre_agent.inbox_generators import gen_skill_curation

        stale = [{"name": "auto-old", "age_days": 60, "reviewed": True, "quarantined": False, "incident_type": "x"}]
        pairs = [{"names": ["auto-a", "auto-b"], "overlap": 0.75, "shared_keywords": ["crashloop"]}]
        with (
            patch("sre_agent.skill_curator.find_stale_skills", return_value=stale),
            patch("sre_agent.skill_curator.find_overlapping_skills", return_value=pairs),
        ):
            items = gen_skill_curation()
        assert len(items) == 2
        assert all(i["metadata"]["generator"] == "skill_curation" for i in items)
        assert items[0]["correlation_key"] == "skill-stale:auto-old"
        assert "archive" in items[0]["summary"]
        assert items[1]["correlation_key"] == "skill-overlap:auto-a:auto-b"
