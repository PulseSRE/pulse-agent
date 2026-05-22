"""Plan-based investigation execution for findings."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cluster_monitor import ClusterMonitor

logger = logging.getLogger("pulse_agent.monitor")


async def try_plan_execution(monitor: ClusterMonitor, finding: dict) -> bool:
    """Try to execute a plan template for this finding."""
    try:
        from ..plan_runtime import PlanRuntime
        from ..plan_templates import match_template

        template = match_template(category=finding.get("category", ""))
        if not template:
            return False

        runtime = PlanRuntime(client=monitor._client)
        finding_id = finding.get("id", "")
        all_phases = [{"id": p.id, "status": "pending", "skill_name": p.skill_name} for p in template.phases]

        async def _on_start(pid, sn):
            logger.info("Plan phase '%s' starting (skill=%s)", pid, sn)
            for p in all_phases:
                if p["id"] == pid:
                    p["status"] = "running"
            await monitor._broadcast_raw(
                {
                    "type": "investigation_progress",
                    "findingId": finding_id,
                    "phases": all_phases,
                    "planId": template.id,
                    "planName": template.name,
                    "timestamp": int(time.time() * 1000),
                }
            )

        async def _on_complete(pid, out):
            logger.info("Plan phase '%s' done (status=%s)", pid, out.status)
            for p in all_phases:
                if p["id"] == pid:
                    p["status"] = out.status
                    p["summary"] = out.evidence_summary[:100] if out.evidence_summary else ""
                    p["confidence"] = out.confidence
            await monitor._broadcast_raw(
                {
                    "type": "investigation_progress",
                    "findingId": finding_id,
                    "phases": all_phases,
                    "planId": template.id,
                    "planName": template.name,
                    "timestamp": int(time.time() * 1000),
                }
            )

        result = await runtime.execute(
            template,
            incident=finding,
            on_phase_start=_on_start,
            on_phase_complete=_on_complete,
        )

        # Generate postmortem from all phase outputs
        if result.phase_outputs:
            try:
                from ..postmortem import Postmortem, save_postmortem

                triage_out = result.phase_outputs.get("triage")
                diagnose_out = (
                    result.phase_outputs.get("diagnose")
                    or result.phase_outputs.get("node_diagnostics")
                    or result.phase_outputs.get("change_analysis")
                )

                timeline_parts = []
                for pid, out in result.phase_outputs.items():
                    if out.evidence_summary:
                        timeline_parts.append(f"[{pid}] {out.evidence_summary}")
                timeline = "\n".join(timeline_parts)

                root_cause = ""
                if diagnose_out and diagnose_out.evidence_summary:
                    root_cause = diagnose_out.evidence_summary
                elif triage_out and triage_out.evidence_summary:
                    root_cause = triage_out.evidence_summary

                all_actions = []
                for out in result.phase_outputs.values():
                    all_actions.extend(out.actions_taken)

                risk_flags = []
                for out in result.phase_outputs.values():
                    risk_flags.extend(out.risk_flags)

                prevention = []
                for out in result.phase_outputs.values():
                    for q in out.open_questions:
                        prevention.append(f"Investigate: {q}")
                if not prevention and root_cause:
                    prevention.append(f"Monitor for recurrence of: {root_cause}")

                pm = Postmortem(
                    id=f"pm-{finding.get('id', 'unknown')}",
                    incident_type=finding.get("category", ""),
                    plan_id=template.id,
                    timeline=timeline,
                    root_cause=root_cause,
                    contributing_factors=risk_flags[:5],
                    actions_taken=all_actions[:10],
                    prevention=prevention[:5],
                    confidence=max((o.confidence for o in result.phase_outputs.values()), default=0),
                    generated_at=int(time.time() * 1000),
                )
                save_postmortem(pm)
            except Exception:
                logger.debug("Postmortem generation failed", exc_info=True)

        # Scaffold skill + plan template from resolution
        try:
            from ..skill_scaffolder import (
                save_scaffolded_skill,
                scaffold_plan_template,
                scaffold_skill_from_resolution,
            )

            tools = [t for out in result.phase_outputs.values() for t in out.actions_taken]
            conf = max((o.confidence for o in result.phase_outputs.values()), default=0)
            if tools:
                diagnose_out = result.phase_outputs.get("diagnose")
                skill_content = scaffold_skill_from_resolution(
                    query=finding.get("title", ""),
                    tools_called=tools,
                    investigation_summary=diagnose_out.evidence_summary if diagnose_out else "",
                    root_cause=diagnose_out.findings.get("root_cause", "unknown") if diagnose_out else "unknown",
                    confidence=conf,
                )
                tokens = finding.get("title", "unknown").lower().split()[:3]
                skill_name = "-".join(t for t in tokens if t.isalnum())[:40] or "auto-skill"
                save_scaffolded_skill(skill_content, skill_name)

                phase_ids = [p.id for p in template.phases]
                scaffold_plan_template(
                    skill_name=skill_name,
                    plan_phases=phase_ids,
                    incident_type=finding.get("category", "unknown"),
                    confidence=conf,
                )

                try:
                    from ..eval_scaffolder import scaffold_eval_from_plan

                    scaffold_eval_from_plan(
                        skill_name=skill_name,
                        finding=finding,
                        plan_result=result,
                        tools_called=tools,
                        confidence=conf,
                        duration_seconds=result.total_duration_ms / 1000.0,
                    )
                except Exception:
                    logger.debug("Eval scaffolding from plan failed", exc_info=True)
        except Exception:
            logger.debug("Skill scaffolding failed", exc_info=True)

        logger.info(
            "Plan execution complete: %s status=%s phases=%d/%d",
            template.name,
            result.status,
            result.phases_completed,
            result.phases_total,
        )
        return True

    except Exception:
        logger.debug("Plan execution failed", exc_info=True)
        return False
