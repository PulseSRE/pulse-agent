"""Tests for intelligent auto-fix planning."""

from __future__ import annotations

from sre_agent.monitor.fix_planner import classify_root_cause, plan_fix


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
