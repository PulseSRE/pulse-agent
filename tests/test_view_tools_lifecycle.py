"""Tests for create_dashboard lifecycle params."""

from __future__ import annotations

import json

from sre_agent.view_tools import SIGNAL_PREFIX


class TestCreateDashboardSignal:
    def test_default_signal_has_custom_type(self):
        from sre_agent.view_tools import create_dashboard

        result = create_dashboard.func("Test Dashboard", "desc")
        signal_json = result.split(SIGNAL_PREFIX, 1)[1].strip()
        sig = json.loads(signal_json)
        assert sig["type"] == "view_spec"
        assert sig["view_type"] == "custom"
        assert sig["status"] == "active"
        assert sig["visibility"] == "private"
        assert sig["trigger_source"] == "user"

    def test_incident_signal(self):
        from sre_agent.view_tools import create_dashboard

        result = create_dashboard.func(
            "CrashLoop Investigation",
            "OOM in payment-api",
            view_type="incident",
            trigger_source="monitor",
            finding_id="f-crash-1",
            visibility="team",
        )
        signal_json = result.split(SIGNAL_PREFIX, 1)[1].strip()
        sig = json.loads(signal_json)
        assert sig["view_type"] == "incident"
        assert sig["trigger_source"] == "monitor"
        assert sig["finding_id"] == "f-crash-1"
        assert sig["visibility"] == "team"
        assert sig["status"] == "investigating"

    def test_plan_signal_defaults_to_team(self):
        from sre_agent.view_tools import create_dashboard

        result = create_dashboard.func(
            "VM Support Plan",
            "Enable VMs for team B",
            view_type="plan",
            trigger_source="agent",
        )
        signal_json = result.split(SIGNAL_PREFIX, 1)[1].strip()
        sig = json.loads(signal_json)
        assert sig["view_type"] == "plan"
        assert sig["status"] == "analyzing"
        assert sig["visibility"] == "team"

    def test_assessment_signal(self):
        from sre_agent.view_tools import create_dashboard

        result = create_dashboard.func(
            "Memory Pressure Forecast",
            "worker-5 trending to pressure",
            view_type="assessment",
            trigger_source="monitor",
        )
        signal_json = result.split(SIGNAL_PREFIX, 1)[1].strip()
        sig = json.loads(signal_json)
        assert sig["view_type"] == "assessment"
        assert sig["status"] == "analyzing"
        assert sig["visibility"] == "team"

    def test_empty_finding_id_becomes_none(self):
        from sre_agent.view_tools import create_dashboard

        result = create_dashboard.func("Test", "desc")
        signal_json = result.split(SIGNAL_PREFIX, 1)[1].strip()
        sig = json.loads(signal_json)
        assert sig["finding_id"] is None
