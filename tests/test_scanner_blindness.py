"""A scanner that cannot see must not report "clean".

Scanners answered an API error with ``return findings`` — an empty list, the
same thing a healthy scan of a healthy cluster returns. On a default install
the operator grants no Secrets, so the cert scanner was blind forever and its
status read "clean". These hold the line in the shared plumbing: ``safe()``,
``safe_async()``, the Prometheus helper, and the SLO evaluation.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.rest import ApiException

from sre_agent.async_k8s import safe_async
from sre_agent.k8s_client import safe
from sre_agent.monitor import scanner_health
from sre_agent.monitor.scanner_health import get_failure, optional_access, reset, scanning


@pytest.fixture(autouse=True)
def _clean():
    reset()
    yield
    reset()


def _raise(status):
    def fn():
        raise ApiException(status=status, reason="nope")

    return fn


def test_a_forbidden_list_marks_the_scanner_failed():
    with scanning("cert_expiry"):
        safe(_raise(403))
    assert get_failure("cert_expiry").startswith("forbidden")


def test_an_api_that_is_not_served_is_not_a_failure():
    """No ClusterOperators on vanilla Kubernetes is not blindness."""
    with scanning("operators"):
        safe(_raise(404))
    assert get_failure("operators") is None


def test_an_optional_check_without_permission_does_not_fail_its_scanner():
    with scanning("audit_auth"):
        with optional_access():
            safe(_raise(403))
    assert get_failure("audit_auth") is None


def test_outside_a_scan_nothing_is_recorded():
    """safe() is also every chat tool's wrapper."""
    safe(_raise(403))
    assert scanner_health._failures_this_cycle == {}


def test_safe_async_reports_too():
    async def boom():
        raise ApiException(status=403, reason="nope")

    async def run():
        with scanning("pending"):
            await safe_async(boom())

    asyncio.run(run())
    assert get_failure("pending") is not None


def test_prometheus_down_fails_the_trend_scanner():
    from sre_agent.monitor import trend_scanners

    client = MagicMock()
    client.query.side_effect = ConnectionError("connection refused")
    with patch.object(trend_scanners, "get_prometheus_client", return_value=client):
        with scanning("trend_memory"):
            assert trend_scanners.scan_memory_pressure_forecast() == []
    assert "Prometheus unreachable" in get_failure("trend_memory")


def _slo_registry(result):
    from sre_agent.slo_registry import SLODefinition, SLORegistry

    reg = SLORegistry()
    reg.register(SLODefinition(service_name="api", slo_type="availability", target=0.999), persist=False)
    client = MagicMock()
    client.query.return_value = result
    return reg, client


def test_a_burning_slo_is_reported():
    """The value parser never matched the tool's tuple, so every SLO read "ok"."""
    reg, client = _slo_registry({"status": "success", "data": {"result": [{"metric": {}, "value": [0, "0.5"]}]}})
    with patch("sre_agent.prometheus.get_prometheus_client", return_value=client):
        statuses = reg.evaluate_with_prometheus()
    assert [s.alert_level for s in statuses] == ["critical"]


def test_an_unmeasured_slo_has_no_verdict():
    reg, client = _slo_registry({"status": "success", "data": {"result": []}})
    with patch("sre_agent.prometheus.get_prometheus_client", return_value=client):
        assert reg.evaluate_with_prometheus() == []


def test_a_failed_slo_query_fails_the_scanner():
    reg, client = _slo_registry({"status": "error", "error": "bad query"})
    with patch("sre_agent.prometheus.get_prometheus_client", return_value=client):
        with scanning("slo"):
            assert reg.evaluate_with_prometheus() == []
    assert "bad query" in get_failure("slo")


def test_a_failed_shared_pod_list_is_not_handed_to_scanners():
    """A ToolError is not a str; passed on, crashloop/oom/image_pull saw 'no pods'."""
    from unittest.mock import AsyncMock

    import sre_agent.monitor.cluster_monitor as cm
    from sre_agent.monitor.scanner_protocol import FunctionScanner, ScannerMeta

    received: list = []

    def crashloop(pods=None):
        received.append(pods)
        return []

    core = MagicMock()

    async def forbidden(*a, **kw):
        raise ApiException(status=403, reason="nope")

    core.list_pod_for_all_namespaces = forbidden
    scanners = [FunctionScanner(ScannerMeta("crashloop", "c", "d", "c"), crashloop, accepts_pods=True)]
    with (
        patch.object(cm, "get_monitor_repo", MagicMock()),
        patch.object(cm, "get_all_scanner_instances", lambda: scanners),
        patch.object(cm, "expire_orphaned_proposals", lambda: 0),
        patch("sre_agent.async_k8s.get_async_core_client", AsyncMock(return_value=core)),
        patch("sre_agent.inbox.bridge_finding_to_inbox", lambda f: None),
        patch("sre_agent.inbox.run_generator_cycle", lambda: None),
        patch("sre_agent.inbox.reconcile_open_items", lambda k: 0),
        patch("sre_agent.monitor.recurrence.process_recurrences", AsyncMock()),
    ):
        m = cm.ClusterMonitor()
        m._correlate_episodes = lambda f: []
        for name in ("run_investigations", "auto_fix", "process_verifications", "process_handoffs"):
            setattr(m, name, AsyncMock())
        m._broadcast_raw = AsyncMock()
        m.broadcast = AsyncMock()
        asyncio.run(m._run_scan_locked())
    assert received == [None], "the scanner lists pods itself, and reports if that fails too"
