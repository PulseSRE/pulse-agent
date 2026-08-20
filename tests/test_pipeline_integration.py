"""The scan → finding → episode → inbox path, end to end.

Every layer here already has unit tests, and they were all green while the
episode engine did something badly wrong: run against a live cluster it opened
seven episodes headed by "Certificate expiring in 9d" and absorbed 21 of 23
findings into them. Nothing caught it, because each layer was correct in
isolation and the fault was in how they composed.

These tests run real findings through the real functions — only the database
and Prometheus are substituted — so a mistake in the seams shows up here
rather than on somebody's cluster.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from sre_agent.inbox import _collapse_episode_symptoms, _finding_corr_key
from sre_agent.monitor import episodes as ep
from sre_agent.monitor.findings import _make_finding

MODULE = "sre_agent.monitor.episodes"


class FakeEpisodeStore:
    """In-memory stand-in for the episode repository, matching its contract."""

    def __init__(self):
        self.episodes: dict[str, dict] = {}
        self.symptoms: dict[str, dict[str, dict]] = {}

    def create(self, **k):
        self.episodes[k["episode_id"]] = {
            "id": k["episode_id"],
            "status": "open",
            "started_at": k["started_at"],
            "cause_category": k["cause_category"],
            "cause_title": k["cause_title"],
            "correlation_key": k["correlation_key"],
            "recurrence_of": k.get("recurrence_of"),
            "symptom_count": 0,
            "namespaces": "[]",
        }

    def get(self, i):
        return self.episodes.get(i)

    def find_open_by_correlation(self, key):
        return next((e for e in self.episodes.values() if e["correlation_key"] == key and e["status"] == "open"), None)

    def find_recent_closed_by_correlation(self, key, since):
        return None

    def touch(self, i, n):
        pass

    def close(self, i, n):
        self.episodes[i]["status"] = "closed"

    def list_open(self):
        return [e for e in self.episodes.values() if e["status"] == "open"]

    def attach(self, eid, key, cat, title, ns, now):
        bucket = self.symptoms.setdefault(eid, {})
        if key in bucket:
            return False
        bucket[key] = {"correlation_key": key, "category": cat, "title": title, "namespace": ns, "detached_at": None}
        return True

    def detach(self, eid, key, actor, now):
        s = self.symptoms.get(eid, {}).get(key)
        if not s or s["detached_at"]:
            return False
        s["detached_at"] = now
        return True

    def detached_keys(self, eid):
        return {k for k, v in self.symptoms.get(eid, {}).items() if v["detached_at"]}

    def symptoms_of(self, eid):
        return [v for v in self.symptoms.get(eid, {}).values() if not v["detached_at"]]

    def symptoms(self, eid):
        return self.symptoms_of(eid)

    def open_symptom_index(self):
        return {
            k: eid
            for eid, bucket in self.symptoms.items()
            if self.episodes.get(eid, {}).get("status") == "open"
            for k, v in bucket.items()
            if not v["detached_at"]
        }

    def refresh_rollup(self, eid):
        rows = self.symptoms_of(eid)
        self.episodes[eid]["symptom_count"] = len(rows)


@pytest.fixture
def store():
    s = FakeEpisodeStore()
    with patch(f"{MODULE}._repo", return_value=s):
        yield s


def f(category, title, namespace="demo", name="thing", kind="Pod", severity="critical", ftype="current"):
    finding = _make_finding(
        severity=severity,
        category=category,
        title=title,
        summary="",
        resources=[{"kind": kind, "name": name, "namespace": namespace}],
        finding_type=ftype,
    )
    return finding


def correlate(findings, store, first_seen=None):
    """Run the real correlation the monitor runs each cycle."""
    now = int(time.time())
    seen = first_seen or {_finding_corr_key(x): now for x in findings}
    for finding in findings:
        eid = ep.open_or_touch(finding)
        if eid:
            ep.attach_symptoms(eid, finding.get("category", ""), findings, seen)
    return store.list_open()


# ── the incident this was all built for ───────────────────────────────────


def test_a_control_plane_outage_becomes_one_episode_not_fourteen(store):
    """The 20:35 case: nine critical deployments and one etcd warning."""
    findings = [
        f("control_plane", "etcdMemberCommunicationSlow", "", "etcd", "Etcd", severity="warning"),
        *[
            f("workloads", f"Deployment {n} degraded (0/2)", f"ns{i}", n, "Deployment")
            for i, n in enumerate(["ocm-controller", "cluster-proxy", "grc-policy", "search-v2", "dns-operator"])
        ],
        *[f("crashloop", f"Pod {n} restarting", f"ns{i}", n) for i, n in enumerate(["web", "api"])],
        f("alerts", "TargetDown", "monitoring", "TargetDown", "Alert"),
    ]
    open_eps = correlate(findings, store)

    assert len(open_eps) == 1
    episode = open_eps[0]
    assert episode["cause_category"] == "control_plane"
    # every one of the other eight is explained by it
    assert len(store.symptoms_of(episode["id"])) == 8


def test_the_inbox_then_shows_one_row_instead_of_nine(store):
    """The end of the path: collapse must actually remove them from the queue."""
    findings = [
        f("control_plane", "etcdMemberCommunicationSlow", "", "etcd", "Etcd", severity="warning"),
        *[f("workloads", f"Deployment d{i} degraded", f"ns{i}", f"d{i}", "Deployment") for i in range(8)],
    ]
    correlate(findings, store)

    items = [{"correlation_key": _finding_corr_key(x), "title": x["title"]} for x in findings]
    kept, collapsed = _collapse_episode_symptoms(items)

    assert collapsed == 8
    assert len(kept) == 1
    assert "etcd" in kept[0]["title"]


def test_a_forecast_never_becomes_the_cause_of_anything(store):
    """The bug a live cluster found: certs expiring in 9d absorbed 21 of 23 findings."""
    findings = [
        f("cert_expiry", "Certificate addon-webhook expiring in 9d", "ns", "cert", "Secret", severity="warning"),
        f("security", "88 privileged containers across 16 namespaces", "", "cluster", "Cluster", severity="warning"),
        *[f("crashloop", f"Pod p{i} restarting", f"ns{i}", f"p{i}") for i in range(5)],
    ]
    assert correlate(findings, store) == []


def test_a_healthy_cluster_produces_no_episodes(store):
    """Workload noise on its own must not manufacture a cause."""
    findings = [f("crashloop", f"Pod p{i} restarting", f"ns{i}", f"p{i}") for i in range(6)]
    assert correlate(findings, store) == []


def test_something_broken_before_the_cause_stays_its_own_problem(store):
    now = int(time.time())
    cause = f("control_plane", "etcd changed leader 12 times", "", "etcd", "Etcd")
    old = f("crashloop", "Pod already-broken restarting", "ns", "already-broken")
    fresh = f("workloads", "Deployment d degraded", "ns", "d", "Deployment")
    first_seen = {
        _finding_corr_key(cause): now,
        _finding_corr_key(old): now - 7200,
        _finding_corr_key(fresh): now,
    }
    open_eps = correlate([cause, old, fresh], store, first_seen)
    attached = {s["correlation_key"] for s in store.symptoms_of(open_eps[0]["id"])}
    assert _finding_corr_key(fresh) in attached
    assert _finding_corr_key(old) not in attached


def test_detaching_returns_the_item_to_the_queue(store):
    """The operator's correction has to flow all the way back to the inbox."""
    cause = f("control_plane", "etcd leader churn", "", "etcd", "Etcd")
    symptom = f("workloads", "Deployment unrelated degraded", "ns", "unrelated", "Deployment")
    open_eps = correlate([cause, symptom], store)
    key = _finding_corr_key(symptom)

    items = [{"correlation_key": key, "title": symptom["title"]}]
    assert _collapse_episode_symptoms(items)[1] == 1

    ep.detach(open_eps[0]["id"], key, "alice")
    kept, collapsed = _collapse_episode_symptoms(items)
    assert collapsed == 0
    assert kept == items


def test_closing_the_cause_releases_every_symptom(store):
    """If they are still failing once the cause is gone, they were never only symptoms."""
    cause = f("control_plane", "etcd leader churn", "", "etcd", "Etcd")
    symptoms = [f("workloads", f"Deployment d{i} degraded", f"ns{i}", f"d{i}", "Deployment") for i in range(3)]
    correlate([cause, *symptoms], store)

    items = [{"correlation_key": _finding_corr_key(s), "title": s["title"]} for s in symptoms]
    assert _collapse_episode_symptoms(items)[1] == 3

    ep.close_for_correlation(_finding_corr_key(cause))
    kept, collapsed = _collapse_episode_symptoms(items)
    assert collapsed == 0
    assert len(kept) == 3


def test_a_second_cycle_does_not_duplicate_symptoms(store):
    """The monitor re-runs correlation every cycle over everything still standing."""
    findings = [
        f("control_plane", "etcd leader churn", "", "etcd", "Etcd"),
        f("workloads", "Deployment d degraded", "ns", "d", "Deployment"),
    ]
    correlate(findings, store)
    correlate(findings, store)
    correlate(findings, store)
    episode = store.list_open()[0]
    assert len(store.symptoms_of(episode["id"])) == 1
