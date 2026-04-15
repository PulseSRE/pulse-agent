"""Tests for skill loader."""

from __future__ import annotations

from pathlib import Path

from sre_agent.skill_loader import (
    Skill,
    _parse_skill_md,
    check_handoff,
    classify_query,
    get_mode_categories,
    get_skill,
    list_skills,
    load_skill_evals,
    load_skills,
)


class TestLoadSkills:
    def test_loads_built_in_skills(self):
        skills = load_skills()
        names = set(skills.keys())
        assert "sre" in names
        assert "security" in names
        assert "view_designer" in names

    def test_at_least_3_skills(self):
        skills = load_skills()
        assert len(skills) >= 3

    def test_capacity_planner_loaded(self):
        skills = load_skills()
        assert "capacity_planner" in skills

    def test_list_skills(self):
        result = list_skills()
        assert len(result) >= 3
        assert all(isinstance(s, Skill) for s in result)


class TestParseSkillMd:
    def test_parse_sre(self):
        skill_dir = Path(__file__).parent.parent / "sre_agent" / "skills" / "sre"
        skill = _parse_skill_md(skill_dir / "skill.md")
        assert skill is not None
        assert skill.name == "sre"
        assert skill.version >= 1
        assert skill.write_tools is True
        assert len(skill.keywords) > 5
        assert len(skill.categories) >= 5
        assert "Security" in skill.system_prompt

    def test_parse_security(self):
        skill_dir = Path(__file__).parent.parent / "sre_agent" / "skills" / "security"
        skill = _parse_skill_md(skill_dir / "skill.md")
        assert skill is not None
        assert skill.name == "security"
        assert skill.write_tools is False
        assert "request_sre_investigation" in skill.requires_tools

    def test_parse_view_designer(self):
        skill_dir = Path(__file__).parent.parent / "sre_agent" / "skills" / "view-designer"
        skill = _parse_skill_md(skill_dir / "skill.md")
        assert skill is not None
        assert skill.name == "view_designer"
        assert skill.write_tools is False
        assert "create_dashboard" in skill.requires_tools

    def test_parse_capacity_planner(self):
        skill_dir = Path(__file__).parent.parent / "sre_agent" / "skills" / "capacity-planner"
        skill = _parse_skill_md(skill_dir / "skill.md")
        assert skill is not None
        assert skill.name == "capacity_planner"
        assert len(skill.configurable) >= 2
        assert skill.handoff_to.get("sre") is not None

    def test_invalid_file_returns_none(self, tmp_path):
        bad_file = tmp_path / "bad.md"
        bad_file.write_text("no frontmatter here")
        assert _parse_skill_md(bad_file) is None

    def test_missing_name_returns_none(self, tmp_path):
        bad_file = tmp_path / "noname.md"
        bad_file.write_text("---\nversion: 1\n---\nSome prompt")
        assert _parse_skill_md(bad_file) is None


class TestClassifyQuery:
    def test_sre_query(self):
        skill = classify_query("why is my pod crashlooping in production?")
        assert skill.name == "sre"

    def test_security_query(self):
        skill = classify_query("scan for RBAC vulnerabilities")
        assert skill.name == "security"

    def test_view_designer_query(self):
        skill = classify_query("create a dashboard for production")
        assert skill.name == "view_designer"

    def test_capacity_query(self):
        skill = classify_query("how much headroom do we have on the cluster?")
        assert skill.name == "capacity_planner"

    def test_capacity_forecast(self):
        skill = classify_query("will we run out of CPU?")
        assert skill.name == "capacity_planner"

    def test_fallback_to_sre(self):
        skill = classify_query("hello")
        assert skill.name == "sre"  # default fallback

    def test_longer_keywords_win(self):
        # "create view" is longer than "create" so view_designer should win
        skill = classify_query("create view for monitoring")
        assert skill.name == "view_designer"


class TestHandoff:
    def test_sre_to_view_designer(self):
        sre = get_skill("sre")
        assert sre is not None
        target = check_handoff(sre, "now create a dashboard")
        assert target is not None
        assert target.name == "view_designer"

    def test_sre_to_security(self):
        sre = get_skill("sre")
        target = check_handoff(sre, "scan for vulnerabilities")
        assert target is not None
        assert target.name == "security"

    def test_no_handoff(self):
        sre = get_skill("sre")
        target = check_handoff(sre, "list pods in production")
        assert target is None

    def test_security_to_sre(self):
        security = get_skill("security")
        assert security is not None
        target = check_handoff(security, "fix the RBAC issue")
        assert target is not None
        assert target.name == "sre"

    def test_capacity_to_sre(self):
        cap = get_skill("capacity_planner")
        assert cap is not None
        target = check_handoff(cap, "scale the deployment")
        assert target is not None
        assert target.name == "sre"


class TestModeCategoriesIntegration:
    def test_builds_from_skills(self):
        cats = get_mode_categories()
        assert "sre" in cats
        assert "security" in cats
        assert "view_designer" in cats
        assert "both" in cats
        assert cats["both"] is None  # all tools

    def test_sre_has_expected_categories(self):
        cats = get_mode_categories()
        sre_cats = cats["sre"]
        assert "diagnostics" in sre_cats
        assert "workloads" in sre_cats
        assert "monitoring" in sre_cats

    def test_security_has_limited_categories(self):
        cats = get_mode_categories()
        sec_cats = cats["security"]
        assert "security" in sec_cats
        assert "operations" not in sec_cats


