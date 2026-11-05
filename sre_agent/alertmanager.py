"""Alertmanager v2 HTTP client.

Silences are an Alertmanager-only API: Thanos and Prometheus have no
equivalent, and the API server's service proxy cannot carry the caller's
token to the ``kube-rbac-proxy`` in front of ``alertmanager-main``. So this
talks to the same in-cluster endpoint the UI's ``/api/alertmanager/`` proxy
does (``alertmanager-main.openshift-monitoring.svc:9094``), with the same
bearer token rule as :mod:`sre_agent.prometheus`: the caller's forwarded
token when a request runs on someone's behalf, the service account's only
when there is no caller (token forwarding off). Alertmanager's proxy then
applies the caller's own RBAC (``monitoring-alertmanager-view`` to read,
``monitoring-alertmanager-edit`` to create or expire a silence).

A failed request raises :class:`AlertmanagerError` carrying the HTTP status
and Alertmanager's own message. Callers report it; nothing here turns an
unreachable or forbidden Alertmanager into an empty answer.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger("pulse_agent.alertmanager")

DEFAULT_URL = "https://alertmanager-main.openshift-monitoring.svc:9094"


class AlertmanagerError(RuntimeError):
    """An Alertmanager request failed. ``status`` is 0 when it never answered."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail

    def describe(self) -> str:
        if self.status == 401:
            return "Alertmanager did not accept your credentials (401). Sign in again."
        if self.status == 403:
            return (
                "Alertmanager refused this (403): your account needs the monitoring-alertmanager-edit role "
                "in openshift-monitoring to change silences, or monitoring-alertmanager-view to read them."
            )
        if self.status == 404:
            return f"Alertmanager has no such object (404): {self.detail}"
        if self.status == 0:
            return f"Alertmanager could not be reached: {self.detail}"
        return f"Alertmanager returned {self.status}: {self.detail}"


def _base_url() -> str:
    from .config import get_settings

    return (get_settings().prometheus.alertmanager_url or DEFAULT_URL).rstrip("/")


def _token() -> str:
    from .k8s_client import get_current_user_token
    from .prometheus import get_prometheus_client

    # Same order as PrometheusClient.request: the caller first, the service
    # account only when no caller is attached to this request.
    return get_current_user_token() or get_prometheus_client()._get_token()


def request(method: str, path: str, body: Any = None, *, params: dict | None = None, timeout: int = 10) -> Any:
    """Call ``/api/v2/<path>`` and return the decoded JSON (``None`` for an empty body)."""
    from .prometheus import get_prometheus_client

    url = f"{_base_url()}/api/v2/{path.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    token = _token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    ctx = get_prometheus_client()._build_ssl_context() if url.startswith("https") else None
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace").strip()[:300]
        except Exception:
            detail = ""
        raise AlertmanagerError(e.code, detail or str(e.reason)) from None
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise AlertmanagerError(0, str(getattr(e, "reason", e))[:300]) from None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        raise AlertmanagerError(502, "Alertmanager returned a non-JSON response") from None


def list_alerts(*, active: bool = True, silenced: bool = False, inhibited: bool = False) -> list[dict]:
    data = request(
        "GET",
        "alerts",
        params={
            "active": str(active).lower(),
            "silenced": str(silenced).lower(),
            "inhibited": str(inhibited).lower(),
        },
    )
    if not isinstance(data, list):
        raise AlertmanagerError(502, "Alertmanager returned an unexpected alerts payload")
    return data


def list_silences() -> list[dict]:
    data = request("GET", "silences")
    if not isinstance(data, list):
        raise AlertmanagerError(502, "Alertmanager returned an unexpected silences payload")
    return data


def get_silence(silence_id: str) -> dict:
    data = request("GET", f"silence/{urllib.parse.quote(silence_id, safe='')}")
    if not isinstance(data, dict):
        raise AlertmanagerError(502, "Alertmanager returned an unexpected silence payload")
    return data


def create_silence(body: dict) -> str:
    data = request("POST", "silences", body)
    silence_id = data.get("silenceID") if isinstance(data, dict) else None
    if not silence_id:
        raise AlertmanagerError(502, "Alertmanager did not return a silence id")
    return str(silence_id)


def expire_silence(silence_id: str) -> None:
    request("DELETE", f"silence/{urllib.parse.quote(silence_id, safe='')}")
