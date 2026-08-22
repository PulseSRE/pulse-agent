"""Alerts as causes: what an alert is about, and when it actually started.

Every alert used to arrive as ``category="alerts"``, which the layer model
reads as signal — never a cause. On the reference cluster that made the episode
layer structurally dead: 15 of 15 standing findings were alerts, ``/episodes``
returned ``[]``, and meanwhile a single LLM investigation of one of those same
alerts correctly tied four of them into one story. The deterministic layer knew
less than the model did, about data it already had.

The numbers in these tests are that cluster's, read from ``ALERTS_FOR_STATE``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sre_agent.inbox import _finding_corr_key
from sre_agent.monitor import episodes as ep
from sre_agent.monitor.alert_layers import alert_layer, is_posture_alert
from sre_agent.monitor.findings import _make_finding
from sre_agent.monitor.layers import (
    L_INFRA,
    L_PLATFORM,
    L_SIGNAL,
    L_WORKLOAD,
    can_explain_finding,
    can_head_episode_finding,
    layer_for_finding,
)
from sre_agent.monitor.scanners import _alert_active_since

MODULE = "sre_agent.monitor.episodes"
NOW = 1_787_260_000
HOUR = 3600


def _alert(name, ns="openshift-monitoring", hours_firing=1.0):
    return _make_finding(
        severity="warning",
        category="alerts",
        title=name,
        summary="",
        resources=[{"kind": "Alert", "name": name, "namespace": ns}],
        layer=alert_layer(name),
        posture=is_posture_alert(name),
        started_at=int(NOW - hours_firing * HOUR),
    )


# ── classifying an alert by what it is about ──────────────────────────────


@pytest.mark.parametrize(
    "name,expected",
    [
        ("HighOverallControlPlaneMemory", L_INFRA),
        ("etcdNoLeader", L_INFRA),
        ("CsvAbnormalFailedOver2Min", L_PLATFORM),
        ("ClusterOperatorDegraded", L_PLATFORM),
        ("KubeJobFailed", L_WORKLOAD),
        ("TargetDown", L_SIGNAL),
    ],
)
def test_an_alert_is_layered_by_its_subject(name, expected):
    assert alert_layer(name) == expected


def test_an_unclassified_alert_stays_at_signal():
    """Being wrong here costs a missed correlation. Being wrong the other way
    costs a confident story about a cause that is not the cause."""
    assert alert_layer("SomeAlertNobodyHasSeenBefore") == L_SIGNAL
    assert not can_head_episode_finding(_alert("SomeAlertNobodyHasSeenBefore"))


def test_a_standing_configuration_is_neither_cause_nor_symptom():
    """AlertmanagerReceiversNotConfigured had been firing for fifty hours. No
    outage caused it and it caused no outage."""
    posture = _alert("AlertmanagerReceiversNotConfigured", hours_firing=50.1)
    assert is_posture_alert("AlertmanagerReceiversNotConfigured")
    assert not can_head_episode_finding(posture)
    assert not can_explain_finding(_alert("HighOverallControlPlaneMemory", hours_firing=23.5), posture)


def test_the_declared_layer_beats_the_category():
    memory = _alert("HighOverallControlPlaneMemory", hours_firing=23.5)
    assert memory["category"] == "alerts"
    assert layer_for_finding(memory) == L_INFRA
    assert can_head_episode_finding(memory)


def test_a_finding_without_a_declared_layer_still_uses_its_category():
    plain = _make_finding("critical", "control_plane", "etcd is unhappy", "", [])
    assert layer_for_finding(plain) == L_INFRA
    assert can_head_episode_finding(plain)


# ── onset parsing ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        "2026-08-19T20:36:01Z",
        "2026-08-19T20:36:01.123456789Z",  # nine fractional digits — fromisoformat refuses
        "2026-08-19T20:36:01.5+00:00",
    ],
)
def test_prometheus_onsets_parse(raw):
    assert _alert_active_since({"activeAt": raw}) == 1_787_171_761


@pytest.mark.parametrize("raw", [None, "", "not a time", 12345])
def test_an_unparseable_onset_is_simply_absent(raw):
    """Never guess. A finding with no onset falls back to when Pulse saw it."""
    assert _alert_active_since({"activeAt": raw}) is None


# ── attaching on real onsets ──────────────────────────────────────────────


@pytest.fixture
def repo():
    r = MagicMock()
    r.get.return_value = {"id": "ep-1", "status": "open", "started_at": NOW}
    r.detached_keys.return_value = set()
    r.attach.return_value = True
    r.open_symptom_index.return_value = {}
    with patch(f"{MODULE}._repo", return_value=r):
        yield r


def test_a_symptom_that_predates_the_cause_is_not_attached(repo):
    cause = _alert("HighOverallControlPlaneMemory", hours_firing=23.5)
    older = _alert("ArgoCDSyncAlert", "openshift-gitops", hours_firing=50.1)
    assert ep.attach_symptoms("ep-1", cause, [older], {}) == 0


def test_onsets_minutes_apart_are_still_one_event(repo):
    """Memory and the OLM install loop began six minutes apart on the real
    cluster. Prometheus `for:` durations differ per rule, so the old 180-second
    grace would have split a pair that was plainly one event."""
    cause = _alert("HighOverallControlPlaneMemory", hours_firing=23.5)
    olm = _alert("CsvAbnormalFailedOver2Min", "openshift-operator-lifecycle-manager", hours_firing=23.6)
    assert ep.attach_symptoms("ep-1", cause, [olm], {}) == 1


def test_a_symptom_that_started_after_the_cause_attaches(repo):
    """A cascade runs downhill in time, so later is the normal direction.

    The gap used to be 21.9 hours here, built on onsets I had guessed rather
    than measured; the real TargetDown on that cluster predated the cause by 26
    hours. Left as written it asserted the magnet — that a cause firing all day
    explains anything that breaks before midnight.
    """
    cause = _alert("HighOverallControlPlaneMemory", hours_firing=23.5)
    later = _alert("TargetDown", "openshift-lightspeed", hours_firing=23.0)
    assert ep.attach_symptoms("ep-1", cause, [later], {}) == 1


def test_without_onsets_it_falls_back_to_when_pulse_first_saw_it(repo):
    """Scanner-detected conditions report no onset of their own."""
    cause = _make_finding("critical", "control_plane", "etcd", "", [])
    pod = _make_finding("critical", "crashloop", "pod", "", [{"kind": "Pod", "name": "p", "namespace": "n"}])
    stale = {_finding_corr_key(pod): NOW - 9 * HOUR}
    assert ep.attach_symptoms("ep-1", cause, [pod], stale) == 0
    assert ep.attach_symptoms("ep-1", cause, [pod], {}) == 1


# ── one event, one cause ──────────────────────────────────────────────────


def test_a_symptom_already_owned_by_another_episode_is_left_alone(repo):
    """Three episodes each listing the same TargetDown is the "N findings that
    are wrong" problem wearing a different hat."""
    cause = _alert("ControlPlaneNodeMemoryHigh", hours_firing=3.0)
    target = _alert("TargetDown", "openshift-lightspeed", hours_firing=1.6)
    repo.open_symptom_index.return_value = {_finding_corr_key(target): "ep-somebody-else"}
    assert ep.attach_symptoms("ep-1", cause, [target], {}) == 0


