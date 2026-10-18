"""Storage, routes and DNS, admission webhooks and maintenance blockers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sre_agent.monitor import blind_spot_scanners as bs

MOD = "sre_agent.monitor.blind_spot_scanners"


def _ago(minutes):
    return datetime.now(UTC) - timedelta(minutes=minutes)


def _vec(rows):
    return [{"metric": m, "value": [0, str(v)]} for m, v in rows]


def _prom(by_query):
    def query(q):
        for needle, rows in by_query.items():
            if needle in q:
                return _vec(rows)
        return []

    return patch(f"{MOD}._query_prometheus", side_effect=query)


# ── storage ──────────────────────────────────────────────────────────────────


def _pvc(name, phase, minutes):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, namespace="shop", creation_timestamp=_ago(minutes)),
        spec=SimpleNamespace(storage_class_name="gp3"),
        status=SimpleNamespace(phase=phase),
    )


def _pending_pod(name, minutes):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, namespace="shop", creation_timestamp=_ago(minutes)),
        status=SimpleNamespace(phase="Pending"),
    )


def _event(reason, pod, message):
    return SimpleNamespace(
        reason=reason, message=message, involved_object=SimpleNamespace(kind="Pod", namespace="shop", name=pod)
    )


def _storage(pvcs=(), pods=(), events=(), usage=()):
    core = MagicMock()
    core.list_persistent_volume_claim_for_all_namespaces.return_value = SimpleNamespace(items=list(pvcs))
    core.list_event_for_all_namespaces.return_value = SimpleNamespace(items=list(events))
    with patch(f"{MOD}.get_core_client", return_value=core), _prom({"kubelet_volume_stats": usage}):
        return bs.scan_storage(SimpleNamespace(items=list(pods)))


def test_a_pvc_that_never_binds_is_found_after_five_minutes():
    findings = _storage(pvcs=[_pvc("data", "Pending", 12), _pvc("fresh", "Pending", 1), _pvc("ok", "Bound", 60)])
    assert [f["title"] for f in findings] == ["PersistentVolumeClaim data is not binding"]


def test_a_pod_that_cannot_mount_says_why():
    findings = _storage(
        pods=[_pending_pod("db-0", 10)],
        events=[_event("FailedAttachVolume", "db-0", "Multi-Attach error for volume pvc-1")],
    )
    assert findings[0]["severity"] == "critical"
    assert "Multi-Attach error" in findings[0]["summary"]


def test_a_nearly_full_volume_escalates_as_it_fills():
    findings = _storage(
        usage=[
            ({"namespace": "shop", "persistentvolumeclaim": "logs"}, 0.92),
            ({"namespace": "shop", "persistentvolumeclaim": "data"}, 0.99),
            ({"namespace": "shop", "persistentvolumeclaim": "cache"}, 0.40),
        ]
    )
    assert {f["title"]: f["severity"] for f in findings} == {
        "Volume logs is nearly full": "warning",
        "Volume data is nearly full": "critical",
    }


# ── ingress and DNS ──────────────────────────────────────────────────────────


def test_a_busy_route_answering_errors_is_found_and_a_quiet_one_is_not():
    busy = {"exported_namespace": "shop", "route": "checkout"}
    quiet = {"exported_namespace": "shop", "route": "admin"}
    with _prom({'code="5xx"': [(busy, 0.3), (quiet, 1.0)], "coredns": [], "haproxy": [(busy, 40), (quiet, 0.01)]}):
        findings = bs.scan_ingress()
    assert [f["title"] for f in findings] == ["Route checkout is returning errors"]
    assert findings[0]["severity"] == "critical"


def test_dns_failing_lookups_is_found():
    with _prom({"coredns": [({}, 0.03)]}):
        findings = bs.scan_ingress()
    assert [f["title"] for f in findings] == ["Cluster DNS is failing lookups"]


# ── webhooks ─────────────────────────────────────────────────────────────────


def test_an_unreachable_webhook_is_critical_and_a_slow_one_a_warning():
    with _prom(
        {
            "rejection_count": [({"name": "policy.kyverno.svc"}, 2.0)],
            "histogram_quantile": [({"name": "mutate.istio.io"}, 9.0), ({"name": "fast.example"}, 0.2)],
        }
    ):
        findings = bs.scan_webhooks()
    assert {f["title"]: f["severity"] for f in findings} == {
        "Admission webhook policy.kyverno.svc is failing": "critical",
        "Admission webhook mutate.istio.io is slow": "warning",
    }


# ── maintenance ──────────────────────────────────────────────────────────────


def _pdb(name, allowed, healthy, desired, expected=3):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, namespace="shop"),
        status=SimpleNamespace(
            disruptions_allowed=allowed, current_healthy=healthy, desired_healthy=desired, expected_pods=expected
        ),
    )


def _maintenance(pdbs=(), pools=None):
    policy, custom = MagicMock(), MagicMock()
    policy.list_pod_disruption_budget_for_all_namespaces.return_value = SimpleNamespace(items=list(pdbs))
    custom.list_cluster_custom_object.return_value = {"items": pools or []}
    with patch(f"{MOD}.get_policy_client", return_value=policy), patch(f"{MOD}.get_custom_client", return_value=custom):
        return bs.scan_maintenance_blockers()


def test_a_pdb_that_can_never_allow_a_drain_is_found():
    findings = _maintenance(
        pdbs=[
            _pdb("api", allowed=0, healthy=3, desired=3),  # minAvailable == replicas
            _pdb("web", allowed=1, healthy=3, desired=2),  # fine
            _pdb("db", allowed=0, healthy=1, desired=2),  # protecting a degraded workload: its job
        ]
    )
    assert [f["title"] for f in findings] == ["PodDisruptionBudget api blocks node drains"]


def test_machine_config_pools_degraded_or_stuck():
    two_hours = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    ten_min = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    pools = [
        {
            "metadata": {"name": "worker"},
            "status": {"conditions": [{"type": "Degraded", "status": "True", "message": "node w3 failed"}]},
        },
        {
            "metadata": {"name": "infra"},
            "status": {
                "machineCount": 3,
                "updatedMachineCount": 1,
                "conditions": [{"type": "Updating", "status": "True", "lastTransitionTime": two_hours}],
            },
        },
        {
            "metadata": {"name": "master"},
            "status": {"conditions": [{"type": "Updating", "status": "True", "lastTransitionTime": ten_min}]},
        },
    ]
    findings = _maintenance(pools=pools)
    assert {f["title"]: f["severity"] for f in findings} == {
        "MachineConfigPool worker is degraded": "critical",
        "MachineConfigPool infra has been updating for over an hour": "warning",
    }


def test_all_four_are_registered():
    from sre_agent.monitor.scanners import get_all_scanner_instances

    names = {s.meta.name for s in get_all_scanner_instances()}
    assert {"storage", "ingress", "webhooks", "maintenance"} <= names
