"""Alert-borne incidents must engage plan templates.

Every alert arrives as category="alerts" — a category no template matches —
so the phased-investigation machinery never ran for the incident class that
dominates the reference cluster's queue. plan_category_for() maps known alert
names (and the control_plane category) onto existing templates; unknown
alerts deliberately fall through to the freeform path.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sre_agent.monitor.alert_plans import _ALERT_TEMPLATE, _CATEGORY_TEMPLATE, plan_category_for


class TestPlanCategoryFor:
    def test_known_alert_maps_to_its_template(self):
        f = {"category": "alerts", "title": "KubeNodeNotReady"}
        assert plan_category_for(f) == "nodes"

    def test_control_plane_memory_alert_engages_node_pressure(self):
        """The reference cluster's dominant incident, previously planless."""
        f = {"category": "alerts", "title": "HighOverallControlPlaneMemory"}
        assert plan_category_for(f) == "nodes"

    def test_unknown_alert_falls_through_unchanged(self):
        """Guessing a template for an unclassified alert runs the wrong
        playbook with confidence — unknowns take the freeform path."""
        f = {"category": "alerts", "title": "SomeVendorSpecificAlert"}
        assert plan_category_for(f) == "alerts"

    def test_control_plane_category_uses_node_pressure_interim(self):
        f = {"category": "control_plane", "title": "kube-controller-manager restarts"}
        assert plan_category_for(f) == "nodes"

    def test_non_alert_categories_pass_through(self):
        assert plan_category_for({"category": "crashloop", "title": "Pod x restarting"}) == "crashloop"
        assert plan_category_for({"category": "oom", "title": "y"}) == "oom"

    def test_every_mapping_targets_a_real_template(self):
        """A mapping to a nonexistent incident_type would silently disable the
        plan path for that alert — the exact failure this module fixes."""
        from sre_agent.plan_templates import list_templates, load_templates

        load_templates()
        known = {t["incident_type"] for t in list_templates()}
        for alert, target in _ALERT_TEMPLATE.items():
            assert target in known, f"alert '{alert}' maps to unknown incident_type '{target}'"
        for cat, target in _CATEGORY_TEMPLATE.items():
            assert target in known, f"category '{cat}' maps to unknown incident_type '{target}'"


class TestPlanExecutorWiring:
    @pytest.mark.asyncio
    async def test_executor_matches_on_the_mapped_category(self):
        from sre_agent.monitor.plan_executor import try_plan_execution

        seen: dict = {}

        def fake_match(*, category=""):
            seen["category"] = category
            return None  # stop before any execution

        monitor = MagicMock()
        finding = {"id": "f-1", "category": "alerts", "title": "KubeNodeNotReady"}
        with patch("sre_agent.plan_templates.match_template", side_effect=fake_match):
            ran = await try_plan_execution(monitor, finding)

        assert ran is False
        assert seen["category"] == "nodes", (
            "the executor must match templates on the mapped category — matching on "
            "raw 'alerts' is how alert-borne incidents never engaged a plan"
        )
