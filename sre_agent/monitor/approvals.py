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


def approve_fix(action_id: str, approver: str) -> dict[str, Any]:
    """Execute a proposed fix on a person's authority. Raises ApprovalError if refused."""
    from ..repositories import get_monitor_repo
    from .actions import get_action_detail, save_action
    from .findings import _ts
    from .fix_planner import default_fix_plan, execute_fix, get_investigation_for_finding, plan_fix

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

    category = finding.get("category", "")
    investigation = get_investigation_for_finding(finding_id)
    plan = plan_fix(investigation, finding) if investigation else None
    if plan is None:
        plan = default_fix_plan(category, finding)
    if plan is None:
        raise ApprovalError("No fix strategy applies to this finding any more", status_code=409)
    if plan.strategy == "require_human_review":
        raise ApprovalError("This fix has no automated form — it needs to be done by hand", status_code=409)

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

    try:
        tool, before_state, after_state = execute_fix(plan)
        report.update(
            tool=tool,
            beforeState=before_state,
            afterState=after_state,
            durationMs=_ts() - started,
        )
    except Exception as e:
        report.update(status="failed", error=str(e)[:500], durationMs=_ts() - started)
        logger.error("Approved fix failed: action=%s error=%s", action_id, e)

    save_action(
        report,
        category=category,
        resources=finding.get("resources", []),
        finding=finding,
    )
    return report


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
