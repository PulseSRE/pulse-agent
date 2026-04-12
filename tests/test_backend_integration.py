"""Tests for backend integration — skill loading, MCP startup, routing delegation."""

from __future__ import annotations


class TestOrchestratorSkillDelegation:
    """Verify orchestrator delegates to skill_loader when skills are loaded."""

    def test_build_config_uses_skill_when_available(self):
        """build_orchestrated_config should use skill-based config for known skills."""
        from sre_agent.orchestrator import build_orchestrated_config

        config = build_orchestrated_config("sre")
        assert "system_prompt" in config
        assert "tool_defs" in config
        assert "tool_map" in config
        assert "write_tools" in config
        # SRE skill has write_tools=true
        assert len(config["write_tools"]) > 0

    def test_build_config_view_designer_has_view_tools(self):
        from sre_agent.orchestrator import build_orchestrated_config

        config = build_orchestrated_config("view_designer")
        tool_names = set(config["tool_map"].keys())
        assert "create_dashboard" in tool_names or "plan_dashboard" in tool_names
        # View designer should NOT have write tools
        assert len(config["write_tools"]) == 0

    def test_build_config_security_no_write_tools(self):
        from sre_agent.orchestrator import build_orchestrated_config

        config = build_orchestrated_config("security")
        assert len(config["write_tools"]) == 0

    def test_build_config_both_has_all_tools(self):
        from sre_agent.orchestrator import build_orchestrated_config

        config = build_orchestrated_config("both")
        sre_config = build_orchestrated_config("sre")
        # "both" should have at least as many tools as SRE
        assert len(config["tool_defs"]) >= len(sre_config["tool_defs"])

    def test_build_config_unknown_mode_falls_back(self):
        """Unknown mode should fall back to SRE (legacy behavior)."""
        from sre_agent.orchestrator import build_orchestrated_config

        config = build_orchestrated_config("nonexistent_mode")
        assert "system_prompt" in config
        assert len(config["tool_defs"]) > 0

    def test_skill_prompt_used_instead_of_legacy(self):
        """When skill exists, its prompt should be used."""
        from sre_agent.orchestrator import build_orchestrated_config
        from sre_agent.skill_loader import get_skill

        config = build_orchestrated_config("sre")
        skill = get_skill("sre")
        if skill:
            # Skill prompt should be in the config
            assert skill.system_prompt in config["system_prompt"]


class TestSkillToolSharing:
    """Verify multiple skills share the same tool registry."""

    def test_sre_and_security_share_diagnostic_tools(self):
        from sre_agent.orchestrator import build_orchestrated_config

        sre = build_orchestrated_config("sre")
        sec = build_orchestrated_config("security")
        sre_tools = set(sre["tool_map"].keys())
        sec_tools = set(sec["tool_map"].keys())
        # Both should have basic diagnostic tools
        shared = sre_tools & sec_tools
        assert len(shared) > 0  # At least some tools shared

    def test_view_designer_gets_all_tools(self):
        """View designer (categories=[]) should get all registered tools."""
        from sre_agent.orchestrator import build_orchestrated_config

        vd = build_orchestrated_config("view_designer")
        sre = build_orchestrated_config("sre")
        # View designer should have at least as many tools as SRE
        assert len(vd["tool_map"]) >= len(sre["tool_map"])

    def test_capacity_planner_gets_monitoring_tools(self):
        """Capacity planner skill should get monitoring category tools."""
        from sre_agent.skill_loader import build_config_from_skill, get_skill

        skill = get_skill("capacity_planner")
        if skill:
            config = build_config_from_skill(skill)
            tool_names = set(config["tool_map"].keys())
            # Should have monitoring tools from its categories
            assert len(tool_names) > 5


