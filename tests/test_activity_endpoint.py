"""Tests for GET /agent/activity endpoint."""

from __future__ import annotations

from unittest.mock import MagicMock


class TestBuildActivityEvents:
    def test_returns_events_list(self):
        from sre_agent.api.monitor_rest import _build_activity_events

        mock_repo = MagicMock()
        mock_repo.fetch_activity_actions.return_value = [
            {"category": "crashloop", "namespace": "production", "cnt": 2, "status": "completed"},
            {"category": "workloads", "namespace": "default", "cnt": 1, "status": "completed"},
        ]
        mock_repo.fetch_self_healed_count.return_value = [{"cnt": 3}]
        mock_repo.fetch_postmortem_activity.return_value = [{"cnt": 1, "latest_summary": "OOM incident in production"}]
        mock_repo.fetch_investigated_findings.return_value = [
            {"finding_type": "node_pressure", "target": "worker-3", "cnt": 1}
        ]

        events = _build_activity_events(mock_repo, days=7)
        assert len(events) >= 2
        assert events[0]["type"] == "auto_fix"
        assert events[0]["count"] == 2
        assert events[0]["namespace"] == "production"
        assert events[0]["link"] == "/inbox?tab=activity"

    def test_empty_when_no_data(self):
        from sre_agent.api.monitor_rest import _build_activity_events

        mock_repo = MagicMock()
        mock_repo.fetch_activity_actions.return_value = []
        mock_repo.fetch_self_healed_count.return_value = [{"cnt": 0}]
        mock_repo.fetch_postmortem_activity.return_value = []
        mock_repo.fetch_investigated_findings.return_value = []

        events = _build_activity_events(mock_repo, days=7)
        assert len(events) == 0

    def test_self_healed_included(self):
        from sre_agent.api.monitor_rest import _build_activity_events

        mock_repo = MagicMock()
        mock_repo.fetch_activity_actions.return_value = []
        mock_repo.fetch_self_healed_count.return_value = [{"cnt": 5}]
        mock_repo.fetch_postmortem_activity.return_value = []
        mock_repo.fetch_investigated_findings.return_value = []

        events = _build_activity_events(mock_repo, days=7)
        assert len(events) == 1
        assert events[0]["type"] == "self_healed"
        assert events[0]["count"] == 5

    def test_postmortem_with_summary(self):
        from sre_agent.api.monitor_rest import _build_activity_events

        mock_repo = MagicMock()
        mock_repo.fetch_activity_actions.return_value = []
        mock_repo.fetch_self_healed_count.return_value = [{"cnt": 0}]
        mock_repo.fetch_postmortem_activity.return_value = [
            {"cnt": 2, "latest_summary": "Memory leak in payment service"}
        ]
        mock_repo.fetch_investigated_findings.return_value = []

        events = _build_activity_events(mock_repo, days=7)
        assert len(events) == 1
        assert events[0]["type"] == "postmortem"
        assert "payment service" in events[0]["description"]

    def test_investigation_events(self):
        from sre_agent.api.monitor_rest import _build_activity_events

        mock_repo = MagicMock()
        mock_repo.fetch_activity_actions.return_value = []
        mock_repo.fetch_self_healed_count.return_value = [{"cnt": 0}]
        mock_repo.fetch_postmortem_activity.return_value = []
        mock_repo.fetch_investigated_findings.return_value = [
            {"finding_type": "node_pressure", "target": "worker-3", "cnt": 1}
        ]

        events = _build_activity_events(mock_repo, days=7)
        assert len(events) == 1
        assert events[0]["type"] == "investigation"
