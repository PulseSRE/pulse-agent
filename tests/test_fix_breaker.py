"""Auto-fix must not repeat a fix that is not working (per-target breaker).

Live evidence (v2.38.1, trust 3, one week): 440 actions, mostly the same three
resources fixed every few minutes — pods of a bad-image Deployment deleted 143
times and recorded "completed", a degraded Deployment "restarted" 140 times
with every attempt failing, and a crashlooping pod deleted over and over.

These tests pin each root cause and replay the loop end to end against the
disposable test database.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sre_agent.config import _reset_settings
from sre_agent.monitor import fix_breaker, fix_planner
from sre_agent.monitor.fix_planner import FixPlan, classify_pull_error, default_fix_plan
from tests.conftest import truncate_tables

HUMAN = "require_human_review"


# ── image_pull: only a transient pull failure is worth retrying ────────────


@pytest.mark.parametrize(
    ("text", "verdict"),
    [
        ('Failed to pull image "quay.io/x/app:v9": manifest unknown', "permanent"),
        ("rpc error: code = NotFound desc = failed to pull and unpack image: not found", "permanent"),
        ("unauthorized: authentication required", "permanent"),
        ("pull access denied for app, repository does not exist", "permanent"),
        ("InvalidImageName: couldn't parse image reference", "permanent"),
        ("toomanyrequests: You have reached your pull rate limit", "transient"),
        ("dial tcp 10.0.0.1:443: i/o timeout", "transient"),
        ("net/http: TLS handshake timeout", "transient"),
        ("received unexpected HTTP status: 503 Service Unavailable", "transient"),
        ('Back-off pulling image "quay.io/x/app:v9"', "unknown"),
        ("", "unknown"),
        # Both seen (an old timeout, then not found): the permanent reading wins.
        ("i/o timeout | manifest unknown", "permanent"),
    ],
)
def test_pull_errors_are_classified(text, verdict):
    assert classify_pull_error(text) == verdict


def _pull_pod(waiting_reason="ImagePullBackOff", waiting_message='Back-off pulling image "app:v9"'):
    cs = SimpleNamespace(state=SimpleNamespace(waiting=SimpleNamespace(reason=waiting_reason, message=waiting_message)))
    return SimpleNamespace(status=SimpleNamespace(init_container_statuses=None, container_statuses=[cs]))


def _core_with(pod, events=()):
    core = MagicMock()
    core.read_namespaced_pod.return_value = pod
    core.list_namespaced_event.return_value = SimpleNamespace(
        items=[SimpleNamespace(reason="Failed", message=m) for m in events]
    )
    return core


POD = [{"kind": "Pod", "name": "kustomize-guestbook-ui-5f58f69bd6-w4x22", "namespace": "eval-staging"}]


def test_the_cause_is_read_from_events_when_the_backoff_message_hides_it():
    core = _core_with(_pull_pod(), events=['Failed to pull image "app:v9": manifest unknown'])
    with patch.object(fix_planner, "get_core_client", return_value=core):
        verdict, evidence = fix_planner.image_pull_cause(POD[0])
    assert verdict == "permanent"
    assert "manifest unknown" in evidence


def test_a_bad_image_is_escalated_not_retried():
    """The 143 pod deletions: the new pod pulls the same missing tag."""
    core = _core_with(_pull_pod(), events=['Failed to pull image "app:v9": not found'])
    with (
        patch.object(fix_planner, "get_core_client", return_value=core),
        patch.object(fix_planner, "_steer", lambda p: p),
    ):
        plan = default_fix_plan("image_pull", {"category": "image_pull", "resources": POD})
    assert plan.strategy == HUMAN
    assert "not found" in plan.description


def test_missing_credentials_are_escalated():
    core = _core_with(_pull_pod("ErrImagePull", "unauthorized: authentication required"))
    with (
        patch.object(fix_planner, "get_core_client", return_value=core),
        patch.object(fix_planner, "_steer", lambda p: p),
    ):
        plan = default_fix_plan("image_pull", {"category": "image_pull", "resources": POD})
    assert plan.strategy == HUMAN


def test_a_transient_registry_error_is_retried_by_deleting_the_pod():
    core = _core_with(_pull_pod("ErrImagePull", "toomanyrequests: rate limit exceeded"))
    with (
        patch.object(fix_planner, "get_core_client", return_value=core),
        patch.object(fix_planner, "_steer", lambda p: p),
    ):
        plan = default_fix_plan("image_pull", {"category": "image_pull", "resources": POD})
    assert plan.strategy == "restart_controller", "retry the pull; a rollback would revert a good image"


def test_an_unreadable_pull_error_is_not_acted_on():
    core = MagicMock()
    core.read_namespaced_pod.side_effect = RuntimeError("apiserver down")
    with (
        patch.object(fix_planner, "get_core_client", return_value=core),
        patch.object(fix_planner, "_steer", lambda p: p),
    ):
        plan = default_fix_plan("image_pull", {"category": "image_pull", "resources": POD})
    assert plan.strategy == HUMAN


def _apps_with_deployment(revision="1", rs_items=()):
    apps = MagicMock()
    dep = MagicMock()
    dep.metadata.annotations = {"deployment.kubernetes.io/revision": revision}
    dep.metadata.uid = "dep-uid"
    dep.metadata.resource_version = "7"
    dep.spec.selector.match_labels = {"app": "ui"}
    apps.read_namespaced_deployment.return_value = dep
    apps.list_namespaced_replica_set.return_value = SimpleNamespace(items=list(rs_items))
    return apps, dep


def test_patch_image_without_a_previous_revision_does_not_delete_the_pod():
    """The old fallback deleted the pod and recorded it as completed."""
    pod = MagicMock()
    pod.spec.containers = [MagicMock(image="app:v9")]
    core = MagicMock()
    core.read_namespaced_pod.return_value = pod
    apps, _ = _apps_with_deployment(revision="1")
    plan = FixPlan("patch_image", "bad_image", 0.9, "", {"resources": POD})
    with (
        patch.object(fix_planner, "get_core_client", return_value=core),
        patch.object(fix_planner, "get_apps_client", return_value=apps),
        patch.object(fix_planner, "_find_owning_deployment", return_value="ui"),
        patch.object(fix_planner, "_snapshot_before"),
        patch("sre_agent.policy.check_write_policy", return_value=None),
    ):
        execution = fix_planner.execute_fix_with_snapshot(plan)
    core.delete_namespaced_pod.assert_not_called()
    assert execution.result[0] == "skip"
    assert not execution.applied, "a skip must not be recorded as a completed fix"


# ── workloads: the fast path targeted a pod that does not exist ────────────

DEPLOYMENT = [{"kind": "Deployment", "name": "kustomize-guestbook-ui", "namespace": "eval-staging"}]
WORKLOAD_FINDING = {"category": "workloads", "severity": "critical", "resources": DEPLOYMENT}


def _pods(*waiting_reasons):
    items = []
    for i, reason in enumerate(waiting_reasons):
        cs = SimpleNamespace(
            state=SimpleNamespace(waiting=SimpleNamespace(reason=reason, message="boom") if reason else None)
        )
        items.append(
            SimpleNamespace(
                metadata=SimpleNamespace(name=f"ui-{i}"),
                status=SimpleNamespace(init_container_statuses=None, container_statuses=[cs], conditions=[]),
            )
        )
    return SimpleNamespace(items=items)


def test_a_degraded_deployment_gets_a_rollout_restart_of_the_deployment():
    apps, _ = _apps_with_deployment()
    core = MagicMock()
    core.list_namespaced_pod.return_value = _pods(None)
    with (
        patch.object(fix_planner, "get_apps_client", return_value=apps),
        patch.object(fix_planner, "get_core_client", return_value=core),
        patch.object(fix_planner, "_steer", lambda p: p),
    ):
        plan = default_fix_plan("workloads", WORKLOAD_FINDING)
    assert plan.strategy == "rollout_restart"


def test_a_restart_that_would_recreate_the_same_failure_goes_to_a_person():
    """guestbook-ui was 0/1 because its pods could not pull: a restart cannot help."""
    apps, _ = _apps_with_deployment()
    core = MagicMock()
    core.list_namespaced_pod.return_value = _pods("ImagePullBackOff")
    with (
        patch.object(fix_planner, "get_apps_client", return_value=apps),
        patch.object(fix_planner, "get_core_client", return_value=core),
        patch.object(fix_planner, "_steer", lambda p: p),
    ):
        plan = default_fix_plan("workloads", WORKLOAD_FINDING)
    assert plan.strategy == HUMAN
    assert "ImagePullBackOff" in plan.description


def test_rollout_restart_patches_the_deployment_not_a_pod():
    apps, dep = _apps_with_deployment()
    dep.spec.template.metadata.annotations = {"keep": "me"}
    dep.spec.replicas = 1
    dep.status.available_replicas = 0
    plan = FixPlan("rollout_restart", "workloads", 0.6, "", {"resources": DEPLOYMENT})
    core = MagicMock()
    with (
        patch.object(fix_planner, "get_apps_client", return_value=apps),
        patch.object(fix_planner, "get_core_client", return_value=core),
        patch.object(fix_planner, "_snapshot_before"),
        patch("sre_agent.policy.check_write_policy", return_value=None),
    ):
        execution = fix_planner.execute_fix_with_snapshot(plan)
    core.read_namespaced_pod.assert_not_called()
    assert execution.applied
    body = apps.patch_namespaced_deployment.call_args.kwargs["body"]
    assert {"op": "test", "path": "/metadata/uid", "value": "dep-uid"} in body
    annotations = body[-1]["value"]
    assert annotations["keep"] == "me", "existing template annotations are preserved"
    assert "kubectl.kubernetes.io/restartedAt" in annotations


def test_rbac_preflight_knows_what_rollout_restart_writes():
    from sre_agent.monitor.rbac_preflight import _STRATEGY_WRITES

    assert _STRATEGY_WRITES["rollout_restart"] == [("apps", "deployments", "patch")]


# ── confidence: the recurrence penalty never matched a pod ─────────────────


def test_recurrence_lowers_confidence_for_a_recreated_pod():
    from sre_agent.monitor.confidence import _estimate_auto_fix_confidence

    finding = {"category": "crashloop", "severity": "warning", "resources": POD}
    fresh = _estimate_auto_fix_confidence(finding, {})
    again = _estimate_auto_fix_confidence(finding, {"Pod:eval-staging:kustomize-guestbook-ui": time.time()})
    assert again < fresh


# ── the breaker itself (disposable test database) ──────────────────────────

KEY = "crashloop:finance:Pod/billing-service-helm-guestbook"


@pytest.fixture
def db():
    from sre_agent.db import get_database

    database = get_database()
    truncate_tables(database, "actions", "inbox_items")
    _reset_settings()
    yield database
    truncate_tables(database, "actions", "inbox_items")
    _reset_settings()


def _insert_attempt(db, i, status="completed", verification="still_failing", key=KEY, age_s=60):
    db.execute(
        "INSERT INTO actions (id, finding_id, timestamp, category, tool, status, verification_status, "
        "correlation_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (f"a-{i}", f"f-{i}", int((time.time() - age_s) * 1000), "crashloop", "delete_pod", status, verification, key),
    )
    db.commit()


def test_under_the_limit_auto_fix_may_act(db):
    _insert_attempt(db, 1)
    _insert_attempt(db, 2, status="failed", verification=None)
    assert not fix_breaker.check(KEY).tripped


def test_n_unsuccessful_attempts_trip_the_breaker(db):
    _insert_attempt(db, 1)
    _insert_attempt(db, 2, status="failed", verification="unverifiable")
    _insert_attempt(db, 3, verification="verified_then_recurred")
    state = fix_breaker.check(KEY)
    assert state.tripped and state.should_escalate
    assert len(state.attempts) == 3


def test_failed_attempts_count(db):
    """Workloads failed 140 of 140; counting only completed fixes never stopped it."""
    for i in range(3):
        _insert_attempt(db, i, status="failed", verification="unverifiable")
    assert fix_breaker.check(KEY).tripped


def test_attempts_outside_the_window_do_not_count(db):
    for i in range(3):
        _insert_attempt(db, i, age_s=2 * 86400)
    assert not fix_breaker.check(KEY).tripped


def test_other_resources_do_not_count(db):
    for i in range(3):
        _insert_attempt(db, i, key="crashloop:finance:Pod/other")
    assert not fix_breaker.check(KEY).tripped


def test_proposals_and_human_review_items_are_not_attempts(db):
    for i, status in enumerate(("proposed", "rejected", "expired")):
        _insert_attempt(db, i, status=status, verification=None)
    assert not fix_breaker.check(KEY).tripped


def test_the_limit_is_a_setting(db, monkeypatch):
    monkeypatch.setenv("PULSE_AGENT_FIX_BREAKER_MAX_ATTEMPTS", "5")
    _reset_settings()
    for i in range(4):
        _insert_attempt(db, i)
    assert not fix_breaker.check(KEY).tripped
    _insert_attempt(db, 9)
    assert fix_breaker.check(KEY).tripped


def test_an_unreadable_history_fails_closed():
    with patch("sre_agent.repositories.monitor_repo.get_monitor_repo", side_effect=RuntimeError("db down")):
        state = fix_breaker.check(KEY)
    assert state.tripped and not state.should_escalate


FINDING = {
    "id": "f-x",
    "category": "crashloop",
    "severity": "critical",
    "title": "Pod billing-service-helm-guestbook-7d9c8b6f5-abcde restarting (6x)",
    "autoFixable": True,
    "resources": [{"kind": "Pod", "name": "billing-service-helm-guestbook-7d9c8b6f5-abcde", "namespace": "finance"}],
}


def _open_breaker_items(db):
    return db.fetchall(
        "SELECT * FROM inbox_items WHERE correlation_key = ? AND status NOT IN ('resolved', 'archived')",
        (fix_breaker.escalation_key(KEY),),
    )


def test_escalation_raises_one_item_with_the_history(db):
    for i in range(3):
        _insert_attempt(db, i, verification="still_failing")
    state = fix_breaker.check(KEY)
    first = fix_breaker.escalate(KEY, FINDING, state)
    second = fix_breaker.escalate(KEY, FINDING, state)
    rows = _open_breaker_items(db)
    assert first and first == second
    assert len(rows) == 1
    assert "delete_pod" in rows[0]["summary"] and "still_failing" in rows[0]["summary"]
    assert rows[0]["created_by"] == fix_breaker.ESCALATION_CREATOR


def test_an_open_escalation_keeps_the_breaker_tripped_after_the_window(db):
    for i in range(3):
        _insert_attempt(db, i)
    fix_breaker.escalate(KEY, FINDING, fix_breaker.check(KEY))
    truncate_tables(db, "actions")  # the attempts age out
    state = fix_breaker.check(KEY)
    assert state.tripped and not state.should_escalate


def test_inbox_reconcile_does_not_resolve_the_escalation(db):
    """Monitor items whose key is not a live finding are auto-resolved each scan."""
    from sre_agent.inbox import reconcile_open_items

    for i in range(3):
        _insert_attempt(db, i)
    fix_breaker.escalate(KEY, FINDING, fix_breaker.check(KEY))
    reconcile_open_items({"some:other:key"})
    assert len(_open_breaker_items(db)) == 1


# ── replay: the same finding recurring every scan ──────────────────────────


class _Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


async def _replay(db, monkeypatch, *, scans, step_s, execute, confidence=0.9):
    from sre_agent.monitor.cluster_monitor import ClusterMonitor

    monkeypatch.setenv("PULSE_AGENT_MAX_TRUST_LEVEL", "3")
    _reset_settings()
    clock = _Clock()
    monkeypatch.setattr(time, "time", clock)

    monitor = ClusterMonitor()
    monitor._subscribers = []
    monitor._broadcast_raw = AsyncMock()
    owner = MagicMock(kind="ReplicaSet")
    owner.name = "billing-service-helm-guestbook-7d9c8b6f5"
    pod = MagicMock()
    pod.metadata.owner_references = [owner]
    with (
        patch("sre_agent.monitor.cluster_monitor.is_autofix_paused", return_value=False),
        patch("sre_agent.monitor.cluster_monitor.get_core_client") as core,
        patch("sre_agent.monitor.cluster_monitor._estimate_auto_fix_confidence", return_value=confidence),
        patch("sre_agent.monitor.cluster_monitor.notify_fix_proposed", new=AsyncMock()),
        patch("sre_agent.monitor.fix_planner.get_investigation_for_finding", return_value=None),
        patch("sre_agent.monitor.rbac_preflight.can_execute", return_value=(True, "")),
        patch("sre_agent.monitor.fix_planner.execute_fix", side_effect=execute),
    ):
        core.return_value.read_namespaced_pod.return_value = pod
        for scan in range(scans):
            # A new pod name every scan, as a recreated pod would have.
            finding = dict(FINDING, id=f"f-{scan}")
            finding["resources"] = [
                dict(
                    FINDING["resources"][0],
                    name=f"billing-service-helm-guestbook-7d9c8b6f5-{'bcdfg'[scan % 5]}x{'245678'[scan % 6]}zz",
                )
            ]
            # What every scan does before auto_fix (_run_scan_locked): the
            # in-memory cooldown and attempt cap are an hour deep.
            cutoff = clock.now - 3600
            monitor._recent_fixes = {k: v for k, v in monitor._recent_fixes.items() if v > cutoff}
            monitor._fix_attempt_counts = {
                k: v for k, v in monitor._fix_attempt_counts.items() if k in monitor._recent_fixes
            }
            await monitor.auto_fix([finding])
            clock.now += step_s
    executed = db.fetchall(
        "SELECT status FROM actions WHERE correlation_key = ? AND status IN ('completed', 'failed')", (KEY,)
    )
    return executed, _open_breaker_items(db)


@pytest.mark.asyncio
async def test_replay_a_fix_that_never_holds_stops_at_n_then_escalates_once(db, monkeypatch):
    """Six hours of scans every 10 minutes. The old hourly cap allowed two
    deletions an hour, forever; now it is three, then one inbox item."""
    executed, items = await _replay(
        db, monkeypatch, scans=36, step_s=600, execute=lambda plan: ("delete_pod", "before", "after")
    )
    assert len(executed) == 3
    assert len(items) == 1


@pytest.mark.asyncio
async def test_replay_a_fix_that_always_fails_stops_at_n_then_escalates_once(db, monkeypatch):
    """The workloads loop: every attempt failed, none counted, retried every scan."""

    def boom(plan):
        raise RuntimeError("pods not found")

    executed, items = await _replay(db, monkeypatch, scans=36, step_s=600, execute=boom)
    assert len(executed) == 3
    assert all(r["status"] == "failed" for r in executed)
    assert len(items) == 1


@pytest.mark.asyncio
async def test_replay_failed_attempts_cool_down_between_scans(db, monkeypatch):
    """A failed attempt used to set no cooldown: one per scan, every scan."""

    def boom(plan):
        raise RuntimeError("pods not found")

    executed, _ = await _replay(db, monkeypatch, scans=4, step_s=60, execute=boom)
    assert len(executed) == 1


@pytest.mark.asyncio
async def test_low_confidence_at_trust_3_proposes_instead_of_executing(db, monkeypatch):
    """Confidence 0.48 executed at trust 3; below the floor it is now a proposal."""
    calls = []

    def run(plan):
        calls.append(plan)
        return ("delete_pod", "before", "after")

    await _replay(db, monkeypatch, scans=3, step_s=600, execute=run, confidence=0.48)
    assert calls == []
    proposals = db.fetchall("SELECT status, reasoning FROM actions WHERE correlation_key = ?", (KEY,))
    assert [p["status"] for p in proposals] == ["proposed"], "one pending proposal, not one per scan"
    assert "below the 0.60 needed" in proposals[0]["reasoning"]


# ── one open item while the owner keeps crashing ──────────────────────────


def _verify(gate_status, active_findings):
    import asyncio

    from sre_agent.monitor import health_gate
    from sre_agent.monitor.verification_pipeline import process_verifications

    deferred = dict(FINDING, id="f-old")
    monitor = SimpleNamespace(_scan_counter=2, _broadcast_raw=AsyncMock(), _pending_verifications={})
    monitor._pending_verifications["a-v"] = {
        "action_id": "a-v",
        "finding_id": "f-old",
        "category": "crashloop",
        "resources": deferred["resources"],
        "verify_resources": [{"kind": "Deployment", "name": "billing-service-helm-guestbook", "namespace": "finance"}],
        "target_scan": 2,
        "deferred_inbox_finding": deferred,
    }
    repo = MagicMock(
        async_update_action_verification=AsyncMock(), async_get_investigation_by_finding_id=AsyncMock(return_value=None)
    )
    with (
        patch.object(health_gate, "check_resources", return_value=(gate_status, "owner 0/1")),
        patch("sre_agent.monitor.verification_pipeline.get_monitor_repo", return_value=repo),
        patch("sre_agent.inbox.resolve_finding_inbox_item") as resolve,
        patch("sre_agent.monitor.verification_pipeline.learn_when_held"),
    ):
        asyncio.run(process_verifications(monitor, active_findings))
    return resolve


def test_an_auto_fixed_item_resolves_only_when_the_owner_is_healthy():
    from sre_agent.monitor import health_gate

    resolve = _verify(health_gate.PASS, [])
    resolve.assert_called_once()
    assert resolve.call_args[0][0] == "f-old"


def test_an_auto_fixed_item_stays_open_when_the_owner_is_still_down():
    """The pod a fix deleted is gone, but the Deployment is 0/1: not resolved."""
    from sre_agent.monitor import health_gate

    resolve = _verify(health_gate.FAIL, [])
    resolve.assert_not_called()


def test_an_auto_fixed_item_stays_open_when_the_replacement_pod_crashes():
    from sre_agent.monitor import health_gate

    again = dict(FINDING, id="f-new")
    resolve = _verify(health_gate.PASS, [again])
    resolve.assert_not_called()


def test_a_recurrence_on_the_open_item_refreshes_its_title(db):
    """Items kept naming the pod a fix had deleted ("...-5zqkq restarting (35x)")."""
    from sre_agent.inbox import bridge_finding_to_inbox, get_inbox_item

    first = dict(FINDING, id="f-1", title="Pod billing-service-helm-guestbook-84ddcd67d8-5zqkq restarting (35x)")
    first["resources"] = [dict(FINDING["resources"][0], name="billing-service-helm-guestbook-84ddcd67d8-5zqkq")]
    second = dict(FINDING, id="f-2", title="Pod billing-service-helm-guestbook-84ddcd67d8-bs5rm restarting (4x)")
    second["resources"] = [dict(FINDING["resources"][0], name="billing-service-helm-guestbook-84ddcd67d8-bs5rm")]
    item_id = bridge_finding_to_inbox(first)
    assert bridge_finding_to_inbox(second) == item_id, "the same owner's condition is one item"
    assert get_inbox_item(item_id)["title"].endswith("bs5rm restarting (4x)")
