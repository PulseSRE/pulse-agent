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
        from .alert_plans import plan_category_for

        # plan_category_for, not the raw category: alert findings arrive as
        # category="alerts", which no template matches — so alert-borne
        # incidents (this cluster's dominant kind) never engaged a plan.
        template = match_template(category=plan_category_for(finding))
        if not template:
            return False

        runtime = PlanRuntime(client=monitor._client)
        finding_id = finding.get("id", "")
        all_phases = [{"id": p.id, "status": "pending", "skill_name": p.skill_name} for p in template.phases]

        async def _on_start(pid, sn):
            logger.info("Plan phase '%s' starting (skill=%s)", pid, sn)
            # The UI shows "Investigating: <skill>" from this event, which was
            # handled but never sent.
            await monitor._broadcast_raw(
                {"type": "skill_activity", "skill_name": sn, "status": "running", "timestamp": int(time.time() * 1000)}
            )
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
                    # A phase downgraded to 'partial' by the contract judge carries
                    # what it failed to produce. Surface it rather than showing an
                    # operator a phase that looks merely less confident.
                    unmet = [q for q in (out.open_questions or []) if "did not produce" in q]
                    if unmet:
                        p["unmetContract"] = unmet
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

        try:
            result = await runtime.execute(
                template,
                incident=finding,
                on_phase_start=_on_start,
                on_phase_complete=_on_complete,
            )
        finally:
            await monitor._broadcast_raw(
                {"type": "skill_activity", "skill_name": "", "status": "idle", "timestamp": int(time.time() * 1000)}
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

        # The investigation this plan amounts to: its diagnosis, the evidence
        # behind it, and the confidence derived from that evidence. Plans ran
        # the common categories (crashloop, oom, image_pull, nodes, ...) and
        # reported only phase progress — no investigation_report, nothing in
        # the investigations table, no evidence for the inbox to show.
        report = await _report_plan_investigation(monitor, finding, template, result)

        # Learning goes through the verified-trajectory gate like every other
        # investigation. This used to scaffold a skill here, from an unverified
        # plan run, named after the finding title's first words — bypassing
        # "learn only from fixes that held" entirely.
        if report and report.get("confidence", 0) >= 0.75 and report.get("suspected_cause"):
            try:
                from ..trajectory import LearningCandidate, candidate_key, get_learner

                get_learner().record(
                    LearningCandidate(
                        key=candidate_key(finding.get("category", ""), finding.get("resources", [])),
                        category=finding.get("category", ""),
                        title=finding.get("title", ""),
                        root_cause=report["suspected_cause"],
                        summary=report.get("summary", ""),
                        confidence=float(report["confidence"]),
                        evidence=list(report.get("evidenceDetail") or []),
                        tools_called=[t for out in result.phase_outputs.values() for t in out.actions_taken],
                    )
                )
            except Exception:
                logger.debug("Recording plan learning candidate failed", exc_info=True)

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


async def _report_plan_investigation(monitor: ClusterMonitor, finding: dict, template, result) -> dict | None:
    """Turn a finished plan into an investigation report: saved, broadcast, on the inbox item."""
    import asyncio
    import uuid

    from .actions import save_investigation
    from .findings import _ts

    outputs = result.phase_outputs or {}
    if not outputs:
        return None
    diagnose = outputs.get("diagnose") or outputs.get("node_diagnostics") or outputs.get("change_analysis")
    lead = diagnose or next(reversed(list(outputs.values())))
    findings = lead.findings if isinstance(lead.findings, dict) else {}
    cause = str(findings.get("root_cause") or findings.get("suspected_cause") or "")
    if cause.lower() in ("unknown", "n/a", "none"):
        cause = ""
    evidence_detail = [e for out in outputs.values() for e in (out.evidence or [])][:10]
    alternatives = [str(a) for a in (findings.get("alternatives_considered") or [])][:10]
    remediation = outputs.get("remediate") or outputs.get("remediation")
    fix = ""
    if remediation is not None:
        fix = str(remediation.findings.get("recommended_fix") or remediation.evidence_summary or "")[:500]

    report_id = f"i-{uuid.uuid4().hex[:12]}"
    report = {
        "type": "investigation_report",
        "id": report_id,
        "finding_id": finding.get("id", ""),
        "findingId": finding.get("id", ""),
        "category": finding.get("category", ""),
        "status": "completed" if result.status in ("complete", "completed", "partial") else "failed",
        "summary": (lead.evidence_summary or "")[:1000],
        "suspected_cause": cause,
        "suspectedCause": cause,
        "recommended_fix": fix,
        "recommendedFix": fix,
        # The diagnosing phase's confidence, which plan_runtime now derives
        # from that phase's evidence instead of defaulting to 0.7.
        "confidence": float(lead.confidence or 0.0),
        "evidence": [str(e.get("observation", "")) for e in evidence_detail if e.get("observation")],
        "evidenceDetail": evidence_detail,
        "alternatives_considered": alternatives,
        "alternativesConsidered": alternatives,
        "planId": template.id,
        "timestamp": _ts(),
    }
    try:
        await asyncio.to_thread(save_investigation, report, finding)
        await monitor._broadcast_raw(report)
        from ..inbox import attach_investigation_to_finding

        result_like = {
            "summary": report["summary"],
            "suspected_cause": cause,
            "recommended_fix": fix,
            "confidence": report["confidence"],
            "evidence": report["evidence"],
            "evidence_detail": evidence_detail,
            "alternatives_considered": alternatives,
        }
        await asyncio.to_thread(attach_investigation_to_finding, finding, result_like, report_id)
    except Exception:
        logger.debug("Reporting plan investigation failed", exc_info=True)
    return report
