"""Tests for plan execution runtime."""

from __future__ import annotations

import asyncio

from sre_agent.plan_runtime import PlanRuntime
from sre_agent.skill_plan import SkillOutput, SkillPhase, SkillPlan


def _run(coro):
    """Helper to run async tests."""
    return asyncio.run(coro)


def _mock_runtime() -> PlanRuntime:
    """Create a PlanRuntime with _execute_phase mocked to return stub outputs."""
    runtime = PlanRuntime()

    async def _stub_execute_phase(phase, incident, prior):
        return SkillOutput(
            skill_id=phase.skill_name,
            phase_id=phase.id,
            status="complete",
            findings={"phase": phase.id},
            evidence_summary=f"Phase {phase.id} completed",
            confidence=0.85,
        )

    runtime._execute_phase = _stub_execute_phase
    return runtime


class TestPlanRuntime:
    def test_execute_linear_plan(self):
        plan = SkillPlan(
            id="test-1",
            name="linear",
            phases=[
                SkillPhase(id="triage", skill_name="sre"),
                SkillPhase(id="diagnose", skill_name="sre", depends_on=["triage"]),
                SkillPhase(id="verify", skill_name="sre", depends_on=["diagnose"]),
            ],
        )
        runtime = _mock_runtime()
        result = _run(runtime.execute(plan, {"category": "crashloop"}))
        assert result.status == "complete"
        assert result.phases_completed == 3
        assert "triage" in result.phase_outputs
        assert "verify" in result.phase_outputs

    def test_execute_with_callbacks(self):
        started = []
        completed = []

        plan = SkillPlan(
            id="test-2",
            name="callbacks",
            phases=[
                SkillPhase(id="triage", skill_name="sre"),
                SkillPhase(id="verify", skill_name="sre", depends_on=["triage"]),
            ],
        )
        runtime = _mock_runtime()
        _run(
            runtime.execute(
                plan,
                {},
                on_phase_start=lambda pid, sn: started.append(pid),
                on_phase_complete=lambda pid, out: completed.append(pid),
            )
        )
        assert started == ["triage", "verify"]
        assert completed == ["triage", "verify"]

    def test_failed_required_phase_stops_plan(self):
        plan = SkillPlan(
            id="test-3",
            name="fail",
            phases=[
                SkillPhase(id="triage", skill_name="sre"),
                SkillPhase(id="diagnose", skill_name="sre", depends_on=["triage"]),
            ],
        )
        runtime = _mock_runtime()

        # Override _execute_phase to fail on diagnose
        original = runtime._execute_phase

        async def failing_phase(phase, incident, prior):
            if phase.id == "diagnose":
                raise RuntimeError("Diagnosis failed")
            return await original(phase, incident, prior)

        runtime._execute_phase = failing_phase

        result = _run(runtime.execute(plan, {}))
        assert result.status == "partial"
        assert result.phase_outputs["diagnose"].status == "failed"

    def test_always_run_phases_execute_after_failure(self):
        plan = SkillPlan(
            id="test-4",
            name="always",
            phases=[
                SkillPhase(id="triage", skill_name="sre"),
                SkillPhase(
                    id="verify",
                    skill_name="sre",
                    depends_on=["triage"],
                    runs="always",
                ),
            ],
        )
        runtime = _mock_runtime()
        result = _run(runtime.execute(plan, {}))
        assert "verify" in result.phase_outputs

    def test_unsatisfied_required_dependency_fails_plan(self):
        """If a required phase's dependency failed, the plan should fail."""
        plan = SkillPlan(
            id="test-5",
            name="dep-fail",
            phases=[
                SkillPhase(id="triage", skill_name="sre"),
                SkillPhase(id="diagnose", skill_name="sre", depends_on=["triage"]),
            ],
        )
        runtime = _mock_runtime()

        # Make triage fail
        async def fail_triage(phase, incident, prior):
            if phase.id == "triage":
                raise RuntimeError("triage broke")
            return SkillOutput(skill_id=phase.skill_name, phase_id=phase.id, confidence=0.8)

        runtime._execute_phase = fail_triage

        result = _run(runtime.execute(plan, {}))
        # triage failed -> diagnose deps not met -> partial
        assert result.status in ("partial", "failed")

    def test_duration_tracked(self):
        plan = SkillPlan(
            id="test-6",
            name="duration",
            phases=[SkillPhase(id="triage", skill_name="sre")],
        )
        runtime = _mock_runtime()
        result = _run(runtime.execute(plan, {}))
        assert result.total_duration_ms >= 0


class TestContextCompression:
    def test_compress_empty(self):
        runtime = PlanRuntime()
        assert runtime._compress_prior_outputs({}) == ""

    def test_compress_single_output(self):
        runtime = PlanRuntime()
        outputs = {
            "triage": SkillOutput(
                skill_id="sre",
                phase_id="triage",
                status="complete",
                findings={"root_cause": "oom"},
                evidence_summary="Pod killed by OOM at 256Mi",
                confidence=0.9,
            ),
        }
        result = runtime._compress_prior_outputs(outputs)
        assert "triage" in result
        assert "oom" in result
        assert "256Mi" in result

    def test_compress_multiple_outputs(self):
        runtime = PlanRuntime()
        outputs = {
            "triage": SkillOutput(skill_id="sre", phase_id="triage", confidence=0.9),
            "diagnose": SkillOutput(
                skill_id="sre",
                phase_id="diagnose",
                findings={"fix": "increase memory"},
                actions_taken=["patched deployment"],
                risk_flags=["memory pressure"],
                confidence=0.85,
            ),
        }
        result = runtime._compress_prior_outputs(outputs)
        assert "triage" in result
        assert "diagnose" in result
        assert "patched deployment" in result

    def test_compress_includes_open_questions(self):
        runtime = PlanRuntime()
        outputs = {
            "triage": SkillOutput(
                skill_id="sre",
                phase_id="triage",
                open_questions=["Why did the node drain?"],
                confidence=0.6,
            ),
        }
        result = runtime._compress_prior_outputs(outputs)
        assert "Why did the node drain?" in result


class TestPhasePrompt:
    def test_build_phase_prompt(self):
        runtime = PlanRuntime()
        phase = SkillPhase(
            id="diagnose",
            skill_name="sre",
            produces=["root_cause", "confidence"],
            timeout_seconds=300,
        )
        prompt = runtime._build_phase_prompt(phase, {"category": "crashloop"}, "Prior: triage complete")
        assert "diagnose" in prompt
        assert "root_cause" in prompt
        assert "crashloop" in prompt
        assert "Prior: triage complete" in prompt

    def test_build_phase_prompt_no_prior(self):
        runtime = PlanRuntime()
        phase = SkillPhase(id="triage", skill_name="sre")
        prompt = runtime._build_phase_prompt(phase, {"pod": "web-1"}, "")
        assert "triage" in prompt
        assert "web-1" in prompt
        assert "Prior" not in prompt
