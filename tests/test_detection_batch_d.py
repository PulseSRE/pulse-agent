"""Batch D: detection reliability.

Each test fails on the code before this change:

- D1 a Prometheus blip read as "no alerts firing" and resolved every alert;
- D2 an open episode absorbed symptoms with no time bound and from any
  namespace;
- D3 a restart re-paged every standing critical finding and wiped the noise
  filter's memory;
- D4 above 500 standing findings, evicted ones were re-announced every scan;
- D5 the briefing said "All quiet" while listing critical items, and ran 8 of
  32 scanners, swallowing their failures;
- D6 resolving one condition on a resource closed the episode another
  condition on it still headed;
- D7 noise-suppressed findings still opened episodes, and webhook/storage
  findings could never be a cause;
- D8 trend degradation was one racy global flag; activeWatches listed 11 of
  32 scanners and the coverage page 27.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sre_agent.monitor.cluster_monitor as cm
from sre_agent.inbox import _finding_corr_key
from sre_agent.monitor import episodes as ep
from sre_agent.monitor import scanner_health
from sre_agent.monitor.findings import _make_finding
from sre_agent.monitor.scanner_protocol import FunctionScanner, ScannerMeta

# ── D1: alerts ─────────────────────────────────────────────────────────────


class TestAlertScannerFailure:
    def _run(self, client):
        from sre_agent.monitor.scanners import scan_firing_alerts

        scanner_health.reset()
        with patch("sre_agent.monitor.scanners.get_prometheus_client", return_value=client):
            with scanner_health.scanning("alerts"):
                findings = scan_firing_alerts()
        return findings, scanner_health.get_failure("alerts")

    def test_unsuccessful_query_is_a_failure_not_clean(self):
        client = MagicMock()
        client.request.return_value = {"status": "error", "error": "query timed out"}
        findings, failure = self._run(client)
        assert findings == []
        assert failure and "timed out" in failure

    def test_unreachable_prometheus_is_a_failure(self):
        client = MagicMock()
        client.request.side_effect = ConnectionResetError("reset by peer")
        _, failure = self._run(client)
        assert failure and "ConnectionResetError" in failure

    def test_a_working_query_is_not_a_failure(self):
        client = MagicMock()
        client.request.return_value = {"status": "success", "data": {"groups": []}}
        _, failure = self._run(client)
        assert failure is None


# ── D2: episode scope ──────────────────────────────────────────────────────


@pytest.fixture
def repo():
    r = MagicMock()
    r.find_open_by_correlation.return_value = None
    r.find_recent_closed_by_correlation.return_value = None
    r.detached_keys.return_value = set()
    r.attach.return_value = True
    r.open_symptom_index.return_value = {}
    with patch("sre_agent.monitor.episodes._repo", return_value=r):
        yield r


def _f(category, title, namespace, name, kind="Pod"):
    return {
        "id": f"f-{name}",
        "category": category,
        "title": title,
        "resources": [{"kind": kind, "name": name, "namespace": namespace}],
    }


class TestEpisodeScope:
    def test_a_symptom_starting_long_after_the_cause_is_not_absorbed(self, repo):
        started = int(time.time()) - 6 * 3600
        repo.get.return_value = {"id": "ep-1", "status": "open", "started_at": started}
        pod = _f("crashloop", "Pod api restarting", "shop", "api")
        first_seen = {_finding_corr_key(pod): started + 5 * 3600}
        assert ep.attach_symptoms("ep-1", "control_plane", [pod], first_seen) == 0

    def test_a_symptom_soon_after_the_cause_still_attaches(self, repo):
        started = int(time.time()) - 600
        repo.get.return_value = {"id": "ep-1", "status": "open", "started_at": started}
        pod = _f("crashloop", "Pod api restarting", "shop", "api")
        assert ep.attach_symptoms("ep-1", "control_plane", [pod], {_finding_corr_key(pod): started + 120}) == 1

    def test_an_app_namespace_cause_does_not_reach_other_namespaces(self, repo):
        now = int(time.time())
        repo.get.return_value = {"id": "ep-1", "status": "open", "started_at": now}
        cause = _f("stuck", "Finalizer stuck", "team-a", "ctrl", "Deployment")
        same = _f("crashloop", "Pod a restarting", "team-a", "a")
        other = _f("crashloop", "Pod b restarting", "team-b", "b")
        assert ep.attach_symptoms("ep-1", cause, [same, other], {}) == 1
        assert repo.attach.call_args.args[1] == _finding_corr_key(same)

    def test_cluster_scoped_and_system_namespace_causes_reach_everywhere(self, repo):
        now = int(time.time())
        repo.get.return_value = {"id": "ep-1", "status": "open", "started_at": now}
        other = _f("crashloop", "Pod b restarting", "team-b", "b")
        sdn = _f("daemonsets", "DaemonSet sdn missing pods", "openshift-sdn", "sdn", "DaemonSet")
        assert ep.attach_symptoms("ep-1", sdn, [other], {}) == 1
        repo.attach.reset_mock()
        node = _f("nodes", "Node w1 NotReady", "", "w1", "Node")
        assert ep.attach_symptoms("ep-1", node, [other], {}) == 1


# ── D7: layers ─────────────────────────────────────────────────────────────


class TestLayers:
    def test_webhooks_and_storage_can_head_an_episode(self):
        from sre_agent.monitor.layers import can_explain, can_head_episode

        assert can_head_episode("webhooks")
        assert can_head_episode("storage")
        assert can_explain("webhooks", "crashloop")
        assert can_explain("storage", "scheduling")

    def test_maintenance_and_anomaly_are_not_causes(self):
        from sre_agent.monitor.layers import can_head_episode

        assert not can_head_episode("maintenance")
        assert not can_head_episode("anomaly")
        assert not can_head_episode("ingress")


# ── scan-loop harness (D3, D4, D6, D7, D8) ─────────────────────────────────


class _Harness:
    def __init__(self, scanners):
        self.scanners = scanners
        self.events: list[dict] = []
        self.monitor: cm.ClusterMonitor | None = None
        self.closed: list[dict] = []
        self.correlated: list[list[dict]] = []

    async def scan(self) -> dict:
        self.events.clear()
        await self.monitor._run_scan_locked()
        # let run_in_executor callbacks land
        await asyncio.sleep(0.05)
        return {
            "new": [e for e in self.events if e.get("type") == "finding"],
            "resolved": [e for e in self.events if e.get("type") == "resolution"],
            "status": next(e for e in self.events if e.get("type") == "monitor_status"),
        }


@pytest.fixture
def harness():
    holder: dict = {}

    with (
        patch.object(cm, "get_monitor_repo", MagicMock()),
        patch.object(cm, "get_all_scanner_instances", lambda: holder["h"].scanners),
        patch.object(cm, "_send_webhook", AsyncMock()) as webhook,
        patch.object(cm, "expire_orphaned_proposals", lambda: 0),
        patch.object(cm, "mark_finding_actions_resolved", lambda *a: None),
        patch.object(cm, "_resolve_finding_inbox", lambda *a: None),
        patch.object(cm, "_close_episode_for", lambda f: holder["h"].closed.append(f)),
        patch.object(cm, "_announced_before", lambda fs: set(), create=True),
        patch("sre_agent.monitor.noise_store.load_counts", lambda: {}),
        patch("sre_agent.monitor.noise_store.save_counts", lambda c: None),
        patch("sre_agent.async_k8s.get_async_core_client", AsyncMock(side_effect=RuntimeError("no k8s"))),
        patch("sre_agent.inbox.bridge_finding_to_inbox", lambda f: None),
        patch("sre_agent.inbox.run_generator_cycle", lambda: None),
        patch("sre_agent.inbox.reconcile_open_items", lambda k: 0),
        patch("sre_agent.monitor.recurrence.process_recurrences", AsyncMock()),
    ):

        def build(scanners):
            h = _Harness(scanners)
            holder["h"] = h
            m = cm.ClusterMonitor()

            def correlate(findings):
                h.correlated.append(list(findings))
                return []

            m._correlate_episodes = correlate
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


def _scanner(name, fn):
    return FunctionScanner(ScannerMeta(name, name, "d", "c"), fn)


def _crit(title, name, category="crashloop"):
    return _make_finding("critical", category, title, "x", [{"kind": "Pod", "name": name, "namespace": "app"}])


class TestRestart:
    def test_restart_does_not_re_page_an_announced_finding(self, harness):
        h = harness([_scanner("crashloop", lambda: [_crit("Pod api restarting", "api")])])
        with patch.object(cm, "_announced_before", lambda fs: {f["id"] for f in fs}):
            out = asyncio.run(h.scan())
        assert len(out["new"]) == 1, "the UI is still told what stands"
        assert h.webhook.await_count == 0

    def test_a_genuinely_new_critical_finding_still_pages(self, harness):
        h = harness([_scanner("crashloop", lambda: [_crit("Pod api restarting", "api")])])
        asyncio.run(h.scan())
        assert h.webhook.await_count == 1

    def test_noise_memory_survives_a_restart(self, harness):
        from sre_agent.monitor.confidence import _finding_key

        finding = _crit("Pod api restarting", "api")
        h = harness([_scanner("crashloop", lambda: [dict(finding)])])
        with patch("sre_agent.monitor.noise_store.load_counts", lambda: {_finding_key(finding): 5}):
            out = asyncio.run(h.scan())
        assert out["new"] == [], "a condition known to flap stays suppressed after a restart"
        assert h.webhook.await_count == 0

    def test_noise_counts_are_saved(self, harness):
        saved: list[dict] = []
        state = {"on": True}
        h = harness([_scanner("crashloop", lambda: [_crit("Pod api restarting", "api")] if state["on"] else [])])
        with patch("sre_agent.monitor.noise_store.save_counts", lambda c: saved.append(dict(c))):
            asyncio.run(h.scan())
            state["on"] = False
            asyncio.run(h.scan())
        assert saved and list(saved[-1].values()) == [1]


class TestEviction:
    def test_findings_reported_this_cycle_are_never_evicted(self, harness):
        h = harness([_scanner("crashloop", lambda: [_crit(f"Pod p{i} restarting", f"p{i}") for i in range(4)])])
        h.monitor._MAX_FINDINGS = 2
        first = asyncio.run(h.scan())
        assert len(first["new"]) == 4
        for _ in range(3):
            assert asyncio.run(h.scan())["new"] == [], "nothing is re-announced"


class TestEpisodeClosing:
    def test_episode_stays_open_while_another_condition_on_its_resource_stands(self, harness):
        state = {"both": True}

        def node():
            out = [_make_finding("critical", "nodes", "Node w1 NotReady", "x", [{"kind": "Node", "name": "w1"}])]
            if state["both"]:
                out.append(
                    _make_finding("warning", "nodes", "Node w1 MemoryPressure", "x", [{"kind": "Node", "name": "w1"}])
                )
            return out

        h = harness([_scanner("nodes", node)])
        asyncio.run(h.scan())
        state["both"] = False
        out = asyncio.run(h.scan())
        assert len(out["resolved"]) == 1
        assert h.closed == [], "NotReady still stands on w1"

    def test_episode_closes_when_nothing_on_its_resource_stands(self, harness):
        state = {"on": True}

        def node():
            if not state["on"]:
                return []
            return [_make_finding("critical", "nodes", "Node w1 NotReady", "x", [{"kind": "Node", "name": "w1"}])]

        h = harness([_scanner("nodes", node)])
        asyncio.run(h.scan())
        state["on"] = False
        asyncio.run(h.scan())
        assert len(h.closed) == 1


class TestNoisyFindingsAndEpisodes:
    def test_noise_suppressed_findings_are_not_correlated(self, harness):
        from sre_agent.monitor.confidence import _finding_key

        noisy = _crit("Pod flaky restarting", "flaky")
        real = _crit("Pod api restarting", "api")
        h = harness([_scanner("crashloop", lambda: [dict(noisy), dict(real)])])
        h.monitor._transient_counts[_finding_key(noisy)] = 5
        asyncio.run(h.scan())
        titles = {f["title"] for f in h.correlated[-1]}
        assert titles == {"Pod api restarting"}


class TestActiveWatches:
    def test_status_lists_every_running_scanner(self, harness):
        names = [f"s{i}" for i in range(14)]
        h = harness([_scanner(n, lambda: []) for n in names])
        out = asyncio.run(h.scan())
        assert out["status"]["activeWatches"] == names


# ── D5: briefing ───────────────────────────────────────────────────────────


class TestBriefing:
    def _monitor(self, findings, results):
        m = MagicMock()
        m._last_findings = {f["id"]: f for f in findings}
        m._last_scanner_results = results
        m._noise_threshold = 0.7
        return m

    def test_critical_items_are_not_all_quiet(self):
        from sre_agent.monitor.actions import get_briefing

        m = self._monitor([_crit("Pod api restarting", "api")], [{"name": "crashloop", "status": "warning"}])
        repo = MagicMock()
        repo.get_actions_since.return_value = []
        repo.get_investigations_since.return_value = []
        with (
            patch("sre_agent.monitor.cluster_monitor.get_cluster_monitor_sync", return_value=m),
            patch("sre_agent.monitor.actions.get_monitor_repo", return_value=repo),
        ):
            b = get_briefing()
        assert "All quiet" not in b["summary"]
        assert "1 critical issue open" in b["summary"]
        assert len(b["currentFindings"]) == 1

    def test_failing_scanners_are_reported_not_hidden(self):
        from sre_agent.monitor.actions import get_briefing

        m = self._monitor([], [{"name": "alerts", "status": "error"}, {"name": "nodes", "status": "clean"}])
        repo = MagicMock()
        repo.get_actions_since.return_value = []
        repo.get_investigations_since.return_value = []
        with (
            patch("sre_agent.monitor.cluster_monitor.get_cluster_monitor_sync", return_value=m),
            patch("sre_agent.monitor.actions.get_monitor_repo", return_value=repo),
        ):
            b = get_briefing()
        assert "All quiet" not in b["summary"]
        assert b["unwatchedScanners"] == ["alerts"]

    def test_quiet_cluster_is_all_quiet(self):
        from sre_agent.monitor.actions import get_briefing

        m = self._monitor([], [{"name": "alerts", "status": "clean"}])
        repo = MagicMock()
        repo.get_actions_since.return_value = []
        repo.get_investigations_since.return_value = []
        with (
            patch("sre_agent.monitor.cluster_monitor.get_cluster_monitor_sync", return_value=m),
            patch("sre_agent.monitor.actions.get_monitor_repo", return_value=repo),
        ):
            b = get_briefing()
        assert b["summary"].startswith("All quiet")


# ── D8: trend degradation and coverage ─────────────────────────────────────


class TestTrendDegraded:
    def test_one_success_does_not_clear_another_scanners_failure(self):
        from sre_agent.monitor import trend_scanners as ts

        ts._degraded_scanners.clear()
        bad, good = MagicMock(), MagicMock()
        bad.query.return_value = {"status": "error", "error": "timeout"}
        good.query.return_value = {"status": "success", "data": {"result": []}}
        with patch.object(ts, "get_prometheus_client", return_value=bad), scanner_health.scanning("trend_disk"):
            ts._query_prometheus("q")
        with patch.object(ts, "get_prometheus_client", return_value=good), scanner_health.scanning("trend_memory"):
            ts._query_prometheus("q")
        degraded = ts.get_trend_degraded_finding()
        assert degraded and "trend_disk" in degraded[0]["summary"]
        with patch.object(ts, "get_prometheus_client", return_value=good), scanner_health.scanning("trend_disk"):
            ts._query_prometheus("q")
        assert ts.get_trend_degraded_finding() == []

    def test_coverage_counts_every_scanner(self):
        from sre_agent.api.scanner_rest import get_scanner_coverage
        from sre_agent.monitor.scanners import get_all_scanner_instances

        with patch("sre_agent.repositories.get_monitor_repo", side_effect=RuntimeError("no db")):
            coverage = get_scanner_coverage()
        assert coverage["total_scanners"] == len(get_all_scanner_instances())
        listed = {s for c in coverage["categories"] for s in c["scanners"]}
        assert listed == {s.meta.name for s in get_all_scanner_instances()}


# ── D3: persistence (disposable DB) ────────────────────────────────────────


class TestNoiseStoreDb:
    @pytest.mark.real_noise_store
    def test_round_trip(self):
        from sre_agent.db import get_database
        from sre_agent.db_migrations import run_migrations
        from sre_agent.monitor.noise_store import load_counts, save_counts

        db = get_database()
        run_migrations(db)
        db.execute("DELETE FROM monitor_noise")
        db.commit()
        save_counts({"k1": 2, "k2": 4})
        save_counts({"k1": 3})
        try:
            assert load_counts() == {"k1": 3, "k2": 4}
        finally:
            db.execute("DELETE FROM monitor_noise")
            db.commit()


class TestAnnouncedBeforeDb:
    def test_open_inbox_item_means_already_announced(self):
        from sre_agent.db import get_database
        from sre_agent.inbox import bridge_finding_to_inbox

        db = get_database()
        db.execute("DELETE FROM inbox_items")
        db.commit()
        announced = _crit("Pod api restarting", "api")
        fresh = _crit("Pod web restarting", "web")
        bridge_finding_to_inbox(dict(announced, confidence=0.9, noiseScore=0.0))
        try:
            assert cm._announced_before([announced, fresh]) == {announced["id"]}
        finally:
            db.execute("DELETE FROM inbox_items")
            db.commit()
