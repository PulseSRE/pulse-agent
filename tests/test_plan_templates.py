"""Tests for plan template loading and matching."""

from __future__ import annotations

from sre_agent.plan_templates import get_template, list_templates, load_templates, match_template


class TestLoadTemplates:
    def test_loads_all_templates(self):
        templates = load_templates()
        assert len(templates) >= 6
        assert "crashloop" in templates
        assert "oom" in templates
        assert "node-pressure" in templates
        assert "deployment-failure" in templates
        assert "security-incident" in templates
        assert "latency-degradation" in templates

    def test_templates_are_valid(self):
        from sre_agent.skill_plan import validate_plan

        templates = load_templates()
        for name, plan in templates.items():
            errors = validate_plan(plan)
            assert errors == [], f"Template '{name}' has errors: {errors}"

    def test_all_templates_have_phases(self):
        templates = load_templates()
        for name, plan in templates.items():
            assert len(plan.phases) >= 2, f"Template '{name}' has too few phases"

    def test_all_templates_have_verify_phase(self):
        templates = load_templates()
        for name, plan in templates.items():
            phase_ids = [p.id for p in plan.phases]
            assert "verify" in phase_ids, f"Template '{name}' missing verify phase"

    def test_crashloop_template_structure(self):
        templates = load_templates()
        plan = templates["crashloop"]
        assert plan.id == "crashloop-resolution-v1"
        assert plan.name == "Crashloop Resolution"
        assert plan.max_total_duration == 900
        phase_ids = [p.id for p in plan.phases]
        assert phase_ids == ["triage", "diagnose", "remediate", "verify"]

    def test_security_template_has_parallel_phases(self):
        templates = load_templates()
        plan = templates["security-incident"]
        # security_scan and rbac_audit should both depend only on triage (parallel)
        scan_phase = next(p for p in plan.phases if p.id == "security_scan")
        audit_phase = next(p for p in plan.phases if p.id == "rbac_audit")
        assert scan_phase.depends_on == ["triage"]
        assert audit_phase.depends_on == ["triage"]

    def test_latency_template_has_branching(self):
        templates = load_templates()
        plan = templates["latency-degradation"]
        investigate_phase = next(p for p in plan.phases if p.id == "investigate")
        assert investigate_phase.branch_on == "root_cause_layer"
        assert "database" in investigate_phase.branches
        assert "pod" in investigate_phase.branches
        assert "network" in investigate_phase.branches


class TestGetTemplate:
    def test_get_by_incident_type(self):
        load_templates()
        plan = get_template("crashloop")
        assert plan is not None
        assert plan.incident_type == "crashloop"

    def test_get_oom_template(self):
        load_templates()
        plan = get_template("oom")
        assert plan is not None
        assert plan.name == "OOM Investigation"

    def test_get_nonexistent(self):
        load_templates()
        assert get_template("nonexistent") is None


class TestMatchTemplate:
    def test_exact_category_match(self):
        load_templates()
        plan = match_template(category="oom")
        assert plan is not None
        assert plan.incident_type == "oom"

    def test_fuzzy_category_match(self):
        load_templates()
        plan = match_template(category="deployment")
        assert plan is not None
        assert "deployment" in plan.incident_type

    def test_keyword_match_latency(self):
        load_templates()
        plan = match_template(keywords=["latency", "slow"])
        assert plan is not None
        assert "latency" in plan.incident_type

    def test_keyword_match_pressure(self):
        load_templates()
        plan = match_template(keywords=["node", "pressure"])
        assert plan is not None
        assert "node-pressure" in plan.incident_type

    def test_no_match(self):
        load_templates()
        plan = match_template(category="teleport", keywords=["warp"])
        assert plan is None

    def test_category_takes_precedence_over_keywords(self):
        load_templates()
        plan = match_template(category="crashloop", keywords=["oom", "memory"])
        assert plan is not None
        assert plan.incident_type == "crashloop"


class TestListTemplates:
    def test_returns_list(self):
        load_templates()
        result = list_templates()
        assert isinstance(result, list)
        assert len(result) >= 6
        assert all("id" in t and "name" in t for t in result)

    def test_list_contains_all_fields(self):
        load_templates()
        result = list_templates()
        for template in result:
            assert "id" in template
            assert "name" in template
            assert "incident_type" in template
            assert "phases" in template
            assert "max_duration" in template
            assert template["phases"] >= 2

    def test_list_includes_crashloop(self):
        load_templates()
        result = list_templates()
        crashloop = next((t for t in result if t["incident_type"] == "crashloop"), None)
        assert crashloop is not None
        assert crashloop["name"] == "Crashloop Resolution"
        assert crashloop["phases"] == 4
        assert crashloop["max_duration"] == 900
