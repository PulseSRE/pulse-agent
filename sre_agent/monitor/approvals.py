"""Approving a fix that was proposed while nobody was there to approve it.

Trust level 2 means *ask first*. Until now the only way to answer was to be
holding a WebSocket open at the moment the question was asked, with 120 seconds
to reply. Nobody is watching a dashboard at 03:00, so on the reference cluster
the answer was never given and `total_actions` stayed at zero.

A proposal is now recorded instead, and this is how it gets answered later.

The proposal is deliberately treated as a *pointer to work*, not as a captured
command. Approving re-derives the fix plan from the finding as it stands right
now rather than replaying parameters frozen hours ago: an image tag, a resource
limit or an owning Deployment may all have moved, and executing a stale plan
against a changed cluster is a worse failure than refusing.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("pulse_agent.monitor")

PROPOSED = "proposed"

# Answered the same way whether a person clicks Approve on it or the sweep
# below gets there first — the condition is gone either way, so the message
# should not depend on who noticed.
STALE_PROPOSAL_MESSAGE = "The condition this was proposed for is no longer being reported — nothing to fix"


class ApprovalError(Exception):
    """Refused, with a reason meant for a person to read."""

    def __init__(self, reason: str, status_code: int = 400):
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


def _current_finding(finding_id: str) -> dict[str, Any] | None:
    """The finding as the monitor currently sees it, or None if it is gone.

    Deliberately reads live scan state rather than the copy stored with the
    proposal. If the condition has cleared on its own, there is nothing to fix
    and acting would be operating on a memory of the cluster.
    """
    from .cluster_monitor import _cluster_monitor

    if _cluster_monitor is None:
        return None
    for finding in _cluster_monitor._last_findings.values():
        if finding.get("id") == finding_id:
            return finding
    return None


def _proposed_strategy(action: dict) -> str:
    """The fix strategy recorded when the proposal was made ("" for older proposals)."""
    import json

    inp = action.get("input")
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except (TypeError, ValueError):
            inp = None
    return str((inp or {}).get("fixStrategy") or "") if isinstance(inp, dict) else ""


def approve_fix(action_id: str, approver: str) -> dict[str, Any]:
    """Execute a proposed fix on a person's authority. Raises ApprovalError if refused."""
    from ..repositories import get_monitor_repo
    from .actions import get_action_detail, save_action
    from .findings import _ts
    from .fix_planner import default_fix_plan, execute_fix_with_snapshot, get_investigation_for_finding, plan_fix

    action = get_action_detail(action_id)
    if action is None:
        raise ApprovalError("No such action", status_code=404)
    if action.get("status") != PROPOSED:
        # Covers the double-click and the already-answered proposal alike.
        raise ApprovalError(f"Action is {action.get('status')}, not a pending proposal", status_code=409)

    finding_id = action.get("findingId") or action.get("finding_id") or ""
    finding = _current_finding(finding_id)
    if finding is None:
        raise ApprovalError(STALE_PROPOSAL_MESSAGE, status_code=409)

    from .autofix import remediation_blocked_reason

    blocked = remediation_blocked_reason()
    if blocked:
        raise ApprovalError(blocked, status_code=409)

    category = finding.get("category", "")
    investigation = get_investigation_for_finding(finding_id)
    plan = plan_fix(investigation, finding) if investigation else None
    if plan is None:
        plan = default_fix_plan(category, finding)
    if plan is None:
        raise ApprovalError("No fix strategy applies to this finding any more", status_code=409)
    proposed_strategy = _proposed_strategy(action)
    if proposed_strategy and plan.strategy != proposed_strategy:
        # The plan is re-derived on purpose (the cluster may have moved), but a
        # person approved one specific fix. Running another on their authority
        # is not what they agreed to.
        #
        # Withdrawn rather than left for a person to reject: rejecting it would
        # file a verdict against a strategy nobody judged, and while it stays
        # pending no new proposal can be made for the condition.
        message = (
            f"The fix Pulse would run now ({plan.strategy}) is not the one proposed ({proposed_strategy}); "
            "nothing was changed. This proposal is withdrawn; Pulse proposes the new fix on its next scan."
        )
        get_monitor_repo().expire_proposal(action_id, f"Superseded: {message}")
        raise ApprovalError(message, status_code=409)
    if plan.strategy == "require_human_review":
        learned = plan.params.get("track_record")
        detail = f" ({learned})" if learned else ""
        raise ApprovalError(f"This fix has no automated form — it needs to be done by hand{detail}", status_code=409)

    # Claim it before doing anything. Two operators clicking at once should
    # produce one fix and one conflict, not two fixes.
    repo = get_monitor_repo()
    if not repo.claim_proposed_action(action_id, approver, _ts()):
        raise ApprovalError("Somebody else just approved this", status_code=409)

    logger.info(
        "Fix approved by %s: action=%s strategy=%s finding=%s",
        approver,
        action_id,
        plan.strategy,
        finding_id,
    )

    started = _ts()
    report: dict[str, Any] = {
        "type": "action_report",
        "id": action_id,
        "findingId": finding_id,
        "tool": "",
        "input": {"category": category, "resources": finding.get("resources", [])},
        "status": "completed",
        "reasoning": f"Approved by {approver}: {plan.description}",
        "timestamp": started,
        "approvedBy": approver,
        "fixStrategy": plan.strategy,
        "causeCategory": plan.cause_category,
        "fixDescription": plan.description,
    }

    # Preflight the agent's own RBAC before touching the cluster: approving a
    # fix the service account provably cannot perform should produce a clear
    # remediation message, not a raw 403 after the fact. Covers proposals
    # created before proposal-time preflighting existed.
    from .rbac_preflight import can_execute

    resources = finding.get("resources") or [{}]
    namespace = resources[0].get("namespace", "default")
    allowed, remediation = can_execute(plan.strategy, namespace)
    if not allowed:
        report.update(status="failed", error=remediation, durationMs=_ts() - started)
        logger.warning("Approved fix blocked by RBAC preflight: action=%s — %s", action_id, remediation)
        save_action(report, category=category, resources=finding.get("resources", []), finding=finding)
        _announce(report)
        return report

    try:
        execution = execute_fix_with_snapshot(plan)
        tool, before_state, after_state = execution.result
        if not execution.applied:
            report.update(status="failed", error=after_state, verificationStatus="unverifiable")
        if execution.snapshot:
            from ..snapshot import to_json

            report["beforeSnapshot"] = to_json(execution.snapshot)
        report.update(
            tool=tool,
            beforeState=before_state,
            afterState=after_state,
            durationMs=_ts() - started,
        )
    except Exception as e:
        from ..errors import classify_exception

        # str(e) on a kubernetes ApiException dumps the whole object, headers
        # and all — classify_exception extracts the structured Status body's
        # message instead, so this stays something a person can read.
        error_message = str(classify_exception(e, plan.strategy))[:500]
        report.update(status="failed", error=error_message, durationMs=_ts() - started)
        logger.error("Approved fix failed: action=%s error=%s", action_id, e)

    if report["status"] == "completed":
        from .cluster_monitor import get_cluster_monitor_sync

        monitor = get_cluster_monitor_sync()
        if monitor is not None and monitor.running:
            report["verificationStatus"] = "pending"
        else:
            monitor = None
            report["verificationStatus"] = "unverifiable"
            report["verificationEvidence"] = "No running monitor available to observe approved mutation recovery"
    else:
        monitor = None

    save_action(
        report,
        category=category,
        resources=finding.get("resources", []),
        finding=finding,
    )
    if monitor is not None:
        # An approved fix is a fix. Without this the finding clearing on the next
        # scan was reported as "self-healed", and the learning candidate for it
        # was discarded as "the diagnosis was not what fixed it" — so at the
        # default trust level, where every fix is approved by a person, nothing
        # verified was ever learned.
        if finding_id:
            monitor._recent_fix_ids.add(finding_id)
        try:
            from ..trajectory import candidate_key, get_learner

            get_learner().restart_clock(candidate_key(category, finding.get("resources", [])))
        except Exception:
            logger.debug("Could not restart learning clock for %s", action_id, exc_info=True)
        monitor._pending_verifications[action_id] = {
            "action_id": action_id,
            "finding_id": finding_id,
            "category": category,
            "resources": finding.get("resources", []),
            "verify_resources": execution.verify_resources or finding.get("resources", []),
            "target_scan": monitor._scan_counter + 1,
            "strategy": plan.strategy,
            "cause": plan.cause_category,
        }
    _announce(report)
    return report


