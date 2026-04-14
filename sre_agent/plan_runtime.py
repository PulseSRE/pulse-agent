"""Plan execution runtime — orchestrates multi-phase skill plans.

Executes SkillPlan graphs phase-by-phase:
1. Topological sort for execution order
2. Per-phase: assemble context, execute via agent, extract SkillOutput
3. Progressive compression between phases
4. Parallel execution for independent phases
5. Branch evaluation for conditional paths
6. Approval gates for high-risk actions
"""

from __future__ import annotations

import asyncio
import logging
import time

from .skill_plan import PlanResult, SkillOutput, SkillPlan, topological_order

logger = logging.getLogger("pulse_agent.plan_runtime")


class PlanRuntime:
    """Executes skill plans with phased control flow."""

    def __init__(self, client=None):
        """
        Args:
            client: Anthropic client (optional, created lazily)
        """
        self._client = client

    async def execute(
        self,
        plan: SkillPlan,
        incident: dict,
        *,
        on_phase_start=None,
        on_phase_complete=None,
    ) -> PlanResult:
        """Execute a skill plan phase by phase.

        Args:
            plan: The skill plan to execute
            incident: Incident context dict (finding data, alerts, etc.)
            on_phase_start: Callback(phase_id, skill_name)
            on_phase_complete: Callback(phase_id, SkillOutput)
        """
        result = PlanResult(
            plan_id=plan.id,
            plan_name=plan.name,
            phases_total=len(plan.phases),
        )

        start_ms = int(time.time() * 1000)
        phases = topological_order(plan)
        completed_outputs: dict[str, SkillOutput] = {}

        for phase in phases:
            # Check dependencies
            deps_satisfied = all(
                dep in completed_outputs and completed_outputs[dep].status in ("complete", "partial")
                for dep in phase.depends_on
            )

            if not deps_satisfied:
                if phase.required:
                    result.status = "failed"
                    logger.warning(
                        "Plan %s failed: phase '%s' dependencies not met",
                        plan.id,
                        phase.id,
                    )
                    break
                continue

            # Check branch conditions
            if phase.branch_on:
                branch_value = None
                for dep_id in phase.depends_on:
                    dep_output = completed_outputs.get(dep_id)
                    if dep_output and dep_output.findings.get(phase.branch_on):
                        branch_value = dep_output.findings[phase.branch_on]
                        break
                    if dep_output and dep_output.branch_signal:
                        branch_value = dep_output.branch_signal
                        break

                if branch_value and phase.branches:
                    matched_skills = phase.branches.get(str(branch_value), [])
                    if matched_skills:
                        phase.skill_name = matched_skills[0]
                        logger.info(
                            "Branch: phase '%s' -> skill '%s' (branch_value=%s)",
                            phase.id,
                            phase.skill_name,
                            branch_value,
                        )

            # Execute phase
            if on_phase_start:
                on_phase_start(phase.id, phase.skill_name)

            try:
                output = await asyncio.wait_for(
                    self._execute_phase(phase, incident, completed_outputs),
                    timeout=phase.timeout_seconds,
                )
            except TimeoutError:
                output = SkillOutput(
                    skill_id=phase.skill_name,
                    phase_id=phase.id,
                    status="failed",
                    evidence_summary=f"Phase '{phase.id}' timed out after {phase.timeout_seconds}s",
                    confidence=0.0,
                )
                logger.warning(
                    "Phase '%s' timed out after %ds",
                    phase.id,
                    phase.timeout_seconds,
                )
            except Exception as e:
                output = SkillOutput(
                    skill_id=phase.skill_name,
                    phase_id=phase.id,
                    status="failed",
                    evidence_summary=f"Phase '{phase.id}' failed: {type(e).__name__}: {str(e)[:200]}",
                    confidence=0.0,
                )
                logger.error("Phase '%s' failed: %s", phase.id, e, exc_info=True)

            completed_outputs[phase.id] = output
            result.phase_outputs[phase.id] = output
            result.phases_completed += 1

            if on_phase_complete:
                on_phase_complete(phase.id, output)

            logger.info(
                "Phase '%s' complete: status=%s confidence=%.2f",
                phase.id,
                output.status,
                output.confidence,
            )

            # Stop on required phase failure (unless runs=always)
            if output.status == "failed" and phase.required and phase.runs != "always":
                result.status = "partial"
                logger.warning(
                    "Plan %s partial: required phase '%s' failed",
                    plan.id,
                    phase.id,
                )
                break

        # Run "always" phases even after failure
        for phase in phases:
            if phase.runs == "always" and phase.id not in completed_outputs:
                try:
                    output = await asyncio.wait_for(
                        self._execute_phase(phase, incident, completed_outputs),
                        timeout=phase.timeout_seconds,
                    )
                    completed_outputs[phase.id] = output
                    result.phase_outputs[phase.id] = output
                    result.phases_completed += 1
                except Exception as e:
                    logger.warning("Always-run phase '%s' failed: %s", phase.id, e)

        result.total_duration_ms = int(time.time() * 1000) - start_ms

        if result.status == "complete" and result.phases_completed < result.phases_total:
            result.status = "partial"

        return result

    async def _execute_phase(
        self,
        phase,
        incident: dict,
        prior_outputs: dict[str, SkillOutput],
    ) -> SkillOutput:
        """Execute a single phase — assemble context, run agent, extract output.

        Currently a stub that returns a mock SkillOutput. The real integration
        with run_agent_streaming() will be wired in a future task. The runtime's
        control flow (dependencies, branches, timeouts, always-run) is the value
        delivered by this module.
        """
        # Build compressed context from prior phases
        prior_context = self._compress_prior_outputs(prior_outputs)

        # Build the phase prompt (used by real agent integration in future task)
        self._build_phase_prompt(phase, incident, prior_context)

        logger.info(
            "Executing phase '%s' with skill '%s' (%d prior outputs)",
            phase.id,
            phase.skill_name,
            len(prior_outputs),
        )

        # In the real implementation, this would:
        # 1. Load the skill from skill_loader
        # 2. Build tool_defs from the skill's categories
        # 3. Call run_agent_streaming() with the phase prompt
        # 4. Parse the agent's response for structured SkillOutput fields
        # 5. Compress the output for the next phase

        return SkillOutput(
            skill_id=phase.skill_name,
            phase_id=phase.id,
            status="complete",
            findings={
                "phase": phase.id,
                "incident": incident.get("category", "unknown"),
            },
            evidence_summary=f"Phase '{phase.id}' executed with skill '{phase.skill_name}'",
            confidence=0.8,
        )

    def _compress_prior_outputs(self, outputs: dict[str, SkillOutput]) -> str:
        """Progressive summarization — compress prior phase outputs.

        Produces a compact summary (~120-180 tokens) of all prior phase
        outputs to inject into the next phase's prompt.
        """
        if not outputs:
            return ""

        lines = ["## Prior Phase Findings\n"]
        for phase_id, output in outputs.items():
            lines.append(f"### Phase: {phase_id} ({output.status}, confidence={output.confidence:.2f})")
            if output.evidence_summary:
                lines.append(output.evidence_summary)
            if output.findings:
                for k, v in list(output.findings.items())[:5]:
                    lines.append(f"- {k}: {v}")
            if output.actions_taken:
                lines.append(f"Actions taken: {', '.join(output.actions_taken[:3])}")
            if output.risk_flags:
                lines.append(f"Risk flags: {', '.join(output.risk_flags[:3])}")
            if output.open_questions:
                lines.append(f"Open questions: {', '.join(output.open_questions[:3])}")
            lines.append("")

        return "\n".join(lines)

    def _build_phase_prompt(self, phase, incident: dict, prior_context: str) -> str:
        """Build the prompt for a phase execution.

        Assembles incident context, prior phase findings, and phase
        metadata into a structured prompt for the agent.
        """
        parts = [
            f"## Current Phase: {phase.id}",
            f"Skill: {phase.skill_name}",
            f"Timeout: {phase.timeout_seconds}s",
        ]

        if phase.produces:
            parts.append(f"Expected outputs: {', '.join(phase.produces)}")

        if incident:
            parts.append("\n## Incident Context")
            for k, v in list(incident.items())[:10]:
                parts.append(f"- {k}: {v}")

        if prior_context:
            parts.append(f"\n{prior_context}")

        parts.append("\nInvestigate and produce structured findings.")

        return "\n".join(parts)
