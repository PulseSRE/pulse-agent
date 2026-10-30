"""One investigation pipeline for monitor findings.

A finding is diagnosed one of two ways: by a plan template (phased, with
contracts) when one matches its category, or by a single read-only agent run
otherwise. Those are two *strategies*. Everything after the diagnosis is one
path, here:

    diagnose()  ->  InvestigationOutcome  ->  complete_investigation()

``investigate()`` runs the whole thing under one timeout and turns a timeout or
an exception into a failed outcome, so a failure is reported the same way
whichever strategy hit it.

This used to be two pipelines (``investigation_runner`` inline for freeform,
``plan_executor`` for plans), each building its own report and doing its own
saving, inbox attachment, memory and learning. They had drifted: plan runs had
no timeout and reported nothing when they failed; freeform runs fed memory the
placeholder tool sequence ``["proactive_investigation"]`` and learned from a
cause of ``"unknown"``; only freeform runs reached the context bus or the
security follow-up.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..async_db import ASYNC_DB_ERRORS as _ASYNC_DB_ERRORS
from ..config import get_settings
from .findings import _ts
from .investigations import _run_proactive_investigation, _run_security_followup

if TYPE_CHECKING:
    from .cluster_monitor import ClusterMonitor

logger = logging.getLogger("pulse_agent.monitor")

# Thresholds an outcome must clear before it is remembered or learned from.
MEMORY_CONFIDENCE = 0.7
LEARNING_CONFIDENCE = 0.75
SECURITY_FOLLOWUP_COOLDOWN = 600

_NO_CAUSE = {"unknown", "n/a", "none"}


@dataclass
class InvestigationOutcome:
    """What one investigation concluded, whichever strategy produced it."""

    status: str  # "completed" | "failed"
    strategy: str  # "plan" | "freeform"
    summary: str = ""
    suspected_cause: str = ""
    recommended_fix: str = ""
    confidence: float = 0.0
    # The model's own figure, kept beside the derived one (freeform only).
    asserted_confidence: float | None = None
    evidence_detail: list[dict] = field(default_factory=list)
    alternatives: list[str] = field(default_factory=list)
    view_plan: list[dict] = field(default_factory=list)
    # Tool names, and the ledger of calls evidence cites (evidence.CallLedger).
    tools_called: list[str] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    plan_id: str = ""
    error: str = ""
    # The raw PlanResult, for the postmortem; never serialized.
    plan_result: Any = field(default=None, repr=False)

    @property
    def completed(self) -> bool:
        return self.status == "completed"

    @property
    def evidence(self) -> list[str]:
        return [str(e.get("observation", "")) for e in self.evidence_detail if e.get("observation")]

    def to_report(self, finding: dict, report_id: str | None = None) -> dict:
        """The ``investigation_report`` that is saved and broadcast (API_CONTRACT)."""
        report: dict[str, Any] = {
            "type": "investigation_report",
            "id": report_id or f"i-{uuid.uuid4().hex[:12]}",
            "finding_id": finding.get("id", ""),
            "findingId": finding.get("id", ""),
            "category": finding.get("category", ""),
            "status": self.status,
            "summary": self.summary,
            "suspected_cause": self.suspected_cause,
            "suspectedCause": self.suspected_cause,
            "recommended_fix": self.recommended_fix,
            "recommendedFix": self.recommended_fix,
            "confidence": float(self.confidence or 0.0),
            "evidence": self.evidence,
            "evidenceDetail": self.evidence_detail,
            "alternatives_considered": self.alternatives,
            "alternativesConsidered": self.alternatives,
        }
        if self.view_plan:
            report["viewPlan"] = self.view_plan
        if self.plan_id:
            report["planId"] = self.plan_id
        if self.error:
            report["error"] = self.error
        report["timestamp"] = _ts()
        return report

    def to_inbox_result(self) -> dict:
        """The shape ``inbox.attach_investigation_to_finding`` reads."""
        return {
            "summary": self.summary,
            "suspected_cause": self.suspected_cause,
            "recommended_fix": self.recommended_fix,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "evidence_detail": self.evidence_detail,
            "alternatives_considered": self.alternatives,
            "view_plan": self.view_plan,
        }


def failed_outcome(strategy: str, error: str, *, plan_id: str = "") -> InvestigationOutcome:
    return InvestigationOutcome(status="failed", strategy=strategy, error=error, plan_id=plan_id)


def _clean_cause(cause: Any) -> str:
    text = str(cause or "").strip()
    return "" if text.lower() in _NO_CAUSE else text


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


def outcome_from_freeform(result: dict) -> InvestigationOutcome:
    """A single-run investigation's result (``_run_proactive_investigation``)."""
    asserted = result.get("asserted_confidence")
    return InvestigationOutcome(
        status="completed",
        strategy="freeform",
        summary=str(result.get("summary") or ""),
        suspected_cause=_clean_cause(result.get("suspected_cause")),
        recommended_fix=str(result.get("recommended_fix") or ""),
        confidence=float(result.get("confidence") or 0.0),
        asserted_confidence=float(asserted) if asserted is not None else None,
        evidence_detail=list(result.get("evidence_detail") or []),
        alternatives=[str(a) for a in (result.get("alternatives_considered") or [])][:10],
        view_plan=list(result.get("view_plan") or []),
        tools_called=list(result.get("tools_called") or []),
        tool_calls=list(result.get("tool_calls") or []),
    )


