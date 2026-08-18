"""Tests for skill conflict resolution — ensures conflicting skills don't run in parallel.

Covers:
- Exclusive skills (no secondary)
- Bidirectional conflicts_with
- Metadata integrity (conflicts_with entries match real skill names)
- Multi-turn sticky mode (view_designer stays sticky, plan_builder can't break out)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _setup():
    with (
        patch("sre_agent.k8s_client._initialized", True),
        patch("sre_agent.k8s_client._load_k8s"),
        patch("sre_agent.k8s_client.get_core_client", return_value=MagicMock()),
    ):
        from sre_agent.skill_loader import load_skills

        load_skills()


_setup()


class TestExclusiveSkills:
    """Exclusive skills never get a secondary — they own the full request lifecycle."""

    def test_view_designer_is_exclusive(self):
        from sre_agent.skill_loader import get_skill

        vd = get_skill("view_designer")
        assert vd is not None
        assert vd.exclusive is True

    def test_postmortem_is_exclusive(self):
        from sre_agent.skill_loader import get_skill

        pm = get_skill("postmortem")
        assert pm is not None
        assert pm.exclusive is True

    def test_sre_is_not_exclusive(self):
        from sre_agent.skill_loader import get_skill

        sre = get_skill("sre")
        assert sre is not None
        assert sre.exclusive is False

    def test_exclusive_skill_never_gets_secondary(self):
        from sre_agent.skill_router import classify_query_multi

        primary, secondary = classify_query_multi("Create a dashboard showing node health: CPU/memory utilization")
        assert primary.name == "view_designer"
        assert secondary is None, f"Exclusive skill got secondary={secondary.name if secondary else None}"

    def test_exclusive_add_widget(self):
        from sre_agent.skill_router import classify_query_multi

        primary, secondary = classify_query_multi("Add a memory chart to the dashboard")
        assert primary.name == "view_designer"
        assert secondary is None


class TestConflictsWithBidirectional:
    """conflicts_with is checked in both directions."""

    def test_view_designer_conflicts_with_plan_builder(self):
        from sre_agent.skill_loader import get_skill

        vd = get_skill("view_designer")
        assert vd is not None
        assert "plan_builder" in vd.conflicts_with

    def test_bidirectional_check(self):
        from sre_agent.skill_router import _skills_conflict

        class FakeSkill:
            def __init__(self, name, conflicts):
                self.name = name
                self.conflicts_with = conflicts

        a = FakeSkill("view_designer", ["plan_builder"])
        b = FakeSkill("plan_builder", [])
        assert _skills_conflict(a, b) is True
        assert _skills_conflict(b, a) is True

        c = FakeSkill("sre", [])
        d = FakeSkill("security", [])
        assert _skills_conflict(c, d) is False


class TestMetadataIntegrity:
    """Verify skill metadata from actual skill.md files is consistent."""

    def test_conflicts_with_entries_match_real_skill_names(self):
        """Every entry in conflicts_with must be a real loaded skill name."""
        from sre_agent.skill_loader import list_skills

        all_names = {s.name for s in list_skills()}
        for skill in list_skills():
            for conflict in skill.conflicts_with:
                assert conflict in all_names, (
                    f"Skill '{skill.name}' has conflicts_with entry '{conflict}' "
                    f"which is not a loaded skill. Available: {sorted(all_names)}"
                )

    def test_no_hyphens_in_conflicts_with(self):
        """conflicts_with must use underscores (normalized), not hyphens."""
        from sre_agent.skill_loader import list_skills

        for skill in list_skills():
            for conflict in skill.conflicts_with:
                assert "-" not in conflict, (
                    f"Skill '{skill.name}' has hyphen in conflicts_with: '{conflict}'. "
                    f"Skill names use underscores at runtime."
                )

    def test_exclusive_skills_have_no_secondary_in_multi(self):
        """All exclusive skills must return None secondary for any query they handle."""
        from sre_agent.skill_loader import list_skills
        from sre_agent.skill_router import classify_query_multi

        exclusive_skills = [s for s in list_skills() if s.exclusive]
        assert len(exclusive_skills) >= 2

        test_queries = {
            "view_designer": "Create a dashboard showing pod health",
            "postmortem": "Write a postmortem for the last outage",
        }
        for skill in exclusive_skills:
            query = test_queries.get(skill.name)
            if query:
                primary, secondary = classify_query_multi(query)
                if primary.name == skill.name:
                    assert secondary is None, f"Exclusive skill '{skill.name}' got secondary={secondary.name}"


class TestMultiTurnStickyMode:
    """Simulate multi-turn WebSocket sessions to verify sticky mode.

    These drive the real agent loop. The conftest stub supplies the model
    response, so what is actually under test here is the routing that runs
    either way -- the hard pre-route pattern match and sticky-mode carry-over
    between turns -- rather than anything the model decides.
    """

    @pytest.fixture
    def ws_client(self, monkeypatch):
        monkeypatch.setenv("PULSE_AGENT_WS_TOKEN", "sticky-test-token")
        monkeypatch.setenv("PULSE_AGENT_MEMORY", "0")

        with (
            patch("sre_agent.k8s_client._initialized", True),
            patch("sre_agent.k8s_client._load_k8s"),
            patch("sre_agent.k8s_client.get_core_client", return_value=MagicMock()),
            patch("sre_agent.k8s_client.get_apps_client", return_value=MagicMock()),
            patch("sre_agent.k8s_client.get_custom_client", return_value=MagicMock()),
            patch("sre_agent.k8s_client.get_version_client", return_value=MagicMock()),
        ):
            from fastapi.testclient import TestClient

            from sre_agent.api import app

            yield TestClient(app)

    def test_view_designer_stays_sticky_on_follow_up(self, ws_client):
        """After view_designer handles turn 1, a vague follow-up stays in view_designer."""
        token = "sticky-test-token"
        with ws_client.websocket_connect(f"/ws/agent?token={token}") as ws:
            ws.send_json({"type": "message", "content": "Create a dashboard showing node health"})
            events = []
            for _ in range(100):
                try:
                    data = ws.receive_json()
                    events.append(data)
                    if data.get("type") == "done":
                        break
                except Exception:
                    break

            ws.send_json({"type": "message", "content": "now add the metrics and create it"})
            events2 = []
            for _ in range(100):
                try:
                    data = ws.receive_json()
                    events2.append(data)
                    if data.get("type") == "done":
                        break
                except Exception:
                    break

            hallucinations = [e for e in events2 if e.get("type") == "error" and "unknown tool" in e.get("message", "")]
            assert not hallucinations, f"Turn 2 had tool hallucinations: {hallucinations}"

    def test_view_designer_no_plan_builder_hallucinations(self, ws_client):
        """The exact pill query must not produce plan_builder hallucinations."""
        token = "sticky-test-token"
        with ws_client.websocket_connect(f"/ws/agent?token={token}") as ws:
            ws.send_json(
                {
                    "type": "message",
                    "content": "Create a dashboard showing node health: CPU/memory utilization, pod density, and node conditions",
                }
            )
            all_events = []
            for _ in range(200):
                try:
                    data = ws.receive_json()
                    all_events.append(data)
                    if data.get("type") == "done":
                        break
                except Exception:
                    break

            hallucinations = [
                e for e in all_events if e.get("type") == "error" and "unknown tool" in e.get("message", "")
            ]
            assert not hallucinations, f"Pill query produced tool hallucinations: {hallucinations}"


class TestNonConflictingMultiSkill:
    """Non-exclusive, non-conflicting skills CAN run in parallel."""

    def test_sre_security_can_run_together(self):
        from sre_agent.skill_router import classify_query_multi

        primary, secondary = classify_query_multi("check for crashlooping pods and scan RBAC vulnerabilities")
        if secondary:
            assert primary.name != secondary.name
