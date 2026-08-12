"""Performance tests — monitor scan cycle time.

A full scan cycle must complete within the configured interval (default 60s).
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

SCAN_CYCLE_THRESHOLD_S = 60.0


def _mock_async_core():
    """Mock async CoreV1Api used by cluster_monitor's shared-resource prefetch.

    Without this, ``_run_scan_locked()`` calls the real ``async_k8s`` client,
    which loads the machine's actual kubeconfig and attempts a live network
    call to whatever cluster is the current context — hanging indefinitely
    if that cluster is unreachable (e.g. VPN down) instead of failing fast.
    """
    core = MagicMock()
    core.list_pod_for_all_namespaces = AsyncMock(return_value=MagicMock(items=[]))
    return core


def _mock_async_apps():
    """Mock async AppsV1Api used by the deployment/daemonset async scanners."""
    apps = MagicMock()
    apps.list_deployment_for_all_namespaces = AsyncMock(return_value=MagicMock(items=[]))
    apps.list_daemon_set_for_all_namespaces = AsyncMock(return_value=MagicMock(items=[]))
    return apps


def _mock_async_custom():
    """Mock async CustomObjectsApi used by the degraded-operator scanner."""
    custom = MagicMock()
    custom.list_cluster_custom_object = AsyncMock(return_value={"items": []})
    return custom


def _mock_async_autoscaling():
    """Mock async AutoscalingV1Api used by the HPA-saturation scanner."""
    autoscaling = MagicMock()
    autoscaling.list_horizontal_pod_autoscaler_for_all_namespaces = AsyncMock(return_value=MagicMock(items=[]))
    return autoscaling


def _mock_core():
    core = MagicMock()
    core.list_pod_for_all_namespaces.return_value = MagicMock(items=[])
    core.list_node.return_value = MagicMock(items=[])
    core.list_namespaced_pod.return_value = MagicMock(items=[])
    core.list_namespaced_event.return_value = MagicMock(items=[])
    core.list_event_for_all_namespaces.return_value = MagicMock(items=[])
    core.list_namespaced_secret.return_value = MagicMock(items=[])
    core.list_secret_for_all_namespaces.return_value = MagicMock(items=[])
    core.list_namespaced_service_account.return_value = MagicMock(items=[])
    return core


def _mock_apps():
    apps = MagicMock()
    apps.list_deployment_for_all_namespaces.return_value = MagicMock(items=[])
    apps.list_daemon_set_for_all_namespaces.return_value = MagicMock(items=[])
    return apps


def _mock_custom():
    custom = MagicMock()
    custom.list_cluster_custom_object.return_value = {"items": []}
    custom.list_namespaced_custom_object.return_value = {"items": []}
    return custom


def _make_monitor():
    from sre_agent.monitor.cluster_monitor import ClusterMonitor
    from sre_agent.monitor.session import MonitorClient

    ws = AsyncMock()
    ws.send_json = AsyncMock()
    monitor = ClusterMonitor()
    client = MonitorClient(ws, trust_level=0, auto_fix_categories=[])
    monitor._subscribers.append(client)
    return monitor


class TestScanCycleLatency:
    def test_scan_cycle_within_threshold(self, monkeypatch):
        monkeypatch.setenv("PULSE_AGENT_WS_TOKEN", "test-token")
        monkeypatch.setenv("PULSE_AGENT_MEMORY", "0")
        monkeypatch.setenv("PULSE_AGENT_AUTOFIX_ENABLED", "false")

        with (
            patch("sre_agent.k8s_client._initialized", True),
            patch("sre_agent.k8s_client._load_k8s"),
            patch("sre_agent.k8s_client.get_core_client", return_value=_mock_core()),
            patch("sre_agent.k8s_client.get_apps_client", return_value=_mock_apps()),
            patch("sre_agent.k8s_client.get_custom_client", return_value=_mock_custom()),
            patch("sre_agent.k8s_client.get_version_client", return_value=MagicMock()),
            patch("sre_agent.async_k8s.get_async_core_client", AsyncMock(return_value=_mock_async_core())),
            patch("sre_agent.async_k8s.get_async_apps_client", AsyncMock(return_value=_mock_async_apps())),
            patch("sre_agent.async_k8s.get_async_custom_client", AsyncMock(return_value=_mock_async_custom())),
            patch(
                "sre_agent.async_k8s.get_async_autoscaling_client", AsyncMock(return_value=_mock_async_autoscaling())
            ),
        ):
            monitor = _make_monitor()

            loop = asyncio.new_event_loop()
            try:
                start = time.monotonic()
                loop.run_until_complete(monitor.run_scan())
                elapsed = time.monotonic() - start
            finally:
                loop.close()

            assert elapsed < SCAN_CYCLE_THRESHOLD_S, (
                f"Scan cycle took {elapsed:.2f}s (threshold: {SCAN_CYCLE_THRESHOLD_S}s)"
            )

    def test_scan_completes_with_empty_cluster(self, monkeypatch):
        monkeypatch.setenv("PULSE_AGENT_WS_TOKEN", "test-token")
        monkeypatch.setenv("PULSE_AGENT_MEMORY", "0")
        monkeypatch.setenv("PULSE_AGENT_AUTOFIX_ENABLED", "false")

        with (
            patch("sre_agent.k8s_client._initialized", True),
            patch("sre_agent.k8s_client._load_k8s"),
            patch("sre_agent.k8s_client.get_core_client", return_value=_mock_core()),
            patch("sre_agent.k8s_client.get_apps_client", return_value=_mock_apps()),
            patch("sre_agent.k8s_client.get_custom_client", return_value=_mock_custom()),
            patch("sre_agent.k8s_client.get_version_client", return_value=MagicMock()),
            patch("sre_agent.async_k8s.get_async_core_client", AsyncMock(return_value=_mock_async_core())),
            patch("sre_agent.async_k8s.get_async_apps_client", AsyncMock(return_value=_mock_async_apps())),
            patch("sre_agent.async_k8s.get_async_custom_client", AsyncMock(return_value=_mock_async_custom())),
            patch(
                "sre_agent.async_k8s.get_async_autoscaling_client", AsyncMock(return_value=_mock_async_autoscaling())
            ),
        ):
            monitor = _make_monitor()
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(monitor.run_scan())
            finally:
                loop.close()
            assert monitor._scan_counter >= 1
