"""Three gaps found by working the reference cluster through Pulse itself.

*What changed just before this started* was always empty, because the window
was anchored on when Pulse opened an episode rather than on when the condition
began — 30 hours apart on the live cluster.

*Nothing reaches a person who is not looking.* The one outbound path fired per
critical finding, which for one control-plane problem would have been 33
messages.

*The one recurring problem it could plausibly fix, it could not touch*, because
every alert arrived under one category and a category cannot carry a remedy.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sre_agent.monitor import episodes as ep
from sre_agent.monitor import webhook
from sre_agent.monitor.fix_planner import alert_fix_plan, default_fix_plan
from sre_agent.monitor.scanners import _alert_is_fixable

MODULE = "sre_agent.monitor.episodes"
HOUR = 3600
POD = [{"kind": "Pod", "name": "olm-operator-7f6", "namespace": "openshift-operator-lifecycle-manager"}]
PLACEHOLDER = [{"kind": "Alert", "name": "CsvAbnormalFailedOver2Min", "namespace": "openshift-monitoring"}]


# ── what changed, measured from the cause ─────────────────────────────────


@pytest.fixture
def repo():
    r = MagicMock()
    with patch(f"{MODULE}._repo", return_value=r):
        yield r


def test_the_change_window_follows_the_cause_not_the_episode(repo):
    """Observed live: a cause firing 30 hours, an episode 12 minutes old, and a
    change window covering the half hour before the episode — a day after
    anything that could have caused it."""
    opened_at = 1_000_000
    began_at = opened_at - 30 * HOUR
    repo.get.return_value = {"id": "ep-1", "started_at": opened_at, "cause_started_at": began_at}
    inbox = MagicMock()
    inbox.fetch_items_by_category_window.return_value = []
    with patch("sre_agent.repositories.inbox_repo.get_inbox_repo", return_value=inbox):
        ep.changes_around("ep-1")

    window_start, window_end = inbox.fetch_items_by_category_window.call_args[0][1:3]
    assert window_end == began_at, "the window ends when the condition began"
    assert window_start == began_at - ep._CHANGE_LOOKBACK_SECONDS


def test_without_a_known_onset_it_still_uses_the_episode(repo):
    """Scanner-detected conditions report no onset of their own. The episode's
    own start is the best that is known for them."""
    opened_at = 1_000_000
    repo.get.return_value = {"id": "ep-1", "started_at": opened_at, "cause_started_at": None}
    inbox = MagicMock()
    inbox.fetch_items_by_category_window.return_value = []
    with patch("sre_agent.repositories.inbox_repo.get_inbox_repo", return_value=inbox):
        ep.changes_around("ep-1")
    assert inbox.fetch_items_by_category_window.call_args[0][2] == opened_at


def test_the_cause_onset_is_recorded_when_the_episode_opens(repo):
    repo.find_open_by_correlation.return_value = None
    repo.find_recent_closed_by_correlation.return_value = None
    repo.open_symptom_index.return_value = {}
    finding = {
        "id": "f-1",
        "category": "control_plane",
        "title": "etcd lost its leader",
        "findingType": "current",
        "startedAt": 1_700_000_000,
        "resources": [{"kind": "Pod", "name": "etcd-0", "namespace": "openshift-etcd"}],
    }
    ep.open_or_touch(finding)
    assert repo.create.call_args.kwargs["cause_started_at"] == 1_700_000_000


# ── reaching a person ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_episode_announces_itself_once():
    with patch.object(webhook, "_post", new=AsyncMock()) as post:
        await webhook.notify_episode_opened("ep-1", {"title": "etcd lost its leader", "severity": "critical"})
    assert post.call_args[0][0] == "episode_opened"
    assert post.call_args[0][1]["cause"] == "etcd lost its leader"


@pytest.mark.asyncio
async def test_a_symptom_of_an_open_episode_stays_quiet():
    """One control-plane problem produced nine findings. Nine messages
    describing one event is how people learn to mute the channel."""
    finding = {"category": "crashloop", "title": "Pod x restarting", "resources": POD, "severity": "critical"}
    with (
        patch.object(webhook, "_post", new=AsyncMock()) as post,
        patch("sre_agent.monitor.episodes.symptom_keys_by_episode", return_value={"crashloop:ns:Pod/x": "ep-1"}),
        patch("sre_agent.inbox._finding_corr_key", return_value="crashloop:ns:Pod/x"),
    ):
        await webhook._send_webhook(finding)
    post.assert_not_called()


@pytest.mark.asyncio
async def test_a_finding_nobody_explains_still_gets_through():
    finding = {"category": "crashloop", "title": "Pod x restarting", "resources": POD, "severity": "critical"}
    with (
        patch.object(webhook, "_post", new=AsyncMock()) as post,
        patch("sre_agent.monitor.episodes.symptom_keys_by_episode", return_value={}),
        patch("sre_agent.inbox._finding_corr_key", return_value="crashloop:ns:Pod/x"),
    ):
        await webhook._send_webhook(finding)
    post.assert_called_once()


@pytest.mark.asyncio
async def test_a_broken_suppression_check_never_silences_a_notification():
    """Sending twice is a nuisance. Sending nothing is the failure this module
    exists to prevent."""
    finding = {"category": "crashloop", "title": "Pod x", "resources": POD, "severity": "critical"}
    with (
        patch.object(webhook, "_post", new=AsyncMock()) as post,
        patch("sre_agent.monitor.episodes.symptom_keys_by_episode", side_effect=RuntimeError("db down")),
    ):
        await webhook._send_webhook(finding)
    post.assert_called_once()


@pytest.mark.asyncio
async def test_a_proposal_says_how_to_answer_it():
    """A message that reports a problem without saying what to do about it is
    only half a notification."""
    action = {"id": "a-1", "findingId": "f-1", "reasoning": "Restart the OLM operator"}
    with patch.object(webhook, "_post", new=AsyncMock()) as post:
        await webhook.notify_fix_proposed(action, {"title": "CsvAbnormalFailedOver2Min", "resources": POD})
    body = post.call_args[0][1]
    assert body["approveWith"] == "POST /fix-history/a-1/approve"


@pytest.mark.asyncio
async def test_no_webhook_configured_is_silent_not_an_error():
    with patch.object(webhook, "_get_webhook_url", return_value=""):
        await webhook._post("anything", {})  # must not raise


# ── a remedy for the one problem it could not touch ───────────────────────


def test_the_stuck_olm_operator_now_has_a_remedy():
    """Firing for 30 hours on the reference cluster with nothing able to act."""
    plan = alert_fix_plan({"title": "CsvAbnormalFailedOver2Min", "resources": POD})
    assert plan is not None
    assert plan.strategy == "restart_controller"
    assert plan.params["resources"] == POD


def test_an_alert_with_no_pod_to_restart_gets_no_plan():
    """A proposal an operator could never carry out is worse than none."""
    assert alert_fix_plan({"title": "CsvAbnormalFailedOver2Min", "resources": PLACEHOLDER}) is None
    assert not _alert_is_fixable("CsvAbnormalFailedOver2Min", PLACEHOLDER)


@pytest.mark.parametrize("alertname", ["TargetDown", "HighOverallControlPlaneMemory", "ArgoCDSyncAlert"])
def test_most_alerts_still_have_no_automated_remedy(alertname):
    """An alert says something is wrong, not what to do about it. Guessing is
    how an automated fixer earns its reputation."""
    assert alert_fix_plan({"title": alertname, "resources": POD}) is None
    assert not _alert_is_fixable(alertname, POD)


def test_the_alert_path_does_not_disturb_the_category_path():
    assert default_fix_plan("crashloop", {"resources": POD}).strategy == "restart_controller"
    assert default_fix_plan("nonsense", {"resources": POD}) is None