def test_something_already_explained_does_not_head_its_own_episode(repo):
    """The OLM loop is a platform-layer cause *and* a symptom of the memory
    pressure beneath it. Heading its own episode too would report one event
    twice, with the same symptoms under each."""
    olm = _alert("CsvAbnormalFailedOver2Min", "openshift-operator-lifecycle-manager", hours_firing=23.6)
    repo.open_symptom_index.return_value = {_finding_corr_key(olm): "ep-memory"}
    assert ep.open_or_touch(olm) is None


def test_it_still_heads_an_episode_when_nothing_explains_it(repo):
    repo.find_open_by_correlation.return_value = None
    repo.find_recent_closed_by_correlation.return_value = None
    olm = _alert("CsvAbnormalFailedOver2Min", "openshift-operator-lifecycle-manager", hours_firing=23.6)
    assert ep.open_or_touch(olm) is not None


# ── the whole cluster, end to end ─────────────────────────────────────────

# Onsets measured on the live cluster with `time() - ALERTS_FOR_STATE`, as
# minutes relative to the cause. An earlier version of this fixture carried
# invented figures for the four alerts the first query truncated away, and
# predicted five symptoms; the measured onsets give two. Recorded here as
# offsets rather than absolute ages so the arithmetic the rule performs is the
# arithmetic the test states.
LIVE_CLUSTER = [
    # (alert, namespace, minutes relative to the cause's onset)
    ("HighOverallControlPlaneMemory", "openshift-machine-config-operator", 0.0),
    ("CsvAbnormalFailedOver2Min", "openshift-operator-lifecycle-manager", -4.5),
    ("KubeJobFailed", "openshift-marketplace", -293.6),
    ("ArgoCDSyncAlert", "openshift-gitops", -144.7),
    ("SearchPVCNotPresent", "open-cluster-management", -1594.2),
    ("TargetDown", "open-cluster-management", -1594.0),
    ("TargetDown", "multicluster-engine", -1594.0),
    ("TargetDown", "openshift-lightspeed", -1593.5),
    ("AlertmanagerReceiversNotConfigured", "openshift-monitoring", -1593.8),
    ("InsightsRecommendationActive", "openshift-insights", 1318.4),
    ("ControlPlaneNodeMemoryHigh", "openshift-monitoring", 1283.5),
]


