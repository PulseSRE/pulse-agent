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

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
