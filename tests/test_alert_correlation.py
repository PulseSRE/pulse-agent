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
    cause = _alert("HighOverallControlPlaneMemory", hours_firing=23.5)
    later = _alert("TargetDown", "openshift-lightspeed", hours_firing=1.6)
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

# Read from the live cluster via `time() - ALERTS_FOR_STATE`.
LIVE_CLUSTER = [
    ("AlertmanagerReceiversNotConfigured", "openshift-monitoring", 50.1),
    ("ArgoCDSyncAlert", "openshift-gitops", 50.1),
    ("CsvAbnormalFailedOver2Min", "openshift-operator-lifecycle-manager", 23.6),
    ("HighOverallControlPlaneMemory", "openshift-machine-config-operator", 23.5),
    ("ControlPlaneNodeMemoryHigh", "openshift-monitoring", 3.0),
    ("InsightsRecommendationActive", "openshift-insights", 1.6),
    ("TargetDown", "multicluster-engine", 1.6),
    ("TargetDown", "openshift-lightspeed", 1.6),
    ("KubeJobFailed", "openshift-marketplace", 1.6),
    ("SearchPVCNotPresent", "open-cluster-management", 1.6),
]


def test_the_real_cluster_resolves_to_one_story():
    """Fifteen flat alerts became one episode with five symptoms — the same
    conclusion Pulse's own investigation reached from the same data."""
    findings = [_alert(n, ns, h) for n, ns, h in LIVE_CLUSTER]

    claimed: dict[str, str] = {}
    episodes: dict[str, list] = {}
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

    headline = next(k for k, v in episodes.items() if v)
    assert "HighOverallControlPlaneMemory" in headline
    assert sorted(episodes[headline]) == [
        "CsvAbnormalFailedOver2Min",
        "KubeJobFailed",
        "SearchPVCNotPresent",
        "TargetDown",
        "TargetDown",
    ]

    # The fifty-hour standing conditions belong to nobody.
    accounted = set(episodes) | set(claimed)
    alone = sorted({f["title"] for f in findings if _finding_corr_key(f) not in accounted})
    assert alone == ["AlertmanagerReceiversNotConfigured", "ArgoCDSyncAlert", "InsightsRecommendationActive"]