def _correlate(findings):
    """The monitor's ordering and ownership rules, over a fixed finding set."""
    claimed: dict[str, str] = {}
    episodes: dict[str, list[str]] = {}
    for f in sorted(findings, key=lambda x: (layer_for_finding(x), x["startedAt"])):
        key = _finding_corr_key(f)
        if not can_head_episode_finding(f) or key in claimed or key in episodes:
            continue
        episodes[key] = []
        for g in findings:
            gk = _finding_corr_key(g)
            if g is f or gk in claimed or not can_explain_finding(f, g):
                continue
            if g["startedAt"] < f["startedAt"] - ep._ONSET_GRACE_SECONDS:
                continue
            claimed[gk] = key
            episodes[key].append(g["title"])
    return episodes, claimed


def test_the_real_cluster_attaches_only_what_started_with_the_cause():
    """The OLM install loop began four and a half minutes before the memory
    alert — inside the window, and the whole reason the window is fifteen
    minutes rather than the old one hundred and eighty seconds. Everything else
    standing on that cluster predates the cause by hours and is nobody's
    symptom."""
    findings = [_alert(n, ns, hours_firing=-m / 60) for n, ns, m in LIVE_CLUSTER]
    episodes, _ = _correlate(findings)

    headline = next(k for k, v in episodes.items() if v)
    assert "HighOverallControlPlaneMemory" in headline
    assert episodes[headline] == ["CsvAbnormalFailedOver2Min"]


def test_alerts_that_predate_the_cause_are_nobody_symptom():
    findings = [_alert(n, ns, hours_firing=-m / 60) for n, ns, m in LIVE_CLUSTER]
    episodes, claimed = _correlate(findings)
    accounted = set(episodes) | set(claimed)
    alone = sorted({f["title"] for f in findings if _finding_corr_key(f) not in accounted})
    assert alone == [
        "AlertmanagerReceiversNotConfigured",
        "ArgoCDSyncAlert",
        "InsightsRecommendationActive",
        "KubeJobFailed",
        "SearchPVCNotPresent",
        "TargetDown",
    ]


def test_one_symptom_never_lands_in_two_episodes():
    """Synthetic, not measured: it takes two causes at different depths sharing
    a symptom that started after both, which the reference cluster did not
    happen to be doing. The rule still has to hold — without it the layer fix
    lets every cause list the same symptom, which is the "N findings that are
    wrong" problem wearing a different hat."""
    deep = _alert("HighOverallControlPlaneMemory", "openshift-machine-config-operator", hours_firing=3.0)
    shallow = _alert("CsvAbnormalFailedOver2Min", "openshift-operator-lifecycle-manager", hours_firing=2.0)
    shared = _alert("TargetDown", "openshift-lightspeed", hours_firing=1.0)

    episodes, _claimed = _correlate([deep, shallow, shared])
    owners = [k for k, v in episodes.items() if "TargetDown" in v]
    assert len(owners) == 1, "a symptom must belong to exactly one episode"
    assert "HighOverallControlPlaneMemory" in owners[0], "the deepest cause owns it"
    # And the shallower cause, being itself explained, heads nothing.
    assert not any("Csv" in k for k in episodes)