class TestBuildConfigFromSkill:
    """Test build_config_from_skill directly."""

    def test_returns_correct_format(self):
        from sre_agent.skill_loader import build_config_from_skill, get_skill

        skill = get_skill("sre")
        assert skill is not None
        config = build_config_from_skill(skill)
        assert isinstance(config["system_prompt"], str)
        assert isinstance(config["tool_defs"], list)
        assert isinstance(config["tool_map"], dict)
        assert isinstance(config["write_tools"], set)

    def test_empty_categories_returns_all_tools(self):
        from sre_agent.skill_loader import Skill, build_config_from_skill

        skill = Skill(
            name="test_all",
            version=1,
            description="test",
            keywords=[],
            categories=[],  # Empty = all tools
            write_tools=False,
            priority=1,
            system_prompt="test prompt",
        )
        config = build_config_from_skill(skill)
        # Should have all tools
        assert len(config["tool_map"]) > 20

    def test_specific_categories_filters_tools(self):
        from sre_agent.skill_loader import Skill, build_config_from_skill

        all_skill = Skill(
            name="all",
            version=1,
            description="",
            keywords=[],
            categories=[],
            write_tools=False,
            priority=1,
            system_prompt="",
        )
        filtered_skill = Skill(
            name="filtered",
            version=1,
            description="",
            keywords=[],
            categories=["security"],
            write_tools=False,
            priority=1,
            system_prompt="",
        )
        all_config = build_config_from_skill(all_skill)
        filtered_config = build_config_from_skill(filtered_skill)
        # Filtered should have fewer tools
        assert len(filtered_config["tool_map"]) < len(all_config["tool_map"])

    def test_write_tools_false_returns_empty_set(self):
        from sre_agent.skill_loader import Skill, build_config_from_skill

        skill = Skill(
            name="readonly",
            version=1,
            description="",
            keywords=[],
            categories=[],
            write_tools=False,
            priority=1,
            system_prompt="",
        )
        config = build_config_from_skill(skill)
        assert config["write_tools"] == set()

    def test_write_tools_true_returns_write_set(self):
        from sre_agent.skill_loader import Skill, build_config_from_skill

        skill = Skill(
            name="writable",
            version=1,
            description="",
            keywords=[],
            categories=["workloads"],
            write_tools=True,
            priority=1,
            system_prompt="",
        )
        config = build_config_from_skill(skill)
        assert len(config["write_tools"]) > 0

    def test_prompt_from_skill_md(self):
        from sre_agent.skill_loader import build_config_from_skill, get_skill

        skill = get_skill("sre")
        config = build_config_from_skill(skill)
        assert "Security" in config["system_prompt"]  # SRE prompt starts with security


class TestMCPStartupIntegration:
    """Test MCP connection at startup."""

    def test_connect_skill_mcp_no_yaml(self, tmp_path):
        """Skills without mcp.yaml should not attempt MCP connection."""
        from sre_agent.mcp_client import connect_skill_mcp

        result = connect_skill_mcp("test", tmp_path)
        assert result is None

    def test_connect_skill_mcp_with_yaml(self, tmp_path):
        """Skills with mcp.yaml should attempt connection."""
        import yaml

        mcp_yaml = tmp_path / "mcp.yaml"
        mcp_yaml.write_text(
            yaml.dump(
                {
                    "server": {"url": "nonexistent_binary_xyz", "transport": "stdio"},
                    "toolsets": ["helm"],
                }
            )
        )

        from sre_agent.mcp_client import connect_skill_mcp

        conn = connect_skill_mcp("test_skill", tmp_path)
        assert conn is not None
        assert not conn.connected  # Command won't be found
        assert conn.error  # Should have an error message


class TestSkillRestRouterRegistered:
    """Verify skill REST endpoints are registered in the app."""

    def test_skills_endpoint_exists(self):
        from sre_agent.api.app import app

        paths = [r.path for r in app.routes]
        assert "/skills" in paths or any("/skills" in p for p in paths)

    def test_components_endpoint_exists(self):
        from sre_agent.api.app import app

        paths = [r.path for r in app.routes]
        assert "/components" in paths or any("/components" in p for p in paths)

    def test_admin_skills_reload_exists(self):
        from sre_agent.api.app import app

        paths = [r.path for r in app.routes]
        assert any("reload" in str(p) for p in paths)
