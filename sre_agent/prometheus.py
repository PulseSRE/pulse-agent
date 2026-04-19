"""Shared Prometheus/Thanos HTTP client.

Consolidates the urllib+SSL+token boilerplate used by get_prometheus_query,
discover_metrics, verify_query, and get_resource_recommendations.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import urllib.parse
import urllib.request

logger = logging.getLogger("pulse_agent.prometheus")

# In-cluster CA paths (OpenShift service-serving-signer and SA CA)
_CA_PATHS = [
    "/var/run/secrets/kubernetes.io/serviceaccount/service-ca.crt",
    "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
]


def _build_ssl_context() -> ssl.SSLContext:
    """Build an SSL context, loading the in-cluster CA if available."""
    # Check for explicit insecure override (dev clusters only)
    if os.environ.get("PULSE_AGENT_PROMETHEUS_INSECURE", "").lower() in ("1", "true"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    # Try to load in-cluster CA certificates
    for ca_path in _CA_PATHS:
        if os.path.exists(ca_path):
            try:
                ctx = ssl.create_default_context(cafile=ca_path)
                return ctx
            except Exception:
                logger.debug("Failed to load CA from %s", ca_path)

    # Fallback: skip verification for internal service traffic
    # This is safe for in-cluster Thanos (service-serving CA differs from SA CA)
    logger.warning(
        "No CA certificate found in %s — falling back to CERT_NONE (TLS verification disabled)",
        _CA_PATHS,
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _get_base_url() -> str:
    """Get the Prometheus/Thanos base URL."""
    return os.environ.get(
        "THANOS_URL",
        "https://thanos-querier.openshift-monitoring.svc:9091",
    )


def _get_token() -> str:
    """Read the in-cluster service account token."""
    try:
        with open("/var/run/secrets/kubernetes.io/serviceaccount/token") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def prometheus_request(endpoint: str, params: dict | None = None, timeout: int = 30) -> dict:
    """Make an HTTP request to Prometheus/Thanos.

    Args:
        endpoint: API path (e.g., "api/v1/query", "api/v1/label/__name__/values")
        params: Query parameters (optional)
        timeout: Request timeout in seconds

    Returns:
        Parsed JSON response dict

    Raises:
        Exception on connection error or non-JSON response
    """
    base_url = _get_base_url()
    url = f"{base_url}/{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    token = _get_token()
    ctx = _build_ssl_context()

    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)
    resp = urllib.request.urlopen(req, context=ctx, timeout=timeout)
    return json.loads(resp.read())
