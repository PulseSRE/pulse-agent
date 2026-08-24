"""Answering a proposal after the moment has passed.

Trust level 2 means ask first, and until now the only way to answer was to be
holding a WebSocket open when the question was asked, with 120 seconds to
reply. Nobody is watching a dashboard at 03:00, which is how the reference
cluster reached 2,528 investigations and zero actions.

The proposal is a pointer to work, not a captured command: approving
re-derives the plan from the finding as it stands now. An image tag, a resource
limit or an owning Deployment may all have moved since it was raised, and
running a stale plan against a changed cluster is worse than refusing.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.rest import ApiException

from sre_agent.monitor.approvals import ApprovalError, approve_fix

MODULE = "sre_agent.monitor.approvals"

PROPOSAL = {"id": "a-1", "findingId": "f-1", "status": "proposed"}
FINDING = {
    "id": "f-1",
    "category": "crashloop",
    "title": "Pod api-7f9 restarting (12x)",
    "resources": [{"kind": "Pod", "name": "api-7f9", "namespace": "prod"}],
}
PLAN = MagicMock(
    strategy="restart_pod",
    cause_category="crash_exit",
    description="Delete the pod so its controller recreates it",
    confidence=0.9,
)


@pytest.fixture
def wired():
    """Patch every collaborator; individual tests override what they care about."""
    repo = MagicMock()
    repo.claim_proposed_action.return_value = True
    saved: list[dict] = []
    with (
        patch("sre_agent.monitor.actions.get_action_detail", return_value=dict(PROPOSAL)) as detail,
        patch("sre_agent.repositories.get_monitor_repo", return_value=repo),
        patch("sre_agent.monitor.actions.save_action", side_effect=lambda r, **kw: saved.append(r)),
        patch(f"{MODULE}._current_finding", return_value=dict(FINDING)) as current,
        patch("sre_agent.monitor.fix_planner.get_investigation_for_finding", return_value=None),
        patch("sre_agent.monitor.fix_planner.default_fix_plan", return_value=PLAN),
        patch("sre_agent.monitor.fix_planner.execute_fix", return_value=("delete_pod", "before", "after")) as ex,
    ):
        yield MagicMock(repo=repo, saved=saved, detail=detail, current=current, execute=ex)


# ── the happy path ────────────────────────────────────────────────────────


def test_approving_runs_the_fix_and_records_who_said_so(wired):
    report = approve_fix("a-1", "sre@example.com")

    wired.execute.assert_called_once()
    assert report["status"] == "completed"
    assert report["tool"] == "delete_pod"
    assert report["approvedBy"] == "sre@example.com"
    assert wired.saved and wired.saved[0]["id"] == "a-1", "the same action transitions, not a new one"


def test_the_approver_is_recorded_before_anything_runs(wired):
    """Claiming first is what makes a double-click produce one fix."""
    approve_fix("a-1", "sre@example.com")
    wired.repo.claim_proposed_action.assert_called_once()
    assert wired.repo.claim_proposed_action.call_args[0][1] == "sre@example.com"


# ── refusals ──────────────────────────────────────────────────────────────


def test_an_unknown_action_is_a_404(wired):
    wired.detail.return_value = None
    with pytest.raises(ApprovalError) as e:
        approve_fix("a-nope", "sre@example.com")
    assert e.value.status_code == 404


@pytest.mark.parametrize("status", ["completed", "failed", "approved", "executing"])
def test_only_a_pending_proposal_can_be_approved(wired, status):
    """Covers the double-click and the already-answered proposal alike."""
    wired.detail.return_value = {**PROPOSAL, "status": status}
    with pytest.raises(ApprovalError) as e:
        approve_fix("a-1", "sre@example.com")
    assert e.value.status_code == 409
    wired.execute.assert_not_called()


def test_a_condition_that_has_cleared_is_not_fixed(wired):
    """The problem went away on its own. Acting now would be operating on a
    memory of the cluster rather than on the cluster."""
    wired.current.return_value = None
    with pytest.raises(ApprovalError) as e:
        approve_fix("a-1", "sre@example.com")
    assert e.value.status_code == 409
    assert "no longer being reported" in e.value.reason
    wired.execute.assert_not_called()


def test_losing_the_race_does_not_fix_twice(wired):
    wired.repo.claim_proposed_action.return_value = False
    with pytest.raises(ApprovalError) as e:
        approve_fix("a-1", "sre@example.com")
    assert e.value.status_code == 409
    wired.execute.assert_not_called()


def test_a_fix_needing_hands_is_refused_rather_than_faked(wired):
    with patch(
        "sre_agent.monitor.fix_planner.default_fix_plan",
        return_value=MagicMock(strategy="require_human_review", description="d", cause_category="c", confidence=0.5),
    ):
        with pytest.raises(ApprovalError) as e:
            approve_fix("a-1", "sre@example.com")
    assert "by hand" in e.value.reason
    wired.execute.assert_not_called()


def test_no_applicable_strategy_is_refused(wired):
    with patch("sre_agent.monitor.fix_planner.default_fix_plan", return_value=None):
        with pytest.raises(ApprovalError) as e:
            approve_fix("a-1", "sre@example.com")
    assert e.value.status_code == 409
    wired.execute.assert_not_called()


# ── the plan is re-derived, not replayed ──────────────────────────────────


def test_the_plan_comes_from_the_finding_as_it_stands_now(wired):
    """Not from parameters frozen into the proposal hours ago."""
    fresh = MagicMock(strategy="patch_image", cause_category="bad_image", description="new tag", confidence=0.9)
    with patch("sre_agent.monitor.fix_planner.default_fix_plan", return_value=fresh):
        report = approve_fix("a-1", "sre@example.com")
    wired.execute.assert_called_once_with(fresh)
    assert report["fixStrategy"] == "patch_image"


def test_an_investigation_beats_the_default_plan(wired):
    targeted = MagicMock(strategy="patch_resources", cause_category="oom", description="raise limits", confidence=0.9)
    with (
        patch("sre_agent.monitor.fix_planner.get_investigation_for_finding", return_value={"id": "inv-1"}),
        patch("sre_agent.monitor.fix_planner.plan_fix", return_value=targeted),
    ):
        approve_fix("a-1", "sre@example.com")
    wired.execute.assert_called_once_with(targeted)


# ── failure is recorded, not swallowed ────────────────────────────────────


def test_a_failed_fix_is_saved_as_failed(wired):
    with patch("sre_agent.monitor.fix_planner.execute_fix", side_effect=RuntimeError("api server said no")):
        report = approve_fix("a-1", "sre@example.com")
    assert report["status"] == "failed"
    assert "api server said no" in report["error"]
    assert wired.saved, "a failed approval still has to reach fix history"


def test_a_forbidden_fix_is_saved_with_a_readable_message_not_a_header_dump(wired):
    """A rejected K8s API call must not surface str(ApiException) — that dumps
    the entire HTTPHeaderDict (Audit-Id, Content-Length, the works) into what
    a person sees in the Inbox. Only the Status body's own message belongs there.
    """
    forbidden = ApiException(status=403, reason="Forbidden")
    forbidden.headers = {"Audit-Id": "d5f6ffee-5dec-485f-9461-7ef164a8a160", "Content-Length": "400"}
    forbidden.body = json.dumps(
        {
            "kind": "Status",
            "apiVersion": "v1",
            "status": "Failure",
            "message": (
                'pods "klusterlet-646d4fdd8b-4kz56" is forbidden: User '
                '"system:serviceaccount:openshiftpulse:pulse-openshift-sre-agent" cannot delete resource '
                '"pods" in API group "" in the namespace "open-cluster-management-agent"'
            ),
            "reason": "Forbidden",
            "code": 403,
        }
    )
    with patch("sre_agent.monitor.fix_planner.execute_fix", side_effect=forbidden):
        report = approve_fix("a-1", "sre@example.com")
    assert report["status"] == "failed"
    assert report["error"] == (
        'pods "klusterlet-646d4fdd8b-4kz56" is forbidden: User '
        '"system:serviceaccount:openshiftpulse:pulse-openshift-sre-agent" cannot delete resource '
        '"pods" in API group "" in the namespace "open-cluster-management-agent"'
    )
    assert "HTTPHeaderDict" not in report["error"]
    assert "Audit-Id" not in report["error"]


def test_an_unexecutable_fix_is_blocked_with_remediation_before_touching_the_cluster(wired):
    """The agent's ClusterRole is read-only unless allowWriteOperations is on.
    Approving a fix the SA provably cannot perform must fail fast with the
    remediation, not run and 403 — and the blocked report still reaches fix
    history."""
    remediation = (
        "The agent's service account cannot delete pods in namespace 'prod' — enable spec.agent.allowWriteOperations."
    )
    with patch(
        "sre_agent.monitor.rbac_preflight.can_execute",
        return_value=(False, remediation),
    ):
        report = approve_fix("a-1", "sre@example.com")

    wired.execute.assert_not_called()
    assert report["status"] == "failed"
    assert "allowWriteOperations" in report["error"]
    assert wired.saved and wired.saved[0]["status"] == "failed"
