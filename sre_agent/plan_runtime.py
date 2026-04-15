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
import json
import logging
import re
import time
import uuid

from .skill_plan import (
    PlanResult,
    SkillOutput,
    SkillPhase,
    SkillPlan,
    topological_order,
    validate_plan,
)

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

        Returns:
            PlanResult with phase outputs, status, and timing.
        """
        result = PlanResult(
            plan_id=plan.id,
            plan_name=plan.name,
            phases_total=len(plan.phases),
        )

        start_ms = int(time.time() * 1000)
        phases = topological_order(plan)
        completed_outputs: dict[str, SkillOutput] = {}

        # Group phases that can run in parallel (same depends_on set)
        remaining = list(phases)
        plan_failed = False

        while remaining and not plan_failed:
            # Find phases whose dependencies are all satisfied
            ready = []
            not_ready = []
            for phase in remaining:
                if phase.runs == "always":
                    not_ready.append(phase)
                    continue
                deps_ok = all(
                    dep in completed_outputs and completed_outputs[dep].status in ("complete", "partial")
                    for dep in phase.depends_on
                )
                if deps_ok:
                    ready.append(phase)
                else:
                    not_ready.append(phase)

            if not ready:
                # No phases can run — check if required phases are blocked
                blocked_required = [p for p in not_ready if p.required and p.runs != "always"]
                if blocked_required:
                    result.status = "failed"
                    logger.warning(
                        "Plan %s failed: phases %s blocked by failed dependencies",
                        plan.id,
                        [p.id for p in blocked_required],
                    )
                break

            # Apply branch conditions
            for phase in ready:
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

            # Check approval gates — skip phases requiring approval or using high-risk skills
            approved_ready = []
            for phase in ready:
                # Check skill risk level
                needs_approval = phase.approval_required
                try:
                    from .skill_loader import get_skill

                    skill_def = get_skill(phase.skill_name)
                    if skill_def and skill_def.risk_level == "high":
                        needs_approval = True
                except Exception:
                    pass

                if needs_approval:
                    logger.info("Phase '%s' requires approval — marking as needs_escalation", phase.id)
                    output = SkillOutput(
                        skill_id=phase.skill_name,
                        phase_id=phase.id,
                        status="needs_escalation",
                        evidence_summary=f"Phase '{phase.id}' requires human approval before execution",
                        confidence=0.0,
                    )
                    completed_outputs[phase.id] = output
                    result.phase_outputs[phase.id] = output
                    result.phases_completed += 1
                    if on_phase_complete:
                        r = on_phase_complete(phase.id, output)
                        if asyncio.iscoroutine(r):
                            await r
                else:
                    approved_ready.append(phase)
            ready = approved_ready
            if not ready:
                remaining = not_ready
                continue

            # Execute ready phases — parallel if multiple, sequential if one
            async def _run_one(p: SkillPhase) -> tuple[str, SkillOutput]:
                if on_phase_start:
                    result_cb = on_phase_start(p.id, p.skill_name)
                    if asyncio.iscoroutine(result_cb):
                        await result_cb
                try:
                    out = await asyncio.wait_for(
                        self._execute_phase(p, incident, completed_outputs),
                        timeout=p.timeout_seconds,
                    )
                except TimeoutError:
                    out = SkillOutput(
                        skill_id=p.skill_name,
                        phase_id=p.id,
                        status="failed",
                        evidence_summary=f"Phase '{p.id}' timed out after {p.timeout_seconds}s",
                        confidence=0.0,
                    )
                    logger.warning("Phase '%s' timed out after %ds", p.id, p.timeout_seconds)
                except Exception as e:
                    out = SkillOutput(
                        skill_id=p.skill_name,
                        phase_id=p.id,
                        status="failed",
                        evidence_summary=f"Phase '{p.id}' failed: {type(e).__name__}: {str(e)[:200]}",
                        confidence=0.0,
                    )
                    logger.error("Phase '%s' failed: %s", p.id, e, exc_info=True)
                return p.id, out

            if len(ready) > 1:
                logger.info("Running %d phases in parallel: %s", len(ready), [p.id for p in ready])
                outputs = await asyncio.gather(*[_run_one(p) for p in ready])
            else:
                outputs = [await _run_one(ready[0])]

            for phase_id, output in outputs:
                completed_outputs[phase_id] = output
                result.phase_outputs[phase_id] = output
                result.phases_completed += 1
                if on_phase_complete:
                    r = on_phase_complete(phase_id, output)
                    if asyncio.iscoroutine(r):
                        await r
                logger.info(
                    "Phase '%s' complete: status=%s confidence=%.2f", phase_id, output.status, output.confidence
                )

                # Stop on required phase failure
                phase_obj = next((p for p in ready if p.id == phase_id), None)
                if output.status == "failed" and phase_obj and phase_obj.required:
                    result.status = "partial"
                    logger.warning("Plan %s partial: required phase '%s' failed", plan.id, phase_id)
                    plan_failed = True

            remaining = not_ready

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
        """Execute a single phase — load skill, call agent, parse output."""
        prior_context = self._compress_prior_outputs(prior_outputs)
        prompt = self._build_phase_prompt(phase, incident, prior_context)

        logger.info(
            "Executing phase '%s' with skill '%s' (%d prior outputs)",
            phase.id,
            phase.skill_name,
            len(prior_outputs),
        )

        try:
            from .agent import create_client, run_agent_streaming
            from .skill_loader import build_config_from_skill, get_skill

            skill = get_skill(phase.skill_name)
            if not skill:
                return SkillOutput(
                    skill_id=phase.skill_name,
                    phase_id=phase.id,
                    status="failed",
                    evidence_summary=f"Unknown skill: {phase.skill_name}",
                )

            config = build_config_from_skill(skill, query=prompt)
            client = self._client or create_client()

            tools_called: list[str] = []

            def on_tool(name):
                tools_called.append(name)

            response = await asyncio.to_thread(
                run_agent_streaming,
                client,
                [{"role": "user", "content": prompt}],
                config["system_prompt"],
                config["tool_defs"],
                config["tool_map"],
                config.get("write_tools", set()),
                on_tool_use=on_tool,
                mode=phase.skill_name,
            )

            # Try to extract structured SkillOutput from response
            output = self._parse_skill_output(response, phase, tools_called)
            return output

        except Exception as e:
            logger.error("Phase '%s' execution failed: %s", phase.id, e, exc_info=True)
            return SkillOutput(
                skill_id=phase.skill_name,
                phase_id=phase.id,
                status="failed",
                evidence_summary=f"Execution error: {type(e).__name__}: {str(e)[:200]}",
            )

    def _parse_skill_output(self, response: str, phase, tools_called: list[str]) -> SkillOutput:
        """Parse agent response into SkillOutput — extract JSON if present, otherwise use raw text."""
        import json
        import re

        # Try to find structured JSON in response
        match = re.search(r"```json\s*(\{.*?\})\s*```", response, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
                return SkillOutput(
                    skill_id=phase.skill_name,
                    phase_id=phase.id,
                    status=data.get("status", "complete"),
                    findings=data.get("findings", {}),
                    branch_signal=data.get("branch_signal"),
                    evidence_summary=data.get("evidence_summary", response[:300]),
                    actions_taken=data.get("actions_taken", tools_called),
                    open_questions=data.get("open_questions", []),
                    risk_flags=data.get("risk_flags", []),
                    confidence=float(data.get("confidence", 0.7)),
                )
            except (json.JSONDecodeError, ValueError):
                pass

        # Fallback: use raw response text
        return SkillOutput(
            skill_id=phase.skill_name,
            phase_id=phase.id,
            status="complete",
            findings={"raw_response": response[:500]},
            evidence_summary=response[:300],
            actions_taken=tools_called,
            confidence=0.7,
        )

    def _compress_prior_outputs(self, outputs: dict[str, SkillOutput]) -> str:
        """Progressive summarization — compress prior phase outputs.

        Produces a compact Markdown summary of prior phase findings to inject
        into the next phase's prompt. Targets ~120-180 tokens to keep context
        budgets under control across long plans.

        Args:
            outputs: Map of phase_id to SkillOutput from completed phases.

        Returns:
            Markdown string summarizing prior outputs, or empty string if none.
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

    def _build_phase_prompt(
        self,
        phase,
        incident: dict,
        prior_context: str,
    ) -> str:
        """Build the prompt for a phase execution.

        Args:
            phase: The SkillPhase to build a prompt for.
            incident: Incident context dict.
            prior_context: Compressed prior phase output string.

        Returns:
            Formatted prompt string for the phase.
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

        # Inject few-shot examples from skill definition
        try:
            from .skill_loader import get_skill

            skill_def = get_skill(phase.skill_name)
            if skill_def and skill_def.examples:
                parts.append("\n## Examples (correct vs wrong approach)")
                for ex in skill_def.examples[:2]:
                    parts.append(f"Scenario: {ex.get('scenario', '')}")
                    parts.append(f"  Correct: {ex.get('correct', '')}")
                    parts.append(f"  Wrong: {ex.get('wrong', '')}")
            if skill_def and skill_def.success_criteria:
                parts.append(f"\nSuccess criteria: {skill_def.success_criteria}")
        except Exception:
            pass

        parts.append("\nInvestigate and produce structured findings.")

        return "\n".join(parts)


def extract_plan_from_response(response: str) -> SkillPlan | None:
    """Extract a SkillPlan from an agent's JSON response.

    Looks for a JSON code block containing plan phases.
    Returns None if no valid plan found.
    """
    # Find JSON code block
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", response, re.DOTALL)
    if not match:
        # Try bare JSON
        match = re.search(r"\{[^{}]*\"phases\"[^{}]*\[.*?\]\s*\}", response, re.DOTALL)

    if not match:
        return None

    try:
        data = json.loads(match.group(1) if match.lastindex else match.group(0))
    except json.JSONDecodeError:
        return None

    if "phases" not in data:
        return None

    phases: list[SkillPhase] = []
    for p in data["phases"]:
        phases.append(
            SkillPhase(
                id=p.get("id", f"phase-{len(phases)}"),
                skill_name=p.get("skill_name", "sre"),
                required=p.get("required", True),
                depends_on=p.get("depends_on", []),
                timeout_seconds=p.get("timeout_seconds", 120),
                produces=p.get("produces", []),
                branch_on=p.get("branch_on"),
                branches=p.get("branches", {}),
                parallel_with=p.get("parallel_with"),
                approval_required=p.get("approval_required", False),
                runs=p.get("runs", "on_success"),
            )
        )

    plan = SkillPlan(
        id=f"dynamic-{uuid.uuid4().hex[:8]}",
        name=data.get("plan_name", "Dynamic Plan"),
        phases=phases,
        incident_type=data.get("incident_type", "unknown"),
        max_total_duration=data.get("max_total_duration", 1800),
        generated_by="auto",
        reviewed=False,
    )

    errors = validate_plan(plan)
    if errors:
        logger.warning("Dynamic plan validation failed: %s", errors)
        return None

    logger.info("Extracted dynamic plan: %s (%d phases)", plan.name, len(plan.phases))
    return plan
