"""Tests for get_briefing() enhanced with live scanner data."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sre_agent.monitor.actions import get_briefing


@pytest.fixture
def mock_empty_db(monkeypatch):
    """Mock an empty database."""

    class MockRepo:
        def get_actions_since(self, *args, **kwargs):
            return []

        def get_investigations_since(self, *args, **kwargs):
            return []

    monkeypatch.setattr("sre_agent.monitor.actions.get_monitor_repo", lambda: MockRepo())


def test_get_briefing_includes_current_findings(mock_empty_db):
    """Test briefing includes live current findings from fast scanners."""
    mock_crashloop_finding = {
        "type": "finding",
        "id": "f-abc123",
        "severity": "critical",
        "category": "crashloop",
        "title": "Pod app-1 restarting (15x)",
        "summary": "Container 'app' has restarted 15 times.",
        "resources": [{"kind": "Pod", "name": "app-1", "namespace": "default"}],
        "autoFixable": True,
        "findingType": "current",
        "timestamp": 1234567890,
    }

    with patch("sre_agent.monitor.scanners.scan_crashlooping_pods") as mock_scanner:
        mock_scanner.return_value = [mock_crashloop_finding]
        with patch("sre_agent.monitor.scanners.scan_pending_pods", return_value=[]):
            with patch("sre_agent.monitor.scanners.scan_oom_killed_pods", return_value=[]):
                with patch("sre_agent.monitor.scanners.scan_firing_alerts", return_value=[]):
                    with patch("sre_agent.monitor.trend_scanners.scan_memory_pressure_forecast", return_value=[]):
                        with patch("sre_agent.monitor.trend_scanners.scan_disk_pressure_forecast", return_value=[]):
                            with patch("sre_agent.monitor.trend_scanners.scan_hpa_exhaustion_trend", return_value=[]):
                                with patch(
                                    "sre_agent.monitor.trend_scanners.scan_error_rate_acceleration", return_value=[]
                                ):
                                    result = get_briefing()

    assert "currentFindings" in result
    assert len(result["currentFindings"]) == 1
    assert result["currentFindings"][0]["category"] == "crashloop"
    assert result["currentFindings"][0]["severity"] == "critical"


def test_get_briefing_includes_trend_findings(mock_empty_db):
    """Test briefing includes trend findings from predictive scanners."""
    mock_memory_trend = {
        "type": "finding",
        "id": "f-xyz789",
        "severity": "warning",
        "category": "memory_pressure",
        "title": "Node worker-1 memory exhaustion predicted in 3 days",
        "summary": "Linear projection shows memory exhaustion.",
        "resources": [{"kind": "Node", "name": "worker-1"}],
        "autoFixable": False,
        "findingType": "trend",
        "confidence": 0.75,
        "timestamp": 1234567890,
    }

    with patch("sre_agent.monitor.scanners.scan_crashlooping_pods", return_value=[]):
        with patch("sre_agent.monitor.scanners.scan_pending_pods", return_value=[]):
            with patch("sre_agent.monitor.scanners.scan_oom_killed_pods", return_value=[]):
                with patch("sre_agent.monitor.scanners.scan_firing_alerts", return_value=[]):
                    with patch("sre_agent.monitor.trend_scanners.scan_memory_pressure_forecast") as mock_trend:
                        mock_trend.return_value = [mock_memory_trend]
                        with patch("sre_agent.monitor.trend_scanners.scan_disk_pressure_forecast", return_value=[]):
                            with patch("sre_agent.monitor.trend_scanners.scan_hpa_exhaustion_trend", return_value=[]):
                                with patch(
                                    "sre_agent.monitor.trend_scanners.scan_error_rate_acceleration", return_value=[]
                                ):
                                    result = get_briefing()

    assert "trendFindings" in result
    assert len(result["trendFindings"]) == 1
    assert result["trendFindings"][0]["category"] == "memory_pressure"
    assert result["trendFindings"][0]["findingType"] == "trend"


def test_get_briefing_priority_ranking(mock_empty_db):
    """Test briefing ranks findings by severity (critical > warning > info)."""
    critical_finding = {
        "id": "f-1",
        "severity": "critical",
        "category": "crashloop",
        "findingType": "current",
        "timestamp": 1,
    }
    warning_finding = {
        "id": "f-2",
        "severity": "warning",
        "category": "pending",
        "findingType": "current",
        "timestamp": 2,
    }
    info_finding = {
        "id": "f-3",
        "severity": "info",
        "category": "audit",
        "findingType": "current",
        "timestamp": 3,
    }

    with patch("sre_agent.monitor.scanners.scan_crashlooping_pods", return_value=[warning_finding]):
        with patch("sre_agent.monitor.scanners.scan_pending_pods", return_value=[info_finding]):
            with patch("sre_agent.monitor.scanners.scan_oom_killed_pods", return_value=[critical_finding]):
                with patch("sre_agent.monitor.scanners.scan_firing_alerts", return_value=[]):
                    with patch("sre_agent.monitor.trend_scanners.scan_memory_pressure_forecast", return_value=[]):
                        with patch("sre_agent.monitor.trend_scanners.scan_disk_pressure_forecast", return_value=[]):
                            with patch("sre_agent.monitor.trend_scanners.scan_hpa_exhaustion_trend", return_value=[]):
                                with patch(
                                    "sre_agent.monitor.trend_scanners.scan_error_rate_acceleration", return_value=[]
                                ):
                                    result = get_briefing()

    assert "priorityItems" in result
    assert len(result["priorityItems"]) == 3
    # Critical should be first
    assert result["priorityItems"][0]["severity"] == "critical"
    assert result["priorityItems"][1]["severity"] == "warning"
    assert result["priorityItems"][2]["severity"] == "info"


def test_get_briefing_limits_priority_to_10(mock_empty_db):
    """Test briefing limits priority items to top 10."""
    many_findings = [{"id": f"f-{i}", "severity": "warning", "timestamp": i} for i in range(20)]

    with patch("sre_agent.monitor.scanners.scan_crashlooping_pods", return_value=many_findings[:10]):
        with patch("sre_agent.monitor.scanners.scan_pending_pods", return_value=many_findings[10:]):
            with patch("sre_agent.monitor.scanners.scan_oom_killed_pods", return_value=[]):
                with patch("sre_agent.monitor.scanners.scan_firing_alerts", return_value=[]):
                    with patch("sre_agent.monitor.trend_scanners.scan_memory_pressure_forecast", return_value=[]):
                        with patch("sre_agent.monitor.trend_scanners.scan_disk_pressure_forecast", return_value=[]):
                            with patch("sre_agent.monitor.trend_scanners.scan_hpa_exhaustion_trend", return_value=[]):
                                with patch(
                                    "sre_agent.monitor.trend_scanners.scan_error_rate_acceleration", return_value=[]
                                ):
                                    result = get_briefing()

    assert len(result["priorityItems"]) == 10


def test_get_briefing_handles_scanner_failures(mock_empty_db):
    """Test briefing continues when individual scanners fail."""

    def failing_scanner():
        raise Exception("Scanner crashed")

    with patch("sre_agent.monitor.scanners.scan_crashlooping_pods", side_effect=failing_scanner):
        with patch("sre_agent.monitor.scanners.scan_pending_pods", return_value=[]):
            with patch("sre_agent.monitor.scanners.scan_oom_killed_pods", return_value=[]):
                with patch("sre_agent.monitor.scanners.scan_firing_alerts", return_value=[]):
                    with patch("sre_agent.monitor.trend_scanners.scan_memory_pressure_forecast", return_value=[]):
                        with patch("sre_agent.monitor.trend_scanners.scan_disk_pressure_forecast", return_value=[]):
                            with patch("sre_agent.monitor.trend_scanners.scan_hpa_exhaustion_trend", return_value=[]):
                                with patch(
                                    "sre_agent.monitor.trend_scanners.scan_error_rate_acceleration", return_value=[]
                                ):
                                    result = get_briefing()

    # Should still return a valid briefing
    assert "currentFindings" in result
    assert "trendFindings" in result
    assert "priorityItems" in result


def test_get_briefing_backward_compatible(mock_empty_db):
    """Test briefing still returns all original fields."""
    with patch("sre_agent.monitor.scanners.scan_crashlooping_pods", return_value=[]):
        with patch("sre_agent.monitor.scanners.scan_pending_pods", return_value=[]):
            with patch("sre_agent.monitor.scanners.scan_oom_killed_pods", return_value=[]):
                with patch("sre_agent.monitor.scanners.scan_firing_alerts", return_value=[]):
                    with patch("sre_agent.monitor.trend_scanners.scan_memory_pressure_forecast", return_value=[]):
                        with patch("sre_agent.monitor.trend_scanners.scan_disk_pressure_forecast", return_value=[]):
                            with patch("sre_agent.monitor.trend_scanners.scan_hpa_exhaustion_trend", return_value=[]):
                                with patch(
                                    "sre_agent.monitor.trend_scanners.scan_error_rate_acceleration", return_value=[]
                                ):
                                    result = get_briefing()

    # Original fields
    assert "greeting" in result
    assert "summary" in result
    assert "hours" in result
    assert "actions" in result
    assert "investigations" in result
    assert "categoriesFixed" in result

    # New fields
    assert "currentFindings" in result
    assert "trendFindings" in result
    assert "priorityItems" in result