# ── a long-running cause is not a magnet ──────────────────────────────────


def test_a_symptom_that_began_long_after_the_cause_is_not_attached(repo):
    """Measured on the reference cluster: a memory alert firing for thirty
    hours had collected 22 symptoms, among them a missing PVC — something
    memory pressure does not cause and cannot cause. Everything that broke
    during those thirty hours qualified, because "started after the cause" was
    the whole test."""
    cause = _alert("HighOverallControlPlaneMemory", hours_firing=30.0)
    much_later = _alert("SearchPVCNotPresentCritical", "open-cluster-management", hours_firing=1.0)
    assert ep.attach_symptoms("ep-1", cause, [much_later], {}) == 0


def test_a_cascade_within_the_hour_still_attaches(repo):
    """The real shape of a cascade: memory starves the API server, the API
    server times out probes, the probes kill pods. Minutes, not days."""
    cause = _alert("HighOverallControlPlaneMemory", hours_firing=30.0)
    cascade = _alert("TargetDown", "openshift-lightspeed", hours_firing=29.5)
    assert ep.attach_symptoms("ep-1", cause, [cascade], {}) == 1


def test_the_window_is_measured_from_the_cause_not_from_now(repo):
    """An old cause with an old symptom is still one event. The test is the gap
    between them, not how long ago either happened."""
    cause = _alert("HighOverallControlPlaneMemory", hours_firing=50.0)
    together = _alert("CsvAbnormalFailedOver2Min", "openshift-operator-lifecycle-manager", hours_firing=49.5)
    assert ep.attach_symptoms("ep-1", cause, [together], {}) == 1


# ── an episode nobody re-detects is over ──────────────────────────────────


def test_an_episode_nobody_has_re_detected_is_closed(repo):
    """Observed live: a master flapped NotReady and recovered. Sixty-eight
    minutes later the episode was still open and still the headline on the
    front door, captioned "running 68m" — which reads as broken for 68 minutes
    when the truth was last seen 68 minutes ago and never re-checked."""
    stale_at = NOW - 68 * 60
    repo.list_stale_open.return_value = [{"id": "ep-stale", "cause_title": "Node NotReady", "last_seen_at": stale_at}]
    assert ep.close_stale() == 1
    repo.close.assert_called_once()
    assert repo.close.call_args[0][0] == "ep-stale"


def test_the_cutoff_is_exactly_the_stale_window_back(repo):
    """Pin the cutoff to a value, not to an inequality.

    The first version asserted only that the allowed silence exceeded the
    slowest scanner's cadence. A mutant that passed `cutoff = 0` satisfied that
    trivially — the silence it implies is fifty years — and all 34 tests went
    green. An assertion a nonsense value can satisfy is not an assertion.
    """
    repo.list_stale_open.return_value = []
    before = ep._now()
    ep.close_stale()
    after = ep._now()
    cutoff = repo.list_stale_open.call_args[0][0]
    assert before - ep._STALE_EPISODE_SECONDS <= cutoff <= after - ep._STALE_EPISODE_SECONDS


def test_the_window_outlasts_the_slowest_scanner():
    """Scanners on scan_every=5 touch their episode only every fifth cycle —
    roughly five and a half minutes at a 65-second interval. The window must
    leave room for more than one missed turn, or a slow scan closes a live
    episode between its own detections."""
    slowest_cadence = 5.5 * 60
    assert 2 * slowest_cadence <= ep._STALE_EPISODE_SECONDS


def test_an_episode_seen_this_cycle_is_left_alone(repo):
    repo.list_stale_open.return_value = []
    assert ep.close_stale() == 0
    repo.close.assert_not_called()


def test_a_failed_sweep_closes_nothing_rather_than_guessing(repo):
    """A database that will not answer is not evidence that every episode is
    over. Closing on a read failure would silently retire live incidents."""
    repo.list_stale_open.side_effect = RuntimeError("db down")
    assert ep.close_stale() == 0
    repo.close.assert_not_called()
