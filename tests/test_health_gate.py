"""The post-fix health gate.

Pulse called a fix verified when the finding stopped appearing. Absence is
also what you get when the workload was deleted or the scanner failed, so
these tests pin the cases where absence and health disagree.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sre_agent.monitor import health_gate


class _ApiError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"api error {status}")
        self.status = status


def _apps(obj=None, exc=None):
    apps = MagicMock()
    for reader in ("read_namespaced_deployment", "read_namespaced_stateful_set", "read_namespaced_daemon_set"):
        if exc is not None:
            getattr(apps, reader).side_effect = exc
        else:
            getattr(apps, reader).return_value = obj
    return apps


def _deployment(desired: int, ready: int | None):
    return SimpleNamespace(
        spec=SimpleNamespace(replicas=desired),
        status=SimpleNamespace(ready_replicas=ready),
    )


class TestWorkloadHealth:
    def test_all_replicas_ready_passes(self):
        with patch("sre_agent.k8s_client.get_apps_client", return_value=_apps(_deployment(3, 3))):
            r = health_gate.check_resource("Deployment", "api", "prod")
        assert r.status == health_gate.PASS
        assert "3/3" in r.detail

    def test_partial_replicas_fails(self):
        with patch("sre_agent.k8s_client.get_apps_client", return_value=_apps(_deployment(3, 1))):
            r = health_gate.check_resource("Deployment", "api", "prod")
        assert r.status == health_gate.FAIL
        assert "1/3" in r.detail

    def test_deleted_workload_fails_rather_than_verifies(self):
        """The bug this gate exists for.

        A deleted Deployment emits no findings, which the old absence check
        scored as a verified fix. Deleting the patient is not a cure.
        """
        with patch("sre_agent.k8s_client.get_apps_client", return_value=_apps(exc=_ApiError(404))):
            r = health_gate.check_resource("Deployment", "api", "prod")
        assert r.status == health_gate.FAIL
        assert "no longer exists" in r.detail

    def test_scaled_to_zero_is_not_healthy(self):
        """0/0 replicas is switched off, not repaired."""
        with patch("sre_agent.k8s_client.get_apps_client", return_value=_apps(_deployment(0, 0))):
            r = health_gate.check_resource("Deployment", "api", "prod")
        assert r.status == health_gate.FAIL
        assert "0 replicas" in r.detail

    def test_unreadable_cluster_is_unverifiable_not_pass(self):
        with patch("sre_agent.k8s_client.get_apps_client", return_value=_apps(exc=_ApiError(503))):
            r = health_gate.check_resource("Deployment", "api", "prod")
        assert r.status == health_gate.UNVERIFIABLE

    def test_ready_replicas_none_counts_as_zero(self):
        with patch("sre_agent.k8s_client.get_apps_client", return_value=_apps(_deployment(2, None))):
            r = health_gate.check_resource("Deployment", "api", "prod")
        assert r.status == health_gate.FAIL


class TestPodHealth:
    def _core(self, pod=None, exc=None):
        core = MagicMock()
        if exc is not None:
            core.read_namespaced_pod.side_effect = exc
        else:
            core.read_namespaced_pod.return_value = pod
        return core

    def test_running_and_ready_passes(self):
        pod = SimpleNamespace(
            status=SimpleNamespace(
                phase="Running",
                container_statuses=[SimpleNamespace(name="app", ready=True, restart_count=2)],
            )
        )
        with patch("sre_agent.k8s_client.get_core_client", return_value=self._core(pod)):
            r = health_gate.check_resource("Pod", "api-1", "prod")
        assert r.status == health_gate.PASS

    def test_running_but_not_ready_fails(self):
        pod = SimpleNamespace(
            status=SimpleNamespace(
                phase="Running",
                container_statuses=[SimpleNamespace(name="app", ready=False, restart_count=9)],
            )
        )
        with patch("sre_agent.k8s_client.get_core_client", return_value=self._core(pod)):
            r = health_gate.check_resource("Pod", "api-1", "prod")
        assert r.status == health_gate.FAIL

    def test_missing_pod_is_unverifiable_not_failure(self):
        """Unlike a Deployment, a vanished pod is routine and proves nothing."""
        with patch("sre_agent.k8s_client.get_core_client", return_value=self._core(exc=_ApiError(404))):
            r = health_gate.check_resource("Pod", "api-1", "prod")
        assert r.status == health_gate.UNVERIFIABLE


class TestAggregate:
    def test_no_resources_is_unverifiable(self):
        status, _ = health_gate.check_resources([])
        assert status == health_gate.UNVERIFIABLE

    def test_any_failure_fails_the_gate(self):
        with patch("sre_agent.k8s_client.get_apps_client") as g:
            g.side_effect = [_apps(_deployment(1, 1)), _apps(_deployment(3, 0))]
            status, _ = health_gate.check_resources(
                [
                    {"kind": "Deployment", "name": "ok", "namespace": "prod"},
                    {"kind": "Deployment", "name": "bad", "namespace": "prod"},
                ]
            )
        assert status == health_gate.FAIL

    def test_unknown_kind_alone_is_unverifiable(self):
        status, _ = health_gate.check_resources([{"kind": "Service", "name": "s", "namespace": "prod"}])
        assert status == health_gate.UNVERIFIABLE

    def test_pass_requires_an_affirmative_reading(self):
        """A gate that read nothing must never report PASS."""
        with patch("sre_agent.k8s_client.get_apps_client", return_value=_apps(exc=_ApiError(500))):
            status, _ = health_gate.check_resources([{"kind": "Deployment", "name": "a", "namespace": "p"}])
        assert status == health_gate.UNVERIFIABLE
