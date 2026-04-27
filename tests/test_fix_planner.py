"""Tests for intelligent auto-fix planning."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sre_agent.monitor.fix_planner import (
    FixPlan,
    classify_root_cause,
    execute_fix,
    get_investigation_for_finding,
    plan_fix,
)


class TestClassifyRootCause:
    def test_bad_image_tag(self):
        cause = "The image registry.example.com/app:v999 does not exist"
        assert classify_root_cause(cause) == "bad_image"

    def test_missing_configmap(self):
        cause = "ConfigMap my-config not found in namespace production"
        assert classify_root_cause(cause) == "missing_config"

    def test_oom_killed(self):
        cause = "Container exceeded memory limit of 256Mi and was OOMKilled"
        assert classify_root_cause(cause) == "oom"

    def test_readiness_probe_failure(self):
        cause = "Readiness probe failed: connection refused on port 8080"
        assert classify_root_cause(cause) == "probe_failure"

    def test_resource_quota_exceeded(self):
        cause = "pods quota exceeded in namespace staging"
        assert classify_root_cause(cause) == "quota_exceeded"

    def test_unknown_cause(self):
        cause = "Something unexpected happened"
        assert classify_root_cause(cause) == "unknown"

    def test_empty_cause(self):
        assert classify_root_cause("") == "unknown"


class TestPlanFix:
    def test_bad_image_returns_patch_strategy(self):
        investigation = {
            "suspectedCause": "Image app:v999 does not exist in the registry",
            "recommendedFix": "Update the image to app:v2.1.0",
            "confidence": 0.95,
        }
        finding = {
            "category": "image_pull",
            "resources": [{"kind": "Pod", "name": "app-abc", "namespace": "prod"}],
        }
        plan = plan_fix(investigation, finding)
        assert plan is not None
        assert plan.strategy == "patch_image"
        assert plan.confidence >= 0.5

    def test_oom_returns_patch_resources(self):
        investigation = {
            "suspectedCause": "Container exceeded memory limit of 256Mi",
            "recommendedFix": "Increase memory limit to 512Mi",
            "confidence": 0.9,
        }
        finding = {
            "category": "crashloop",
            "resources": [{"kind": "Deployment", "name": "api", "namespace": "prod"}],
        }
        plan = plan_fix(investigation, finding)
        assert plan is not None
        assert plan.strategy == "patch_resources"

    def test_unknown_cause_returns_none(self):
        investigation = {
            "suspectedCause": "Something unclear happened",
            "recommendedFix": "Check the logs",
            "confidence": 0.3,
        }
        finding = {
            "category": "crashloop",
            "resources": [{"kind": "Pod", "name": "x", "namespace": "default"}],
        }
        plan = plan_fix(investigation, finding)
        assert plan is None

    def test_low_confidence_returns_none(self):
        investigation = {
            "suspectedCause": "Image might be wrong",
            "recommendedFix": "Try a different tag",
            "confidence": 0.3,
        }
        finding = {
            "category": "image_pull",
            "resources": [{"kind": "Pod", "name": "x", "namespace": "default"}],
        }
        plan = plan_fix(investigation, finding)
        assert plan is None


class TestExecuteFix:
    @patch("sre_agent.monitor.fix_planner.get_apps_client")
    @patch("sre_agent.monitor.fix_planner.get_core_client")
    def test_patch_image_rolls_back(self, mock_core_fn, mock_apps_fn):
        core = MagicMock()
        apps = MagicMock()
        mock_core_fn.return_value = core
        mock_apps_fn.return_value = apps

        # Pod with owner
        pod = MagicMock()
        pod.spec.containers = [MagicMock(name="app", image="app:v999")]
        pod.metadata.owner_references = [MagicMock(kind="ReplicaSet", name="app-rs")]
        core.read_namespaced_pod.return_value = pod

        # RS -> Deployment
        rs = MagicMock()
        rs.metadata.owner_references = [MagicMock(kind="Deployment", name="app")]
        apps.read_namespaced_replica_set.return_value = rs

        # Deployment
        dep = MagicMock()
        dep.metadata.annotations = {"deployment.kubernetes.io/revision": "3"}
        dep.spec.selector.match_labels = {"app": "myapp"}
        apps.read_namespaced_deployment.return_value = dep

        # Previous RS (revision 2)
        prev_rs = MagicMock()
        prev_rs.metadata.annotations = {"deployment.kubernetes.io/revision": "2"}
        prev_rs.spec.template.spec.containers = [MagicMock(name="app", image="app:v2.0")]
        apps.list_namespaced_replica_set.return_value = MagicMock(items=[prev_rs])

        plan = FixPlan(
            strategy="patch_image",
            cause_category="bad_image",
            confidence=0.95,
            description="rollback",
            params={
                "suspected_cause": "bad image",
                "recommended_fix": "rollback",
                "resources": [{"kind": "Pod", "name": "app-abc", "namespace": "prod"}],
            },
        )
        tool, _before, after = execute_fix(plan)
        assert tool == "rollback_deployment"
        assert "app:v2.0" in after
        apps.patch_namespaced_deployment.assert_called_once()

    @patch("sre_agent.monitor.fix_planner.get_apps_client")
    def test_patch_resources_doubles_memory(self, mock_apps_fn):
        apps = MagicMock()
        mock_apps_fn.return_value = apps

        dep = MagicMock()
        container = MagicMock()
        container.name = "app"
        container.resources.limits = {"memory": "256Mi"}
        dep.spec.template.spec.containers = [container]
        dep.metadata.annotations = {}
        apps.read_namespaced_deployment.return_value = dep

        plan = FixPlan(
            strategy="patch_resources",
            cause_category="oom",
            confidence=0.9,
            description="increase memory",
            params={
                "suspected_cause": "OOM",
                "recommended_fix": "increase",
                "resources": [{"kind": "Deployment", "name": "api", "namespace": "prod"}],
            },
        )
        tool, _before, after = execute_fix(plan)
        assert tool == "patch_resources"
        assert "512Mi" in after
        apps.patch_namespaced_deployment.assert_called_once()

    def test_unknown_strategy_raises(self):
        plan = FixPlan(
            strategy="teleport",
            cause_category="unknown",
            confidence=0.99,
            description="impossible",
            params={"resources": []},
        )
        with pytest.raises(ValueError, match="teleport"):
            execute_fix(plan)

    def test_noop_strategy_returns_skip(self):
        plan = FixPlan(
            strategy="create_configmap",
            cause_category="missing_config",
            confidence=0.9,
            description="create configmap",
            params={"resources": []},
        )
        tool, _before, _after = execute_fix(plan)
        assert tool == "skip"


class TestGetInvestigation:
    @patch("sre_agent.repositories.monitor_repo.get_monitor_repo")
    def test_returns_latest_investigation(self, mock_get_repo):
        repo = MagicMock()
        repo.get_investigation_for_finding.return_value = {
            "suspected_cause": "Image does not exist",
            "recommended_fix": "Roll back",
            "confidence": 0.95,
        }
        mock_get_repo.return_value = repo

        result = get_investigation_for_finding("f-abc123")
        assert result is not None
        assert result["suspected_cause"] == "Image does not exist"

    @patch("sre_agent.repositories.monitor_repo.get_monitor_repo")
    def test_returns_none_when_no_investigation(self, mock_get_repo):
        repo = MagicMock()
        repo.get_investigation_for_finding.return_value = None
        mock_get_repo.return_value = repo

        result = get_investigation_for_finding("f-nonexistent")
        assert result is None

    @patch("sre_agent.repositories.monitor_repo.get_monitor_repo")
    def test_returns_none_on_db_error(self, mock_get_repo):
        mock_get_repo.side_effect = Exception("DB down")
        result = get_investigation_for_finding("f-abc")
        assert result is None
