"""Tests for the liveness scanners — stuck deletions and hot reconcile loops.

These deliberately use *relative* timestamps. An earlier round of burst tests
pinned absolute dates, and they passed until a threshold moved and every
fixture drifted outside the window, at which point the tests kept passing while
asserting nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from sre_agent.errors import ToolError
from sre_agent.monitor.registry import SEVERITY_CRITICAL, SEVERITY_WARNING
from sre_agent.monitor.stuck_scanners import (
    _scan_client_error_loops,
    _scan_controller_retries,
    _scan_write_amplification,
    scan_hot_reconcile_loops,
    scan_stuck_deletions,
)

MODULE = "sre_agent.monitor.stuck_scanners"


def _ago(**kwargs) -> datetime:
    return datetime.now(UTC) - timedelta(**kwargs)


def _list(items):
    return SimpleNamespace(items=items)


def _namespace(name="demo", deleting_since=None, conditions=(), finalizers=()):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, deletion_timestamp=deleting_since),
        spec=SimpleNamespace(finalizers=list(finalizers)),
        status=SimpleNamespace(conditions=list(conditions)),
    )


def _condition(type_, message, status="True"):
    return SimpleNamespace(type=type_, status=status, message=message, reason="")


def _pod(name="web-0", namespace="demo", deleting_since=None, finalizers=(), grace=30):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name, namespace=namespace, deletion_timestamp=deleting_since, finalizers=list(finalizers)
        ),
        spec=SimpleNamespace(termination_grace_period_seconds=grace),
    )


def _pvc(name="data-0", namespace="demo", deleting_since=None, finalizers=()):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name, namespace=namespace, deletion_timestamp=deleting_since, finalizers=list(finalizers)
        )
    )


def _crd_payload(name, deleting_since=None, finalizers=()):
    meta: dict = {"name": name, "finalizers": list(finalizers)}
    if deleting_since is not None:
        meta["deletionTimestamp"] = deleting_since.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"items": [{"metadata": meta}]}


@pytest.fixture
def k8s():
    """Patch both K8s clients with empty lists; tests fill in what they need."""
    core = MagicMock()
    core.list_namespace.return_value = _list([])
    core.list_pod_for_all_namespaces.return_value = _list([])
    core.list_persistent_volume_claim_for_all_namespaces.return_value = _list([])
    custom = MagicMock()
    custom.list_cluster_custom_object.return_value = {"items": []}
    with (
        patch(f"{MODULE}.get_core_client", return_value=core),
        patch(f"{MODULE}.get_custom_client", return_value=custom),
    ):
        yield core, custom


# ── Stuck deletions ───────────────────────────────────────────────────────


def test_recently_deleted_namespace_is_not_a_finding(k8s):
    core, _ = k8s
    core.list_namespace.return_value = _list([_namespace(deleting_since=_ago(minutes=2))])
    assert scan_stuck_deletions() == []


def test_namespace_terminating_past_threshold_is_reported(k8s):
    core, _ = k8s
    core.list_namespace.return_value = _list([_namespace(name="agentit", deleting_since=_ago(hours=1))])
    findings = scan_stuck_deletions()
    assert len(findings) == 1
    assert findings[0]["severity"] == SEVERITY_WARNING
    assert "agentit" in findings[0]["title"]
    assert findings[0]["resources"] == [{"kind": "Namespace", "name": "agentit"}]


def test_long_stuck_namespace_escalates_to_critical(k8s):
    core, _ = k8s
    core.list_namespace.return_value = _list([_namespace(deleting_since=_ago(days=5))])
    finding = scan_stuck_deletions()[0]
    assert finding["severity"] == SEVERITY_CRITICAL
    assert "5d" in finding["title"]


def test_namespace_finding_names_what_is_blocking_it(k8s):
    """The API server already computed the blockers; the finding must carry them.

    Without this the operator gets 'stuck for 5d' and has to go diagnose it by
    hand — which is the same dead end that made the original problem invisible.
    """
    core, _ = k8s
    core.list_namespace.return_value = _list(
        [
            _namespace(
                deleting_since=_ago(hours=2),
                conditions=[
                    _condition(
                        "NamespaceContentRemaining", "Some resources are remaining: dnsrecords.kuadrant.io has 3"
                    ),
                    _condition(
                        "NamespaceFinalizersRemaining",
                        "Some content in the namespace has finalizers remaining: kuadrant.io/dnsrecord in 3",
                    ),
                ],
                finalizers=["kubernetes"],
            )
        ]
    )
    summary = scan_stuck_deletions()[0]["summary"]
    assert "dnsrecords.kuadrant.io" in summary
    assert "kuadrant.io/dnsrecord" in summary
    assert "spec.finalizers: kubernetes" in summary


def test_namespace_with_no_conditions_still_reports(k8s):
    core, _ = k8s
    core.list_namespace.return_value = _list([_namespace(deleting_since=_ago(hours=1))])
    assert "no blocking condition" in scan_stuck_deletions()[0]["summary"]


def test_stuck_pod_reports_its_finalizers(k8s):
    core, _ = k8s
    core.list_pod_for_all_namespaces.return_value = _list(
        [_pod(deleting_since=_ago(minutes=45), finalizers=["kuadrant.io/ratelimit"])]
    )
    finding = scan_stuck_deletions()[0]
    assert "kuadrant.io/ratelimit" in finding["summary"]
    assert finding["resources"][0]["kind"] == "Pod"


def test_stuck_pod_without_finalizers_points_at_the_kubelet(k8s):
    core, _ = k8s
    core.list_pod_for_all_namespaces.return_value = _list([_pod(deleting_since=_ago(minutes=45))])
    assert "kubelet" in scan_stuck_deletions()[0]["summary"]


def test_pods_come_from_the_shared_list_when_offered(k8s):
    """The scan runs alongside others that already listed every pod."""
    core, _ = k8s
    shared = _list([_pod(name="from-shared", deleting_since=_ago(hours=1))])
    findings = scan_stuck_deletions(pods=shared)
    assert "from-shared" in findings[0]["title"]
    core.list_pod_for_all_namespaces.assert_not_called()


def test_stuck_pvc_is_reported(k8s):
    core, _ = k8s
    core.list_persistent_volume_claim_for_all_namespaces.return_value = _list(
        [_pvc(deleting_since=_ago(hours=3), finalizers=["kubernetes.io/pvc-protection"])]
    )
    finding = scan_stuck_deletions()[0]
    assert finding["resources"][0]["kind"] == "PersistentVolumeClaim"
    assert "pvc-protection" in finding["summary"]


def test_stuck_crd_is_reported(k8s):
    """The Kuadrant case, caught directly rather than through its symptoms."""
    _, custom = k8s
    custom.list_cluster_custom_object.return_value = _crd_payload(
        "dnsrecords.kuadrant.io",
        deleting_since=_ago(days=120),
        finalizers=["customresourcecleanup.apiextensions.k8s.io"],
    )
    finding = scan_stuck_deletions()[0]
    assert finding["severity"] == SEVERITY_CRITICAL
    assert "120d" in finding["title"]
    assert finding["resources"][0]["kind"] == "CustomResourceDefinition"


def test_healthy_crd_is_ignored(k8s):
    _, custom = k8s
    custom.list_cluster_custom_object.return_value = _crd_payload("dnsrecords.kuadrant.io")
    assert scan_stuck_deletions() == []


def test_unparseable_crd_timestamp_is_skipped_not_crashed(k8s):
    _, custom = k8s
    custom.list_cluster_custom_object.return_value = {
        "items": [{"metadata": {"name": "x", "deletionTimestamp": "soon"}}]
    }
    assert scan_stuck_deletions() == []


def test_api_error_on_one_sub_scan_does_not_lose_the_others(k8s):
    """A CRD listing denied by RBAC must not cost us the namespace findings."""
    core, custom = k8s
    core.list_namespace.return_value = _list([_namespace(deleting_since=_ago(hours=1))])
    custom.list_cluster_custom_object.side_effect = RuntimeError("forbidden")
    findings = scan_stuck_deletions()
    assert len(findings) == 1
    assert findings[0]["resources"][0]["kind"] == "Namespace"


def test_tool_error_from_a_list_call_yields_no_findings(k8s):
    core, _ = k8s
    core.list_namespace.return_value = ToolError("forbidden", "forbidden")
    assert scan_stuck_deletions() == []


def test_system_namespaces_are_not_skipped(k8s):
    """Unlike the health scanners: a wedged openshift-* namespace is real."""
    core, _ = k8s
    core.list_namespace.return_value = _list([_namespace(name="openshift-kuadrant", deleting_since=_ago(hours=4))])
    assert len(scan_stuck_deletions()) == 1


# ── Hot reconcile loops ───────────────────────────────────────────────────


def _promql_result(metric: dict, value: float):
    return {"metric": metric, "value": [0, str(value)]}


def test_controller_retry_loop_is_reported():
    results = [_promql_result({"name": "dnsrecord", "namespace": "kuadrant-system"}, 42.0)]
    with patch(f"{MODULE}._query", return_value=results):
        findings = _scan_controller_retries()
    assert len(findings) == 1
    assert findings[0]["severity"] == SEVERITY_WARNING
    assert "dnsrecord" in findings[0]["title"]
    # Not "Pod": a work-queue name is not an object anyone can go look at.
    assert findings[0]["resources"] == [{"kind": "Controller", "name": "dnsrecord", "namespace": "kuadrant-system"}]


def test_extreme_retry_rate_is_critical():
    results = [_promql_result({"name": "dnsrecord", "namespace": "kuadrant-system"}, 400.0)]
    with patch(f"{MODULE}._query", return_value=results):
        assert _scan_controller_retries()[0]["severity"] == SEVERITY_CRITICAL


def test_retry_threshold_is_expressed_in_the_query():
    """Filtering happens in PromQL, so the threshold must reach Prometheus."""
    seen = []
    with patch(f"{MODULE}._query", side_effect=lambda q: seen.append(q) or []):
        _scan_controller_retries()
    assert "workqueue_retries_total[1h]" in seen[0]
    assert "> 20.0" in seen[0]


def test_write_amplification_query_excludes_structurally_noisy_resources():
    """Leases renew at ~20/s on a healthy cluster and would fire every scan."""
    seen = []
    with patch(f"{MODULE}._query", side_effect=lambda q: seen.append(q) or []):
        _scan_write_amplification()
    assert "leases" in seen[0]
    assert "resource!~" in seen[0]
    assert "subjectaccessreviews" in seen[0]


def test_write_amplification_is_reported_with_its_api_group():
    results = [_promql_result({"resource": "dnsrecords", "verb": "PATCH", "group": "kuadrant.io"}, 60.0)]
    with patch(f"{MODULE}._query", return_value=results):
        finding = _scan_write_amplification()[0]
    assert finding["severity"] == SEVERITY_CRITICAL
    assert "dnsrecords.kuadrant.io" in finding["title"]


def test_write_amplification_handles_a_core_resource_without_a_group():
    results = [_promql_result({"resource": "pods", "verb": "DELETE", "group": ""}, 8.0)]
    with patch(f"{MODULE}._query", return_value=results):
        assert "DELETE pods" in _scan_write_amplification()[0]["title"]


def test_client_error_loop_on_4xx_suggests_rbac():
    results = [_promql_result({"pod": "kuadrant-operator-0", "namespace": "kuadrant-system", "code": "403"}, 30.0)]
    with patch(f"{MODULE}._query", return_value=results):
        finding = _scan_client_error_loops()[0]
    assert "RBAC" in finding["summary"]
    assert finding["resources"][0]["name"] == "kuadrant-operator-0"


def test_client_error_loop_on_5xx_suggests_the_api_server():
    results = [_promql_result({"pod": "op-0", "namespace": "ns", "code": "503"}, 30.0)]
    with patch(f"{MODULE}._query", return_value=results):
        assert "API server health" in _scan_client_error_loops()[0]["summary"]


def test_unparseable_sample_is_skipped():
    with patch(f"{MODULE}._query", return_value=[{"metric": {"name": "c"}, "value": [0, "NaN-ish"]}]):
        assert _scan_controller_retries() == []


def test_prometheus_failure_yields_no_findings_not_an_exception():
    with patch(f"{MODULE}._query", side_effect=RuntimeError("prometheus down")):
        assert scan_hot_reconcile_loops() == []


def test_all_three_hot_loop_checks_run():
    calls = []
    with patch(f"{MODULE}._query", side_effect=lambda q: calls.append(q) or []):
        scan_hot_reconcile_loops()
    assert len(calls) == 3


def test_hot_loop_findings_are_never_auto_fixable():
    """Force-removing a finalizer orphans cloud resources. A human decides."""
    results = [_promql_result({"name": "c", "namespace": "ns"}, 50.0)]
    with patch(f"{MODULE}._query", return_value=results):
        assert all(not f["autoFixable"] for f in _scan_controller_retries())


def test_stuck_findings_are_never_auto_fixable(k8s):
    core, _ = k8s
    core.list_namespace.return_value = _list([_namespace(deleting_since=_ago(days=5))])
    assert all(not f["autoFixable"] for f in scan_stuck_deletions())


# ── Registration ──────────────────────────────────────────────────────────


def test_both_scanners_are_reachable_from_every_dispatch_path():
    """Registering in one path and not the other is how a scanner never runs."""
    from sre_agent.monitor.scanners import _get_all_scanners, get_all_scanner_instances

    by_function = {name for name, _ in _get_all_scanners()}
    by_instance = {s.meta.name for s in get_all_scanner_instances()}
    assert {"stuck", "hot_loop"} <= by_function
    assert {"stuck", "hot_loop"} <= by_instance


def test_registry_entries_exist_for_both():
    from sre_agent.monitor.registry import SCANNER_REGISTRY

    for name in ("stuck", "hot_loop"):
        assert SCANNER_REGISTRY[name]["category"] == "liveness"
        assert SCANNER_REGISTRY[name]["checks"]
