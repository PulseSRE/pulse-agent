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
        self._write_mutex = asyncio.Lock()  # serializes write operations across parallel phases

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
                    logger.debug("Could not check risk level for skill '%s'", phase.skill_name, exc_info=True)

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

                out: SkillOutput | None = None
                for attempt in range(max(p.retry_limit, 1)):
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
                        break  # don't retry timeouts
                    except Exception as e:
                        out = SkillOutput(
                            skill_id=p.skill_name,
                            phase_id=p.id,
                            status="failed",
                            evidence_summary=f"Phase '{p.id}' failed: {type(e).__name__}: {str(e)[:200]}",
                            confidence=0.0,
                        )
                        logger.error("Phase '%s' failed (attempt %d/%d): %s", p.id, attempt + 1, p.retry_limit, e)
                        if attempt + 1 < p.retry_limit:
                            continue  # retry
                        break

                    # Check success_condition if defined
                    if p.success_condition and out.status == "complete":
                        if not self._check_success_condition(p.success_condition, out):
                            if attempt + 1 < p.retry_limit:
                                logger.info(
                                    "Phase '%s' success condition not met, retrying (%d/%d)",
                                    p.id,
                                    attempt + 1,
                                    p.retry_limit,
                                )
                                continue
                            out.status = "partial"
                            out.evidence_summary += f" (success condition not met: {p.success_condition})"
                    break  # success or exhausted retries

                # Store full reasoning trace to DB (compressed output goes to next phase)
                self._store_phase_trace(plan.id, p.id, out)

                assert out is not None  # loop always assigns out
                return p.id, out

            if len(ready) > 1:
                logger.info("Running %d phases in parallel: %s", len(ready), [p.id for p in ready])
                # Race parallel phases — first high-confidence result cancels siblings
                outputs = await self._race_parallel(ready, _run_one)
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

        # Record plan execution for analytics
        self._record_execution(plan, result, incident)

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
            _owns_client = self._client is None
            client = self._client or create_client()

            tools_called: list[str] = []

            def on_tool(name):
                tools_called.append(name)

            from .tool_usage import build_tool_result_handler

            on_tool_result = build_tool_result_handler(
                session_id=f"plan-{phase.id}",
                agent_mode=f"pipeline:plan:{phase.skill_name}",
            )

            try:
                response = await asyncio.to_thread(
                    run_agent_streaming,
                    client,
                    [{"role": "user", "content": prompt}],
                    config["system_prompt"],
                    config["tool_defs"],
                    config["tool_map"],
                    config.get("write_tools", set()),
                    on_tool_use=on_tool,
                    on_tool_result=on_tool_result,
                    mode=phase.skill_name,
                )
            finally:
                if _owns_client:
                    try:
                        client.close()
                    except Exception:
                        pass

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
                    evidence_summary=data.get("evidence_summary", response[:2000]),
                    actions_taken=data.get("actions_taken", tools_called),
                    open_questions=data.get("open_questions", []),
                    risk_flags=data.get("risk_flags", []),
                    confidence=float(data.get("confidence", 0.7)),
                )
            except (json.JSONDecodeError, ValueError) as e:
                logger.debug("Could not parse structured output for phase '%s': %s", phase.id, e)

        # Fallback: use raw response text
        return SkillOutput(
            skill_id=phase.skill_name,
            phase_id=phase.id,
            status="complete",
            findings={"raw_response": response[:2000]},
            evidence_summary=response[:2000],
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

    async def _race_parallel(
        self,
        phases: list,
        run_fn,
    ) -> list[tuple[str, SkillOutput]]:
        """Race parallel phases — first high-confidence result cancels siblings.

        If any phase completes with confidence >= 0.85, cancel remaining tasks.
        All completed results are returned (cancelled phases get 'skipped' status).
        """
        tasks: dict[str, asyncio.Task] = {}
        for p in phases:
            task = asyncio.create_task(run_fn(p))
            tasks[p.id] = task

        results: list[tuple[str, SkillOutput]] = []
        winner_found = False

        # Wait for tasks as they complete
        pending = set(tasks.values())
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                phase_id, output = task.result()
                results.append((phase_id, output))

                # Check if this is a high-confidence winner
                if output.confidence >= 0.85 and output.status == "complete" and not winner_found:
                    winner_found = True
                    logger.info(
                        "Parallel winner: phase '%s' (confidence=%.2f) — cancelling %d siblings",
                        phase_id,
                        output.confidence,
                        len(pending),
                    )
                    # Cancel remaining tasks
                    for remaining in pending:
                        remaining.cancel()
                    # Collect cancelled results
                    for p in phases:
                        if p.id not in {r[0] for r in results}:
                            results.append(
                                (
                                    p.id,
                                    SkillOutput(
                                        skill_id=p.skill_name,
                                        phase_id=p.id,
                                        status="skipped",
                                        evidence_summary="Cancelled — sibling phase found answer first",
                                        confidence=0.0,
                                    ),
                                )
                            )
                    break
            if winner_found:
                break

        return results

    def _check_success_condition(self, condition: str, output: SkillOutput) -> bool:
        """Check if a phase's success condition is met.

        For PromQL conditions, queries Prometheus. For simple conditions,
        checks against the output findings.
        """
        if not condition:
            return True

        # Simple field checks: "confidence > 0.8"
        if "confidence" in condition:
            try:
                import re as _re

                match = _re.search(r"confidence\s*[><=]+\s*([\d.]+)", condition)
                if match:
                    threshold = float(match.group(1))
                    return output.confidence >= threshold
            except Exception:
                logger.debug("Failed to parse confidence condition '%s'", condition, exc_info=True)

        # PromQL conditions: "p99_latency < 500ms"
        try:
            from .k8s_tools.monitoring import get_prometheus_query

            result = get_prometheus_query(query=condition)
            if isinstance(result, str) and "error" not in result.lower():
                return True  # query succeeded = condition met
        except Exception:
            logger.debug("Success condition PromQL check failed for '%s'", condition, exc_info=True)

        # Default: consider met if output status is complete
        return output.status == "complete"

    def _record_execution(self, plan: SkillPlan, result: PlanResult, incident: dict) -> None:
        """Record plan execution to plan_executions table for analytics."""
        try:
            import json
            import uuid

            from .db import get_database

            db = get_database()

            phase_details = []
            for pid, out in result.phase_outputs.items():
                phase_details.append(
                    {
                        "phase_id": pid,
                        "status": out.status,
                        "confidence": out.confidence,
                        "tools_used": out.actions_taken[:5],
                        "evidence_length": len(out.evidence_summary) if out.evidence_summary else 0,
                    }
                )

            max_confidence = max((o.confidence for o in result.phase_outputs.values()), default=0)

            db.execute(
                "INSERT INTO plan_executions "
                "(id, template_id, template_name, incident_type, finding_id, status, "
                "phases_total, phases_completed, total_duration_ms, phase_details, confidence) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (
                    f"pe-{uuid.uuid4().hex[:12]}",
                    plan.id,
                    plan.name,
                    plan.incident_type,
                    incident.get("id", ""),
                    result.status,
                    result.phases_total,
                    result.phases_completed,
                    result.total_duration_ms,
                    json.dumps(phase_details),
                    max_confidence,
                ),
            )
            db.commit()
        except Exception:
            logger.debug("Failed to record plan execution", exc_info=True)

    def _store_phase_trace(self, plan_id: str, phase_id: str, output: SkillOutput | None) -> None:
        """Store full reasoning trace to DB for audit/replay."""
        if not output:
            return
        try:
            import json

            from .db import get_database

            db = get_database()
            db.execute(
                "INSERT INTO skill_selection_log "
                "(session_id, query_summary, selected_skill, threshold_used, "
                "selection_ms, channel_weights) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    f"plan:{plan_id}",
                    f"phase:{phase_id} status={output.status} confidence={output.confidence:.2f}",
                    output.skill_id,
                    0.0,
                    0,
                    json.dumps(
                        {
                            "phase_id": phase_id,
                            "status": output.status,
                            "findings": output.findings,
                            "evidence": output.evidence_summary[:2000],
                            "actions": output.actions_taken,
                            "risk_flags": output.risk_flags,
                            "confidence": output.confidence,
                        }
                    ),
                ),
            )
            db.commit()
        except Exception:
            logger.debug("Failed to store phase trace", exc_info=True)

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
            logger.debug("Could not inject skill examples for phase '%s'", phase.id, exc_info=True)

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


