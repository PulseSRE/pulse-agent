"""Tests for episodes — the noun the product was missing.

The scenario every one of these is drawn from: at 20:35 on a real cluster the
monitor produced fourteen findings in one second. Nine "Deployment degraded"
rated critical, three pod restarts, and one etcdMemberCommunicationSlow rated
*warning* — which was the cause of the other thirteen.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from sre_agent.monitor import episodes as ep
from sre_agent.monitor.layers import can_explain, layer_name, layer_of

MODULE = "sre_agent.monitor.episodes"


def _finding(category, title, namespace="demo", name="thing", kind="Pod"):
    return {
        "id": f"f-{title[:8]}",
        "category": category,
        "title": title,
        "resources": [{"kind": kind, "name": name, "namespace": namespace}],
    }


ETCD = _finding("control_plane", "etcd changed leader 12 times in an hour", "", "cluster", "Etcd")
DEPLOY = _finding(
    "workloads", "Deployment ocm-controller degraded (0/2)", "multicluster-engine", "ocm-controller", "Deployment"
)
POD = _finding(
    "crashloop", "Pod grc-policy-propagator restarting (14x)", "open-cluster-management", "grc-policy-propagator"
)
ALERT = _finding("alerts", "TargetDown", "open-cluster-management", "TargetDown", "Alert")


# ── the layer model ───────────────────────────────────────────────────────


def test_infrastructure_can_explain_a_workload():
    assert can_explain("control_plane", "workloads")
    assert can_explain("control_plane", "crashloop")
    assert can_explain("nodes", "alerts")


def test_a_workload_can_never_explain_infrastructure():
    """A crashing pod does not take down the API server."""
    assert not can_explain("crashloop", "control_plane")
    assert not can_explain("workloads", "nodes")


def test_a_layer_cannot_absorb_its_own_peers():
    """Two unrelated crashloops are not evidence about each other."""
    assert not can_explain("crashloop", "workloads")
    assert not can_explain("crashloop", "crashloop")


def test_pulse_self_checks_are_never_symptoms_or_causes():
    """An outage does not break a scanner; a broken scanner does not crash pods."""
    assert not can_explain("control_plane", "degraded")
    assert not can_explain("degraded", "crashloop")


def test_an_unknown_category_sits_at_workload_level():
    """A category added later must degrade safely, not crash or take precedence."""
    assert layer_name("something_invented_later") == "workload"
    # It can be explained by infrastructure...
    assert can_explain("control_plane", "something_invented_later")
    # ...and can never outrank infrastructure or absorb its own peers.
    assert not can_explain("something_invented_later", "control_plane")
    assert not can_explain("something_invented_later", "crashloop")


def test_an_unknown_category_can_never_head_an_episode(repo):
    """Sitting at workload level is what keeps a new scanner from absorbing the cluster."""
    assert ep.open_or_touch(_finding("something_invented_later", "new thing")) is None


def test_the_20_35_pairing_resolves_the_right_way_round():
    assert can_explain(ETCD["category"], DEPLOY["category"])
    assert not can_explain(DEPLOY["category"], ETCD["category"])
    assert layer_of(ETCD["category"]) < layer_of(DEPLOY["category"])


# ── opening episodes ──────────────────────────────────────────────────────


@pytest.fixture
def repo():
    r = MagicMock()
    r.find_open_by_correlation.return_value = None
    r.find_recent_closed_by_correlation.return_value = None
    r.detached_keys.return_value = set()
    r.attach.return_value = True
    with patch(f"{MODULE}._repo", return_value=r):
        yield r


def test_an_infrastructure_finding_opens_an_episode(repo):
    assert ep.open_or_touch(ETCD) is not None
    assert repo.create.called
    assert repo.create.call_args.kwargs["cause_category"] == "control_plane"


def test_a_workload_finding_does_not_head_an_episode(repo):
    """A single restarting pod must not absorb signal findings cluster-wide."""
    assert ep.open_or_touch(POD) is None
    assert not repo.create.called


def test_a_platform_finding_may_head_an_episode(repo):
    """A stuck finalizer genuinely does explain workloads beneath it."""
    assert ep.open_or_touch(_finding("stuck", "Namespace stuck terminating", "")) is not None


def test_a_self_check_finding_never_heads_an_episode(repo):
    assert ep.open_or_touch(_finding("degraded", "Scanner alerts has failed 7 runs")) is None


def test_re_detecting_the_same_cause_reuses_its_episode(repo):
    repo.find_open_by_correlation.return_value = {"id": "ep-existing", "status": "open", "started_at": 1}
    assert ep.open_or_touch(ETCD) == "ep-existing"
    assert not repo.create.called
    assert repo.touch.called


def test_the_same_cause_returning_within_a_day_is_marked_a_recurrence(repo):
    """'Sixth time today, escalating' is the most actionable sentence available."""
    repo.find_recent_closed_by_correlation.return_value = {"id": "ep-earlier"}
    ep.open_or_touch(ETCD)
    assert repo.create.call_args.kwargs["recurrence_of"] == "ep-earlier"


# ── attaching symptoms ────────────────────────────────────────────────────


def _open_episode(started_at):
    return {"id": "ep-1", "status": "open", "started_at": started_at}


def test_symptoms_beneath_the_cause_attach(repo):
    now = int(time.time())
    repo.get.return_value = _open_episode(now)
    attached = ep.attach_symptoms("ep-1", "control_plane", [ETCD, DEPLOY, POD, ALERT], {})
    assert attached == 3


def test_the_cause_does_not_attach_to_itself(repo):
    now = int(time.time())
    repo.get.return_value = _open_episode(now)
    ep.attach_symptoms("ep-1", "control_plane", [ETCD], {})
    assert not repo.attach.called


def test_something_already_broken_before_the_cause_is_not_a_symptom(repo):
    """A pod crashlooping an hour before etcd wobbled was not caused by etcd."""
    now = int(time.time())
    repo.get.return_value = _open_episode(now)
    from sre_agent.inbox import _finding_corr_key

    first_seen = {_finding_corr_key(POD): now - 3600}
    ep.attach_symptoms("ep-1", "control_plane", [POD], first_seen)
    assert not repo.attach.called


def test_a_symptom_just_inside_the_grace_window_still_attaches(repo):
    """Causes are usually detected a cycle or two after the damage starts."""
    now = int(time.time())
    repo.get.return_value = _open_episode(now)
    from sre_agent.inbox import _finding_corr_key

    first_seen = {_finding_corr_key(POD): now - 60}
    ep.attach_symptoms("ep-1", "control_plane", [POD], first_seen)
    assert repo.attach.called


def test_a_detached_symptom_is_never_re_attached(repo):
    """An operator said this was not related. Do not argue with them every minute."""
    now = int(time.time())
    repo.get.return_value = _open_episode(now)
    from sre_agent.inbox import _finding_corr_key

    repo.detached_keys.return_value = {_finding_corr_key(POD)}
    ep.attach_symptoms("ep-1", "control_plane", [POD], {})
    attached_keys = [c.args[1] for c in repo.attach.call_args_list]
    assert _finding_corr_key(POD) not in attached_keys


def test_a_closed_episode_attaches_nothing(repo):
    repo.get.return_value = {"id": "ep-1", "status": "closed", "started_at": 1}
    assert ep.attach_symptoms("ep-1", "control_plane", [DEPLOY], {}) == 0


def test_the_rollup_is_refreshed_only_when_something_attached(repo):
    now = int(time.time())
    repo.get.return_value = _open_episode(now)
    ep.attach_symptoms("ep-1", "control_plane", [ETCD], {})
    assert not repo.refresh_rollup.called
    ep.attach_symptoms("ep-1", "control_plane", [DEPLOY], {})
    assert repo.refresh_rollup.called


# ── closing and detaching ─────────────────────────────────────────────────


def test_closing_by_correlation_finds_the_open_episode(repo):
    repo.find_open_by_correlation.return_value = {"id": "ep-1"}
    assert ep.close_for_correlation("control_plane::Etcd/cluster") is True
    assert repo.close.called


def test_closing_a_cause_with_no_episode_is_not_an_error(repo):
    repo.find_open_by_correlation.return_value = None
    assert ep.close_for_correlation("crashloop:demo:Pod/x") is False


def test_detaching_records_who_said_so(repo):
    repo.detach.return_value = True
    assert ep.detach("ep-1", "crashloop:demo:Pod/x", "alice") is True
    assert repo.detach.call_args.args[2] == "alice"
    assert repo.refresh_rollup.called


def test_detaching_something_not_attached_returns_false(repo):
    repo.detach.return_value = False
    assert ep.detach("ep-1", "nope", "alice") is False


def test_the_symptom_index_survives_a_database_error(repo):
    """Ranking must degrade to 'no episodes' rather than take the scan down."""
    repo.open_symptom_index.side_effect = RuntimeError("db down")
    assert ep.symptom_keys_by_episode() == {}


# ── over-attachment, found by running against a live cluster ──────────────
# The unit tests above all passed while the engine did this: a full scan of a
# real cluster produced seven episodes headed by "Certificate expiring in 9d",
# between them absorbing 21 of 23 findings. Every guard was working; the layer
# assignment was simply wrong. These fix the class.


def test_a_certificate_expiring_next_week_cannot_head_an_episode(repo):
    """It has not happened yet, so it has caused nothing."""
    assert ep.open_or_touch(_finding("cert_expiry", "Certificate addon-webhook expiring in 9d")) is None


def test_a_security_posture_finding_cannot_head_an_episode(repo):
    """A standing posture is a property of the cluster, not an event in it."""
    assert ep.open_or_touch(_finding("security", "88 privileged containers across 16 namespaces")) is None


def test_a_forecast_cannot_head_an_episode_even_at_the_infrastructure_layer(repo):
    """Node memory exhaustion predicted in 3 days did not crash anything today."""
    forecast = _finding("memory_pressure", "Node ip-10-0-1-24 memory exhaustion predicted in 3 days")
    forecast["findingType"] = "trend"
    assert ep.open_or_touch(forecast) is None


def test_a_current_infrastructure_finding_still_heads_one(repo):
    """The fix must not silence the case the feature exists for."""
    assert ep.open_or_touch(ETCD) is not None


@pytest.mark.parametrize(
    "category,finding_type,expected",
    [
        ("control_plane", "current", True),
        ("nodes", "current", True),
        ("stuck", "current", True),
        ("hot_loop", "current", True),
        ("cert_expiry", "current", False),
        ("security", "current", False),
        ("memory_pressure", "trend", False),
        ("crashloop", "current", False),
        ("alerts", "current", False),
        ("degraded", "current", False),
    ],
)
def test_the_full_can_head_matrix(category, finding_type, expected):
    from sre_agent.monitor.layers import can_head_episode

    assert can_head_episode(category, finding_type) is expected
