"""Every fix owns its evidence; denial and absence cannot become recovery."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes.client import (
    V1Container,
    V1Deployment,
    V1DeploymentSpec,
    V1LabelSelector,
    V1ObjectMeta,
    V1PodSpec,
    V1PodTemplateSpec,
    V1ResourceRequirements,
)
from kubernetes.client.rest import ApiException

from sre_agent.monitor import fix_planner
from sre_agent.monitor.cluster_monitor import ClusterMonitor
from sre_agent.temporal import incident_activities


def plan(strategy="patch_resources", name="api", kind="Deployment", namespace="default"):
    return fix_planner.FixPlan(
        strategy, "oom", 0.9, "synthetic test", {"resources": [{"kind": kind, "name": name, "namespace": namespace}]}
    )


def test_concurrent_worker_snapshots_stay_with_their_action():
    barrier = Barrier(2)

    def executor(fix):
        name = fix.params["resources"][0]["name"]
        fix_planner._snapshot_before("Deployment", name, "default")
        barrier.wait(timeout=5)
        return "patch_resources", name, "applied"

    with (
        patch.dict(fix_planner._EXECUTORS, {"test": executor}),
        patch("sre_agent.snapshot.capture", side_effect=lambda kind, name, ns: {"kind": kind, "name": name}),
    ):
        with ThreadPoolExecutor(max_workers=2) as pool:
            executions = list(pool.map(fix_planner.execute_fix_with_snapshot, [plan("test", "a"), plan("test", "b")]))
    assert [execution.snapshot["name"] for execution in executions] == ["a", "b"]
    assert [execution.verify_resources[0]["name"] for execution in executions] == ["a", "b"]
    assert fix_planner.take_last_snapshot() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["blocked", "skip", "require_human_review", "404"])
async def test_monitor_nonmutations_do_not_create_success_or_pending_verification(result):
    monitor = ClusterMonitor()
    monitor._dispatch_durable_fix = AsyncMock(return_value=False)
    monitor._broadcast_raw = AsyncMock()
    report = {"id": "a-test", "status": "executing"}
    finding = {"id": "f-test", "title": "synthetic"}
    execution = MagicMock(
        side_effect=ApiException(status=404) if result == "404" else None,
        return_value=(result, "before", "not applied"),
    )
    with (
        patch("sre_agent.monitor.fix_planner.execute_fix", execution),
        patch("sre_agent.monitor.cluster_monitor.save_action") as save,
        patch("sre_agent.monitor.cluster_monitor._resolve_finding_inbox") as resolve,
        patch("sre_agent.context_bus.get_context_bus") as bus,
        patch("sre_agent.monitor.cluster_monitor._METRICS_AVAILABLE", False),
    ):
        success = await monitor._execute_fix(
            action_report=report,
            targeted_plan=plan(),
            category="oom",
            resources=[],
            resource_key="test",
            finding=finding,
            verify_resources=None,
        )
    assert not success
    assert report["status"] == "failed"
    assert report["verificationStatus"] == "unverifiable"
    assert not monitor._pending_verifications
    assert not monitor._recent_fix_ids
    assert not monitor._fix_attempt_counts
    bus.assert_not_called()
    resolve.assert_not_called()
    save.assert_called_once()


def deployment():
    return V1Deployment(
        metadata=V1ObjectMeta(name="api", uid="uid-a", resource_version="7"),
        spec=V1DeploymentSpec(
            replicas=1,
            selector=V1LabelSelector(match_labels={"app": "api"}),
            template=V1PodTemplateSpec(
                spec=V1PodSpec(
                    containers=[
                        V1Container(
                            name="app",
                            image="old",
                            resources=V1ResourceRequirements(
                                limits={"memory": "256Mi", "cpu": "1"}, requests={"cpu": "100m"}
                            ),
                        )
                    ]
                )
            ),
        ),
    )


def test_monitor_patch_contains_atomic_identity_and_version_tests():
    apps = MagicMock()
    apps.read_namespaced_deployment.return_value = deployment()
    with (
        patch("sre_agent.monitor.fix_planner.get_apps_client", return_value=apps),
        patch("sre_agent.snapshot.capture", return_value=None),
    ):
        result = fix_planner.execute_fix(plan())
    assert result[0] == "patch_resources"
    args = apps.patch_namespaced_deployment.call_args.kwargs
    assert args["_content_type"] == "application/json-patch+json"
    assert args["body"][:2] == [
        {"op": "test", "path": "/metadata/uid", "value": "uid-a"},
        {"op": "test", "path": "/metadata/resourceVersion", "value": "7"},
    ]
    assert args["body"][2]["value"] == {"memory": "512Mi", "cpu": "1"}


def test_snapshot_of_changed_resource_refuses_monitor_mutation():
    apps = MagicMock()
    apps.read_namespaced_deployment.return_value = deployment()
    with (
        patch("sre_agent.monitor.fix_planner.get_apps_client", return_value=apps),
        patch("sre_agent.snapshot.capture", return_value={"uid": "other", "resourceVersion": "8"}),
    ):
        with pytest.raises(ValueError, match="Target changed"):
            fix_planner.execute_fix_with_snapshot(plan())
    apps.patch_namespaced_deployment.assert_not_called()


def test_delete_uses_observed_pod_preconditions_and_owner_target():
    pod = SimpleNamespace(
        metadata=SimpleNamespace(
            uid="pod-uid",
            resource_version="8",
            owner_references=[SimpleNamespace(kind="StatefulSet", name="owner", controller=True, uid="owner-uid")],
        ),
        status=SimpleNamespace(phase="Running", container_statuses=[]),
    )
    core = MagicMock()
    core.read_namespaced_pod.return_value = pod
    apps = MagicMock()
    apps.read_namespaced_stateful_set.return_value = SimpleNamespace(metadata=SimpleNamespace(uid="owner-uid"))
    with (
        patch("sre_agent.monitor.fix_planner.get_core_client", return_value=core),
        patch("sre_agent.monitor.fix_planner.get_apps_client", return_value=apps),
    ):
        execution = fix_planner.execute_fix_with_snapshot(plan("restart_controller", "pod", "Pod"))
    conditions = core.delete_namespaced_pod.call_args.kwargs["body"].preconditions
    assert conditions.uid == "pod-uid" and conditions.resource_version == "8"
    assert execution.verify_resources == [{"kind": "StatefulSet", "name": "owner", "namespace": "default"}]
    assert execution.snapshot is None


def test_protected_namespace_image_fallback_cannot_bypass_delete_policy():
    pod = SimpleNamespace(metadata=SimpleNamespace(owner_references=[]), spec=SimpleNamespace(containers=[]))
    core, apps = MagicMock(), MagicMock()
    core.read_namespaced_pod.return_value = pod
    with (
        patch("sre_agent.monitor.fix_planner.get_core_client", return_value=core),
        patch("sre_agent.monitor.fix_planner.get_apps_client", return_value=apps),
        patch("sre_agent.k8s_client._load_k8s", side_effect=AssertionError("real client initialization")),
        patch("sre_agent.monitor.fix_planner._find_owning_deployment", return_value=None),
    ):
        execution = fix_planner.execute_fix_with_snapshot(plan("patch_image", "pod", "Pod", "production"))
    assert not execution.applied
    assert execution.result[0] == "blocked"
    core.delete_namespaced_pod.assert_not_called()
    apps.patch_namespaced_deployment.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["blocked", "skip", "require_human_review"])
async def test_durable_apply_never_reports_skipped_write_as_applied(tool):
    with (
        patch("sre_agent.monitor.fix_planner.execute_fix", return_value=(tool, "", "not applied")),
        patch("sre_agent.temporal.incident_activities.activity.heartbeat") as heartbeat,
    ):
        with pytest.raises(Exception, match="Fix was not applied") as error:
            await incident_activities.apply_fix({"strategy": "create_configmap"})
    assert getattr(error.value, "type", None) == "FixNotApplied"
    assert heartbeat.call_args_list[-1].args == ("starting",)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["fail", "unverifiable"])
async def test_durable_verification_cannot_turn_unknown_or_failure_into_health(status):
    from sre_agent.monitor.health_gate import GateResult

    with patch(
        "sre_agent.monitor.health_gate.check_resource", return_value=GateResult(status, "Pod default/a", "unknown")
    ):
        with pytest.raises(RuntimeError, match="not verified"):
            await incident_activities.verify_fix({"kind": "Pod", "name": "a"})
        if status == "unverifiable":
            with pytest.raises(RuntimeError, match="unverifiable"):
                await incident_activities.check_recurrence({"kind": "Pod", "name": "a"})


@pytest.mark.asyncio
async def test_durable_recurrence_preserves_identity_bound_restart_trend():
    from sre_agent.monitor.health_gate import GateResult

    observed = GateResult("pass", "Pod default/a", "ready", {"uid": "same", "restarts": 4})
    with patch("sre_agent.monitor.health_gate.check_resource", return_value=observed):
        assert (
            await incident_activities.check_recurrence(
                {"kind": "Pod", "name": "a", "uid_at_fix": "same", "restarts_at_fix": 3}
            )
        )["recurred"]
        with pytest.raises(RuntimeError, match="baseline"):
            await incident_activities.check_recurrence({"kind": "Pod", "name": "a"})
        with pytest.raises(RuntimeError, match="identity changed"):
            await incident_activities.check_recurrence(
                {"kind": "Pod", "name": "a", "uid_at_fix": "other", "restarts_at_fix": 3}
            )


@pytest.mark.parametrize("kind,controller", [("ConfigMap", True), ("ReplicaSet", False)])
def test_noncontrolling_or_unsupported_owner_cannot_authorize_delete(kind, controller):
    pod = SimpleNamespace(
        metadata=SimpleNamespace(
            uid="pod",
            resource_version="8",
            owner_references=[SimpleNamespace(kind=kind, name="owner", uid="owner", controller=controller)],
        ),
        status=SimpleNamespace(phase="Running", container_statuses=[]),
    )
    core = MagicMock()
    core.read_namespaced_pod.return_value = pod
    with patch("sre_agent.monitor.fix_planner.get_core_client", return_value=core):
        assert not fix_planner.execute_fix_with_snapshot(plan("restart_controller", "pod", "Pod")).applied
    core.delete_namespaced_pod.assert_not_called()


def test_recreated_owner_cannot_redirect_mutation_or_verification():
    pod = SimpleNamespace(
        metadata=SimpleNamespace(
            uid="pod",
            resource_version="8",
            owner_references=[SimpleNamespace(kind="ReplicaSet", name="same", uid="original", controller=True)],
        ),
        status=SimpleNamespace(phase="Running", container_statuses=[]),
    )
    core, apps = MagicMock(), MagicMock()
    core.read_namespaced_pod.return_value = pod
    apps.read_namespaced_replica_set.return_value = SimpleNamespace(metadata=SimpleNamespace(uid="replacement"))
    with (
        patch("sre_agent.monitor.fix_planner.get_core_client", return_value=core),
        patch("sre_agent.monitor.fix_planner.get_apps_client", return_value=apps),
    ):
        with pytest.raises(fix_planner.FixPreconditionError, match="identity changed"):
            fix_planner.execute_fix_with_snapshot(plan("restart_controller", "pod", "Pod"))
    core.delete_namespaced_pod.assert_not_called()
    apps.patch_namespaced_deployment.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [fix_planner.FixPreconditionError("prewrite refused"), ApiException(status=409)])
async def test_proven_prewrite_refusal_is_typed_no_mutation_for_saga(failure):
    with (
        patch("sre_agent.monitor.fix_planner.execute_fix_with_snapshot", side_effect=failure),
        patch("sre_agent.temporal.incident_activities.activity.heartbeat"),
    ):
        with pytest.raises(Exception) as error:
            await incident_activities.apply_fix({"strategy": "patch_resources"})
    assert getattr(error.value, "type", None) == "FixNotApplied"


@pytest.mark.asyncio
async def test_legacy_outcome_links_only_one_dispatched_action_and_propagates_db_failure():
    repo = MagicMock()
    repo.get_action_by_id.return_value = None
    repo.db.fetchall.return_value = [{"id": "actual-action"}]
    with patch("sre_agent.repositories.monitor_repo.get_monitor_repo", return_value=repo):
        await incident_activities.record_outcome("legacy-finding", "unverifiable", "unknown")
        assert repo.update_action_verification.call_args.args[0] == "actual-action"
        repo.db.fetchall.return_value = [{"id": "a"}, {"id": "b"}]
        with pytest.raises(Exception, match="unique linked"):
            await incident_activities.record_outcome("legacy-finding", "verified", "claim")
        repo.get_action_by_id.return_value = {"id": "actual-action"}
        repo.update_action_verification.side_effect = RuntimeError("database write refused")
        with pytest.raises(RuntimeError, match="write refused"):
            await incident_activities.record_outcome("actual-action", "verified", "readiness")


def activity_failure(kind="Unknown"):
    from temporalio.exceptions import ActivityError, ApplicationError, RetryState

    error = ActivityError(
        "synthetic failure",
        scheduled_event_id=1,
        started_event_id=2,
        identity="test",
        activity_type="test",
        activity_id="test",
        retry_state=RetryState.NON_RETRYABLE_FAILURE,
    )
    error.__cause__ = ApplicationError("synthetic refusal", type=kind, non_retryable=True)
    return error


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["verification_failed", "cancel", "blocked"])
async def test_durable_none_snapshot_does_not_restore_unmodified_resource_or_claim_compensation(monkeypatch, mode):
    from sre_agent.temporal import incident_workflow as module

    calls = []

    async def execute(function, *pos, **kw):
        name = function.__name__
        calls.append((name, pos, kw))
        if name == "capture_snapshot":
            return {"kind": "Deployment", "name": "unmodified"}
        if name == "apply_fix":
            if mode == "blocked":
                raise activity_failure("FixNotApplied")
            return {
                "tool": "delete_pod",
                "snapshot": None,
                "verify_resource": {"kind": "StatefulSet", "name": "actual"},
            }
        if name == "verify_fix":
            if mode == "cancel":
                raise asyncio.CancelledError()
            raise activity_failure()
        if name == "restore_snapshot":
            assert pos[0] is None
            return "no snapshot captured; nothing to restore"
        return None

    monkeypatch.setattr(module.workflow, "execute_activity", execute)
    monkeypatch.setattr(module.workflow, "patched", lambda _: True)
    result = await module.IncidentWorkflow().run(
        module.IncidentInput("finding", require_approval=False, action_id="action")
    )
    assert result["compensated"] is False
    assert result["verdict"] != "rolled_back"
    records = [entry for entry in calls if entry[0] == "record_outcome"]
    assert records[0][2]["args"][0] == "action"
    if mode == "blocked":
        assert not any(entry[0] == "restore_snapshot" for entry in calls)


@pytest.mark.asyncio
async def test_legacy_missing_recurrence_evidence_records_unverifiable_not_success(monkeypatch):
    from sre_agent.temporal import incident_workflow as module

    records = []

    async def execute(function, *pos, **kw):
        name = function.__name__
        if name == "capture_snapshot":
            return None
        if name == "apply_fix":
            return {"tool": "delete_pod"}
        if name == "verify_fix":
            return {"healthy": True, "evidence": "historical observation"}
        if name == "check_recurrence":
            raise activity_failure("MissingBaseline")
        if name == "record_outcome":
            records.append(kw["args"])
        return None

    monkeypatch.setattr(module.workflow, "execute_activity", execute)
    monkeypatch.setattr(module.workflow, "patched", lambda _: False)
    monkeypatch.setattr(module.workflow, "sleep", AsyncMock())
    result = await module.IncidentWorkflow().run(module.IncidentInput("legacy-finding", require_approval=False))
    assert result["verdict"] == "unverifiable"
    assert records[0][:2] == ["legacy-finding", "unverifiable"]
