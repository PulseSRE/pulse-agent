"""Shared Prometheus/Thanos HTTP client with dual-backend support.

Supports two backends:
- LOCAL: single-cluster thanos-querier (openshift-monitoring)
- ACM: multi-cluster Thanos via ACM multicluster-observability-operator
"""

from __future__ import annotations

import enum
import json
import logging
import os
import ssl
import time as _time
import urllib.parse
import urllib.request

logger = logging.getLogger("pulse_agent.prometheus")

_LOCAL_DEFAULT = "https://thanos-querier.openshift-monitoring.svc:9091"
_ACM_DEFAULT = "http://observability-thanos-query.open-cluster-management-observability.svc:9090"
_ACM_NAMESPACE = "open-cluster-management-observability"

_CA_PATHS = [
    "/var/run/secrets/kubernetes.io/serviceaccount/service-ca.crt",
    "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
]

_TIME_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

CHART_COLORS = ["#60a5fa", "#34d399", "#fbbf24", "#f87171", "#a78bfa", "#38bdf8", "#fb923c", "#e879f9"]

ACM_NOT_AVAILABLE_MSG = (
    "ACM multicluster-observability is not available on this cluster. "
    "Fleet metrics require the ACM Observatorium stack. "
    "Check that the open-cluster-management-observability namespace exists and Thanos is running."
)

_TOKEN_TTL = 300


def parse_time_range(time_range: str) -> int:
    """Parse a time range string like '5m', '1h', '24h' into seconds."""
    try:
        unit = time_range[-1]
        amount = int(time_range[:-1])
        return amount * _TIME_UNITS.get(unit, 3600)
    except (ValueError, IndexError):
        return 3600


class PrometheusBackend(enum.Enum):
    LOCAL = "local"
    ACM = "acm"


class PrometheusClient:
    """Unified Prometheus/Thanos HTTP client with dual-backend support.

    SSL context and SA token are cached. ACM detection is cached for the
    process lifetime after first check.
    """

    def __init__(self) -> None:
        self._acm_available: bool | None = None
        self._ssl_ctx: ssl.SSLContext | None = None
        self._token: str | None = None
        self._token_read_at: float = 0.0

    def _get_settings(self):
        from .config import get_settings

        return get_settings()

    def query(self, promql: str, *, backend: PrometheusBackend = PrometheusBackend.LOCAL, timeout: int = 30) -> dict:
        return self.request("api/v1/query", {"query": promql}, timeout, backend)

    def query_range(
        self,
        promql: str,
        start: int,
        end: int,
        step: int,
        *,
        backend: PrometheusBackend = PrometheusBackend.LOCAL,
        timeout: int = 30,
    ) -> dict:
        return self.request(
            "api/v1/query_range",
            {"query": promql, "start": str(start), "end": str(end), "step": str(step)},
            timeout,
            backend,
        )

    def label_values(
        self, label: str, *, backend: PrometheusBackend = PrometheusBackend.LOCAL, timeout: int = 15
    ) -> list[str]:
        data = self.request(f"api/v1/label/{label}/values", None, timeout, backend)
        if data.get("status") == "success":
            return data.get("data", [])
        return []

    def is_acm_available(self) -> bool:
        if self._acm_available is not None:
            return self._acm_available

        settings = self._get_settings()
        if settings.acm_thanos_enabled is not None:
            self._acm_available = settings.acm_thanos_enabled
            return self._acm_available

        self._acm_available = self._detect_acm()
        return self._acm_available

    def _detect_acm(self) -> bool:
        try:
            from .k8s_client import get_core_client

            core = get_core_client()
            core.read_namespace(_ACM_NAMESPACE)
            logger.info("ACM Observability namespace detected — multi-cluster metrics available")
            return True
        except Exception:
            logger.debug("ACM Observability namespace not found — single-cluster mode")
            return False

    def _get_url(self, backend: PrometheusBackend) -> str:
        settings = self._get_settings()
        if backend == PrometheusBackend.ACM:
            return settings.acm_thanos_url or _ACM_DEFAULT
        return settings.thanos_url or _LOCAL_DEFAULT

    def _build_ssl_context(self) -> ssl.SSLContext:
        if self._ssl_ctx is not None:
            return self._ssl_ctx

        settings = self._get_settings()
        if settings.prometheus_insecure:
            logger.warning("prometheus_insecure=True — TLS verification disabled for Prometheus")
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self._ssl_ctx = ctx
            return ctx

        for ca_path in _CA_PATHS:
            if os.path.exists(ca_path):
                try:
                    ctx = ssl.create_default_context(cafile=ca_path)
                    self._ssl_ctx = ctx
                    return ctx
                except Exception:
                    logger.debug("Failed to load CA from %s", ca_path)

        raise RuntimeError(
            "No CA certificate found for Prometheus TLS. Set PULSE_AGENT_PROMETHEUS_INSECURE=true to skip verification."
        )

    def _get_token(self) -> str:
        now = _time.monotonic()
        if self._token is not None and (now - self._token_read_at) < _TOKEN_TTL:
            return self._token
        try:
            with open("/var/run/secrets/kubernetes.io/serviceaccount/token") as f:
                self._token = f.read().strip()
        except FileNotFoundError:
            self._token = ""
        self._token_read_at = now
        return self._token

    def request(self, endpoint: str, params: dict | None, timeout: int, backend: PrometheusBackend) -> dict:
        base_url = self._get_url(backend)
        url = f"{base_url}/{endpoint}"
        if params:
            url += "?" + urllib.parse.urlencode(params)

        ctx = self._build_ssl_context()
        headers: dict[str, str] = {}
        if base_url.startswith("https"):
            token = self._get_token()
            if token:
                headers["Authorization"] = f"Bearer {token}"

        req = urllib.request.Request(url, headers=headers)
        resp = urllib.request.urlopen(req, context=ctx, timeout=timeout)
        return json.loads(resp.read())


_client: PrometheusClient | None = None


def get_prometheus_client() -> PrometheusClient:
    global _client
    if _client is None:
        _client = PrometheusClient()
    return _client


def _reset_prometheus_client() -> None:
    """Reset the singleton — for testing only."""
    global _client
    _client = None


def prometheus_request(endpoint: str, params: dict | None = None, timeout: int = 30) -> dict:
    """Backward-compatible wrapper — routes to LOCAL backend."""
    return get_prometheus_client().request(endpoint, params, timeout, PrometheusBackend.LOCAL)
