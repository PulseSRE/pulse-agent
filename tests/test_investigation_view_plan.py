"""Tests for viewPlan field in investigation prompt and parsing."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sre_agent.monitor.investigations import _build_investigation_prompt


class TestInvestigationPromptViewPlan:
    def test_prompt_includes_view_plan_schema(self):
        finding = {
            "severity": "warning",
            "category": "crashloop",
            "title": "Pod restarting",
            "summary": "api-pod restarting every 30s",
            "resources": [{"kind": "Pod", "name": "api-pod", "namespace": "prod"}],
        }
        prompt = _build_investigation_prompt(finding)
        assert "viewPlan" in prompt
        assert '"kind"' in prompt

    def test_prompt_includes_valid_component_kinds(self):
        finding = {
            "severity": "info",
            "category": "cert_expiry",
            "title": "Cert expiring",
            "summary": "TLS cert expires in 5d",
            "resources": [],
        }
        prompt = _build_investigation_prompt(finding)
        assert "chart" in prompt
        assert "data_table" in prompt
        assert "resolution_tracker" in prompt

    def test_prompt_includes_tool_names(self):
        finding = {
            "severity": "warning",
            "category": "scheduling",
            "title": "Pending pod",
            "summary": "Pod stuck pending",
            "resources": [],
        }
        prompt = _build_investigation_prompt(finding)
        assert "Valid tools:" in prompt
        # Should have either registry tools or fallback tools
        assert "get_events" in prompt or "create_inbox_task" in prompt


def _patch_investigation_deps():
    """Patch all lazy imports used by _run_proactive_investigation."""
    return (
        patch("sre_agent.agent.create_async_client"),
        patch("sre_agent.agent.run_agent_streaming", new_callable=AsyncMock, return_value="{}"),
        patch("sre_agent.harness.build_cached_system_prompt", return_value="sys"),
        patch("sre_agent.harness.get_cluster_context", return_value=""),
        patch("sre_agent.harness.get_component_hint", return_value=""),
        patch("sre_agent.skill_loader.select_tools", return_value=([], {}, [])),
        patch("sre_agent.config.get_settings", return_value=MagicMock(memory=False, model="claude-sonnet-4-6")),
    )


class TestInvestigationResponsePassthrough:
    @pytest.mark.asyncio
    async def test_view_plan_returned_from_investigation(self):
        from sre_agent.monitor.investigations import _run_proactive_investigation

        fake_response = '{"summary":"OOM","suspected_cause":"memory leak","recommended_fix":"increase limit","confidence":0.8,"evidence":[],"alternatives_considered":[],"viewPlan":[{"kind":"chart","title":"Memory","props":{"query":"up"}}]}'

        patches = _patch_investigation_deps()
        for p in patches:
            p.start()
        patch("sre_agent.agent.run_agent_streaming", new_callable=AsyncMock, return_value=fake_response).start()
        try:
            result = await _run_proactive_investigation(
                {"title": "OOM", "severity": "warning", "category": "oom", "summary": "x", "resources": []}
            )
        finally:
            patch.stopall()

        assert "viewPlan" in result
        assert len(result["viewPlan"]) == 1
        assert result["viewPlan"][0]["kind"] == "chart"

    @pytest.mark.asyncio
    async def test_missing_view_plan_returns_empty_list(self):
        from sre_agent.monitor.investigations import _run_proactive_investigation

        fake_response = '{"summary":"OK","suspected_cause":"none","recommended_fix":"none","confidence":0.5,"evidence":[],"alternatives_considered":[]}'

        patches = _patch_investigation_deps()
        for p in patches:
            p.start()
        patch("sre_agent.agent.run_agent_streaming", new_callable=AsyncMock, return_value=fake_response).start()
        try:
            result = await _run_proactive_investigation(
                {"title": "Test", "severity": "info", "category": "test", "summary": "test", "resources": []}
            )
        finally:
            patch.stopall()

        assert result["viewPlan"] == []
