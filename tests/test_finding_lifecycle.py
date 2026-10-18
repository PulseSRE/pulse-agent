"""Finding identity and lifecycle through the real scan loop.

These run ``ClusterMonitor._run_scan_locked`` itself with fake scanners, not a
copy of its logic: the bugs here lived in how the loop decides that a finding
is the same one as last cycle, and that it has gone away.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sre_agent.monitor.cluster_monitor as cm
from sre_agent.monitor.confidence import _finding_key, _stable_title, _strip_pod_hash
from sre_agent.monitor.findings import _make_finding
from sre_agent.monitor.scanner_protocol import FunctionScanner, ScannerMeta


class _Harness:
    def __init__(self, scanners):
        self.scanners = scanners
        self.events: list[dict] = []
        self.monitor: cm.ClusterMonitor | None = None

    async def scan(self) -> dict:
        self.events.clear()
        await self.monitor._run_scan_locked()
        return {
            "new": [e for e in self.events if e.get("type") == "finding"],
            "resolved": [e for e in self.events if e.get("type") == "resolution"],
            "snapshot": next(e for e in self.events if e.get("type") == "findings_snapshot")["activeIds"],
        }


@pytest.fixture
def harness():
    holder: dict = {}

    def make(scanners):
        h = _Harness(scanners)
        holder["h"] = h
        return h

    with (
        patch.object(cm, "get_monitor_repo", MagicMock()),
        patch.object(cm, "get_all_scanner_instances", lambda: holder["h"].scanners),
        patch.object(cm, "_send_webhook", AsyncMock()) as webhook,
        patch.object(cm, "expire_orphaned_proposals", lambda: 0),
        patch.object(cm, "mark_finding_actions_resolved", lambda *a: None),
        patch.object(cm, "_resolve_finding_inbox", lambda *a: None),
        patch.object(cm, "_close_episode_for", lambda *a: None),
        patch("sre_agent.async_k8s.get_async_core_client", AsyncMock(side_effect=RuntimeError("no k8s"))),
        patch("sre_agent.inbox.bridge_finding_to_inbox", lambda f: None),
        patch("sre_agent.inbox.run_generator_cycle", lambda: None),
        patch("sre_agent.inbox.reconcile_open_items", lambda k: 0),
        patch("sre_agent.monitor.recurrence.process_recurrences", AsyncMock()),
    ):

        def build(scanners):
            h = make(scanners)
            m = cm.ClusterMonitor()
            m._correlate_episodes = lambda f: []
            for name in ("run_investigations", "auto_fix", "process_verifications", "process_handoffs"):
                setattr(m, name, AsyncMock())

            async def record(d):
                h.events.append(d)

            m._broadcast_raw = record
            m.broadcast = record
            h.monitor = m
            h.webhook = webhook
            return h

        yield build


def _scanner(name, fn, every=1):
    return FunctionScanner(ScannerMeta(name, name, "d", "c", scan_every=every), fn)


def test_a_moving_number_in_the_title_is_the_same_finding(harness):
    """'pending for 6m' then '7m' is one condition, not a resolve and a new one."""
    minutes = {"n": 5}

    def pending():
        minutes["n"] += 1
        return [
            _make_finding(
                "critical",
                "pending",
                f"Pod api-0 pending for {minutes['n']}m",
                "x",
                [{"kind": "Pod", "name": "api-0", "namespace": "app"}],
            )
        ]

    h = harness([_scanner("pending", pending)])
    first = asyncio.run(h.scan())
    assert len(first["new"]) == 1
    fid = first["new"][0]["id"]
    for _ in range(4):
        out = asyncio.run(h.scan())
        assert out["new"] == [] and out["resolved"] == []
        assert out["snapshot"] == [fid]
    assert h.webhook.await_count == 1, "a standing critical finding is announced once"
    stored = next(iter(h.monitor._last_findings.values()))
    assert stored["title"].endswith("pending for 10m"), "the stored copy shows the latest numbers"
    assert stored["id"] == fid


def test_a_scanner_that_did_not_run_resolves_nothing(harness):
    """A scan_every=3 scanner's finding stands between its runs."""

    def node():
        return [_make_finding("critical", "nodes", "Node w1 NotReady", "x", [{"kind": "Node", "name": "w1"}])]

    h = harness([_scanner("nodes", node, every=3), _scanner("crashloop", lambda: [])])
    seen_new = 0
    for _ in range(15):
        out = asyncio.run(h.scan())
        seen_new += len(out["new"])
        assert out["resolved"] == []
    assert seen_new == 1
    assert h.monitor._noise_suppressed == 0, "flapping used to push it past the noise threshold"
    assert len(out["snapshot"]) == 1, "the UI keeps it on cycles the scanner skipped"


