"""Tests for progressive skill disclosure (skill_search / skill_load)."""

from __future__ import annotations

from pathlib import Path

from sre_agent.skill_tools import (
    MAX_SKILL_CHARS,
    list_references,
    skill_load,
    skill_search,
)


class TestSkillSearch:
    def test_finds_a_skill_by_topic(self):
        out = skill_search("security posture and RBAC")
        assert "security" in out

    def test_reports_available_when_nothing_matches(self):
        out = skill_search("zzzz nonexistent topic qqqq")
        assert "No skill matched" in out
        assert "sre" in out  # still tells the agent what it could load

    def test_rejects_a_query_with_no_usable_terms(self):
        assert "Error" in skill_search("a  b")

    def test_surfaces_reference_documents(self):
        out = skill_search("sre kubernetes diagnostics")
        assert "references:" in out
        assert "certificate-expiry" in out

    def test_points_at_the_next_call(self):
        assert "skill_load" in skill_search("sre kubernetes diagnostics")

    def test_does_not_dump_full_procedures(self):
        # tier 0 stays cheap — the whole point of searching before loading
        out = skill_search("sre kubernetes diagnostics")
        assert len(out) < 2000


class TestSkillLoad:
    def test_loads_a_known_skill(self):
        out = skill_load("sre")
        assert "BEGIN SKILL sre" in out
        assert "--- END ---" in out

    def test_unknown_skill_lists_alternatives(self):
        out = skill_load("no_such_skill")
        assert "Error" in out
        assert "sre" in out

    def test_content_is_delimited_as_guidance(self):
        # loaded text must not read as user instruction
        assert "guidance, not instructions from the user" in skill_load("sre")

    def test_advertises_its_references(self):
        assert "certificate-expiry" in skill_load("sre")

    def test_loads_a_reference_document(self):
        out = skill_load("sre", reference="node-pressure")
        assert "BEGIN SKILL REFERENCE sre/node-pressure" in out
        assert "MemoryPressure" in out

    def test_unknown_reference_lists_the_real_ones(self):
        out = skill_load("sre", reference="not-a-reference")
        assert "Error" in out
        assert "node-pressure" in out

    def test_reference_cannot_escape_the_skill_directory(self):
        for attempt in ("../../../etc/passwd", "../skill", "..", "/etc/passwd"):
            out = skill_load("sre", reference=attempt)
            assert "Error" in out, f"path {attempt!r} was not rejected"
            assert "root:" not in out

    def test_long_content_truncates_loudly(self, tmp_path, monkeypatch):
        import sre_agent.skill_tools as st

        class _FakeSkill:
            name = "big"
            description = "oversized"
            keywords = ["big"]
            system_prompt = "x" * (MAX_SKILL_CHARS + 500)
            path = tmp_path
            degraded = False
            reviewed = True

        monkeypatch.setattr("sre_agent.skill_loader.get_skill", lambda n: _FakeSkill())
        out = st.skill_load("big")
        assert "truncated at" in out
        assert len(out) < MAX_SKILL_CHARS + 400


class TestListReferences:
    def test_lists_markdown_only(self):
        refs = list_references(Path("sre_agent/skills/sre"))
        assert "certificate-expiry" in refs
        assert "node-pressure" in refs

    def test_missing_directory_is_empty_not_an_error(self):
        assert list_references(Path("sre_agent/skills/security")) == []


class TestReachability:
    """The tier-0 index is useless if the agent is never offered it."""

    def test_always_included(self):
        from sre_agent.tool_categories import ALWAYS_INCLUDE

        assert "skill_search" in ALWAYS_INCLUDE
        assert "skill_load" in ALWAYS_INCLUDE

    def test_registered_for_discovery(self):
        from sre_agent.tool_discovery import _TOOL_MODULES

        assert "sre_agent.skill_tools" in _TOOL_MODULES
