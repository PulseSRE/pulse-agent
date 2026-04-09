"""Comprehensive tests for all 11 scanner functions in monitor.py."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from sre_agent.db import Database, reset_database, set_database
from sre_agent.monitor import (
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    scan_crashlooping_pods,
    scan_daemonset_gaps,
    scan_degraded_operators,
    scan_expiring_certs,
    scan_failed_deployments,
    scan_firing_alerts,
    scan_hpa_saturation,
    scan_image_pull_errors,
    scan_node_pressure,
    scan_oom_killed_pods,
    scan_pending_pods,
)
from tests.conftest import _TEST_DB_URL

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _use_temp_db(monkeypatch):
    """Use a temp database for each test."""
    import sre_agent.context_bus as _cb
    import sre_agent.monitor as _mon

    db = Database(_TEST_DB_URL)
    set_database(db)
    _mon._tables_ensured = False
    _cb._tables_ensured = False
    for table in (
        "actions",
        "investigations",
        "findings",
        "context_entries",
        "incidents",
        "runbooks",
        "patterns",
        "metrics",
    ):
        try:
            db.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        except Exception:
            pass
    db.commit()
    _mon._tables_ensured = False
    _cb._tables_ensured = False
    _mon._ensure_tables()
    _cb._ensure_tables()
    yield
    reset_database()
    _mon._tables_ensured = False
    _cb._tables_ensured = False


def _list_result(items):
    return SimpleNamespace(items=items)


def _now():
    return datetime.now(UTC)


# ── Helpers to build mock K8s objects ─────────────────────────────────────────


def _make_pod(
    name="test-pod",
    namespace="default",
    restart_count=0,
    waiting_reason=None,
    oom_killed=False,
    image_pull_error=None,
    container_name="main",
    created_minutes_ago=30,
    container_statuses=None,
):
    """Build a mock pod for scanner tests."""
    # Build waiting state
    waiting = None
    if waiting_reason:
        waiting = SimpleNamespace(reason=waiting_reason, message=f"Error: {waiting_reason}")
    elif image_pull_error:
        waiting = SimpleNamespace(reason=image_pull_error, message=f"pull error: {image_pull_error}")

    # Build last_state for OOM
    last_terminated = None
    if oom_killed:
        last_terminated = SimpleNamespace(reason="OOMKilled", exit_code=137)
    last_state = SimpleNamespace(terminated=last_terminated)

    state = SimpleNamespace(
        waiting=waiting,
        running=None if waiting else SimpleNamespace(),
        terminated=None,
    )

    cs = SimpleNamespace(
        name=container_name,
        restart_count=restart_count,
        state=state,
        last_state=last_state,
        ready=waiting is None and not oom_killed,
    )

    created = _now() - timedelta(minutes=created_minutes_ago)

    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            namespace=namespace,
            labels={"app": name},
            creation_timestamp=created,
        ),
        status=SimpleNamespace(
            phase="Running",
            container_statuses=container_statuses if container_statuses is not None else [cs],
            conditions=[],
        ),
    )


def _make_node(name="node-1", conditions=None):
    """Build a mock node with custom conditions."""
    if conditions is None:
        conditions = [
            SimpleNamespace(type="Ready", status="True", message="kubelet is ready", reason="KubeletReady"),
        ]
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        status=SimpleNamespace(conditions=conditions),
    )


def _make_deployment(name="web", namespace="default", replicas=3, available=3):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, namespace=namespace),
        spec=SimpleNamespace(replicas=replicas),
        status=SimpleNamespace(available_replicas=available),
    )


def _make_daemonset(name="agent", namespace="default", desired=5, ready=5):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, namespace=namespace),
        status=SimpleNamespace(desired_number_scheduled=desired, number_ready=ready),
    )


def _make_hpa(name="web-hpa", namespace="default", max_replicas=10, current_replicas=5):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, namespace=namespace),
        spec=SimpleNamespace(max_replicas=max_replicas),
        status=SimpleNamespace(current_replicas=current_replicas),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 1. scan_crashlooping_pods
# ═══════════════════════════════════════════════════════════════════════════════


class TestScanCrashloopingPods:
    def test_detects_crashlooping_pod(self):
        pod = _make_pod(name="api-1", namespace="prod", restart_count=5, waiting_reason="CrashLoopBackOff")
        pods = _list_result([pod])
        findings = scan_crashlooping_pods(pods=pods)
        assert len(findings) == 1
        assert findings[0]["severity"] == SEVERITY_WARNING
        assert findings[0]["category"] == "crashloop"
        assert "api-1" in findings[0]["title"]
        assert "5" in findings[0]["title"]
        assert findings[0]["resources"] == [{"kind": "Pod", "name": "api-1", "namespace": "prod"}]
        assert findings[0]["autoFixable"] is True

    def test_critical_severity_at_10_restarts(self):
        pod = _make_pod(name="api-1", namespace="prod", restart_count=15)
        findings = scan_crashlooping_pods(pods=_list_result([pod]))
        assert len(findings) == 1
        assert findings[0]["severity"] == SEVERITY_CRITICAL

    def test_warning_severity_below_10_restarts(self):
        pod = _make_pod(name="api-1", namespace="prod", restart_count=5)
        findings = scan_crashlooping_pods(pods=_list_result([pod]))
        assert len(findings) == 1
        assert findings[0]["severity"] == SEVERITY_WARNING

    def test_no_finding_below_threshold(self):
        pod = _make_pod(name="healthy", namespace="default", restart_count=1)
        findings = scan_crashlooping_pods(pods=_list_result([pod]))
        assert len(findings) == 0

    def test_skips_system_namespaces(self):
        pods = _list_result(
            [
                _make_pod(name="p1", namespace="openshift-monitoring", restart_count=100),
                _make_pod(name="p2", namespace="kube-system", restart_count=100),
                _make_pod(name="p3", namespace="openshift", restart_count=100),
            ]
        )
        findings = scan_crashlooping_pods(pods=pods)
        assert len(findings) == 0

    def test_does_not_skip_default_namespace(self):
        pod = _make_pod(name="app", namespace="default", restart_count=5)
        findings = scan_crashlooping_pods(pods=_list_result([pod]))
        assert len(findings) == 1

    def test_empty_pod_list(self):
        findings = scan_crashlooping_pods(pods=_list_result([]))
        assert findings == []

    def test_none_container_statuses(self):
        pod = _make_pod(name="init", namespace="default", container_statuses=None)
        # container_statuses is None -> "or []" guard in the scanner
        pod.status.container_statuses = None
        findings = scan_crashlooping_pods(pods=_list_result([pod]))
        assert findings == []

    def test_api_error_returns_empty(self):
        with patch("sre_agent.monitor.get_core_client") as mock_core:
            mock_core.return_value.list_pod_for_all_namespaces.side_effect = Exception("API down")
            findings = scan_crashlooping_pods(pods=None)
            # The safe() call will produce a ToolError, scanner returns []
            assert findings == []

    def test_custom_threshold_via_env(self, monkeypatch):
        monkeypatch.setenv("PULSE_AGENT_CRASHLOOP_THRESHOLD", "10")
        from sre_agent.config import _reset_settings

        _reset_settings()
        pod = _make_pod(name="app", namespace="default", restart_count=7)
        findings = scan_crashlooping_pods(pods=_list_result([pod]))
        assert len(findings) == 0  # below threshold of 10

    def test_multiple_containers_in_pod(self):
        cs1 = SimpleNamespace(
            name="sidecar",
            restart_count=0,
            state=SimpleNamespace(waiting=None, running=SimpleNamespace(), terminated=None),
            last_state=SimpleNamespace(terminated=None),
        )
        cs2 = SimpleNamespace(
            name="main",
            restart_count=8,
            state=SimpleNamespace(waiting=SimpleNamespace(reason="CrashLoopBackOff"), running=None, terminated=None),
            last_state=SimpleNamespace(terminated=None),
        )
        pod = _make_pod(name="multi", namespace="default", container_statuses=[cs1, cs2])
        findings = scan_crashlooping_pods(pods=_list_result([pod]))
        assert len(findings) == 1
        assert "main" in findings[0]["summary"]


# ═══════════════════════════════════════════════════════════════════════════════
# 2. scan_pending_pods
# ═══════════════════════════════════════════════════════════════════════════════


class TestScanPendingPods:
    def test_detects_pending_pod(self):
        pod = _make_pod(name="stuck", namespace="prod", created_minutes_ago=15)
        pod.status.conditions = [
            SimpleNamespace(type="PodScheduled", status="False", reason="Unschedulable", message="No nodes available"),
        ]
        with patch("sre_agent.monitor.get_core_client") as mock_core:
            mock_core.return_value.list_pod_for_all_namespaces.return_value = _list_result([pod])
            findings = scan_pending_pods()
        assert len(findings) == 1
        assert findings[0]["category"] == "scheduling"
        assert "stuck" in findings[0]["title"]
        assert "15" in findings[0]["title"]
        assert "No nodes available" in findings[0]["summary"]

    def test_warning_under_30_minutes(self):
        pod = _make_pod(name="stuck", namespace="default", created_minutes_ago=10)
        with patch("sre_agent.monitor.get_core_client") as mock_core:
            mock_core.return_value.list_pod_for_all_namespaces.return_value = _list_result([pod])
            findings = scan_pending_pods()
        assert len(findings) == 1
        assert findings[0]["severity"] == SEVERITY_WARNING

    def test_critical_over_30_minutes(self):
        pod = _make_pod(name="stuck", namespace="default", created_minutes_ago=60)
        with patch("sre_agent.monitor.get_core_client") as mock_core:
            mock_core.return_value.list_pod_for_all_namespaces.return_value = _list_result([pod])
            findings = scan_pending_pods()
        assert len(findings) == 1
        assert findings[0]["severity"] == SEVERITY_CRITICAL

    def test_ignores_recently_created_pods(self):
        pod = _make_pod(name="new", namespace="default", created_minutes_ago=2)
        with patch("sre_agent.monitor.get_core_client") as mock_core:
            mock_core.return_value.list_pod_for_all_namespaces.return_value = _list_result([pod])
            findings = scan_pending_pods()
        assert len(findings) == 0

    def test_skips_system_namespaces(self):
        pod = _make_pod(name="sys", namespace="kube-system", created_minutes_ago=60)
        with patch("sre_agent.monitor.get_core_client") as mock_core:
            mock_core.return_value.list_pod_for_all_namespaces.return_value = _list_result([pod])
            findings = scan_pending_pods()
        assert len(findings) == 0

    def test_empty_list(self):
        with patch("sre_agent.monitor.get_core_client") as mock_core:
            mock_core.return_value.list_pod_for_all_namespaces.return_value = _list_result([])
            findings = scan_pending_pods()
        assert findings == []

    def test_api_error_returns_empty(self):
        with patch("sre_agent.monitor.get_core_client") as mock_core:
            mock_core.return_value.list_pod_for_all_namespaces.side_effect = Exception("timeout")
            findings = scan_pending_pods()
        assert findings == []

    def test_no_conditions_still_reports(self):
        """Pod pending > 5 min with no conditions still produces a finding (empty reason)."""
        pod = _make_pod(name="nocond", namespace="default", created_minutes_ago=10)
        pod.status.conditions = None
        with patch("sre_agent.monitor.get_core_client") as mock_core:
            mock_core.return_value.list_pod_for_all_namespaces.return_value = _list_result([pod])
            findings = scan_pending_pods()
        assert len(findings) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# 3. scan_failed_deployments
# ═══════════════════════════════════════════════════════════════════════════════


class TestScanFailedDeployments:
    def test_detects_degraded_deployment(self):
        dep = _make_deployment(name="api", namespace="prod", replicas=3, available=1)
        with patch("sre_agent.monitor.get_apps_client") as mock_apps:
            mock_apps.return_value.list_deployment_for_all_namespaces.return_value = _list_result([dep])
            findings = scan_failed_deployments()
        assert len(findings) == 1
        assert findings[0]["category"] == "workloads"
        assert findings[0]["severity"] == SEVERITY_WARNING  # available > 0
        assert "api" in findings[0]["title"]
        assert "1/3" in findings[0]["title"]
        assert findings[0]["autoFixable"] is True

    def test_critical_when_zero_available(self):
        dep = _make_deployment(name="api", namespace="prod", replicas=3, available=0)
        with patch("sre_agent.monitor.get_apps_client") as mock_apps:
            mock_apps.return_value.list_deployment_for_all_namespaces.return_value = _list_result([dep])
            findings = scan_failed_deployments()
        assert len(findings) == 1
        assert findings[0]["severity"] == SEVERITY_CRITICAL

    def test_healthy_deployment_no_findings(self):
        dep = _make_deployment(name="web", namespace="default", replicas=3, available=3)
        with patch("sre_agent.monitor.get_apps_client") as mock_apps:
            mock_apps.return_value.list_deployment_for_all_namespaces.return_value = _list_result([dep])
            findings = scan_failed_deployments()
        assert findings == []

    def test_skips_system_namespaces(self):
        dep = _make_deployment(name="api", namespace="openshift-operators", replicas=3, available=0)
        with patch("sre_agent.monitor.get_apps_client") as mock_apps:
            mock_apps.return_value.list_deployment_for_all_namespaces.return_value = _list_result([dep])
            findings = scan_failed_deployments()
        assert findings == []

    def test_zero_replicas_no_finding(self):
        dep = _make_deployment(name="scaled-down", namespace="default", replicas=0, available=0)
        with patch("sre_agent.monitor.get_apps_client") as mock_apps:
            mock_apps.return_value.list_deployment_for_all_namespaces.return_value = _list_result([dep])
            findings = scan_failed_deployments()
        assert findings == []

    def test_none_available_replicas(self):
        """available_replicas can be None when no pods are ready."""
        dep = _make_deployment(name="broken", namespace="default", replicas=2, available=None)
        dep.status.available_replicas = None
        with patch("sre_agent.monitor.get_apps_client") as mock_apps:
            mock_apps.return_value.list_deployment_for_all_namespaces.return_value = _list_result([dep])
            findings = scan_failed_deployments()
        assert len(findings) == 1
        assert findings[0]["severity"] == SEVERITY_CRITICAL

    def test_api_error_returns_empty(self):
        with patch("sre_agent.monitor.get_apps_client") as mock_apps:
            mock_apps.return_value.list_deployment_for_all_namespaces.side_effect = Exception("fail")
            findings = scan_failed_deployments()
        assert findings == []


# ═══════════════════════════════════════════════════════════════════════════════
# 4. scan_node_pressure
# ═══════════════════════════════════════════════════════════════════════════════


class TestScanNodePressure:
    def test_detects_disk_pressure(self):
        node = _make_node(
            "worker-1",
            conditions=[
                SimpleNamespace(type="DiskPressure", status="True", message="disk is full", reason="DiskPressure"),
                SimpleNamespace(type="Ready", status="True", message="ok", reason="KubeletReady"),
            ],
        )
        with patch("sre_agent.monitor.get_core_client") as mock_core:
            mock_core.return_value.list_node.return_value = _list_result([node])
            findings = scan_node_pressure()
        assert len(findings) == 1
        assert "DiskPressure" in findings[0]["title"]
        assert findings[0]["severity"] == SEVERITY_CRITICAL
        assert findings[0]["resources"] == [{"kind": "Node", "name": "worker-1"}]

    def test_detects_memory_pressure(self):
        node = _make_node(
            "worker-2",
            conditions=[
                SimpleNamespace(type="MemoryPressure", status="True", message="low memory", reason="MemoryPressure"),
                SimpleNamespace(type="Ready", status="True", message="ok", reason="KubeletReady"),
            ],
        )
        with patch("sre_agent.monitor.get_core_client") as mock_core:
            mock_core.return_value.list_node.return_value = _list_result([node])
            findings = scan_node_pressure()
        assert len(findings) == 1
        assert "MemoryPressure" in findings[0]["title"]

    def test_detects_pid_pressure(self):
        node = _make_node(
            "worker-3",
            conditions=[
                SimpleNamespace(type="PIDPressure", status="True", message="too many pids", reason="PIDPressure"),
                SimpleNamespace(type="Ready", status="True", message="ok", reason="KubeletReady"),
            ],
        )
        with patch("sre_agent.monitor.get_core_client") as mock_core:
            mock_core.return_value.list_node.return_value = _list_result([node])
            findings = scan_node_pressure()
        assert len(findings) == 1
        assert "PIDPressure" in findings[0]["title"]

    def test_detects_not_ready_node(self):
        node = _make_node(
            "worker-1",
            conditions=[
                SimpleNamespace(type="Ready", status="False", message="kubelet stopped", reason="KubeletNotReady"),
            ],
        )
        with patch("sre_agent.monitor.get_core_client") as mock_core:
            mock_core.return_value.list_node.return_value = _list_result([node])
            findings = scan_node_pressure()
        assert len(findings) == 1
        assert "NotReady" in findings[0]["title"]
        assert findings[0]["severity"] == SEVERITY_CRITICAL

    def test_multiple_conditions_on_one_node(self):
        node = _make_node(
            "sick-node",
            conditions=[
                SimpleNamespace(type="DiskPressure", status="True", message="disk", reason="dp"),
                SimpleNamespace(type="MemoryPressure", status="True", message="mem", reason="mp"),
                SimpleNamespace(type="Ready", status="False", message="down", reason="down"),
            ],
        )
        with patch("sre_agent.monitor.get_core_client") as mock_core:
            mock_core.return_value.list_node.return_value = _list_result([node])
            findings = scan_node_pressure()
        # DiskPressure + MemoryPressure + NotReady = 3 findings
        assert len(findings) == 3

    def test_healthy_node_no_findings(self):
        node = _make_node(
            "healthy",
            conditions=[
                SimpleNamespace(type="Ready", status="True", message="ok", reason="KubeletReady"),
                SimpleNamespace(type="DiskPressure", status="False", message="ok", reason="ok"),
                SimpleNamespace(type="MemoryPressure", status="False", message="ok", reason="ok"),
            ],
        )
        with patch("sre_agent.monitor.get_core_client") as mock_core:
            mock_core.return_value.list_node.return_value = _list_result([node])
            findings = scan_node_pressure()
        assert findings == []

    def test_empty_node_list(self):
        with patch("sre_agent.monitor.get_core_client") as mock_core:
            mock_core.return_value.list_node.return_value = _list_result([])
            findings = scan_node_pressure()
        assert findings == []

    def test_none_conditions(self):
        node = _make_node("noinfo")
        node.status.conditions = None
        with patch("sre_agent.monitor.get_core_client") as mock_core:
            mock_core.return_value.list_node.return_value = _list_result([node])
            findings = scan_node_pressure()
        assert findings == []

    def test_api_error_returns_empty(self):
        with patch("sre_agent.monitor.get_core_client") as mock_core:
            mock_core.return_value.list_node.side_effect = Exception("fail")
            findings = scan_node_pressure()
        assert findings == []


# ═══════════════════════════════════════════════════════════════════════════════
# 5. scan_expiring_certs
# ═══════════════════════════════════════════════════════════════════════════════


class TestScanExpiringCerts:
    def _make_tls_secret(self, name="my-tls", namespace="default", cert_data=None):
        return SimpleNamespace(
            metadata=SimpleNamespace(name=name, namespace=namespace),
            data={"tls.crt": cert_data} if cert_data else {},
        )

    def test_skips_system_namespaces(self):
        secret = self._make_tls_secret(namespace="openshift-ingress", cert_data="dGVzdA==")
        with patch("sre_agent.monitor.get_core_client") as mock_core:
            mock_core.return_value.list_secret_for_all_namespaces.return_value = _list_result([secret])
            findings = scan_expiring_certs()
        assert findings == []

    def test_skips_secret_without_cert_data(self):
        secret = self._make_tls_secret(namespace="default", cert_data=None)
        with patch("sre_agent.monitor.get_core_client") as mock_core:
            mock_core.return_value.list_secret_for_all_namespaces.return_value = _list_result([secret])
            findings = scan_expiring_certs()
        assert findings == []

    def test_empty_secrets_list(self):
        with patch("sre_agent.monitor.get_core_client") as mock_core:
            mock_core.return_value.list_secret_for_all_namespaces.return_value = _list_result([])
            findings = scan_expiring_certs()
        assert findings == []

    def test_api_error_returns_empty(self):
        with patch("sre_agent.monitor.get_core_client") as mock_core:
            mock_core.return_value.list_secret_for_all_namespaces.side_effect = Exception("fail")
            findings = scan_expiring_certs()
        assert findings == []

    def test_detects_expired_cert(self):
        """Uses a real self-signed cert that is already expired."""
        import base64

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        # Generate expired cert
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime(2020, 1, 1, tzinfo=UTC))
            .not_valid_after(datetime(2021, 1, 1, tzinfo=UTC))
            .sign(key, hashes.SHA256())
        )
        cert_pem = cert.public_bytes(serialization.Encoding.PEM)
        cert_b64 = base64.b64encode(cert_pem).decode()

        secret = self._make_tls_secret(name="expired-cert", namespace="default", cert_data=cert_b64)
        with patch("sre_agent.monitor.get_core_client") as mock_core:
            mock_core.return_value.list_secret_for_all_namespaces.return_value = _list_result([secret])
            findings = scan_expiring_certs()

        assert len(findings) == 1
        assert findings[0]["severity"] == SEVERITY_CRITICAL
        assert "EXPIRED" in findings[0]["title"]
        assert findings[0]["category"] == "cert_expiry"

    def test_detects_expiring_soon_cert(self):
        """Cert expiring in 10 days should be warning."""
        import base64

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_now() - timedelta(days=350))
            .not_valid_after(_now() + timedelta(days=10))
            .sign(key, hashes.SHA256())
        )
        cert_pem = cert.public_bytes(serialization.Encoding.PEM)
        cert_b64 = base64.b64encode(cert_pem).decode()

        secret = self._make_tls_secret(name="soon-cert", namespace="default", cert_data=cert_b64)
        with patch("sre_agent.monitor.get_core_client") as mock_core:
            mock_core.return_value.list_secret_for_all_namespaces.return_value = _list_result([secret])
            findings = scan_expiring_certs()

        assert len(findings) == 1
        assert findings[0]["severity"] == SEVERITY_WARNING
        assert "expiring" in findings[0]["title"]

    def test_valid_cert_no_finding(self):
        """Cert expiring in 365 days should not trigger."""
        import base64

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_now() - timedelta(days=10))
            .not_valid_after(_now() + timedelta(days=365))
            .sign(key, hashes.SHA256())
        )
        cert_pem = cert.public_bytes(serialization.Encoding.PEM)
        cert_b64 = base64.b64encode(cert_pem).decode()

        secret = self._make_tls_secret(name="valid-cert", namespace="default", cert_data=cert_b64)
        with patch("sre_agent.monitor.get_core_client") as mock_core:
            mock_core.return_value.list_secret_for_all_namespaces.return_value = _list_result([secret])
            findings = scan_expiring_certs()

        assert findings == []


# ═══════════════════════════════════════════════════════════════════════════════
# 6. scan_firing_alerts
# ═══════════════════════════════════════════════════════════════════════════════


class TestScanFiringAlerts:
    def _make_alert_response(self, alerts):
        """Build the Prometheus rules API response structure."""
        rules = []
        for a in alerts:
            rules.append(
                {
                    "name": a.get("alertname", "TestAlert"),
                    "state": "firing",
                    "alerts": [a],
                }
            )
        return {
            "status": "success",
            "data": {"groups": [{"rules": rules}]},
        }

    def test_detects_firing_critical_alert(self):
        alert = {
            "state": "firing",
            "labels": {
                "alertname": "KubePodCrashLooping",
                "severity": "critical",
                "namespace": "prod",
                "pod": "web-1",
            },
            "annotations": {"summary": "Pod is crash looping"},
        }
        response = MagicMock()
        response.data = json.dumps(self._make_alert_response([alert]))

        with patch("sre_agent.monitor.get_core_client") as mock_core:
            mock_core.return_value.connect_get_namespaced_service_proxy_with_path.return_value = response
            findings = scan_firing_alerts()

        assert len(findings) == 1
        assert findings[0]["severity"] == SEVERITY_CRITICAL
        assert findings[0]["category"] == "alerts"
        assert findings[0]["title"] == "KubePodCrashLooping"
        assert findings[0]["resources"] == [{"kind": "Pod", "name": "web-1", "namespace": "prod"}]

    def test_detects_warning_alert(self):
        alert = {
            "state": "firing",
            "labels": {"alertname": "HighLatency", "severity": "warning", "namespace": "prod"},
            "annotations": {"summary": "Latency is high"},
        }
        response = MagicMock()
        response.data = json.dumps(self._make_alert_response([alert]))
        with patch("sre_agent.monitor.get_core_client") as mock_core:
            mock_core.return_value.connect_get_namespaced_service_proxy_with_path.return_value = response
            findings = scan_firing_alerts()
        assert len(findings) == 1
        assert findings[0]["severity"] == SEVERITY_WARNING

    def test_skips_watchdog_and_info_inhibitor(self):
        alerts = [
            {
                "state": "firing",
                "labels": {"alertname": "Watchdog", "severity": "none", "namespace": ""},
                "annotations": {},
            },
            {
                "state": "firing",
                "labels": {"alertname": "InfoInhibitor", "severity": "info", "namespace": ""},
                "annotations": {},
            },
        ]
        response = MagicMock()
        response.data = json.dumps(self._make_alert_response(alerts))
        with patch("sre_agent.monitor.get_core_client") as mock_core:
            mock_core.return_value.connect_get_namespaced_service_proxy_with_path.return_value = response
            findings = scan_firing_alerts()
        assert findings == []

    def test_deployment_resource(self):
        alert = {
            "state": "firing",
            "labels": {"alertname": "DeployDown", "severity": "critical", "namespace": "ns", "deployment": "web"},
            "annotations": {"summary": "deploy down"},
        }
        response = MagicMock()
        response.data = json.dumps(self._make_alert_response([alert]))
        with patch("sre_agent.monitor.get_core_client") as mock_core:
            mock_core.return_value.connect_get_namespaced_service_proxy_with_path.return_value = response
            findings = scan_firing_alerts()
        assert findings[0]["resources"] == [{"kind": "Deployment", "name": "web", "namespace": "ns"}]

    def test_node_resource(self):
        alert = {
            "state": "firing",
            "labels": {"alertname": "NodeDown", "severity": "critical", "namespace": "", "node": "worker-1"},
            "annotations": {"summary": "node down"},
        }
        response = MagicMock()
        response.data = json.dumps(self._make_alert_response([alert]))
        with patch("sre_agent.monitor.get_core_client") as mock_core:
            mock_core.return_value.connect_get_namespaced_service_proxy_with_path.return_value = response
            findings = scan_firing_alerts()
        assert findings[0]["resources"] == [{"kind": "Node", "name": "worker-1"}]

    def test_non_success_status_returns_empty(self):
        response = MagicMock()
        response.data = json.dumps({"status": "error", "data": {}})
        with patch("sre_agent.monitor.get_core_client") as mock_core:
            mock_core.return_value.connect_get_namespaced_service_proxy_with_path.return_value = response
            findings = scan_firing_alerts()
        assert findings == []

    def test_api_error_returns_empty(self):
        with patch("sre_agent.monitor.get_core_client") as mock_core:
            mock_core.return_value.connect_get_namespaced_service_proxy_with_path.side_effect = Exception(
                "no monitoring"
            )
            findings = scan_firing_alerts()
        assert findings == []

    def test_info_severity_alert(self):
        alert = {
            "state": "firing",
            "labels": {"alertname": "InfoAlert", "severity": "info", "namespace": "ns"},
            "annotations": {"summary": "just info"},
        }
        response = MagicMock()
        response.data = json.dumps(self._make_alert_response([alert]))
        with patch("sre_agent.monitor.get_core_client") as mock_core:
            mock_core.return_value.connect_get_namespaced_service_proxy_with_path.return_value = response
            findings = scan_firing_alerts()
        assert len(findings) == 1
        assert findings[0]["severity"] == SEVERITY_INFO


# ═══════════════════════════════════════════════════════════════════════════════
# 7. scan_oom_killed_pods
# ═══════════════════════════════════════════════════════════════════════════════


class TestScanOOMKilledPods:
    def test_detects_oom_killed(self):
        pod = _make_pod(name="oom-pod", namespace="prod", oom_killed=True)
        findings = scan_oom_killed_pods(pods=_list_result([pod]))
        assert len(findings) == 1
        assert findings[0]["severity"] == SEVERITY_CRITICAL
        assert findings[0]["category"] == "oom"
        assert "OOMKilled" in findings[0]["title"]
        assert "137" in findings[0]["summary"]
        assert findings[0]["resources"] == [{"kind": "Pod", "name": "oom-pod", "namespace": "prod"}]

    def test_no_oom_no_finding(self):
        pod = _make_pod(name="healthy", namespace="default")
        findings = scan_oom_killed_pods(pods=_list_result([pod]))
        assert findings == []

    def test_skips_system_namespaces(self):
        pod = _make_pod(name="oom-sys", namespace="openshift-monitoring", oom_killed=True)
        findings = scan_oom_killed_pods(pods=_list_result([pod]))
        assert findings == []

    def test_empty_pod_list(self):
        findings = scan_oom_killed_pods(pods=_list_result([]))
        assert findings == []

    def test_none_container_statuses(self):
        pod = _make_pod(name="x", namespace="default")
        pod.status.container_statuses = None
        findings = scan_oom_killed_pods(pods=_list_result([pod]))
        assert findings == []

    def test_last_state_none(self):
        """last_state.terminated is None -> no finding."""
        pod = _make_pod(name="x", namespace="default")
        findings = scan_oom_killed_pods(pods=_list_result([pod]))
        assert findings == []

    def test_api_error_returns_empty(self):
        with patch("sre_agent.monitor.get_core_client") as mock_core:
            mock_core.return_value.list_pod_for_all_namespaces.side_effect = Exception("fail")
            findings = scan_oom_killed_pods(pods=None)
        assert findings == []


# ═══════════════════════════════════════════════════════════════════════════════
# 8. scan_image_pull_errors
# ═══════════════════════════════════════════════════════════════════════════════


class TestScanImagePullErrors:
    def test_detects_image_pull_backoff(self):
        pod = _make_pod(name="bad-image", namespace="prod", image_pull_error="ImagePullBackOff")
        findings = scan_image_pull_errors(pods=_list_result([pod]))
        assert len(findings) == 1
        assert findings[0]["severity"] == SEVERITY_WARNING
        assert findings[0]["category"] == "image_pull"
        assert "ImagePullBackOff" in findings[0]["title"]
        assert findings[0]["autoFixable"] is True

    def test_detects_err_image_pull(self):
        pod = _make_pod(name="bad-image-2", namespace="default", image_pull_error="ErrImagePull")
        findings = scan_image_pull_errors(pods=_list_result([pod]))
        assert len(findings) == 1
        assert "ErrImagePull" in findings[0]["title"]

    def test_no_image_error_no_finding(self):
        pod = _make_pod(name="healthy", namespace="default")
        findings = scan_image_pull_errors(pods=_list_result([pod]))
        assert findings == []

    def test_skips_system_namespaces(self):
        pod = _make_pod(name="sys", namespace="kube-public", image_pull_error="ImagePullBackOff")
        findings = scan_image_pull_errors(pods=_list_result([pod]))
        assert findings == []

    def test_empty_pod_list(self):
        findings = scan_image_pull_errors(pods=_list_result([]))
        assert findings == []

    def test_other_waiting_reason_ignored(self):
        pod = _make_pod(name="other", namespace="default", waiting_reason="ContainerCreating")
        findings = scan_image_pull_errors(pods=_list_result([pod]))
        assert findings == []

    def test_api_error_returns_empty(self):
        with patch("sre_agent.monitor.get_core_client") as mock_core:
            mock_core.return_value.list_pod_for_all_namespaces.side_effect = Exception("fail")
            findings = scan_image_pull_errors(pods=None)
        assert findings == []

    def test_none_state(self):
        """cs.state is None -> no crash."""
        pod = _make_pod(name="x", namespace="default")
        pod.status.container_statuses[0].state = None
        findings = scan_image_pull_errors(pods=_list_result([pod]))
        assert findings == []


# ═══════════════════════════════════════════════════════════════════════════════
# 9. scan_degraded_operators
# ═══════════════════════════════════════════════════════════════════════════════


class TestScanDegradedOperators:
    def test_detects_degraded_operator(self):
        result = {
            "items": [
                {
                    "metadata": {"name": "authentication"},
                    "status": {
                        "conditions": [
                            {
                                "type": "Degraded",
                                "status": "True",
                                "message": "auth backend down",
                                "reason": "AuthFailed",
                            },
                            {"type": "Available", "status": "True", "message": "ok"},
                        ],
                    },
                }
            ],
        }
        with patch("sre_agent.monitor.get_custom_client") as mock_custom:
            mock_custom.return_value.list_cluster_custom_object.return_value = result
            findings = scan_degraded_operators()
        assert len(findings) == 1
        assert findings[0]["severity"] == SEVERITY_CRITICAL
        assert findings[0]["category"] == "operators"
        assert "authentication" in findings[0]["title"]
        assert "auth backend down" in findings[0]["summary"]
        assert findings[0]["resources"] == [{"kind": "ClusterOperator", "name": "authentication"}]

    def test_healthy_operator_no_finding(self):
        result = {
            "items": [
                {
                    "metadata": {"name": "console"},
                    "status": {
                        "conditions": [
                            {"type": "Degraded", "status": "False", "message": "ok"},
                            {"type": "Available", "status": "True", "message": "ok"},
                        ],
                    },
                }
            ],
        }
        with patch("sre_agent.monitor.get_custom_client") as mock_custom:
            mock_custom.return_value.list_cluster_custom_object.return_value = result
            findings = scan_degraded_operators()
        assert findings == []

    def test_empty_items(self):
        with patch("sre_agent.monitor.get_custom_client") as mock_custom:
            mock_custom.return_value.list_cluster_custom_object.return_value = {"items": []}
            findings = scan_degraded_operators()
        assert findings == []

    def test_missing_status(self):
        result = {"items": [{"metadata": {"name": "x"}}]}
        with patch("sre_agent.monitor.get_custom_client") as mock_custom:
            mock_custom.return_value.list_cluster_custom_object.return_value = result
            findings = scan_degraded_operators()
        assert findings == []

    def test_missing_conditions(self):
        result = {"items": [{"metadata": {"name": "x"}, "status": {}}]}
        with patch("sre_agent.monitor.get_custom_client") as mock_custom:
            mock_custom.return_value.list_cluster_custom_object.return_value = result
            findings = scan_degraded_operators()
        assert findings == []

    def test_api_error_returns_empty(self):
        with patch("sre_agent.monitor.get_custom_client") as mock_custom:
            mock_custom.return_value.list_cluster_custom_object.side_effect = Exception("fail")
            findings = scan_degraded_operators()
        assert findings == []

    def test_multiple_degraded_operators(self):
        result = {
            "items": [
                {
                    "metadata": {"name": "auth"},
                    "status": {"conditions": [{"type": "Degraded", "status": "True", "message": "down"}]},
                },
                {
                    "metadata": {"name": "dns"},
                    "status": {"conditions": [{"type": "Degraded", "status": "True", "reason": "DNSFailed"}]},
                },
            ],
        }
        with patch("sre_agent.monitor.get_custom_client") as mock_custom:
            mock_custom.return_value.list_cluster_custom_object.return_value = result
            findings = scan_degraded_operators()
        assert len(findings) == 2


# ═══════════════════════════════════════════════════════════════════════════════
# 10. scan_daemonset_gaps
# ═══════════════════════════════════════════════════════════════════════════════


class TestScanDaemonsetGaps:
    def test_detects_gap(self):
        ds = _make_daemonset(name="fluentd", namespace="logging", desired=5, ready=3)
        with patch("sre_agent.monitor.get_apps_client") as mock_apps:
            mock_apps.return_value.list_daemon_set_for_all_namespaces.return_value = _list_result([ds])
            findings = scan_daemonset_gaps()
        assert len(findings) == 1
        assert findings[0]["category"] == "daemonsets"
        assert findings[0]["severity"] == SEVERITY_WARNING  # ready > 0
        assert "3/5" in findings[0]["title"]
        assert findings[0]["resources"] == [{"kind": "DaemonSet", "name": "fluentd", "namespace": "logging"}]

    def test_critical_when_zero_ready(self):
        ds = _make_daemonset(name="agent", namespace="infra", desired=3, ready=0)
        with patch("sre_agent.monitor.get_apps_client") as mock_apps:
            mock_apps.return_value.list_daemon_set_for_all_namespaces.return_value = _list_result([ds])
            findings = scan_daemonset_gaps()
        assert len(findings) == 1
        assert findings[0]["severity"] == SEVERITY_CRITICAL

    def test_healthy_daemonset_no_finding(self):
        ds = _make_daemonset(name="ok", namespace="default", desired=5, ready=5)
        with patch("sre_agent.monitor.get_apps_client") as mock_apps:
            mock_apps.return_value.list_daemon_set_for_all_namespaces.return_value = _list_result([ds])
            findings = scan_daemonset_gaps()
        assert findings == []

    def test_skips_system_namespaces(self):
        ds = _make_daemonset(name="sys", namespace="openshift-sdn", desired=5, ready=2)
        with patch("sre_agent.monitor.get_apps_client") as mock_apps:
            mock_apps.return_value.list_daemon_set_for_all_namespaces.return_value = _list_result([ds])
            findings = scan_daemonset_gaps()
        assert findings == []

    def test_zero_desired_no_finding(self):
        ds = _make_daemonset(name="empty", namespace="default", desired=0, ready=0)
        with patch("sre_agent.monitor.get_apps_client") as mock_apps:
            mock_apps.return_value.list_daemon_set_for_all_namespaces.return_value = _list_result([ds])
            findings = scan_daemonset_gaps()
        assert findings == []

    def test_none_ready(self):
        ds = _make_daemonset(name="x", namespace="default", desired=3, ready=None)
        ds.status.number_ready = None
        with patch("sre_agent.monitor.get_apps_client") as mock_apps:
            mock_apps.return_value.list_daemon_set_for_all_namespaces.return_value = _list_result([ds])
            findings = scan_daemonset_gaps()
        assert len(findings) == 1
        assert findings[0]["severity"] == SEVERITY_CRITICAL

    def test_empty_list(self):
        with patch("sre_agent.monitor.get_apps_client") as mock_apps:
            mock_apps.return_value.list_daemon_set_for_all_namespaces.return_value = _list_result([])
            findings = scan_daemonset_gaps()
        assert findings == []

    def test_api_error_returns_empty(self):
        with patch("sre_agent.monitor.get_apps_client") as mock_apps:
            mock_apps.return_value.list_daemon_set_for_all_namespaces.side_effect = Exception("fail")
            findings = scan_daemonset_gaps()
        assert findings == []


# ═══════════════════════════════════════════════════════════════════════════════
# 11. scan_hpa_saturation
# ═══════════════════════════════════════════════════════════════════════════════


class TestScanHpaSaturation:
    def test_detects_saturated_hpa(self):
        hpa = _make_hpa(name="web-hpa", namespace="prod", max_replicas=10, current_replicas=10)
        with patch("sre_agent.monitor.get_autoscaling_client") as mock_as:
            mock_as.return_value.list_horizontal_pod_autoscaler_for_all_namespaces.return_value = _list_result([hpa])
            findings = scan_hpa_saturation()
        assert len(findings) == 1
        assert findings[0]["severity"] == SEVERITY_WARNING
        assert findings[0]["category"] == "hpa"
        assert "10/10" in findings[0]["title"]
        assert findings[0]["resources"] == [{"kind": "HorizontalPodAutoscaler", "name": "web-hpa", "namespace": "prod"}]

    def test_hpa_over_max(self):
        """current > max should still trigger (edge case during scale-down lag)."""
        hpa = _make_hpa(name="over", namespace="default", max_replicas=5, current_replicas=7)
        with patch("sre_agent.monitor.get_autoscaling_client") as mock_as:
            mock_as.return_value.list_horizontal_pod_autoscaler_for_all_namespaces.return_value = _list_result([hpa])
            findings = scan_hpa_saturation()
        assert len(findings) == 1

    def test_healthy_hpa_no_finding(self):
        hpa = _make_hpa(name="ok", namespace="default", max_replicas=10, current_replicas=5)
        with patch("sre_agent.monitor.get_autoscaling_client") as mock_as:
            mock_as.return_value.list_horizontal_pod_autoscaler_for_all_namespaces.return_value = _list_result([hpa])
            findings = scan_hpa_saturation()
        assert findings == []

    def test_skips_system_namespaces(self):
        hpa = _make_hpa(name="sys", namespace="openshift-ingress", max_replicas=5, current_replicas=5)
        with patch("sre_agent.monitor.get_autoscaling_client") as mock_as:
            mock_as.return_value.list_horizontal_pod_autoscaler_for_all_namespaces.return_value = _list_result([hpa])
            findings = scan_hpa_saturation()
        assert findings == []

    def test_zero_max_replicas_no_finding(self):
        hpa = _make_hpa(name="zero", namespace="default", max_replicas=0, current_replicas=0)
        with patch("sre_agent.monitor.get_autoscaling_client") as mock_as:
            mock_as.return_value.list_horizontal_pod_autoscaler_for_all_namespaces.return_value = _list_result([hpa])
            findings = scan_hpa_saturation()
        assert findings == []

    def test_none_current_replicas(self):
        hpa = _make_hpa(name="x", namespace="default", max_replicas=10, current_replicas=None)
        hpa.status.current_replicas = None
        with patch("sre_agent.monitor.get_autoscaling_client") as mock_as:
            mock_as.return_value.list_horizontal_pod_autoscaler_for_all_namespaces.return_value = _list_result([hpa])
            findings = scan_hpa_saturation()
        assert findings == []

    def test_empty_list(self):
        with patch("sre_agent.monitor.get_autoscaling_client") as mock_as:
            mock_as.return_value.list_horizontal_pod_autoscaler_for_all_namespaces.return_value = _list_result([])
            findings = scan_hpa_saturation()
        assert findings == []

    def test_api_error_returns_empty(self):
        with patch("sre_agent.monitor.get_autoscaling_client") as mock_as:
            mock_as.return_value.list_horizontal_pod_autoscaler_for_all_namespaces.side_effect = Exception("fail")
            findings = scan_hpa_saturation()
        assert findings == []
