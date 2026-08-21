"""Remediation must not depend on somebody having a browser tab open.

``auto_fix`` is called only at trust level >= 2, and the effective trust level
was "max among subscribers, or 1 if none". Subscribers are browser tabs. So
with nobody watching, trust was 1, ``auto_fix`` was never entered, and the
agent quietly did nothing about problems it had correctly diagnosed.

Measured on the reference cluster after days of running: 2,528 investigations,
``total_actions: 0``, and not a single auto-fix line anywhere in the logs — the
function had never been called. This is the same shape as the scan loop only
running while a client was connected. That half was fixed months later; this
half was left behind.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes.client.rest import ApiException

from sre_agent.monitor.cluster_monitor import ClusterMonitor


def _client(trust_level=1, categories=None):
    c = MagicMock()
    c.trust_level = trust_level
    c.auto_fix_categories = set(categories or [])
    return c


# ── trust level ───────────────────────────────────────────────────────────


def test_nobody_watching_does_not_mean_untrusted():
    """The whole bug in one assertion."""
    monitor = ClusterMonitor()
    monitor._subscribers = []
    with patch("sre_agent.monitor.cluster_monitor.get_settings") as settings:
        settings.return_value.monitor.max_trust_level = 2
        assert monitor.effective_trust_level == 2


def test_a_subscriber_may_supervise_more_closely():
    """A human electing a higher trust level still wins."""
    monitor = ClusterMonitor()
    monitor._subscribers = [_client(trust_level=3)]
    with patch("sre_agent.monitor.cluster_monitor.get_settings") as settings:
        settings.return_value.monitor.max_trust_level = 2
        assert monitor.effective_trust_level == 3


def test_a_subscriber_cannot_lower_the_configured_level():
    """A tab open at trust 1 must not disarm a server configured for 2 — that
    was the old behaviour by accident, and it is the wrong default."""
    monitor = ClusterMonitor()
    monitor._subscribers = [_client(trust_level=1)]
    with patch("sre_agent.monitor.cluster_monitor.get_settings") as settings:
        settings.return_value.monitor.max_trust_level = 2
        assert monitor.effective_trust_level == 2


def test_a_conservative_deployment_stays_conservative():
    monitor = ClusterMonitor()
    monitor._subscribers = []
    with patch("sre_agent.monitor.cluster_monitor.get_settings") as settings:
        settings.return_value.monitor.max_trust_level = 1
        assert monitor.effective_trust_level == 1


# ── categories ────────────────────────────────────────────────────────────


def test_what_the_server_can_fix_is_allowed_with_no_tab_open():
    """An empty union meant a trust-3 deployment filtered every category out."""
    from sre_agent.monitor.autofix import AUTO_FIX_HANDLERS

    monitor = ClusterMonitor()
    monitor._subscribers = []
    assert monitor.effective_auto_fix_categories == set(AUTO_FIX_HANDLERS)


def test_a_subscriber_can_widen_the_categories():
    monitor = ClusterMonitor()
    monitor._subscribers = [_client(categories={"something_custom"})]
    assert "something_custom" in monitor.effective_auto_fix_categories
    assert "crashloop" in monitor.effective_auto_fix_categories


# ── ask-first with nobody to ask ──────────────────────────────────────────


@pytest.fixture
def monitor():
    m = ClusterMonitor()
    m._subscribers = []
    m._broadcast_raw = AsyncMock()
    m._recent_fixes = []
    return m


_PLAN = MagicMock(
    strategy="restart_pod",
    cause_category="crashloop",
    description="Delete the pod so its controller recreates it",
    confidence=0.9,
)

FINDING = {
    "id": "f-1",
    "category": "crashloop",
    "title": "Pod api-7f9 restarting (12x)",
    "autoFixable": True,
    "resources": [{"kind": "Pod", "name": "api-7f9", "namespace": "prod"}],
}


@pytest.mark.asyncio
async def test_it_proposes_rather_than_waiting_for_an_approval_nobody_can_give(monitor):
    """Trust 2 means ask first. With no subscriber there is no one to ask, and
    blocking 120s per finding would stall a 65-second scan loop."""
    saved = []
    with (
        patch("sre_agent.monitor.cluster_monitor.is_autofix_paused", return_value=False),
        patch("sre_agent.monitor.cluster_monitor.get_settings") as settings,
        patch("sre_agent.monitor.cluster_monitor.save_action", side_effect=lambda r, **kw: saved.append(r)),
        patch("sre_agent.monitor.cluster_monitor.get_core_client") as core,
        patch("sre_agent.monitor.cluster_monitor._estimate_auto_fix_confidence", return_value=0.9),
        patch("sre_agent.monitor.fix_planner.get_investigation_for_finding", return_value=None),
        patch("sre_agent.monitor.fix_planner.default_fix_plan", return_value=_PLAN),
    ):
        settings.return_value.monitor.autofix_enabled = True
        settings.return_value.monitor.max_trust_level = 2
        core.return_value.read_namespaced_pod.return_value = MagicMock(
            metadata=MagicMock(owner_references=[MagicMock(kind="ReplicaSet", name="api")])
        )
        await monitor.auto_fix([dict(FINDING)])

    assert len(saved) == 1, "the proposal must be recorded, not dropped"
    assert saved[0]["status"] == "proposed"
    assert "nobody was connected" in saved[0]["reasoning"]


@pytest.mark.asyncio
async def test_it_never_executes_unsupervised_just_because_nobody_is_looking(monitor):
    """Absence of a reviewer is not consent."""
    saved = []
    with (
        patch("sre_agent.monitor.cluster_monitor.is_autofix_paused", return_value=False),
        patch("sre_agent.monitor.cluster_monitor.get_settings") as settings,
        patch("sre_agent.monitor.cluster_monitor.save_action", side_effect=lambda r, **kw: saved.append(r)),
        patch("sre_agent.monitor.cluster_monitor.get_core_client") as core,
        patch("sre_agent.monitor.cluster_monitor._estimate_auto_fix_confidence", return_value=0.9),
        patch("sre_agent.monitor.fix_planner.get_investigation_for_finding", return_value=None),
        patch("sre_agent.monitor.fix_planner.default_fix_plan", return_value=_PLAN),
        patch("sre_agent.monitor.autofix.AUTO_FIX_HANDLERS", {"crashloop": MagicMock()}) as handlers,
    ):
        settings.return_value.monitor.autofix_enabled = True
        settings.return_value.monitor.max_trust_level = 2
        core.return_value.read_namespaced_pod.return_value = MagicMock(
            metadata=MagicMock(owner_references=[MagicMock(kind="ReplicaSet", name="api")])
        )
        await monitor.auto_fix([dict(FINDING)])

        handlers["crashloop"].assert_not_called()
    assert saved and saved[0]["status"] == "proposed"


@pytest.mark.asyncio
async def test_it_does_not_ask_the_same_question_every_scan(monitor):
    """One hour of unattended proposing produced 701 rows for two findings on
    the reference cluster. A proposal is a question; asking it again every 65
    seconds because nobody has answered is a flood, not persistence."""
    saved = []
    repo = MagicMock()
    repo.check_pending_proposal.return_value = {"id": "a-already-asked"}
    with (
        patch("sre_agent.monitor.cluster_monitor.is_autofix_paused", return_value=False),
        patch("sre_agent.monitor.cluster_monitor.get_settings") as settings,
        patch("sre_agent.monitor.cluster_monitor.get_monitor_repo", return_value=repo),
        patch("sre_agent.monitor.cluster_monitor.save_action", side_effect=lambda r, **kw: saved.append(r)),
        patch("sre_agent.monitor.cluster_monitor.get_core_client") as core,
        patch("sre_agent.monitor.cluster_monitor._estimate_auto_fix_confidence", return_value=0.9),
        patch("sre_agent.monitor.fix_planner.get_investigation_for_finding", return_value=None),
        patch("sre_agent.monitor.fix_planner.default_fix_plan", return_value=_PLAN),
    ):
        settings.return_value.monitor.autofix_enabled = True
        settings.return_value.monitor.max_trust_level = 2
        core.return_value.read_namespaced_pod.return_value = MagicMock(
            metadata=MagicMock(owner_references=[MagicMock(kind="ReplicaSet", name="api")])
        )
        await monitor.auto_fix([dict(FINDING)])

    assert saved == [], "an unanswered proposal must not be raised again"
    asked_with = repo.check_pending_proposal.call_args[0][0]
    assert asked_with != FINDING["id"], "the finding id is per-scan and can never match"
    assert asked_with == "crashloop:prod:Pod/api-7f9"


@pytest.mark.asyncio
async def test_the_first_proposal_is_still_recorded(monitor):
    saved = []
    repo = MagicMock()
    repo.check_pending_proposal.return_value = None
    with (
        patch("sre_agent.monitor.cluster_monitor.is_autofix_paused", return_value=False),
        patch("sre_agent.monitor.cluster_monitor.get_settings") as settings,
        patch("sre_agent.monitor.cluster_monitor.get_monitor_repo", return_value=repo),
        patch("sre_agent.monitor.cluster_monitor.save_action", side_effect=lambda r, **kw: saved.append(r)),
        patch("sre_agent.monitor.cluster_monitor.get_core_client") as core,
        patch("sre_agent.monitor.cluster_monitor._estimate_auto_fix_confidence", return_value=0.9),
        patch("sre_agent.monitor.fix_planner.get_investigation_for_finding", return_value=None),
        patch("sre_agent.monitor.fix_planner.default_fix_plan", return_value=_PLAN),
    ):
        settings.return_value.monitor.autofix_enabled = True
        settings.return_value.monitor.max_trust_level = 2
        core.return_value.read_namespaced_pod.return_value = MagicMock(
            metadata=MagicMock(owner_references=[MagicMock(kind="ReplicaSet", name="api")])
        )
        await monitor.auto_fix([dict(FINDING)])

    assert len(saved) == 1 and saved[0]["status"] == "proposed"


# ── a failed fix is recorded readably, not as an object dump ──────────────


@pytest.mark.asyncio
async def test_a_forbidden_autofix_is_saved_with_a_readable_message_not_a_header_dump(monitor):
    """str(ApiException) dumps the whole object — HTTPHeaderDict, Audit-Id,
    Content-Length, the works. Only the Status body's own message belongs in
    what a person sees for a failed unsupervised fix.
    """
    forbidden = ApiException(status=403, reason="Forbidden")
    forbidden.headers = {"Audit-Id": "d5f6ffee-5dec-485f-9461-7ef164a8a160"}
    forbidden.body = json.dumps(
        {
            "kind": "Status",
            "status": "Failure",
            "message": 'pods "klusterlet-646d4fdd8b-4kz56" is forbidden: cannot delete resource "pods"',
            "reason": "Forbidden",
            "code": 403,
        }
    )
    saved = []
    with (
        patch("sre_agent.monitor.cluster_monitor.is_autofix_paused", return_value=False),
        patch("sre_agent.monitor.cluster_monitor.get_settings") as settings,
        patch("sre_agent.monitor.cluster_monitor.save_action", side_effect=lambda r, **kw: saved.append(r)),
        patch("sre_agent.monitor.cluster_monitor.get_core_client") as core,
        patch("sre_agent.monitor.cluster_monitor._estimate_auto_fix_confidence", return_value=0.9),
        patch("sre_agent.monitor.fix_planner.get_investigation_for_finding", return_value=None),
        patch("sre_agent.monitor.fix_planner.default_fix_plan", return_value=_PLAN),
        patch("sre_agent.monitor.fix_planner.execute_fix", side_effect=forbidden),
    ):
        settings.return_value.monitor.autofix_enabled = True
        settings.return_value.monitor.max_trust_level = 3
        core.return_value.read_namespaced_pod.return_value = MagicMock(
            metadata=MagicMock(owner_references=[MagicMock(kind="ReplicaSet", name="api")])
        )
        await monitor.auto_fix([dict(FINDING)])

    assert len(saved) == 1
    assert saved[0]["status"] == "failed"
    assert saved[0]["error"] == 'pods "klusterlet-646d4fdd8b-4kz56" is forbidden: cannot delete resource "pods"'
    assert "HTTPHeaderDict" not in saved[0]["error"]
    assert "Audit-Id" not in saved[0]["error"]


# ── against the real database, because mocks could not see this ───────────


def test_the_finding_id_can_never_match_its_own_previous_proposal():
    """The bug in one test, run against a real Postgres.

    Mocks could not catch it: they answer whatever key they are handed. Only
    real rows show that ``_make_finding`` mints a new ``f-{uuid4}`` every scan,
    so a finding-id lookup misses the proposal it made 65 seconds ago and
    proposes again — 718 rows on the reference cluster, one per sighting.
    """
    from sre_agent.db import get_database
    from sre_agent.inbox import _finding_corr_key
    from sre_agent.monitor.actions import save_action
    from sre_agent.monitor.findings import _make_finding
    from sre_agent.repositories import get_monitor_repo

    resources = [{"kind": "Pod", "name": "olm-operator-7f6-abcde", "namespace": "olm-dedupe-test"}]
    first = _make_finding("critical", "crashloop", "Pod restarting", "", resources)
    save_action(
        {"id": "a-dedupe-1", "findingId": first["id"], "status": "proposed", "tool": "", "reasoning": "proposed"},
        category="crashloop",
        resources=resources,
        finding=first,
    )

    # The next scan. Same condition, new finding id — always.
    again = _make_finding("critical", "crashloop", "Pod restarting", "", resources)
    assert again["id"] != first["id"]
    assert _finding_corr_key(again) == _finding_corr_key(first)

    db = get_database()
    by_finding_id = db.fetchone("SELECT id FROM actions WHERE finding_id = ? AND status = 'proposed'", (again["id"],))
    assert by_finding_id is None, "this is why the guard never fired"

    by_key = get_monitor_repo().check_pending_proposal(_finding_corr_key(again))
    assert by_key is not None and by_key["id"] == "a-dedupe-1"