# ---------------------------------------------------------------------------
# Parallel multi-skill execution (per-turn, not plan-based)
# ---------------------------------------------------------------------------

from .synthesis import ParallelSkillResult


async def run_parallel_skills(
    primary,
    secondary,
    query: str,
    messages: list[dict],
    client,
    websocket=None,
    session_id: str = "",
) -> ParallelSkillResult:
    """Run two skills in parallel with progressive result streaming.

    Uses SkillExecutor for full event pipeline (tool recording, components,
    confirmation flow for primary, memory augmentation per-skill).
    """
    from .agent import create_client
    from .context_bus import get_context_bus
    from .skill_loader import build_config_from_skill

    _owns_client = client is None
    if _owns_client:
        client = create_client()

    start = time.monotonic()
    bus = get_context_bus()
    task_id = f"parallel-{uuid.uuid4().hex[:8]}"
    bus.start_buffering(task_id)

    primary_output = ""
    secondary_output = ""
    primary_tokens: dict[str, int] = {}
    secondary_tokens: dict[str, int] = {}
    primary_components: list[dict] = []
    secondary_components: list[dict] = []

    try:
        primary_config = build_config_from_skill(primary, query=query)
        secondary_config = build_config_from_skill(secondary, query=query)

        sec_write_tools = secondary_config["write_tools"]
        if primary_config["write_tools"] and sec_write_tools:
            sec_write_tools = set()

        if websocket is not None:
            from .api.agent_ws import SkillExecutor

            loop = asyncio.get_running_loop()
            executor = SkillExecutor(websocket, session_id, loop)

            primary_task = asyncio.create_task(
                executor.run(
                    primary_config,
                    messages,
                    client,
                    primary_config["write_tools"],
                    primary.name,
                    skill_tag=primary.name,
                )
            )
            secondary_task = asyncio.create_task(
                executor.run(
                    secondary_config, messages, client, sec_write_tools, secondary.name, skill_tag=secondary.name
                )
            )

            tasks = {primary_task: primary.name, secondary_task: secondary.name}
            results: dict[str, object] = {}

            try:
                remaining = set(tasks)
                deadline = asyncio.get_event_loop().time() + 120

                while remaining:
                    timeout_left = max(deadline - asyncio.get_event_loop().time(), 0)
                    done, remaining = await asyncio.wait(
                        remaining, return_when=asyncio.FIRST_COMPLETED, timeout=timeout_left
                    )

                    if not done and remaining:
                        logger.warning("Parallel skills timed out — cancelling remaining")
                        for t in remaining:
                            t.cancel()
                        break

                    for task in done:
                        skill_name = tasks[task]
                        try:
                            output = task.result()
                            results[skill_name] = output
                        except Exception as e:
                            logger.warning("Parallel skill %s failed: %s", skill_name, e)
                            results[skill_name] = None

                        try:
                            await websocket.send_json(
                                {"type": "skill_progress", "skill": skill_name, "status": "complete"}
                            )
                        except Exception:
                            pass

                        if remaining and output and output.text.strip():
                            try:
                                await websocket.send_json(
                                    {
                                        "type": "text_delta",
                                        "text": f"## {skill_name.upper()} Analysis\n\n{output.text}\n\n*{tasks[next(iter(remaining))]} analysis still running...*\n\n",
                                    }
                                )
                            except Exception:
                                pass

            except TimeoutError:
                for t in tasks:
                    if not t.done():
                        t.cancel()

            from .api.agent_ws import SkillOutput

            p_result = results.get(primary.name)
            s_result = results.get(secondary.name)
            p = p_result if isinstance(p_result, SkillOutput) else None
            s = s_result if isinstance(s_result, SkillOutput) else None
            primary_output = p.text if p else ""
            secondary_output = s.text if s else ""
            primary_tokens = p.token_usage if p else {}
            secondary_tokens = s.token_usage if s else {}
            primary_components = p.components if p else []
            secondary_components = s.components if s else []

        else:
            from .agent import run_agent_streaming

            async def _run_bare(config, skill_name, write_tools):
                try:
                    result = await asyncio.to_thread(
                        run_agent_streaming,
                        client=client,
                        messages=list(messages),
                        system_prompt=config["system_prompt"],
                        tool_defs=config["tool_defs"],
                        tool_map=config["tool_map"],
                        write_tools=write_tools,
                        mode=skill_name,
                    )
                    return result if isinstance(result, str) else result[0]
                except Exception:
                    logger.warning("Parallel skill %s failed", skill_name, exc_info=True)
                    return ""

            gathered = await asyncio.wait_for(
                asyncio.gather(
                    _run_bare(primary_config, primary.name, primary_config["write_tools"]),
                    _run_bare(secondary_config, secondary.name, sec_write_tools),
                    return_exceptions=True,
                ),
                timeout=120,
            )
            primary_output = gathered[0] if isinstance(gathered[0], str) else ""
            secondary_output = gathered[1] if isinstance(gathered[1], str) else ""

    finally:
        bus.flush_buffer(task_id)
        if _owns_client:
            try:
                client.close()
            except Exception:
                pass

    elapsed_ms = int((time.monotonic() - start) * 1000)

    return ParallelSkillResult(
        primary_output=primary_output,
        secondary_output=secondary_output,
        primary_skill=primary.name,
        secondary_skill=secondary.name,
        primary_confidence=0.0,
        secondary_confidence=0.0,
        duration_ms=elapsed_ms,
        primary_tokens=primary_tokens,
        secondary_tokens=secondary_tokens,
        primary_components=primary_components,
        secondary_components=secondary_components,
    )