def diagnosing_output(template, outputs: dict):
    """The phase output that holds the plan's diagnosis.

    The phase whose contract promises a ``root_cause``, in template order. This
    used to be looked up by three hard-coded phase ids, so a plan naming its
    diagnosis anything else (oom's ``memory_analysis``, latency's
    ``investigate``) reported the *last* phase instead — verify, which declares
    no cause — and the report carried no cause and verify's confidence.
    """
    phases = list(getattr(template, "phases", None) or [])
    for wanted in ("root_cause", "confidence"):
        for phase in phases:
            out = outputs.get(phase.id)
            if out is not None and wanted in (phase.produces or []) and out.status != "needs_escalation":
                return out
    named = outputs.get("diagnose") or outputs.get("node_diagnostics") or outputs.get("change_analysis")
    return named or next(reversed(list(outputs.values())))


def outcome_from_plan(template, result) -> InvestigationOutcome:
    """A finished plan's diagnosis, evidence and confidence."""
    plan_id = str(getattr(template, "id", "") or "")
    outputs = getattr(result, "phase_outputs", None) or {}
    if not outputs:
        return failed_outcome("plan", "The plan produced no phase output", plan_id=plan_id)
    lead = diagnosing_output(template, outputs)
    findings = lead.findings if isinstance(lead.findings, dict) else {}
    cause = _clean_cause(findings.get("root_cause") or findings.get("suspected_cause"))
    remediation = outputs.get("remediate") or outputs.get("remediation")
    fix = ""
    if remediation is not None:
        fix = str(remediation.findings.get("recommended_fix") or remediation.evidence_summary or "")[:500]
    return InvestigationOutcome(
        status="completed" if result.status in ("complete", "completed", "partial") else "failed",
        strategy="plan",
        summary=(lead.evidence_summary or "")[:1000],
        suspected_cause=cause,
        recommended_fix=fix,
        # The diagnosing phase's confidence, derived from that phase's evidence.
        confidence=float(lead.confidence or 0.0),
        evidence_detail=[e for out in outputs.values() for e in (out.evidence or [])][:10],
        alternatives=[str(a) for a in (findings.get("alternatives_considered") or [])][:10],
        tools_called=[t for out in outputs.values() for t in out.actions_taken],
        tool_calls=[c for out in outputs.values() for c in (getattr(out, "tool_calls", None) or [])],
        plan_id=plan_id,
        plan_result=result,
    )


