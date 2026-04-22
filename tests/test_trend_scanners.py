"""Tests for trend-based predictive scanners."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sre_agent.monitor.trend_scanners import (
    scan_disk_pressure_forecast,
    scan_error_rate_acceleration,
    scan_hpa_exhaustion_trend,
    scan_memory_pressure_forecast,
)


@pytest.fixture
def mock_prometheus_response():
    """Factory for creating mock Prometheus query() responses."""

    def _make_response(results: list[dict]) -> dict:
        return {"status": "success", "data": {"result": results}}

    return _make_response


def _patch_prom(response):
    """Patch get_prometheus_client().query() to return response."""
    mock_client = MagicMock()
    mock_client.query.return_value = response
    return patch("sre_agent.monitor.trend_scanners.get_prometheus_client", return_value=mock_client)


def test_scan_memory_pressure_forecast_detects_exhaustion(mock_prometheus_response):
    """Test memory pressure forecast detects nodes predicted to run out of memory."""
    results = [
        {
            "metric": {"instance": "node1", "node": "node1"},
            "value": [1234567890, "-1000000000"],
        }
    ]

    with _patch_prom(mock_prometheus_response(results)):
        findings = scan_memory_pressure_forecast()

        assert len(findings) == 1
        assert findings[0]["severity"] == "warning"
        assert findings[0]["category"] == "memory_pressure"
        assert findings[0]["findingType"] == "trend"
        assert "node1" in findings[0]["title"]
        assert "3 days" in findings[0]["summary"]
        assert findings[0]["confidence"] == 0.75


def test_scan_memory_pressure_forecast_no_exhaustion(mock_prometheus_response):
    """Test memory pressure forecast returns no findings when memory is stable."""
    results = [
        {
            "metric": {"instance": "node1"},
            "value": [1234567890, "5000000000"],
        }
    ]

    with _patch_prom(mock_prometheus_response(results)):
        findings = scan_memory_pressure_forecast()

        assert len(findings) == 0


def test_scan_disk_pressure_forecast_detects_pvc_exhaustion(mock_prometheus_response):
    """Test disk pressure forecast detects PVCs predicted to fill up."""
    results = [
        {
            "metric": {
                "namespace": "default",
                "persistentvolumeclaim": "data-pvc",
                "pod": "app-pod",
            },
            "value": [1234567890, "1"],
        }
    ]

    with _patch_prom(mock_prometheus_response(results)):
        findings = scan_disk_pressure_forecast()

        assert len(findings) == 1
        assert findings[0]["severity"] == "warning"
        assert findings[0]["category"] == "disk_pressure"
        assert findings[0]["findingType"] == "trend"
        assert "data-pvc" in findings[0]["title"]
        assert "7 days" in findings[0]["summary"]
        assert findings[0]["resources"][0]["kind"] == "PersistentVolumeClaim"


def test_scan_disk_pressure_forecast_pod_fallback(mock_prometheus_response):
    """Test disk pressure forecast uses pod name when PVC is not available."""
    results = [
        {
            "metric": {
                "namespace": "default",
                "pod": "app-pod",
            },
            "value": [1234567890, "1"],
        }
    ]

    with _patch_prom(mock_prometheus_response(results)):
        findings = scan_disk_pressure_forecast()

        assert len(findings) == 1
        assert "app-pod" in findings[0]["title"]
        assert findings[0]["resources"][0]["kind"] == "Pod"


def test_scan_hpa_exhaustion_trend_detects_sustained_high_usage(mock_prometheus_response):
    """Test HPA exhaustion trend detects sustained near-max capacity."""
    results = [
        {
            "metric": {
                "namespace": "default",
                "horizontalpodautoscaler": "web-hpa",
            },
            "value": [1234567890, "0.95"],
        }
    ]

    with _patch_prom(mock_prometheus_response(results)):
        findings = scan_hpa_exhaustion_trend()

        assert len(findings) == 1
        assert findings[0]["severity"] == "warning"
        assert findings[0]["category"] == "hpa"
        assert findings[0]["findingType"] == "trend"
        assert "web-hpa" in findings[0]["title"]
        assert "95%" in findings[0]["summary"]
        assert "48 hours" in findings[0]["summary"]
        assert findings[0]["confidence"] == 0.85


def test_scan_hpa_exhaustion_trend_no_findings_below_threshold(mock_prometheus_response):
    """Test HPA exhaustion trend with empty Prometheus results."""
    results = []

    with _patch_prom(mock_prometheus_response(results)):
        findings = scan_hpa_exhaustion_trend()

        assert len(findings) == 0


def test_scan_error_rate_acceleration_detects_increasing_errors(mock_prometheus_response):
    """Test error rate acceleration detects increasing 5xx errors."""
    results = [
        {
            "metric": {
                "namespace": "default",
                "service": "api-service",
                "code": "500",
            },
            "value": [1234567890, "0.005"],
        }
    ]

    with _patch_prom(mock_prometheus_response(results)):
        findings = scan_error_rate_acceleration()

        assert len(findings) == 1
        assert findings[0]["severity"] == "warning"
        assert findings[0]["category"] == "errors"
        assert findings[0]["findingType"] == "trend"
        assert "api-service" in findings[0]["title"]
        assert "increasing" in findings[0]["summary"]
        assert "24 hours" in findings[0]["summary"]
        assert findings[0]["confidence"] == 0.70


def test_scan_error_rate_acceleration_no_findings_stable_rate(mock_prometheus_response):
    """Test error rate acceleration ignores stable or decreasing error rates."""
    results = [
        {
            "metric": {
                "namespace": "default",
                "service": "api-service",
                "code": "500",
            },
            "value": [1234567890, "-0.001"],
        }
    ]

    with _patch_prom(mock_prometheus_response(results)):
        findings = scan_error_rate_acceleration()

        assert len(findings) == 0


def test_all_scanners_handle_prometheus_errors():
    """Test all scanners gracefully handle Prometheus errors."""
    mock_client = MagicMock()
    mock_client.query.side_effect = Exception("Connection refused")

    with patch("sre_agent.monitor.trend_scanners.get_prometheus_client", return_value=mock_client):
        assert scan_memory_pressure_forecast() == []
        assert scan_disk_pressure_forecast() == []
        assert scan_hpa_exhaustion_trend() == []
        assert scan_error_rate_acceleration() == []


def test_all_scanners_handle_invalid_json():
    """Test all scanners handle non-dict responses."""
    mock_client = MagicMock()
    mock_client.query.side_effect = ValueError("not json")

    with patch("sre_agent.monitor.trend_scanners.get_prometheus_client", return_value=mock_client):
        assert scan_memory_pressure_forecast() == []
        assert scan_disk_pressure_forecast() == []
        assert scan_hpa_exhaustion_trend() == []
        assert scan_error_rate_acceleration() == []


def test_all_scanners_handle_prometheus_failure_status(mock_prometheus_response):
    """Test all scanners handle Prometheus failure status."""
    mock_client = MagicMock()
    mock_client.query.return_value = {"status": "error", "error": "query timeout"}

    with patch("sre_agent.monitor.trend_scanners.get_prometheus_client", return_value=mock_client):
        assert scan_memory_pressure_forecast() == []
        assert scan_disk_pressure_forecast() == []
        assert scan_hpa_exhaustion_trend() == []
        assert scan_error_rate_acceleration() == []