MAX_REJECTION_REASON = 500


def find_open_inbox_item(repo, finding_id: str, correlation_key: str):
    """The open inbox item for a proposal's finding, or None.

    By finding id first, then by the condition's correlation key: finding ids
    are minted per sighting and change after a restart, so an older proposal's
    id may no longer name the item that tracks the same condition.
    """
    row = repo.find_active_by_finding_id(finding_id) if finding_id else None
    if row is None and correlation_key:
        row = repo.find_active_by_correlation_task(correlation_key)
    return row


def _proposal_input(action: dict) -> dict:
    import json

    inp = action.get("input")
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except (TypeError, ValueError):
            inp = None
    return inp if isinstance(inp, dict) else {}


def reject_fix(action_id: str, rejecter: str, reason: str) -> dict[str, Any]:
    """Decline a proposed fix on a person's authority. Raises ApprovalError if refused.

    Until now a proposal could only be approved or left to expire, so "no"
    looked exactly like "nobody looked" — and fix learning never heard it.
    A rejection:

    - answers the proposal (``failed``, "Rejected by …", the same status the
      live WebSocket rejection records) so it stops asking;
    - files a ``rejected`` verdict against the proposal's (cause, strategy),
      so a fix people keep declining stops being proposed automatically;
    - notes the rejection and its reason on the finding's open inbox item,
      which stays open: declining a fix does not make the problem go away;
    - is announced, so every open UI drops it from its pending list.
    """
    from ..fix_learning import record_rejection
    from ..repositories import get_monitor_repo
    from .actions import get_action_detail
    from .findings import _ts

    reason = " ".join((reason or "").split())[:MAX_REJECTION_REASON]
    if not reason:
        raise ApprovalError("A reason is required to reject a proposed fix", status_code=400)

    action = get_action_detail(action_id)
    if action is None:
        raise ApprovalError("No such action", status_code=404)
    if action.get("status") != PROPOSED:
        raise ApprovalError(f"Action is {action.get('status')}, not a pending proposal", status_code=409)

    now = _ts()
    message = f"Rejected by {rejecter}: {reason}"
    if not get_monitor_repo().reject_proposal(action_id, rejecter, message, now):
        raise ApprovalError("Somebody else just answered this proposal", status_code=409)

    inp = _proposal_input(action)
    strategy = str(inp.get("fixStrategy") or "")
    cause = str(inp.get("causeCategory") or "")
    correlation_key = action.get("correlationKey") or ""
    learned = record_rejection(action_id, cause, strategy, correlation_key)
    finding_id = action.get("findingId") or ""
    logger.info(
        "Fix rejected by %s: action=%s strategy=%s cause=%s learned=%s",
        rejecter,
        action_id,
        strategy or "-",
        cause or "-",
        learned,
    )

    try:
        from ..observability import AUTOFIX_TOTAL

        AUTOFIX_TOTAL.labels(outcome="rejected").inc()
    except Exception:
        logger.debug("Could not count rejection", exc_info=True)

    inbox_item_id = _note_rejection_on_inbox(finding_id, correlation_key, action_id, rejecter, reason, now)

    report: dict[str, Any] = {
        "type": "action_report",
        "id": action_id,
        "findingId": finding_id,
        "tool": action.get("tool") or "",
        "input": inp,
        "category": action.get("category") or "",
        "resources": action.get("resources") or [],
        "status": "failed",
        "error": message,
        "reasoning": action.get("reasoning") or "",
        "timestamp": now,
        "rejectedBy": rejecter,
        "rejectionReason": reason,
        "fixStrategy": strategy,
        "causeCategory": cause,
        # Whether fix learning counted it: false for proposals that predate the
        # stored cause, a repeat rejection for the same condition, or a failed write.
        "learned": learned,
        "inboxItemId": inbox_item_id,
    }
    _announce(report)
    return report