async def diagnose(
    finding: dict,
    *,
    template=None,
    client=None,
    on_phase_start=None,
    on_phase_complete=None,
) -> InvestigationOutcome:
    """Diagnose ``finding`` with a plan when ``template`` is given, else with one agent run."""
    if template is not None:
        from ..plan_runtime import PlanRuntime

        # No on_confirm: a monitor plan's writes are denied.
        result = await PlanRuntime(client=client).execute(
            template,
            incident=finding,
            on_phase_start=on_phase_start,
            on_phase_complete=on_phase_complete,
        )
        return outcome_from_plan(template, result)
    return outcome_from_freeform(await _run_proactive_investigation(finding, client=client))


def investigation_timeout(template=None) -> float:
    """The budget for one investigation. A plan gets at least the sum of its phase timeouts."""
    base = float(get_settings().monitor.investigation_timeout)
    if template is None:
        return base
    phases = sum(float(getattr(p, "timeout_seconds", 0) or 0) for p in getattr(template, "phases", None) or [])
    return max(base, phases)


# ---------------------------------------------------------------------------
# Running and completing an investigation
# ---------------------------------------------------------------------------


async def investigate(monitor: ClusterMonitor, finding: dict, template=None) -> InvestigationOutcome:
    """Diagnose, then complete: report, save, inbox, memory and learning, whichever strategy ran.

    ``client=None`` on purpose: a fresh client per investigation, never the
    monitor's shared one. ``wait_for`` can cancel the call mid-stream, and
    cancelling an in-flight streamed call on a shared client can corrupt its
    connection pool for every investigation after it.
    """
    strategy = "plan" if template is not None else "freeform"
    plan_id = str(getattr(template, "id", "") or "")
    timeout = investigation_timeout(template)
    on_start, on_complete = _plan_progress(monitor, finding, template) if template is not None else (None, None)
    try:
        outcome = await asyncio.wait_for(
            diagnose(
                finding,
                template=template,
                client=None,
                on_phase_start=on_start,
                on_phase_complete=on_complete,
            ),
            timeout=timeout,
        )
    except TimeoutError:
        outcome = failed_outcome(strategy, f"Investigation timed out after {timeout:g}s", plan_id=plan_id)
        logger.warning(
            "Investigation timed out after %gs for finding %s (category=%s, title=%s, strategy=%s)",
            timeout,
            finding.get("id", ""),
            finding.get("category", ""),
            finding.get("title", "")[:60],
            strategy,
        )
    except Exception as e:
        outcome = failed_outcome(strategy, str(e) or type(e).__name__, plan_id=plan_id)
        # Surfaced in the application log, not only in the saved report: a
        # client stuck failing once ran for hundreds of investigations unseen.
        logger.exception(
            "Investigation failed for finding %s (category=%s, title=%s, strategy=%s)",
            finding.get("id", ""),
            finding.get("category", ""),
            finding.get("title", "")[:60],
            strategy,
        )
    finally:
        if template is not None:
            await monitor._broadcast_raw(
                {"type": "skill_activity", "skill_name": "", "status": "idle", "timestamp": int(time.time() * 1000)}
            )

    if template is not None and outcome.plan_result is not None:
        _save_postmortem(finding, template, outcome.plan_result)
    await complete_investigation(monitor, finding, outcome)
    return outcome


async def complete_investigation(monitor: ClusterMonitor, finding: dict, outcome: InvestigationOutcome) -> dict:
    """Everything after a diagnosis, the same for every strategy. Returns the report."""
    report = outcome.to_report(finding)

    if outcome.completed:
        followup = await _security_followup(monitor, finding)
        if followup:
            report["securityFollowup"] = followup

    await _save(report, finding)
    await monitor._broadcast_raw(report)

    if not outcome.completed:
        return report

    try:
        from ..inbox import attach_investigation_to_finding

        await asyncio.to_thread(attach_investigation_to_finding, finding, outcome.to_inbox_result(), report["id"])
    except Exception:
        logger.debug("Attaching investigation to inbox failed", exc_info=True)

    _publish(finding, outcome)
    _remember(finding, outcome)
    _hold_for_learning(finding, outcome)
    return report


