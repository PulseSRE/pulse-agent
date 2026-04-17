"""Tests for auto-generated eval scenarios from skill scaffolding."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import pytest

from sre_agent.eval_scaffolder import (
    scaffold_eval_from_investigation,
    scaffold_eval_from_plan,
)


@dataclass
class FakeSkillOutput:
    status: str = "completed"
    findings: dict = field(default_factory=dict)
    evidence_summary: str = ""
    actions_taken: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    confidence: float = 0.9
    branch_signal: str = ""


@dataclass
class FakePlanResult:
    plan_id: str = "test-plan"
    plan_name: str = "Test Plan"
    status: str = "complete"
    phase_outputs: dict = field(default_factory=dict)
    total_duration_ms: int = 45000
    phases_completed: int = 3
    phases_total: int = 3


@pytest.fixture()
def scaffold_dirs(tmp_path: Path):
    scenarios_dir = tmp_path / "scenarios_data"
    fixtures_dir = tmp_path / "fixtures"
    scenarios_dir.mkdir()
    fixtures_dir.mkdir()

    with (
        patch("sre_agent.eval_scaffolder._SCENARIOS_DIR", scenarios_dir),
        patch("sre_agent.eval_scaffolder._FIXTURES_DIR", fixtures_dir),
        patch("sre_agent.eval_scaffolder._SUITE_FILE", scenarios_dir / "scaffolded.json"),
    ):
        yield scenarios_dir, fixtures_dir


def _make_plan_result(**overrides) -> FakePlanResult:
    diagnose = FakeSkillOutput(
        evidence_summary="Container exceeded memory limit",
        findings={"root_cause": "OOM kill due to memory leak"},
        actions_taken=["describe_pod", "get_pod_logs"],
    )
    verify = FakeSkillOutput(
        status="completed",
        evidence_summary="Pod stable after patch",
        actions_taken=["list_pods"],
    )
    defaults = {
        "phase_outputs": {"diagnose": diagnose, "verify": verify},
        "total_duration_ms": 45000,
    }
    defaults.update(overrides)
    return FakePlanResult(**defaults)


def _make_finding(**overrides) -> dict:
    defaults = {
        "id": "f-001",
        "title": "OOM killed pods in production",
        "category": "oom",
        "severity": "critical",
    }
    defaults.update(overrides)
    return defaults


class TestScaffoldEvalFromPlan:
    def test_creates_scenario_and_fixture(self, scaffold_dirs):
        scenarios_dir, fixtures_dir = scaffold_dirs

        result = scaffold_eval_from_plan(
            skill_name="oom-api-server",
            finding=_make_finding(),
            plan_result=_make_plan_result(),
            tools_called=["describe_pod", "get_pod_logs", "patch_resource"],
            confidence=0.92,
            duration_seconds=45.0,
        )

        assert result is True

        suite_file = scenarios_dir / "scaffolded.json"
        assert suite_file.exists()
        suite = json.loads(suite_file.read_text())
        assert suite["suite_name"] == "scaffolded"
        assert len(suite["scenarios"]) == 1

        scenario = suite["scenarios"][0]
        assert scenario["scenario_id"] == "scaffolded_oom-api-server_oom"
        assert scenario["category"] == "sre"
        assert scenario["description"].startswith("Auto-generated:")
        assert scenario["tool_calls"] == ["describe_pod", "get_pod_logs", "patch_resource"]
        assert scenario["duration_seconds"] == 45.0
        assert scenario["verification_passed"] is True
        assert scenario["rollback_available"] is True
        assert scenario["expected"]["should_block_release"] is False

        fixture_file = fixtures_dir / "scaffolded_oom-api-server_oom.json"
        assert fixture_file.exists()
        fixture = json.loads(fixture_file.read_text())
        assert fixture["name"] == "scaffolded_oom-api-server_oom"
        assert fixture["prompt"] == "OOM killed pods in production"
        assert "should_mention" in fixture["expected"]
        assert "should_use_tools" in fixture["expected"]

    def test_deduplication_by_scenario_id(self, scaffold_dirs):
        scenarios_dir, _ = scaffold_dirs

        for _ in range(2):
            scaffold_eval_from_plan(
                skill_name="oom-api-server",
                finding=_make_finding(),
                plan_result=_make_plan_result(),
                tools_called=["describe_pod"],
                confidence=0.9,
                duration_seconds=30.0,
            )

        suite = json.loads((scenarios_dir / "scaffolded.json").read_text())
        assert len(suite["scenarios"]) == 1

    def test_bootstrap_creates_suite_file(self, scaffold_dirs):
        scenarios_dir, _ = scaffold_dirs
        suite_file = scenarios_dir / "scaffolded.json"
        assert not suite_file.exists()

        scaffold_eval_from_plan(
            skill_name="test-skill",
            finding=_make_finding(category="crashloop"),
            plan_result=_make_plan_result(),
            tools_called=["list_pods"],
            confidence=0.8,
            duration_seconds=20.0,
        )

        assert suite_file.exists()
        suite = json.loads(suite_file.read_text())
        assert suite["suite_name"] == "scaffolded"
        assert suite["description"].startswith("Auto-generated")

    def test_path_traversal_sanitized(self, scaffold_dirs):
        scenarios_dir, _fixtures_dir = scaffold_dirs

        result = scaffold_eval_from_plan(
            skill_name="../../etc/passwd",
            finding=_make_finding(),
            plan_result=_make_plan_result(),
            tools_called=["list_pods"],
            confidence=0.8,
            duration_seconds=20.0,
        )

        # Sanitizer strips dangerous chars — the ID is safe
        assert result is True
        suite = json.loads((scenarios_dir / "scaffolded.json").read_text())
        scenario_id = suite["scenarios"][0]["scenario_id"]
        assert ".." not in scenario_id
        assert "/" not in scenario_id

    def test_empty_skill_name_rejected(self, scaffold_dirs):
        _scenarios_dir, _ = scaffold_dirs

        result = scaffold_eval_from_plan(
            skill_name="///",
            finding=_make_finding(),
            plan_result=_make_plan_result(),
            tools_called=["list_pods"],
            confidence=0.8,
            duration_seconds=20.0,
        )

        assert result is False

    def test_evidence_capped_at_500_chars(self, scaffold_dirs):
        _, fixtures_dir = scaffold_dirs
        long_evidence = "A" * 2000

        diagnose = FakeSkillOutput(
            evidence_summary=long_evidence,
            findings={"root_cause": "memory leak"},
            actions_taken=["describe_pod"],
        )
        plan_result = _make_plan_result(phase_outputs={"diagnose": diagnose})

        scaffold_eval_from_plan(
            skill_name="long-evidence",
            finding=_make_finding(),
            plan_result=plan_result,
            tools_called=["describe_pod"],
            confidence=0.9,
            duration_seconds=30.0,
        )

        fixture_file = fixtures_dir / "scaffolded_long-evidence_oom.json"
        assert fixture_file.exists()
        fixture = json.loads(fixture_file.read_text())
        for response in fixture["recorded_responses"].values():
            assert len(response) <= 500


class TestScaffoldEvalFromInvestigation:
    def test_creates_scenario_only(self, scaffold_dirs):
        scenarios_dir, fixtures_dir = scaffold_dirs

        result = scaffold_eval_from_investigation(
            skill_name="node-pressure",
            finding=_make_finding(category="nodes", title="Node memory pressure detected"),
            investigation_result={
                "summary": "Node worker-2 is under memory pressure",
                "suspectedCause": "Too many pods scheduled",
                "confidence": 0.85,
            },
        )

        assert result is True

        suite = json.loads((scenarios_dir / "scaffolded.json").read_text())
        assert len(suite["scenarios"]) == 1
        scenario = suite["scenarios"][0]
        assert scenario["scenario_id"] == "scaffolded_node-pressure_nodes"
        assert scenario["tool_calls"] == ["proactive_investigation"]
        assert scenario["verification_passed"] is None
        assert scenario["expected"]["should_block_release"] is False

        # No fixture for flat investigation path
        fixture_files = list(fixtures_dir.glob("scaffolded_node-pressure*"))
        assert len(fixture_files) == 0
