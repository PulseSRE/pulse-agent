"""Tests for The Prophet (predictive remediation) tools."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.rest import ApiException

from sre_agent.predict_tools import (
    forecast_quota_exhaustion,
    analyze_hpa_thrashing,
    suggest_remediation,
)


@pytest.fixture
def mock_predict():
    with (
        patch("sre_agent.predict_tools.get_core_client") as core_p,
        patch("sre_agent.predict_tools.get_autoscaling_client") as auto_p,
        patch("sre_agent.predict_tools._query_prometheus_trend") as prom_p,
    ):
        core = MagicMock()
        auto = MagicMock()
        core_p.return_value = core
        auto_p.return_value = auto
        prom_p.return_value = None  # Default: Prometheus not available
        yield {"core": core, "auto": auto, "prom": prom_p}


class TestForecastQuotaExhaustion:
    def test_basic_forecast(self, mock_predict):
        quota = SimpleNamespace(
            metadata=SimpleNamespace(name="compute"),
            status=SimpleNamespace(
                hard={"cpu": "4", "memory": "8Gi", "pods": "20"},
                used={"cpu": "3", "memory": "6Gi", "pods": "15"},
            ),
        )
        mock_predict["core"].list_namespaced_resource_quota.return_value = SimpleNamespace(items=[quota])
        mock_predict["core"].list_namespaced_pod.return_value = SimpleNamespace(
            items=[SimpleNamespace() for _ in range(15)]
        )

        result = forecast_quota_exhaustion.call({"namespace": "default"})
        assert "cpu" in result
        assert "memory" in result
        assert "pods" in result
        assert "WATCH" in result  # 75% usage = WATCH level

    def test_critical_threshold(self, mock_predict):
        quota = SimpleNamespace(
            metadata=SimpleNamespace(name="compute"),
            status=SimpleNamespace(
                hard={"cpu": "4"},
                used={"cpu": "3900m"},
            ),
        )
        mock_predict["core"].list_namespaced_resource_quota.return_value = SimpleNamespace(items=[quota])
        mock_predict["core"].list_namespaced_pod.return_value = SimpleNamespace(items=[])

        result = forecast_quota_exhaustion.call({"namespace": "default"})
        assert "CRITICAL" in result

    def test_no_quotas(self, mock_predict):
        mock_predict["core"].list_namespaced_resource_quota.return_value = SimpleNamespace(items=[])
        result = forecast_quota_exhaustion.call({"namespace": "default"})
        assert "No ResourceQuotas" in result

    def test_prometheus_trend(self, mock_predict):
        """When Prometheus returns a growth rate, show time-based forecast."""
        quota = SimpleNamespace(
            metadata=SimpleNamespace(name="compute"),
            status=SimpleNamespace(
                hard={"cpu": "8"},
                used={"cpu": "4"},
            ),
        )
        mock_predict["core"].list_namespaced_resource_quota.return_value = SimpleNamespace(items=[quota])
        mock_predict["core"].list_namespaced_pod.return_value = SimpleNamespace(items=[])
        # 500m per hour growth → 4000m remaining / 500m/h = 8 hours
        mock_predict["prom"].return_value = 500.0

        result = forecast_quota_exhaustion.call({"namespace": "default"})
        assert "EXHAUSTION IN" in result
        assert "prometheus" in result

    def test_api_error(self, mock_predict):
        mock_predict["core"].list_namespaced_resource_quota.side_effect = ApiException(
            status=403, reason="Forbidden"
        )
        result = forecast_quota_exhaustion.call({"namespace": "default"})
        assert "Error (403)" in result


class TestAnalyzeHpaThrashing:
    def test_healthy_hpa(self, mock_predict):
        hpa = SimpleNamespace(
            metadata=SimpleNamespace(name="web-hpa", namespace="default"),
            spec=SimpleNamespace(
                min_replicas=2,
                max_replicas=10,
                scale_target_ref=SimpleNamespace(kind="Deployment", name="web"),
            ),
            status=SimpleNamespace(
                current_replicas=3,
                conditions=[],
                current_metrics=[],
            ),
        )
        mock_predict["auto"].list_horizontal_pod_autoscaler_for_all_namespaces.return_value = SimpleNamespace(items=[hpa])
        result = analyze_hpa_thrashing.call({"namespace": "ALL"})
        assert "No HPA issues" in result

    def test_wide_range_thrashing(self, mock_predict):
        hpa = SimpleNamespace(
            metadata=SimpleNamespace(name="api-hpa", namespace="prod"),
            spec=SimpleNamespace(
                min_replicas=1,
                max_replicas=20,
                scale_target_ref=SimpleNamespace(kind="Deployment", name="api"),
            ),
            status=SimpleNamespace(
                current_replicas=12,
                conditions=[],
                current_metrics=[],
            ),
        )
        mock_predict["auto"].list_horizontal_pod_autoscaler_for_all_namespaces.return_value = SimpleNamespace(items=[hpa])
        result = analyze_hpa_thrashing.call({"namespace": "ALL"})
        assert "Wide scaling range" in result
        assert "Suggested min-replicas" in result

    def test_at_max_high_utilization(self, mock_predict):
        hpa = SimpleNamespace(
            metadata=SimpleNamespace(name="hpa", namespace="default"),
            spec=SimpleNamespace(
                min_replicas=2,
                max_replicas=5,
                scale_target_ref=SimpleNamespace(kind="Deployment", name="app"),
            ),
            status=SimpleNamespace(
                current_replicas=5,
                conditions=[],
                current_metrics=[SimpleNamespace(
                    type="Resource",
                    resource=SimpleNamespace(
                        name="cpu",
                        current=SimpleNamespace(average_utilization=90),
                    ),
                )],
            ),
        )
        mock_predict["auto"].list_horizontal_pod_autoscaler_for_all_namespaces.return_value = SimpleNamespace(items=[hpa])
        result = analyze_hpa_thrashing.call({"namespace": "ALL"})
        assert "max replicas" in result.lower()
        assert "increasing" in result.lower() or "increase" in result.lower()


class TestSuggestRemediation:
    def test_crashloopbackoff(self):
        result = suggest_remediation.call({"error_type": "CrashLoopBackOff"})
        assert "CrashLoopBackOff" in result
        assert "get_pod_logs" in result
        assert "Cause:" in result

    def test_oomkilled(self):
        result = suggest_remediation.call({"error_type": "OOMKilled"})
        assert "memory limit" in result.lower()
        assert "get_pod_metrics" in result

    def test_imagepullbackoff(self):
        result = suggest_remediation.call({"error_type": "ImagePullBackOff"})
        assert "pull secret" in result.lower() or "registry" in result.lower()

    def test_pending(self):
        result = suggest_remediation.call({"error_type": "Pending"})
        assert "FailedScheduling" in result

    def test_nodenotready(self):
        result = suggest_remediation.call({"error_type": "NodeNotReady"})
        assert "cordon" in result.lower() or "drain" in result.lower()

    def test_case_insensitive(self):
        result = suggest_remediation.call({"error_type": "crashloopbackoff"})
        assert "crashloopbackoff" in result.lower()

    def test_unknown_error(self):
        result = suggest_remediation.call({"error_type": "SomeUnknownError"})
        assert "No specific remediation" in result
        assert "Available guides" in result

    def test_with_context(self):
        result = suggest_remediation.call({
            "error_type": "OOMKilled",
            "namespace": "prod",
            "resource_name": "web-1",
        })
        assert "prod/web-1" in result
