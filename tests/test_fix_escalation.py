"""A fix that did not hold is escalated to a person once, in plain English.

Live evidence (trust 3, one week): 440 auto-fix actions, mostly the same fix
repeated on an ImagePullBackOff app with a bad image and a crash-looping app
with a code/config bug. After a fix does not hold Pulse stops and opens one
inbox item per workload+cause that says what was tried, why restarting cannot
fix it, who owns the workload (and where that came from), and the next step.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sre_agent.config import _reset_settings
from sre_agent.monitor import fix_breaker, fix_escalation, fix_planner
from sre_agent.monitor.fix_escalation import Workload
from tests.conftest import truncate_tables

NS = "shop"


def _cs(name="app", image="quay.io/acme/checkout:v9", waiting=None, message="", last_terminated=None):
    return SimpleNamespace(
        name=name,
        image=image,
        state=SimpleNamespace(
            waiting=SimpleNamespace(reason=waiting, message=message) if waiting else None,
            terminated=None,
        ),
        last_state=SimpleNamespace(terminated=last_terminated),
    )


def _terminated(exit_code=1, reason="Error", message=None):
    return SimpleNamespace(exit_code=exit_code, reason=reason, message=message)


def _pod(name="checkout-7d9c8b6f5-bx4zz", statuses=(), limits=None, owner=None):
    container = SimpleNamespace(
        name="app", image="quay.io/acme/checkout:v9", resources=SimpleNamespace(limits=limits or {})
    )
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name, namespace=NS, labels={}, annotations={}, owner_references=[owner] if owner else []
        ),
        spec=SimpleNamespace(containers=[container], init_containers=None, service_account_name="checkout"),
        status=SimpleNamespace(container_statuses=list(statuses), init_container_statuses=None),
    )


def _workload(*pods, labels=None, annotations=None):
    return Workload(
        kind="Deployment",
        name="checkout",
        namespace=NS,
        labels=labels or {},
        annotations=annotations or {},
        pods=list(pods),
        found=True,
    )


FINDING = {
    "id": "f-1",
    "category": "crashloop",
    "severity": "critical",
    "title": "Pod checkout-7d9c8b6f5-bx4zz restarting (6x)",
    "autoFixable": True,
    "resources": [{"kind": "Pod", "name": "checkout-7d9c8b6f5-bx4zz", "namespace": NS}],
}
KEY = "crashloop:shop:Pod/checkout"


def _core(events=(), log="", log_error=None, namespace=None):
    core = MagicMock()
    core.list_namespaced_event.return_value = SimpleNamespace(
        items=[SimpleNamespace(reason="Failed", message=m) for m in events]
    )
    if log_error is not None:
        core.read_namespaced_pod_log.side_effect = log_error
    else:
        core.read_namespaced_pod_log.return_value = log
    core.read_namespace.return_value = namespace or SimpleNamespace(metadata=SimpleNamespace(labels={}, annotations={}))
    return core


@pytest.fixture
def cluster(monkeypatch):
    """Point every read at one mock core client."""
    holder = {"core": _core()}
    monkeypatch.setattr(fix_escalation, "get_core_client", lambda: holder["core"])
    monkeypatch.setattr(fix_planner, "get_core_client", lambda: holder["core"])
    return holder


# ── why restarting can't fix it, by cause ──────────────────────────────────


def test_bad_image_names_the_image_and_the_registry_error(cluster):
    pod = _pod(statuses=[_cs(waiting="ImagePullBackOff", message='Back-off pulling image "quay.io/acme/checkout:v9"')])
    cluster["core"] = _core(events=['Failed to pull image "quay.io/acme/checkout:v9": manifest unknown'])
    cluster["core"].read_namespaced_pod.return_value = pod
    cause = fix_escalation.explain_cause(FINDING, _workload(pod))
    assert cause.kind == fix_escalation.CAUSE_IMAGE_PULL
    assert cause.evidence["image"] == "quay.io/acme/checkout:v9"
    assert "manifest unknown" in cause.why and "quay.io/acme/checkout:v9" in cause.why
    assert "Fix the image reference" in cause.next_step


def test_registry_auth_points_at_the_pull_secret(cluster):
    pod = _pod(statuses=[_cs(waiting="ErrImagePull", message="unauthorized: authentication required")])
    cluster["core"].read_namespaced_pod.return_value = pod
    cause = fix_escalation.explain_cause(FINDING, _workload(pod))
    assert cause.kind == fix_escalation.CAUSE_IMAGE_PULL
    assert cause.evidence["subtype"] == "registry_auth"
    assert "pull secret" in cause.next_step and "unauthorized" in cause.why


def test_crashloop_quotes_the_last_error_line_masked(cluster):
    log = (
        "starting checkout\n"
        "connecting to postgres://app:s3cr3tpw@db:5432/shop\n"
        "FATAL: password authentication failed for user app (password=hunter2xx)\n"
        "exiting\n"
    )
    cluster["core"] = _core(log=log)
    pod = _pod(statuses=[_cs(waiting="CrashLoopBackOff", last_terminated=_terminated(1))])
    cause = fix_escalation.explain_cause(FINDING, _workload(pod))
    assert cause.kind == fix_escalation.CAUSE_CRASHLOOP
    assert cause.evidence["container"] == "app" and cause.evidence["exit_code"] == 1
    assert "FATAL: password authentication failed" in cause.evidence["last_error"]
    assert "hunter2xx" not in cause.why and "[redacted]" in cause.why
    assert "exits with code 1" in cause.why
    kwargs = cluster["core"].read_namespaced_pod_log.call_args.kwargs
    assert kwargs["previous"] is True and kwargs["container"] == "app"


def test_crashloop_with_unreadable_logs_says_why_instead_of_guessing(cluster):
    err = Exception("forbidden")
    err.status = 403
    cluster["core"] = _core(log_error=err)
    pod = _pod(statuses=[_cs(last_terminated=_terminated(137, reason="Error"))])
    cause = fix_escalation.explain_cause(FINDING, _workload(pod))
    assert cause.kind == fix_escalation.CAUSE_CRASHLOOP
    assert cause.evidence["last_error"] is None
    assert "403" in cause.why


def test_missing_secret_is_named(cluster):
    pod = _pod(statuses=[_cs(waiting="CreateContainerConfigError", message='secret "db-creds" not found')])
    cause = fix_escalation.explain_cause(FINDING, _workload(pod))
    assert cause.kind == fix_escalation.CAUSE_CONFIG
    assert cause.evidence["missing"] == "Secret db-creds"
    assert "Create the Secret db-creds in namespace shop" in cause.next_step


def test_missing_configmap_key_is_named(cluster):
    msg = "couldn't find key LOG_LEVEL in ConfigMap shop/checkout-config"
    pod = _pod(statuses=[_cs(waiting="CreateContainerConfigError", message=msg)])
    cause = fix_escalation.explain_cause(FINDING, _workload(pod))
    assert cause.evidence["missing"] == "key LOG_LEVEL in ConfigMap checkout-config"


def test_oom_compares_the_limit_with_the_measured_peak(cluster):
    pod = _pod(statuses=[_cs(last_terminated=_terminated(137, reason="OOMKilled"))], limits={"memory": "256Mi"})
    peak = {"data": {"result": [{"value": [0, str(300 * 1024**2)]}]}}
    with patch("sre_agent.prometheus.prometheus_request", return_value=peak) as prom:
        cause = fix_escalation.explain_cause(FINDING, _workload(pod))
    assert cause.kind == fix_escalation.CAUSE_OOM
    assert cause.evidence["limit"] == "256Mi" and cause.evidence["peak"] == "300 MiB"
    assert "256Mi (256 MiB)" in cause.why and "300 MiB" in cause.why
    assert "patch_workload_container" in cause.next_step
    query = prom.call_args.args[1]["query"]
    assert 'namespace="shop"' in query and 'container="app"' in query and "checkout" in query


def test_oom_with_no_metrics_says_the_peak_is_unknown(cluster):
    pod = _pod(statuses=[_cs(last_terminated=_terminated(137, reason="OOMKilled"))], limits={"memory": "1Gi"})
    with patch("sre_agent.prometheus.prometheus_request", side_effect=OSError("down")):
        cause = fix_escalation.explain_cause(FINDING, _workload(pod))
    assert cause.evidence["peak"] is None
    assert "peak is unknown" in cause.why and "1Gi" in cause.why


def test_unreadable_workload_falls_back_without_guessing():
    w = Workload(
        kind="Pod", name="checkout-7d9c8b6f5-bx4zz", namespace=NS, read_error="could not be read (ApiException)"
    )
    cause = fix_escalation.explain_cause(FINDING, w)
    assert cause.kind == fix_escalation.CAUSE_UNKNOWN
    assert "could not read" in cause.why and "ApiException" in cause.why


def test_resolve_workload_follows_the_pod_to_its_deployment(monkeypatch):
    rs_ref = SimpleNamespace(kind="ReplicaSet", name="checkout-7d9c8b6f5", controller=True)
    dep_ref = SimpleNamespace(kind="Deployment", name="checkout", controller=True)
    pod = _pod(owner=rs_ref)
    core = _core()
    core.read_namespaced_pod.return_value = pod
    core.list_namespaced_pod.return_value = SimpleNamespace(items=[pod])
    apps = MagicMock()
    apps.read_namespaced_replica_set.return_value = SimpleNamespace(
        metadata=SimpleNamespace(owner_references=[dep_ref])
    )
    apps.read_namespaced_deployment.return_value = SimpleNamespace(
        metadata=SimpleNamespace(name="checkout", labels={"team": "payments"}, annotations={}),
        spec=SimpleNamespace(
            selector=SimpleNamespace(match_labels={"app": "checkout"}),
            template=SimpleNamespace(spec=SimpleNamespace(service_account_name="checkout")),
        ),
    )
    monkeypatch.setattr(fix_escalation, "get_core_client", lambda: core)
    monkeypatch.setattr(fix_escalation, "get_apps_client", lambda: apps)
    w = fix_escalation.resolve_workload(FINDING["resources"][0])
    assert (w.kind, w.name, w.found) == ("Deployment", "checkout", True)
    assert w.pods == [pod] and w.labels["team"] == "payments"
    assert core.list_namespaced_pod.call_args.kwargs["label_selector"] == "app=checkout"


def test_resolve_workload_finds_the_deployment_after_the_pod_was_deleted(monkeypatch):
    gone = Exception("gone")
    gone.status = 404
    core = _core()
    core.read_namespaced_pod.side_effect = gone
    core.list_namespaced_pod.return_value = SimpleNamespace(items=[])
    apps = MagicMock()
    apps.read_namespaced_deployment.return_value = SimpleNamespace(
        metadata=SimpleNamespace(name="checkout", labels={}, annotations={}),
        spec=SimpleNamespace(selector=SimpleNamespace(match_labels={"app": "checkout"}), template=None),
    )
    monkeypatch.setattr(fix_escalation, "get_core_client", lambda: core)
    monkeypatch.setattr(fix_escalation, "get_apps_client", lambda: apps)
    w = fix_escalation.resolve_workload(FINDING["resources"][0])
    assert (w.kind, w.name) == ("Deployment", "checkout")
    assert apps.read_namespaced_deployment.call_args.args[:2] == ("checkout", NS)


# ── who owns it, and where that came from ──────────────────────────────────


def test_owner_sources_are_named(cluster):
    cluster["core"] = _core(
        namespace=SimpleNamespace(metadata=SimpleNamespace(labels={}, annotations={"openshift.io/requester": "alice"}))
    )
    w = _workload(
        labels={"team": "payments", "app.kubernetes.io/managed-by": "kustomize"}, annotations={"owner": "bob"}
    )
    owners = fix_escalation.find_owners(w)
    got = {(p["value"], p["source"]) for p in owners["people"]}
    assert ("payments", "label team on Deployment shop/checkout") in got
    assert ("bob", "annotation owner on Deployment shop/checkout") in got
    assert ("alice", "annotation openshift.io/requester on namespace shop") in got
    assert owners["managed_by"] == [
        {"tool": "kustomize", "app": "", "source": "label app.kubernetes.io/managed-by on Deployment shop/checkout"}
    ]
    assert owners["unknown"] is False


def test_no_owner_metadata_is_unknown_never_guessed(cluster):
    w = _workload(labels={"app.kubernetes.io/name": "checkout", "app": "checkout"})
    owners = fix_escalation.find_owners(w)
    assert owners["unknown"] is True and owners["people"] == [] and owners["managed_by"] == []
    item = fix_escalation.build_item(KEY, FINDING, [], "test", w)
    assert "unknown owner" in item["summary"]
    assert "checkout" not in item["summary"].split("Likely owner:")[1].split("Next step")[0].split("—")[0]


def test_unreadable_namespace_is_reported_not_skipped(cluster):
    cluster["core"].read_namespace.side_effect = RuntimeError("forbidden")
    owners = fix_escalation.find_owners(_workload())
    assert owners["unknown"] is True
    assert owners["unreadable"] == ["namespace shop could not be read (RuntimeError)"]


def test_argo_cd_owner_brings_its_repo_and_a_git_next_step(cluster):
    source = json.dumps(
        {"repo_url": "https://github.com/acme/shop-deploy", "path": "checkout", "target_revision": "main"}
    )
    w = _workload(annotations={"argocd.argoproj.io/tracking-id": "shop-checkout:apps/Deployment:shop/checkout"})
    pod = _pod(statuses=[_cs(waiting="CreateContainerConfigError", message='secret "db-creds" not found')])
    w.pods = [pod]
    with patch("sre_agent.git_tools.get_argo_app_source") as tool:
        tool.func.return_value = source
        item = fix_escalation.build_item(KEY, FINDING, [], "test", w)
    managed = item["metadata"]["owners"]["managed_by"][0]
    assert managed["tool"] == "Argo CD" and managed["app"] == "shop-checkout"
    assert managed["repo_url"] == "https://github.com/acme/shop-deploy"
    assert "annotation argocd.argoproj.io/tracking-id on Deployment shop/checkout" in managed["source"]
    assert "make the change in git" in item["metadata"]["next_step"]
    assert "propose_git_change" in item["metadata"]["next_step"]
    tool.func.assert_called_with("shop-checkout", "openshift-gitops")


# ── one item, raised when a fix did not hold, resolved once it holds ──────


@pytest.fixture
def db():
    from sre_agent.db import get_database

    database = get_database()
    truncate_tables(database, "actions", "inbox_items", "inbox_mutes")
    _reset_settings()
    yield database
    truncate_tables(database, "actions", "inbox_items", "inbox_mutes")
    _reset_settings()


def _insert_attempt(db, i, verification="still_failing", tool="delete_pod", key=KEY, status="completed"):
    db.execute(
        "INSERT INTO actions (id, finding_id, timestamp, category, tool, status, verification_status, "
        "correlation_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (f"a-{i}", f"f-{i}", int((time.time() - 60 * (i + 1)) * 1000), "crashloop", tool, status, verification, key),
    )
    db.commit()


def _open_items(db, key=KEY):
    return db.fetchall(
        "SELECT * FROM inbox_items WHERE correlation_key = ? AND status NOT IN ('resolved', 'archived')",
        (fix_breaker.escalation_key(key),),
    )


def test_escalation_says_what_was_tried_and_is_one_item(db):
    _insert_attempt(db, 0)
    first = fix_escalation.escalate(KEY, FINDING, None, "delete_pod did not hold")
    _insert_attempt(db, 1, status="failed", verification="unverifiable")
    second = fix_escalation.escalate(KEY, FINDING, None, "3 fixes did not hold")
    rows = _open_items(db)
    assert first and first == second and len(rows) == 1
    summary = rows[0]["summary"]
    assert "Pulse tried delete_pod 2 times" in summary
    assert "Why restarting can't fix it" in summary and "Next step" in summary
    md = json.loads(rows[0]["metadata"])
    assert md["attempt_count"] == 2, "the open item is refreshed, not duplicated"
    assert md["reason"] == "3 fixes did not hold"


def test_a_different_cause_on_the_same_workload_is_a_different_item(db):
    fix_escalation.escalate(KEY, FINDING, [], "x")
    oom_key = "oom:shop:Pod/checkout"
    fix_escalation.escalate(oom_key, dict(FINDING, category="oom"), [], "x")
    assert len(_open_items(db)) == 1 and len(_open_items(db, oom_key)) == 1


def test_an_open_escalation_trips_the_breaker_after_one_attempt(db):
    _insert_attempt(db, 0)
    assert not fix_breaker.check(KEY).tripped
    fix_escalation.escalate(KEY, FINDING, None, "did not hold")
    state = fix_breaker.check(KEY)
    assert state.tripped and not state.should_escalate


def test_resolves_only_after_staying_gone_for_the_recurrence_window(db, monkeypatch):
    monkeypatch.setenv("PULSE_AGENT_RECURRENCE_WINDOW", "1800")
    _reset_settings()
    fix_escalation.escalate(KEY, FINDING, [], "did not hold")
    t0 = int(time.time())
    assert fix_escalation.resolve_held({KEY}, now=t0) == 0
    assert fix_escalation.resolve_held(set(), now=t0 + 60) == 0, "gone for one scan is not held"
    assert fix_escalation.resolve_held(set(), now=t0 + 60 + 1700) == 0
    # It came back inside the window: the clock restarts.
    assert fix_escalation.resolve_held({KEY}, now=t0 + 60 + 1750) == 0
    assert fix_escalation.resolve_held(set(), now=t0 + 60 + 1800) == 0
    assert len(_open_items(db)) == 1
    assert fix_escalation.resolve_held(set(), now=t0 + 60 + 1800 + 1800) == 1
    assert _open_items(db) == []
    row = db.fetchone("SELECT metadata FROM inbox_items WHERE correlation_key = ?", (fix_breaker.escalation_key(KEY),))
    assert "stayed gone for 30 min" in json.loads(row["metadata"])["resolved_reason"]


def test_resolution_reopens_auto_fix_and_a_new_failure_raises_a_new_item(db, monkeypatch):
    monkeypatch.setenv("PULSE_AGENT_RECURRENCE_WINDOW", "0")
    _reset_settings()
    _insert_attempt(db, 0)
    first = fix_escalation.escalate(KEY, FINDING, None, "did not hold")
    assert fix_escalation.resolve_held(set()) == 1
    assert not fix_breaker.check(KEY).tripped
    second = fix_escalation.escalate(KEY, FINDING, None, "did not hold again")
    assert second and second != first, "not the recently-resolved item's id: something must be open"
    assert fix_breaker.check(KEY).tripped


def test_other_open_items_are_not_resolved_by_resolve_held(db):
    from sre_agent.inbox import create_inbox_item

    create_inbox_item(
        {
            "item_type": "task",
            "title": "t",
            "summary": "s",
            "severity": "warning",
            "correlation_key": KEY,
            "created_by": "system:monitor",
        }
    )
    assert fix_escalation.resolve_held(set()) == 0


def test_verification_that_did_not_hold_escalates(db):
    from sre_agent.monitor import health_gate
    from sre_agent.monitor.verification_pipeline import process_verifications

    _insert_attempt(db, 0)
    monitor = SimpleNamespace(_scan_counter=2, _broadcast_raw=AsyncMock(), _pending_verifications={})
    monitor._pending_verifications["a-0"] = {
        "action_id": "a-0",
        "finding_id": "f-0",
        "category": "crashloop",
        "resources": FINDING["resources"],
        "verify_resources": FINDING["resources"],
        "target_scan": 2,
        "strategy": "restart_controller",
        "cause": "crashloop",
    }
    with (
        patch.object(health_gate, "check_resources", return_value=(health_gate.FAIL, "0/1 ready")),
        patch("sre_agent.monitor.verification_pipeline.learn_when_held"),
        patch("sre_agent.fix_learning.record_verdict"),
    ):
        asyncio.run(process_verifications(monitor, []))
    rows = _open_items(db)
    assert len(rows) == 1
    assert "restart_controller did not hold" in json.loads(rows[0]["metadata"])["reason"]


@pytest.mark.parametrize("verdict", ["verified", "unverifiable"])
def test_a_fix_that_held_or_could_not_be_judged_is_not_escalated(db, verdict):
    from sre_agent.monitor import health_gate
    from sre_agent.monitor.verification_pipeline import process_verifications

    gate = health_gate.PASS if verdict == "verified" else health_gate.UNVERIFIABLE
    monitor = SimpleNamespace(_scan_counter=2, _broadcast_raw=AsyncMock(), _pending_verifications={})
    monitor._pending_verifications["a-0"] = {
        "action_id": "a-0",
        "finding_id": "f-0",
        "category": "crashloop",
        "resources": FINDING["resources"],
        "target_scan": 2,
    }
    with (
        patch.object(health_gate, "check_resources", return_value=(gate, "ok")),
        patch("sre_agent.monitor.verification_pipeline.learn_when_held"),
    ):
        asyncio.run(process_verifications(monitor, []))
    assert _open_items(db) == []


def test_a_recurrence_inside_the_window_escalates(db):
    from sre_agent.monitor.recurrence import process_recurrences

    monitor = SimpleNamespace(_broadcast_raw=AsyncMock())
    row = {"id": "a-0", "tool": "delete_pod", "verification_timestamp": int(time.time() * 1000) - 600_000}
    with (
        patch("sre_agent.monitor.recurrence._find_recurred_actions", return_value=(KEY, [row])),
        patch("sre_agent.monitor.recurrence._downgrade_action", return_value="recurred"),
        patch("sre_agent.monitor.recurrence._feed_learning"),
    ):
        asyncio.run(process_recurrences(monitor, [FINDING]))
    rows = _open_items(db)
    assert len(rows) == 1
    assert "came back 10 min later" in json.loads(rows[0]["metadata"])["reason"]


@pytest.mark.asyncio
async def test_monitor_does_not_retry_while_the_escalation_is_open(db, monkeypatch):
    """One fix, a verdict that it did not hold, then many scans: no second fix."""
    from sre_agent.monitor import health_gate
    from sre_agent.monitor.cluster_monitor import ClusterMonitor

    monkeypatch.setenv("PULSE_AGENT_MAX_TRUST_LEVEL", "3")
    _reset_settings()
    clock = SimpleNamespace(now=time.time())
    monkeypatch.setattr(time, "time", lambda: clock.now)

    monitor = ClusterMonitor()
    monitor._subscribers = []
    monitor._broadcast_raw = AsyncMock()
    owner = MagicMock(kind="ReplicaSet")
    owner.name = "checkout-7d9c8b6f5"
    pod = MagicMock()
    pod.metadata.owner_references = [owner]
    executed = []
    with (
        patch("sre_agent.monitor.cluster_monitor.is_autofix_paused", return_value=False),
        patch("sre_agent.monitor.cluster_monitor.get_core_client") as core,
        patch("sre_agent.monitor.cluster_monitor._estimate_auto_fix_confidence", return_value=0.9),
        patch("sre_agent.monitor.cluster_monitor.notify_fix_proposed", new=AsyncMock()),
        patch("sre_agent.monitor.fix_planner.get_investigation_for_finding", return_value=None),
        patch("sre_agent.monitor.rbac_preflight.can_execute", return_value=(True, "")),
        patch(
            "sre_agent.monitor.fix_planner.execute_fix",
            side_effect=lambda plan: executed.append(plan) or ("delete_pod", "before", "after"),
        ),
        patch.object(health_gate, "check_resources", return_value=(health_gate.FAIL, "0/1 ready")),
        patch("sre_agent.monitor.verification_pipeline.learn_when_held"),
    ):
        core.return_value.read_namespaced_pod.return_value = pod
        for scan in range(12):
            monitor._scan_counter = scan
            finding = dict(FINDING, id=f"f-{scan}")
            finding["resources"] = [
                dict(FINDING["resources"][0], name=f"checkout-7d9c8b6f5-{'bcdfg'[scan % 5]}x{'245678'[scan % 6]}zz")
            ]
            # The scan's order: the last fix's verdict, then auto-fix.
            await monitor.process_verifications([finding])
            await monitor.auto_fix([finding])
            clock.now += 600
    assert len(executed) == 1
    assert len(_open_items(db)) == 1