def _plan_progress(monitor: ClusterMonitor, finding: dict, template):
    """Phase start/complete callbacks that broadcast the plan's progress."""
    finding_id = finding.get("id", "")
    phases = [{"id": p.id, "status": "pending", "skill_name": p.skill_name} for p in template.phases]

    async def _progress() -> None:
        await monitor._broadcast_raw(
            {
                "type": "investigation_progress",
                "findingId": finding_id,
                "phases": phases,
                "planId": template.id,
                "planName": template.name,
                "timestamp": int(time.time() * 1000),
            }
        )

    async def on_start(pid, skill_name):
        logger.info("Plan phase '%s' starting (skill=%s)", pid, skill_name)
        await monitor._broadcast_raw(
            {
                "type": "skill_activity",
                "skill_name": skill_name,
                "status": "running",
                "timestamp": int(time.time() * 1000),
            }
        )
        for p in phases:
            if p["id"] == pid:
                p["status"] = "running"
        await _progress()

    async def on_complete(pid, out):
        logger.info("Plan phase '%s' done (status=%s)", pid, out.status)
        for p in phases:
            if p["id"] == pid:
                p["status"] = out.status
                p["summary"] = out.evidence_summary[:100] if out.evidence_summary else ""
                p["confidence"] = out.confidence
                # A phase downgraded to 'partial' by the contract judge carries
                # what it failed to produce; show it rather than a phase that
                # looks merely less confident.
                unmet = [q for q in (out.open_questions or []) if "did not produce" in q]
                if unmet:
                    p["unmetContract"] = unmet
        await _progress()

    return on_start, on_complete


async def _save(report: dict, finding: dict) -> None:
    try:
        from ..repositories.monitor_repo import get_monitor_repo

        await get_monitor_repo().async_save_investigation(
            report=report, finding=finding, timestamp=report.get("timestamp", _ts())
        )
    except _ASYNC_DB_ERRORS:
        from .actions import save_investigation

        await asyncio.to_thread(save_investigation, report, finding)
    except Exception:
        logger.warning("Saving investigation %s failed", report.get("id"), exc_info=True)


async def _security_followup(monitor: ClusterMonitor, finding: dict) -> dict | None:
    """At most one security follow-up per cooldown, after a completed investigation."""
    if not get_settings().monitor.security_followup:
        return None
    now = time.time()
    if now - monitor._last_security_followup < SECURITY_FOLLOWUP_COOLDOWN:
        return None
    # Claimed before the call, so concurrent investigations do not each run one.
    monitor._last_security_followup = now
    try:
        result = await asyncio.wait_for(
            _run_security_followup(finding, client=None),
            timeout=get_settings().monitor.investigation_timeout,
        )
    except TimeoutError:
        logger.warning("Security followup timed out for finding %s", finding.get("id", ""))
        return None
    except Exception as e:
        logger.warning("Security followup failed for finding %s: %s", finding.get("id", ""), e, exc_info=True)
        return None
    logger.info("Security followup completed for finding %s", finding.get("id", ""))
    return {"issues": result.get("security_issues", []), "riskLevel": result.get("risk_level", "unknown")}


def _publish(finding: dict, outcome: InvestigationOutcome) -> None:
    try:
        from ..context_bus import ContextEntry, get_context_bus

        resources = finding.get("resources") or [{}]
        get_context_bus().publish(
            ContextEntry(
                source="monitor",
                category="investigation",
                summary=f"Investigated {finding.get('category')}: {outcome.summary}",
                details={
                    "suspected_cause": outcome.suspected_cause,
                    "recommended_fix": outcome.recommended_fix,
                    "confidence": outcome.confidence,
                },
                namespace=(resources[0] or {}).get("namespace", ""),
                resources=finding.get("resources", []),
            )
        )
    except Exception:
        logger.debug("Publishing investigation to the context bus failed", exc_info=True)


