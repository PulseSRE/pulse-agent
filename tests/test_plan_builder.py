"""Tests for dynamic plan construction."""

from __future__ import annotations

from sre_agent.plan_runtime import extract_plan_from_response


class TestExtractPlan:
    def test_extracts_from_json_block(self):
        response = """Here is my investigation plan:

```json
{
  "plan_name": "pod-crash-investigation",
  "incident_type": "crashloop",
  "phases": [
    {"id": "triage", "skill_name": "sre", "required": true, "timeout_seconds": 120, "produces": ["severity"]},
    {"id": "diagnose", "skill_name": "sre", "depends_on": ["triage"], "required": true},
    {"id": "verify", "skill_name": "sre", "depends_on": ["diagnose"], "runs": "always"}
  ]
}
```

I will now execute this plan."""

        plan = extract_plan_from_response(response)
        assert plan is not None
        assert plan.name == "pod-crash-investigation"
        assert len(plan.phases) == 3
        assert plan.generated_by == "auto"
        assert plan.reviewed is False

    def test_returns_none_for_no_json(self):
        assert extract_plan_from_response("Just some text without a plan") is None

    def test_returns_none_for_invalid_json(self):
        response = '```json\n{"invalid": true}\n```'
        assert extract_plan_from_response(response) is None

    def test_returns_none_for_invalid_plan(self):
        response = """```json
{
  "plan_name": "bad",
  "phases": [
    {"id": "a", "skill_name": "sre", "depends_on": ["b"]},
    {"id": "b", "skill_name": "sre", "depends_on": ["a"]}
  ]
}
```"""
        plan = extract_plan_from_response(response)
        assert plan is None  # Cycle detected

    def test_extracts_with_defaults(self):
        response = """```json
{
  "phases": [
    {"id": "step1", "skill_name": "sre"},
    {"id": "step2", "depends_on": ["step1"]}
  ]
}
```"""
        plan = extract_plan_from_response(response)
        assert plan is not None
        assert plan.phases[1].skill_name == "sre"  # default
        assert plan.phases[0].timeout_seconds == 120  # default


class TestPlanBuilderSkill:
    def test_skill_loads(self):
        from sre_agent.skill_loader import get_skill

        _ = get_skill("plan_builder")
        # May or may not be loaded depending on skills dir
        # Just verify no crash
