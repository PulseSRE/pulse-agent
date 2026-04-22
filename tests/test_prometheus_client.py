"""Tests for the unified PrometheusClient with dual-backend support."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sre_agent.prometheus import (
    _ACM_DEFAULT,
    _LOCAL_DEFAULT,
    PrometheusBackend,
    PrometheusClient,
    get_prometheus_client,
    prometheus_request,
)


class TestPrometheusBackendRouting:
    def test_local_url_default(self):
        client = PrometheusClient()
        with patch.object(client, "_get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(thanos_url="", acm_thanos_url="")
            assert client._get_url(PrometheusBackend.LOCAL) == _LOCAL_DEFAULT

    def test_local_url_override(self):
        client = PrometheusClient()
        with patch.object(client, "_get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(thanos_url="https://custom:9091")
            assert client._get_url(PrometheusBackend.LOCAL) == "https://custom:9091"

    def test_acm_url_default(self):
        client = PrometheusClient()
        with patch.object(client, "_get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(acm_thanos_url="")
            assert client._get_url(PrometheusBackend.ACM) == _ACM_DEFAULT

    def test_acm_url_override(self):
        client = PrometheusClient()
        with patch.object(client, "_get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(acm_thanos_url="http://custom-thanos:9090")
            assert client._get_url(PrometheusBackend.ACM) == "http://custom-thanos:9090"


class TestACMDetection:
    def test_acm_forced_on(self):
        client = PrometheusClient()
        with patch.object(client, "_get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(acm_thanos_enabled=True)
            assert client.is_acm_available() is True

    def test_acm_forced_off(self):
        client = PrometheusClient()
        with patch.object(client, "_get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(acm_thanos_enabled=False)
            assert client.is_acm_available() is False

    def test_acm_auto_detect_namespace_exists(self):
        client = PrometheusClient()
        with (
            patch.object(client, "_get_settings") as mock_settings,
            patch("sre_agent.k8s_client.get_core_client") as mock_core,
        ):
            mock_settings.return_value = MagicMock(acm_thanos_enabled=None)
            mock_core.return_value.read_namespace.return_value = MagicMock()
            assert client.is_acm_available() is True

    def test_acm_auto_detect_namespace_missing(self):
        client = PrometheusClient()
        with (
            patch.object(client, "_get_settings") as mock_settings,
            patch("sre_agent.k8s_client.get_core_client") as mock_core,
        ):
            mock_settings.return_value = MagicMock(acm_thanos_enabled=None)
            mock_core.return_value.read_namespace.side_effect = Exception("not found")
            assert client.is_acm_available() is False

    def test_acm_detection_cached(self):
        client = PrometheusClient()
        client._acm_available = True
        assert client.is_acm_available() is True


class TestBackwardCompat:
    def test_prometheus_request_wrapper(self):
        with patch("sre_agent.prometheus.get_prometheus_client") as mock_get:
            mock_client = MagicMock()
            mock_client._request.return_value = {"status": "success"}
            mock_get.return_value = mock_client

            result = prometheus_request("api/v1/query", {"query": "up"}, 15)

            mock_client._request.assert_called_once_with("api/v1/query", {"query": "up"}, 15, PrometheusBackend.LOCAL)
            assert result == {"status": "success"}

    def test_get_prometheus_client_singleton(self):
        import sre_agent.prometheus as mod

        mod._client = None
        c1 = get_prometheus_client()
        c2 = get_prometheus_client()
        assert c1 is c2
        mod._client = None


class TestQueryMethods:
    def test_query_calls_request(self):
        client = PrometheusClient()
        with patch.object(client, "_request") as mock_req:
            mock_req.return_value = {"status": "success", "data": {"result": []}}
            client.query("up", backend=PrometheusBackend.ACM, timeout=10)
            mock_req.assert_called_once_with("api/v1/query", {"query": "up"}, 10, PrometheusBackend.ACM)

    def test_query_range_calls_request(self):
        client = PrometheusClient()
        with patch.object(client, "_request") as mock_req:
            mock_req.return_value = {"status": "success"}
            client.query_range("up", 1000, 2000, 60, backend=PrometheusBackend.LOCAL)
            mock_req.assert_called_once_with(
                "api/v1/query_range",
                {"query": "up", "start": "1000", "end": "2000", "step": "60"},
                30,
                PrometheusBackend.LOCAL,
            )

    def test_label_values(self):
        client = PrometheusClient()
        with patch.object(client, "_request") as mock_req:
            mock_req.return_value = {"status": "success", "data": ["up", "node_cpu"]}
            result = client.label_values("__name__")
            assert result == ["up", "node_cpu"]

    def test_label_values_failure(self):
        client = PrometheusClient()
        with patch.object(client, "_request") as mock_req:
            mock_req.return_value = {"status": "error"}
            result = client.label_values("__name__")
            assert result == []


class TestSSL:
    def test_insecure_mode(self):
        client = PrometheusClient()
        with patch.object(client, "_get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(prometheus_insecure=True)
            ctx = client._build_ssl_context()
            import ssl

            assert ctx.verify_mode == ssl.CERT_NONE