def _remember(finding: dict, outcome: InvestigationOutcome) -> None:
    """A confident diagnosis becomes an unconfirmed incident in memory."""
    if not get_settings().agent.memory or outcome.confidence < MEMORY_CONFIDENCE:
        return
    try:
        from ..memory import get_manager

        manager = get_manager()
        if not manager:
            return
        resources = finding.get("resources") or []
        first = resources[0] if resources else {}
        manager.store_incident(
            {
                "query": f"Investigation: {finding.get('title', '')}",
                # The tools the investigation actually used. This was the
                # placeholder ["proactive_investigation"], which taught nothing.
                "tool_sequence": outcome.tools_called or [f"{outcome.strategy}_investigation"],
                "resolution": outcome.summary,
                "namespace": first.get("namespace", ""),
                "resource_type": first.get("kind", "").lower(),
                "error_type": finding.get("category", ""),
            },
            confirmed=False,
        )
        logger.info(
            "Stored high-confidence investigation: %s (confidence=%.2f)", finding.get("title", ""), outcome.confidence
        )
    except Exception as e:
        logger.warning("Failed to store investigation: %s", e)


def _hold_for_learning(finding: dict, outcome: InvestigationOutcome) -> None:
    """Record a learning candidate; the verification pipeline promotes it only if the fix holds.

    Needs a named cause: an investigation that could not say what went wrong
    has nothing to teach, however confident it sounded.
    """
    if outcome.confidence < LEARNING_CONFIDENCE or not outcome.suspected_cause:
        return
    try:
        from ..trajectory import LearningCandidate, candidate_key, get_learner

        get_learner().record(
            LearningCandidate(
                key=candidate_key(finding.get("category", ""), finding.get("resources", [])),
                category=finding.get("category", ""),
                title=finding.get("title", ""),
                root_cause=outcome.suspected_cause,
                summary=outcome.summary,
                confidence=float(outcome.confidence),
                evidence=list(outcome.evidence_detail),
                tools_called=list(outcome.tools_called),
            )
        )
    except Exception:
        logger.debug("Recording learning candidate failed", exc_info=True)


def _save_postmortem(finding: dict, template, result) -> None:
    """A postmortem from every phase of a plan run."""
    outputs = getattr(result, "phase_outputs", None) or {}
    if not outputs:
        return
    try:
        from ..postmortem import Postmortem, save_postmortem

        triage = outputs.get("triage")
        diagnose_out = diagnosing_output(template, outputs)
        timeline = "\n".join(f"[{pid}] {out.evidence_summary}" for pid, out in outputs.items() if out.evidence_summary)
        root_cause = ""
        if diagnose_out and diagnose_out.evidence_summary:
            root_cause = diagnose_out.evidence_summary
        elif triage and triage.evidence_summary:
            root_cause = triage.evidence_summary
        actions = [a for out in outputs.values() for a in out.actions_taken]
        risks = [r for out in outputs.values() for r in out.risk_flags]
        prevention = [f"Investigate: {q}" for out in outputs.values() for q in out.open_questions]
        if not prevention and root_cause:
            prevention.append(f"Monitor for recurrence of: {root_cause}")
        save_postmortem(
            Postmortem(
                id=f"pm-{finding.get('id', 'unknown')}",
                incident_type=finding.get("category", ""),
                plan_id=template.id,
                timeline=timeline,
                root_cause=root_cause,
                contributing_factors=risks[:5],
                actions_taken=actions[:10],
                prevention=prevention[:5],
                confidence=max((o.confidence for o in outputs.values()), default=0),
                generated_at=int(time.time() * 1000),
            )
        )
    except Exception:
        logger.debug("Postmortem generation failed", exc_info=True)
