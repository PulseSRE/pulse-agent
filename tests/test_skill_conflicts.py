"""Tests for skill conflict resolution — ensures conflicting skills don't run in parallel."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def _setup():
    with (
        patch("sre_agent.k8s_client._initialized", True),
        patch("sre_agent.k8s_client._load_k8s"),
        patch("sre_agent.k8s_client.get_core_client", return_value=MagicMock()),
    ):
        from sre_agent.skill_loader import load_skills

        load_skills()


_setup()


class TestViewDesignerConflicts:
    def test_dashboard_creation_no_secondary(self):
        from sre_agent.skill_router import classify_query_multi

        primary, secondary = classify_query_multi("Create a dashboard showing node health: CPU/memory utilization")
        assert primary.name == "view_designer"
        assert secondary is None or secondary.name != "plan-builder", (
            f"plan-builder should not run alongside view_designer, got secondary={secondary.name if secondary else None}"
        )

    def test_add_widget_no_secondary(self):
        from sre_agent.skill_router import classify_query_multi

        primary, secondary = classify_query_multi("Add a memory chart to the dashboard")
        assert primary.name == "view_designer"
        assert secondary is None or secondary.name != "plan-builder"

    def test_conflicts_with_field_set(self):
        from sre_agent.skill_loader import get_skill

        vd = get_skill("view_designer")
        assert vd is not None
        assert "plan-builder" in vd.conflicts_with


class TestNonConflictingMultiSkill:
    def test_sre_security_can_run_together(self):
        from sre_agent.skill_router import classify_query_multi

        primary, secondary = classify_query_multi("check for crashlooping pods and scan RBAC vulnerabilities")
        if secondary:
            assert primary.name != secondary.name
