"""Agent-authored skills must be born unreviewed.

Skill.reviewed defaults to True, so a create path that simply omits the key
produces a skill indistinguishable from one a person wrote and vetted. Both
write paths did exactly that.
"""

from __future__ import annotations

import yaml

from sre_agent.skill_loader import _parse_skill_md


def _frontmatter(text: str) -> dict:
    parts = text.split("---")
    return yaml.safe_load(parts[1])


def test_create_skill_frontmatter_marks_unreviewed(tmp_path, monkeypatch):
    from sre_agent import self_tools, skill_loader

    # create_skill imports these from skill_loader at call time, so patch there.
    monkeypatch.setattr(skill_loader, "_get_user_skills_dir", lambda: tmp_path)
    monkeypatch.setattr(skill_loader, "reload_skills", lambda: {})

    self_tools.create_skill(
        name="etcdwatch",
        description="Watch etcd latency",
        keywords="etcd, latency",
        prompt="Investigate etcd latency.",
    )
    written = tmp_path / "etcdwatch" / "skill.md"
    if not written.exists():  # dir layout differs; find it
        found = list(tmp_path.rglob("skill.md"))
        assert found, "create_skill wrote no skill.md"
        written = found[0]

    meta = _frontmatter(written.read_text())
    assert meta.get("reviewed") is False
    assert meta.get("generated_by") == "auto"


def test_parsed_skill_object_is_unreviewed(tmp_path):
    """The flag must survive the round trip through _parse_skill_md."""
    content = (
        "---\n"
        "name: agentmade\n"
        "description: written by the agent\n"
        "keywords: [a, b]\n"
        "reviewed: false\n"
        "generated_by: auto\n"
        "---\n\nBody.\n"
    )
    f = tmp_path / "skill.md"
    f.write_text(content)
    parsed = _parse_skill_md(f)
    assert parsed is not None
    assert parsed.reviewed is False


def test_default_is_reviewed_for_human_skills(tmp_path):
    """Human-authored skills omit the key and must stay routable."""
    f = tmp_path / "skill.md"
    f.write_text("---\nname: sre\ndescription: d\nkeywords: [a]\n---\n\nBody.\n")
    parsed = _parse_skill_md(f)
    assert parsed is not None
    assert parsed.reviewed is True