def _note_rejection_on_inbox(
    finding_id: str, correlation_key: str, action_id: str, rejecter: str, reason: str, now_ms: int
) -> str | None:
    """Record the rejection on the finding's open inbox item; return its id, if any."""
    from ..inbox import _publish_event, record_interaction

    item_id: str | None = None
    try:
        from ..repositories.inbox_repo import get_inbox_repo

        repo = get_inbox_repo()
        row = find_open_inbox_item(repo, finding_id, correlation_key)
        if row is not None:
            item_id = row["id"]
            import json

            metadata = row.get("metadata") or {}
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except (TypeError, ValueError):
                    metadata = {}
            metadata["fix_rejected"] = {"action_id": action_id, "by": rejecter, "reason": reason, "at": now_ms}
            repo.update_metadata(item_id, metadata, now_ms // 1000)
            _publish_event("inbox_item_updated", item_id, {"fix_rejected": metadata["fix_rejected"]})
    except Exception:
        logger.debug("Could not note rejection of %s on the inbox", action_id, exc_info=True)

    record_interaction(
        actor=rejecter,
        interaction_type="reject_fix",
        item_id=item_id,
        action_id=action_id,
        decision="rejected",
        metadata={"reason": reason},
    )
    return item_id


def _announce(report: dict[str, Any]) -> None:
    """Tell connected UIs the proposal is answered.

    An approval over REST never reached /ws/monitor, so every open tab kept the
    proposal in its pending list (and its badge) until reload.
    """
    try:
        from .cluster_monitor import broadcast_threadsafe

        broadcast_threadsafe({"type": "action_report", **report})
    except Exception:
        logger.debug("Could not announce approved action %s", report.get("id"), exc_info=True)


def expire_orphaned_proposals() -> int:
    """Answer every pending proposal whose condition has already cleared.

    ``approve_fix`` already refuses these with ``STALE_PROPOSAL_MESSAGE`` — but
    only at the moment a person happens to click Approve. Until then the
    proposal keeps counting toward "N fixes waiting on you", asking for a
    decision about a fix that no longer applies. Called once per scan, after
    ``_last_findings`` has settled for the cycle, so a self-healed condition
    stops asking instead of waiting for someone to find out the hard way that
    there is nothing left to fix.

    Returns the number of proposals answered this way.
    """
    from ..repositories import get_monitor_repo

    repo = get_monitor_repo()
    expired = 0
    for row in repo.fetch_proposed_actions():
        finding_id = row["finding_id"] or ""
        if not finding_id:
            continue
        if _current_finding(finding_id) is None and repo.expire_proposal(row["id"], STALE_PROPOSAL_MESSAGE):
            expired += 1
    return expired
