"""Verification contracts for interactive write tools.

The monitor's auto-fix path had preflight, snapshots, and an affirmative
health gate; a write tool called from chat had none of them. These tests pin
the contract: a missing target refuses the write before it happens, a
successful write is recorded with a snapshot and a scheduled probe, and the
probes state tool-specific facts (a scale-to-0 verifies as 0 ready, a
rollback verifies the revision moved) instead of generic health.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from sre_agent import tool_contracts
from sre_agent.monitor import health_gate


class _ApiError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"api error {status}")
        self.status = status


def _deployment(desired=3, ready=3, revision="4"):
    return SimpleNamespace(
        spec=SimpleNamespace(replicas=desired),
        status=SimpleNamespace(ready_replicas=ready),
        metadata=SimpleNamespace(annotations={"deployment.kubernetes.io/revision": revision}),
    )


def _apps(dep=None, exc=None):
    apps = MagicMock()
    if exc is not None:
        apps.read_namespaced_deployment.side_effect = exc
    else:
        apps.read_namespaced_deployment.return_value = dep
    return apps


def _pod(owner_kind=None, owner_name="owner"):
    refs = [SimpleNamespace(kind=owner_kind, name=owner_name)] if owner_kind else []
    return SimpleNamespace(metadata=SimpleNamespace(owner_references=refs))


def _node(unschedulable):
    return SimpleNamespace(spec=SimpleNamespace(unschedulable=unschedulable))


class TestPrecondition:
    def test_missing_deployment_refuses_the_write(self):
        called = []
        with patch("sre_agent.k8s_client.get_apps_client", return_value=_apps(exc=_ApiError(404))):
            out = tool_contracts.execute_with_contract(
                "restart_deployment", {"namespace": "prod", "name": "api"}, lambda: called.append(1)
            )
        assert not called, "the write ran despite a failed precondition"
        assert "not found" in out
        assert "was not attempted" in out

    def test_unreadable_target_fails_closed(self):
        called = []
        with patch("sre_agent.k8s_client.get_apps_client", return_value=_apps(exc=RuntimeError("boom"))):
            out = tool_contracts.execute_with_contract(
                "scale_deployment", {"namespace": "prod", "name": "api", "replicas": 2}, lambda: called.append(1)
            )
        assert not called
        assert "was not attempted" in out

    def test_uncontracted_tool_passes_through(self):
        out = tool_contracts.execute_with_contract("list_pods", {"namespace": "prod"}, lambda: "pods!")
        assert out == "pods!"


class TestExecutionRecording:
    def _run(self, tool_name, args, tool_result, monitor=None):
        saved = {}

        def fake_save_action(action, category="", resources=None, finding=None):
            saved["action"] = action
            saved["category"] = category
            saved["resources"] = resources

        with (
            patch("sre_agent.k8s_client.get_apps_client", return_value=_apps(_deployment())),
            patch("sre_agent.snapshot.capture", return_value={"kind": "Deployment", "name": "api", "namespace": "p"}),
            patch("sre_agent.monitor.actions.save_action", side_effect=fake_save_action),
            patch("sre_agent.monitor.cluster_monitor.get_cluster_monitor_sync", return_value=monitor),
            patch("sre_agent.monitor.actions.update_action_verification") as upd,
        ):
            out = tool_contracts.execute_with_contract(tool_name, args, lambda: tool_result)
        saved["update_verification"] = upd
        return out, saved

    def test_success_records_action_and_schedules_probe(self):
        monitor = MagicMock()
        out, saved = self._run(
            "restart_deployment",
            {"namespace": "prod", "name": "api"},
            "Rolling restart triggered for prod/api.",
            monitor=monitor,
        )
        assert "Verification contract" in out
        assert "snapshot captured" in out
        action = saved["action"]
        assert action["status"] == "completed"
        assert action["verificationStatus"] == "pending"
        assert action["beforeSnapshot"]
        assert saved["category"] == "chat_action"
        monitor.schedule_contract_verification.assert_called_once()
        _, kwargs = monitor.schedule_contract_verification.call_args
        assert kwargs["probe"]["tool"] == "restart_deployment"
        assert kwargs["probe"]["pre"]["revision"] == "4"

    def test_no_monitor_is_said_plainly_not_left_pending(self):
        out, saved = self._run(
            "restart_deployment",
            {"namespace": "prod", "name": "api"},
            "Rolling restart triggered for prod/api.",
            monitor=None,
        )
        assert "no monitor loop is running" in out
        status_arg = saved["update_verification"].call_args[0][1]
        assert status_arg == "unverifiable"

    def test_error_result_passes_through_unrecorded(self):
        out, saved = self._run(
            "restart_deployment",
            {"namespace": "prod", "name": "api"},
            "Error (forbidden): cannot patch deployments",
        )
        assert "Verification contract" not in out
        assert "action" not in saved, "an error result must not become a completed action row"

    def test_component_tuple_shape_is_preserved(self):
        monitor = MagicMock()
        out, _ = self._run(
            "restart_deployment",
            {"namespace": "prod", "name": "api"},
            ("Rolling restart triggered for prod/api.", {"kind": "section"}),
            monitor=monitor,
        )
        assert isinstance(out, tuple) and out[1] == {"kind": "section"}


class TestProbes:
    def test_scale_to_zero_verifies_as_zero_ready(self):
        """The generic gate calls 0/0 a failure; the scale probe must not."""
        with patch("sre_agent.k8s_client.get_apps_client", return_value=_apps(_deployment(desired=0, ready=0))):
            status, evidence = tool_contracts.run_probe(
                {"tool": "scale_deployment", "args": {"namespace": "p", "name": "api", "replicas": 0}, "pre": {}}
            )
        assert status == health_gate.PASS
        assert "scaled to 0 as requested" in evidence

    def test_scale_spec_drift_fails(self):
        with patch("sre_agent.k8s_client.get_apps_client", return_value=_apps(_deployment(desired=5, ready=5))):
            status, evidence = tool_contracts.run_probe(
                {"tool": "scale_deployment", "args": {"namespace": "p", "name": "api", "replicas": 2}, "pre": {}}
            )
        assert status == health_gate.FAIL
        assert "did not hold" in evidence

    def test_rollback_same_revision_fails_despite_health(self):
        dep = _deployment(desired=3, ready=3, revision="4")
        with patch("sre_agent.k8s_client.get_apps_client", return_value=_apps(dep)):
            status, evidence = tool_contracts.run_probe(
                {
                    "tool": "rollback_deployment",
                    "args": {"namespace": "p", "name": "api"},
                    "pre": {"revision_before": "4"},
                }
            )
        assert status == health_gate.FAIL
        assert "did not take effect" in evidence

    def test_rollback_moved_revision_passes(self):
        dep = _deployment(desired=3, ready=3, revision="5")
        with patch("sre_agent.k8s_client.get_apps_client", return_value=_apps(dep)):
            status, evidence = tool_contracts.run_probe(
                {
                    "tool": "rollback_deployment",
                    "args": {"namespace": "p", "name": "api"},
                    "pre": {"revision_before": "4"},
                }
            )
        assert status == health_gate.PASS
        assert "4 -> 5" in evidence

    def test_bare_pod_delete_is_unverifiable_never_verified(self):
        status, evidence = tool_contracts.run_probe(
            {"tool": "delete_pod", "args": {"namespace": "p", "pod_name": "x"}, "pre": {"owner": None}}
        )
        assert status == health_gate.UNVERIFIABLE
        assert "permanent" in evidence

    def test_pod_delete_verifies_through_owner(self):
        dep = _deployment(desired=3, ready=3)
        with patch("sre_agent.k8s_client.get_apps_client", return_value=_apps(dep)):
            status, evidence = tool_contracts.run_probe(
                {
                    "tool": "delete_pod",
                    "args": {"namespace": "p", "pod_name": "x"},
                    "pre": {"owner": {"kind": "Deployment", "name": "api", "namespace": "p"}},
                }
            )
        assert status == health_gate.PASS
        assert "verified through owner" in evidence

    def test_cordon_that_did_not_hold_fails(self):
        core = MagicMock()
        core.read_node.return_value = _node(unschedulable=False)
        with patch("sre_agent.k8s_client.get_core_client", return_value=core):
            status, evidence = tool_contracts.run_probe(
                {"tool": "cordon_node", "args": {"node_name": "n1"}, "pre": {}}
            )
        assert status == health_gate.FAIL
        assert "did not hold" in evidence

    def test_unknown_probe_is_unverifiable(self):
        status, _ = tool_contracts.run_probe({"tool": "not_a_tool"})
        assert status == health_gate.UNVERIFIABLE

    def test_crashed_probe_is_a_fact_about_the_check(self):
        with patch("sre_agent.k8s_client.get_apps_client", side_effect=RuntimeError("boom")):
            status, _evidence = tool_contracts.run_probe(
                {"tool": "restart_deployment", "args": {"namespace": "p", "name": "api"}, "pre": {}}
            )
        assert status == health_gate.UNVERIFIABLE


class TestPreFacts:
    def test_delete_pod_captures_deployment_owner_through_replicaset(self):
        core = MagicMock()
        core.read_namespaced_pod.return_value = _pod(owner_kind="ReplicaSet", owner_name="api-abc")
        apps = MagicMock()
        apps.read_namespaced_replica_set.return_value = SimpleNamespace(
            metadata=SimpleNamespace(owner_references=[SimpleNamespace(kind="Deployment", name="api")])
        )
        with (
            patch("sre_agent.k8s_client.get_core_client", return_value=core),
            patch("sre_agent.k8s_client.get_apps_client", return_value=apps),
        ):
            pre = tool_contracts._pre_delete_pod({"namespace": "p", "pod_name": "api-abc-x"})
        assert pre["owner"] == {"kind": "Deployment", "name": "api", "namespace": "p"}

    def test_cordon_records_prior_state(self):
        core = MagicMock()
        core.read_node.return_value = _node(unschedulable=False)
        with patch("sre_agent.k8s_client.get_core_client", return_value=core):
            pre = tool_contracts._pre_cordon_node({"node_name": "n1"})
        assert pre == {"was_unschedulable": False}


class TestRollbackWiring:
    def test_restore_snapshot_rollback_executes(self):
        """_make_rollback_info persisted restore_snapshot actions that
        execute_rollback then refused as unsupported — the persisted-but-dead
        path this milestone wires up."""
        from sre_agent.monitor import actions

        detail = {
            "status": "completed",
            "rollbackAction": {
                "tool": "restore_snapshot",
                "input": {"snapshot": '{"kind": "Deployment", "name": "api", "namespace": "p", "spec": {}}'},
            },
        }
        repo = MagicMock()
        with (
            patch.object(actions, "get_action_detail", return_value=detail),
            patch.object(actions, "get_monitor_repo", return_value=repo),
            patch("sre_agent.snapshot.restore", return_value="Restored Deployment p/api from snapshot") as restore,
        ):
            out = actions.execute_rollback("a-123")
        assert out["status"] == "rolled_back"
        restore.assert_called_once()
        repo.update_action_status.assert_called_once_with("a-123", "rolled_back", "rolled_back")

    def test_unparseable_snapshot_is_an_error_not_a_crash(self):
        from sre_agent.monitor import actions

        detail = {
            "status": "completed",
            "rollbackAction": {"tool": "restore_snapshot", "input": {"snapshot": "not json"}},
        }
        with patch.object(actions, "get_action_detail", return_value=detail):
            out = actions.execute_rollback("a-123")
        assert "error" in out


@pytest.mark.asyncio
class TestPipelineProbeDispatch:
    async def _monitor_with(self, payload):
        monitor = MagicMock()
        monitor._pending_verifications = {"a-1": payload}
        monitor._scan_counter = 5
        monitor._broadcast_raw = _async_noop
        return monitor

    async def test_probe_grace_keeps_a_failing_rollout_pending(self):
        from sre_agent.monitor.verification_pipeline import process_verifications

        payload = {
            "action_id": "a-1",
            "finding_id": "",
            "category": "chat_action",
            "resources": [],
            "verify_resources": [],
            "target_scan": 5,
            "probe": {"tool": "restart_deployment", "args": {"namespace": "p", "name": "api"}, "pre": {}},
            "grace_scans": 2,
        }
        monitor = await self._monitor_with(payload)
        with patch("sre_agent.tool_contracts.run_probe", return_value=(health_gate.FAIL, "1/3 ready")):
            await process_verifications(monitor, findings=[])
        assert "a-1" in monitor._pending_verifications, "grace window should keep the probe pending"
        assert payload["grace_scans"] == 1
        assert payload["target_scan"] == 6

    async def test_probe_pass_records_verified(self):
        from sre_agent.monitor.verification_pipeline import process_verifications

        payload = {
            "action_id": "a-1",
            "finding_id": "",
            "category": "chat_action",
            "resources": [],
            "verify_resources": [],
            "target_scan": 5,
            "probe": {"tool": "restart_deployment", "args": {"namespace": "p", "name": "api"}, "pre": {}},
            "grace_scans": 2,
        }
        monitor = await self._monitor_with(payload)
        with (
            patch("sre_agent.tool_contracts.run_probe", return_value=(health_gate.PASS, "3/3 ready")),
            patch("sre_agent.monitor.verification_pipeline.get_monitor_repo") as repo_factory,
        ):
            repo = repo_factory.return_value
            repo.async_update_action_verification = _async_noop_fn()
            await process_verifications(monitor, findings=[])
        assert "a-1" not in monitor._pending_verifications
        args = repo.async_update_action_verification.calls[0]
        assert args[1] == "verified"
        assert "Postcondition probe passed" in args[2]

    async def test_grace_exhausted_records_still_failing(self):
        from sre_agent.monitor.verification_pipeline import process_verifications

        payload = {
            "action_id": "a-1",
            "finding_id": "",
            "category": "chat_action",
            "resources": [],
            "verify_resources": [],
            "target_scan": 5,
            "probe": {"tool": "restart_deployment", "args": {"namespace": "p", "name": "api"}, "pre": {}},
            "grace_scans": 0,
        }
        monitor = await self._monitor_with(payload)
        with (
            patch("sre_agent.tool_contracts.run_probe", return_value=(health_gate.FAIL, "1/3 ready")),
            patch("sre_agent.monitor.verification_pipeline.get_monitor_repo") as repo_factory,
        ):
            repo = repo_factory.return_value
            repo.async_update_action_verification = _async_noop_fn()
            await process_verifications(monitor, findings=[])
        assert "a-1" not in monitor._pending_verifications
        args = repo.async_update_action_verification.calls[0]
        assert args[1] == "still_failing"


async def _async_noop(*args, **kwargs):
    return None


def _async_noop_fn():
    class _Recorder:
        def __init__(self):
            self.calls = []

        async def __call__(self, *args, **kwargs):
            self.calls.append(args)
            return None

    return _Recorder()
