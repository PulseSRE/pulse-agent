"""Tests for the dynamic prompt builder."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from sre_agent.prompt_builder import FLEET_PREFIX, INTENT_PREFIX, assemble_prompt
from sre_agent.skill_loader import Skill


def _make_skill(name: str = "sre", **kwargs) -> Skill:
    """Create a test skill with sensible defaults."""
    defaults = {
        "version": 1,
        "description": "Test skill",
        "keywords": ["test"],
        "categories": ["diagnostics"],
        "write_tools": False,
        "priority": 10,
        "system_prompt": "You are a test agent.",
        "path": Path("."),
    }
    defaults.update(kwargs)
    return Skill(name=name, **defaults)


class TestAssemblePrompt:
    def test_returns_static_and_dynamic(self):
        skill = _make_skill()
        static, dynamic = assemble_prompt(skill, "test query", "sre", ["list_pods"])
        assert isinstance(static, str)
        assert isinstance(dynamic, str)

    def test_static_includes_base_prompt(self):
        skill = _make_skill(system_prompt="You are a K8s expert.")
        static, _ = assemble_prompt(skill, "test", "sre", [])
        assert "You are a K8s expert." in static

    def test_static_includes_intent_prefix(self):
        skill = _make_skill()
        static, _ = assemble_prompt(skill, "test", "sre", [])
        assert "Intent Analysis" in static
        assert "diagnose" in static
        assert "Entities" in static

    def test_static_includes_component_hint_for_sre(self):
        skill = _make_skill(name="sre")
        static, _ = assemble_prompt(skill, "test", "sre", ["list_pods"])
        assert "data_table" in static

    def test_no_component_hint_for_security(self):
        skill = _make_skill(name="security")
        static, _ = assemble_prompt(skill, "test", "security", [])
        assert "Component Catalog" not in static

    def test_no_component_hint_for_view_designer(self):
        skill = _make_skill(name="view_designer")
        static, _ = assemble_prompt(skill, "test", "view_designer", [])
        assert "Component Catalog" not in static

    def test_fleet_mode_prefix(self):
        skill = _make_skill()
        static, _ = assemble_prompt(skill, "test", "sre", [], fleet_mode=True)
        assert "FLEET MODE" in static
        assert "fleet_list_pods" in static

    def test_no_fleet_prefix_by_default(self):
        skill = _make_skill()
        static, _ = assemble_prompt(skill, "test", "sre", [], fleet_mode=False)
        assert "FLEET MODE" not in static

    def test_style_hint_included(self):
        skill = _make_skill()
        static, _ = assemble_prompt(skill, "test", "sre", [], style_hint="Be brief.")
        assert "Be brief." in static

    def test_shared_context_in_dynamic(self):
        skill = _make_skill()
        _, dynamic = assemble_prompt(skill, "test", "sre", [], shared_context="Previous finding: OOM in production")
        assert "OOM in production" in dynamic

    def test_ui_context_in_dynamic(self):
        skill = _make_skill()
        _, dynamic = assemble_prompt(skill, "test", "sre", [], ui_context="[UI Context] namespace=production")
        assert "namespace=production" in dynamic

    @patch("sre_agent.runbooks.select_runbooks", return_value="## Runbook: CrashLoop\n1. Check pods")
    def test_runbooks_included_for_sre(self, mock_runbooks):
        skill = _make_skill()
        _, dynamic = assemble_prompt(skill, "pod crashloop", "sre", [])
        assert "CrashLoop" in dynamic
        mock_runbooks.assert_called_once_with("pod crashloop")

    @patch("sre_agent.runbooks.select_runbooks")
    def test_no_runbooks_for_security(self, mock_runbooks):
        skill = _make_skill(name="security")
        assemble_prompt(skill, "scan rbac", "security", [])
        mock_runbooks.assert_not_called()

    def test_assembly_order_static(self):
        """Base prompt comes before intent prefix, which comes before component hint."""
        skill = _make_skill(system_prompt="BASE PROMPT HERE")
        static, _ = assemble_prompt(skill, "test", "sre", ["list_pods"])
        base_pos = static.index("BASE PROMPT HERE")
        intent_pos = static.index("Intent Analysis")
        assert base_pos < intent_pos, "Base prompt must come before intent prefix"

    def test_intent_prefix_content(self):
        """Intent prefix should contain all classification categories."""
        assert "diagnose" in INTENT_PREFIX
        assert "monitor" in INTENT_PREFIX
        assert "build" in INTENT_PREFIX
        assert "scan" in INTENT_PREFIX
        assert "fix" in INTENT_PREFIX
        assert "Entities" in INTENT_PREFIX
        assert "Scope" in INTENT_PREFIX
        assert "Complexity" in INTENT_PREFIX

    def test_fleet_prefix_content(self):
        assert "fleet_list_pods" in FLEET_PREFIX
        assert "fleet_compare_resource" in FLEET_PREFIX