class TestSkillEvals:
    def test_load_sre_evals(self):
        scenarios = load_skill_evals("sre")
        assert len(scenarios) >= 4
        ids = [s["id"] for s in scenarios]
        assert "sre_crashloop" in ids

    def test_load_security_evals(self):
        scenarios = load_skill_evals("security")
        assert len(scenarios) >= 4

    def test_load_capacity_evals(self):
        scenarios = load_skill_evals("capacity_planner")
        assert len(scenarios) >= 4

    def test_load_nonexistent_skill(self):
        scenarios = load_skill_evals("nonexistent_skill_xyz")
        assert scenarios == []


class TestSchemaValidation:
    def test_valid_skill_no_warnings(self):
        from sre_agent.skill_loader import _validate_schema

        meta = {
            "name": "test",
            "version": 1,
            "description": "Test skill",
            "keywords": ["test"],
            "categories": ["diagnostics"],
            "priority": 5,
        }
        errors = _validate_schema(meta, Path("."))
        assert errors == []

    def test_invalid_version(self):
        from sre_agent.skill_loader import _validate_schema

        errors = _validate_schema({"name": "t", "version": -1, "description": "t", "keywords": ["x"]}, Path("."))
        assert any("version" in e for e in errors)

    def test_missing_description(self):
        from sre_agent.skill_loader import _validate_schema

        errors = _validate_schema({"name": "t", "version": 1, "keywords": ["x"]}, Path("."))
        assert any("description" in e for e in errors)

    def test_no_keywords_warning(self):
        from sre_agent.skill_loader import _validate_schema

        errors = _validate_schema({"name": "t", "version": 1, "description": "t"}, Path("."))
        assert any("keywords" in e for e in errors)

    def test_invalid_category(self):
        from sre_agent.skill_loader import _validate_schema

        errors = _validate_schema(
            {"name": "t", "version": 1, "description": "t", "keywords": ["x"], "categories": ["invalid_xyz"]},
            Path("."),
        )
        assert any("unknown category" in e for e in errors)

    def test_valid_categories_accepted(self):
        from sre_agent.skill_loader import _validate_schema

        errors = _validate_schema(
            {
                "name": "t",
                "version": 1,
                "description": "t",
                "keywords": ["x"],
                "categories": ["diagnostics", "monitoring", "workloads"],
            },
            Path("."),
        )
        cat_errors = [e for e in errors if "category" in e]
        assert cat_errors == []

    def test_invalid_priority(self):
        from sre_agent.skill_loader import _validate_schema

        errors = _validate_schema(
            {"name": "t", "version": 1, "description": "t", "keywords": ["x"], "priority": 999},
            Path("."),
        )
        assert any("priority" in e for e in errors)

    def test_invalid_handoff_type(self):
        from sre_agent.skill_loader import _validate_schema

        errors = _validate_schema(
            {"name": "t", "version": 1, "description": "t", "keywords": ["x"], "handoff_to": {"sre": "not_a_list"}},
            Path("."),
        )
        assert any("handoff_to" in e for e in errors)

    def test_invalid_configurable_type(self):
        from sre_agent.skill_loader import _validate_schema

        errors = _validate_schema(
            {
                "name": "t",
                "version": 1,
                "description": "t",
                "keywords": ["x"],
                "configurable": [{"field1": {"type": "invalid_type"}}],
            },
            Path("."),
        )
        assert any("invalid type" in e for e in errors)

    def test_enum_without_options(self):
        from sre_agent.skill_loader import _validate_schema

        errors = _validate_schema(
            {
                "name": "t",
                "version": 1,
                "description": "t",
                "keywords": ["x"],
                "configurable": [{"field1": {"type": "enum"}}],
            },
            Path("."),
        )
        assert any("no options" in e for e in errors)

    def test_number_min_greater_than_max(self):
        from sre_agent.skill_loader import _validate_schema

        errors = _validate_schema(
            {
                "name": "t",
                "version": 1,
                "description": "t",
                "keywords": ["x"],
                "configurable": [{"field1": {"type": "number", "min": 100, "max": 10}}],
            },
            Path("."),
        )
        assert any("min > max" in e for e in errors)

    def test_built_in_skills_pass_validation(self):
        """All built-in skill packages should pass validation cleanly."""
        from sre_agent.skill_loader import _parse_skill_md

        skills_dir = Path(__file__).parent.parent / "sre_agent" / "skills"
        for skill_dir in skills_dir.iterdir():
            skill_file = skill_dir / "skill.md" if skill_dir.is_dir() else None
            if skill_file and skill_file.exists():
                skill = _parse_skill_md(skill_file)
                assert skill is not None, f"Failed to parse {skill_file}"


class TestSkillToDict:
    def test_serialization(self):
        skill = get_skill("sre")
        d = skill.to_dict()
        assert d["name"] == "sre"
        assert d["version"] >= 1
        assert isinstance(d["keywords"], list)
        assert isinstance(d["prompt_length"], int)
        assert d["prompt_length"] > 0