def test_a_scanner_that_errored_resolves_nothing(harness):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("apiserver timeout")
        return [
            _make_finding(
                "warning", "operators", "Operator dns degraded", "x", [{"kind": "ClusterOperator", "name": "dns"}]
            )
        ]

    h = harness([_scanner("operators", flaky)])
    asyncio.run(h.scan())
    assert asyncio.run(h.scan())["resolved"] == []
    assert asyncio.run(h.scan())["new"] == []


def test_a_finding_that_really_goes_away_still_resolves(harness):
    state = {"up": True}

    def crash():
        if not state["up"]:
            return []
        return [
            _make_finding(
                "critical",
                "crashloop",
                "Pod web crash-looping",
                "x",
                [{"kind": "Pod", "name": "web-0", "namespace": "a"}],
            )
        ]

    h = harness([_scanner("crashloop", crash)])
    fid = asyncio.run(h.scan())["new"][0]["id"]
    state["up"] = False
    out = asyncio.run(h.scan())
    assert [r["findingId"] for r in out["resolved"]] == [fid]
    assert out["snapshot"] == []


def test_auto_fix_credit_survives_later_scans(harness):
    """A fix applied on the third sighting is credited to the stored finding id."""
    state = {"up": True}

    def crash():
        if not state["up"]:
            return []
        return [
            _make_finding(
                "critical",
                "crashloop",
                "Pod web crash-looping (3 restarts)",
                "x",
                [{"kind": "Pod", "name": "web-0", "namespace": "a"}],
            )
        ]

    h = harness([_scanner("crashloop", crash)])
    asyncio.run(h.scan())
    asyncio.run(h.scan())
    seen: list[dict] = []
    h.monitor.auto_fix = AsyncMock(side_effect=lambda findings: seen.extend(findings))
    asyncio.run(h.scan())
    h.monitor._recent_fix_ids.add(seen[0]["id"])  # what _execute_fix records
    state["up"] = False
    out = asyncio.run(h.scan())
    assert out["resolved"][0]["resolvedBy"] == "auto-fix"


def test_a_scanner_that_is_disabled_lets_its_findings_go(harness):
    h = harness(
        [
            _scanner(
                "hpa",
                lambda: [
                    _make_finding(
                        "warning",
                        "hpa",
                        "HPA web at max",
                        "x",
                        [{"kind": "HorizontalPodAutoscaler", "name": "web", "namespace": "a"}],
                    )
                ],
            )
        ]
    )
    asyncio.run(h.scan())
    h.scanners = []
    assert len(asyncio.run(h.scan())["resolved"]) == 1


def test_two_reports_of_one_condition_become_one_finding(harness):
    def dup():
        def f():
            return _make_finding(
                "warning", "alerts", "Alert KubePodNotReady", "x", [{"kind": "Pod", "name": "x-0", "namespace": "a"}]
            )

        return [f(), f()]

    h = harness([_scanner("alerts", dup)])
    seen: list[dict] = []
    h.monitor.auto_fix = AsyncMock(side_effect=lambda findings: seen.extend(findings))
    asyncio.run(h.scan())
    assert len(seen) == 1


@pytest.mark.parametrize(
    ("title", "stable"),
    [
        ("Pod web crash-looping (5 restarts)", "Pod web crash-looping (# restarts)"),
        ("Pod api-0 pending for 12m", "Pod api-0 pending for #"),
        ("DaemonSet fluentd (2/3) ready", "DaemonSet fluentd (#/#) ready"),
        ("Certificate tls expiring in 3d", "Certificate tls expiring in #"),
        ("Error budget 99.5% burned", "Error budget # burned"),
        ("Target 10.0.0.1:9100 down", "Target 10.0.0.1:# down"),
        ("Cluster version v4.14 degraded", "Cluster version v4.14 degraded"),
        ("Pod redis-master-0 OOMKilled", "Pod redis-master-0 OOMKilled"),
    ],
)
def test_stable_title_masks_measurements_not_names(title, stable):
    assert _stable_title(title) == stable


@pytest.mark.parametrize(
    ("name", "stripped"),
    [
        ("web-5f58f69bd6-w4x22", "web"),
        ("node-exporter-x7k9p", "node-exporter"),
        ("redis-master-0", "redis-master-0"),
        ("redis-replica-0", "redis-replica-0"),
        ("my-app", "my-app"),
        ("redis-cache", "redis-cache"),
    ],
)
def test_strip_pod_hash_only_strips_generated_suffixes(name, stripped):
    assert _strip_pod_hash(name) == stripped


def test_recreated_pod_named_in_the_title_keeps_its_key():
    def f(pod):
        return _make_finding(
            "critical", "crashloop", f"Pod {pod} crash-looping", "x", [{"kind": "Pod", "name": pod, "namespace": "a"}]
        )

    assert _finding_key(f("web-5f58f69bd6-w4x22")) == _finding_key(f("web-5f58f69bd6-q9z2m"))
    assert _finding_key(f("redis-master-0")) != _finding_key(f("redis-replica-0"))
