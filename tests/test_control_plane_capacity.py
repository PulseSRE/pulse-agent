"""Control-plane capacity scanner: masters' memory and etcd headroom.

Each scenario in tests/fixtures/prometheus/control_plane_capacity is a set of
Prometheus /api/v1/query responses, keyed by the query the scanner sends. The
warning scenario is modelled on the cluster that prompted the scanner: two
masters past 80% of allocatable memory, etcdHighFsyncDurations firing, one
election in the hour, and an etcd database that is mostly free pages.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sre_agent.monitor import control_plane_capacity as cpc
from sre_agent.monitor import scanner_health, trend_scanners
from sre_agent.monitor.stuck_scanners import etcd_leader_churn_query

FIXTURES = Path(__file__).parent / "fixtures" / "prometheus" / "control_plane_capacity"
SCANNER = "control_plane_capacity"
MASTERS = ["ip-10-0-14-31.ec2.internal", "ip-10-0-55-120.ec2.internal", "ip-10-0-90-7.ec2.internal"]

_QUERY_KEYS = {
    cpc.CP_NODES_QUERY: "cp_nodes",
    cpc.CP_MEMORY_USED_QUERY: "memory_used",
    cpc.CP_MEMORY_ALLOCATABLE_QUERY: "memory_allocatable",
    cpc.CP_MEMORY_FORECAST_QUERY: "memory_forecast",
    cpc.ETCD_FSYNC_QUERY: "etcd_fsync",
    cpc.ETCD_COMMIT_QUERY: "etcd_commit",
    cpc.ETCD_LEADER_CHANGES_QUERY: "etcd_leader_changes",
    etcd_leader_churn_query(): "etcd_leader_churn_stall",
    cpc.ETCD_DB_TOTAL_QUERY: "etcd_db_total",
    cpc.ETCD_DB_IN_USE_QUERY: "etcd_db_in_use",
    cpc.ETCD_DB_QUOTA_QUERY: "etcd_db_quota",
    cpc.RELATED_ALERTS_QUERY: "related_alerts",
}

_EMPTY = {"status": "success", "data": {"resultType": "vector", "result": []}}


def _load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


class _RecordedPrometheus:
    """Answers each query from a recorded scenario; unknown queries fail the test."""

    def __init__(self, scenario: dict, overrides: dict | None = None, fail: set[str] | None = None):
        self.scenario = scenario
        self.overrides = overrides or {}
        self.fail = fail or set()
        self.queries: list[str] = []

    def query(self, promql: str, **_kw) -> dict:
        self.queries.append(promql)
        for node, resp in self.scenario.get("top_pods", {}).items():
            if promql == cpc.top_pods_query(node):
                return resp
        if promql.startswith("topk("):
            return _EMPTY
        key = _QUERY_KEYS.get(promql)
        assert key is not None, f"scanner sent an unrecorded query: {promql}"
        if key in self.fail:
            return {"status": "error", "error": "bad_data: query timed out"}
        return self.overrides.get(key, self.scenario[key])


@pytest.fixture(autouse=True)
def _clean_state():
    scanner_health.reset()
    trend_scanners._degraded_scanners.clear()
    yield
    scanner_health.reset()
    trend_scanners._degraded_scanners.clear()


def _run(client) -> tuple[list[dict], str | None]:
    with (
        patch("sre_agent.monitor.trend_scanners.get_prometheus_client", return_value=client),
        patch("sre_agent.prometheus.get_prometheus_client", return_value=client),
        scanner_health.scanning(SCANNER),
    ):
        findings = cpc.scan_control_plane_capacity()
        failure = scanner_health.get_failure(SCANNER)
    return findings, failure


def _by_title(findings: list[dict], fragment: str) -> list[dict]:
    return [f for f in findings if fragment in f["title"]]


# ── Scenarios ─────────────────────────────────────────────────────────────


def test_healthy_cluster_reports_nothing_and_is_not_a_failure():
    findings, failure = _run(_RecordedPrometheus(_load("healthy")))
    assert findings == []
    assert failure is None
    assert SCANNER not in trend_scanners._degraded_scanners


def test_warning_scenario_reports_each_problem_once_with_numbers_and_next_step():
    findings, failure = _run(_RecordedPrometheus(_load("warning")))
    assert failure is None

    memory = _by_title(findings, "memory at")
    assert sorted(f["resources"][0]["name"] for f in memory) == MASTERS[:2]
    m0 = next(f for f in memory if f["resources"][0]["name"] == MASTERS[0])
    assert m0["severity"] == "warning"
    assert m0["title"] == f"Control-plane node {MASTERS[0]} memory at 84% of allocatable"
    assert "12.3 GiB" in m0["summary"] and "14.6 GiB allocatable" in m0["summary"]
    assert "86% by this time tomorrow" in m0["summary"]
    # A non-platform pod on the master is named, with its usage.
    assert "acme-batch/report-builder-7c9f6d5b8-x2k4q (2.3 GiB)" in m0["summary"]
    m1 = next(f for f in memory if f["resources"][0]["name"] == MASTERS[1])
    # Nothing but platform there: the next step is a bigger machine.
    assert "larger control-plane machine size" in m1["summary"]

    fsync = _by_title(findings, "fsync")
    assert len(fsync) == 1
    assert fsync[0]["severity"] == "warning"
    assert fsync[0]["resources"] == [{"kind": "Etcd", "name": f"etcd-{MASTERS[0]}"}]
    assert "14ms" in fsync[0]["summary"] and "under 10ms" in fsync[0]["summary"]
    assert "disk class" in fsync[0]["summary"]
    # Correlated with the alert behind it, not raised as a second finding.
    assert "also firing etcdHighFsyncDurations" in fsync[0]["summary"]

    commit = _by_title(findings, "backend commit")
    assert [f["resources"][0]["name"] for f in commit] == [f"etcd-{MASTERS[1]}"]
    assert "41ms" in commit[0]["summary"]

    leader = _by_title(findings, "changed leader")
    assert len(leader) == 1
    assert leader[0]["title"] == "etcd changed leader 1 time in the last hour"
    assert "etcdHighNumberOfLeaderChanges" in leader[0]["summary"]

    db = _by_title(findings, "database")
    assert len(db) == 3
    assert all(f["severity"] == "warning" for f in db)
    assert "74% of its quota" in db[0]["title"]
    assert "Only 2.1 GiB of it is live data" in db[0]["summary"]
    assert "defragment" in db[0]["summary"]

    assert len(findings) == 2 + 1 + 1 + 1 + 3
    for f in findings:
        assert f["category"] == "control_plane"
        assert f["autoFixable"] is False
        assert "Next step:" in f["summary"]


def test_critical_scenario():
    findings, failure = _run(_RecordedPrometheus(_load("critical")))
    assert failure is None

    memory = _by_title(findings, "memory at")
    assert [f["resources"][0]["name"] for f in memory] == [MASTERS[2]]
    assert memory[0]["severity"] == "critical"
    assert "93%" in memory[0]["title"]
    assert "ExtremelyHighIndividualControlPlaneMemory" in memory[0]["summary"]

    fsync = _by_title(findings, "fsync")
    assert len(fsync) == 1 and fsync[0]["severity"] == "critical"
    assert "71ms" in fsync[0]["summary"]

    db = _by_title(findings, "database")
    assert db and all(f["severity"] == "critical" for f in db)
    assert "96%" in db[0]["title"]
    # Live data, not fragmentation: defragmenting is not the advice.
    assert "will not help much" in db[0]["summary"]
    assert "prune" in db[0]["summary"]
    # Near the quota it can head an episode; not a posture any more.
    assert not db[0].get("posture")


def test_forecast_only_is_a_trend_warning_not_a_measurement():
    findings, failure = _run(_RecordedPrometheus(_load("forecast_only")))
    assert failure is None
    assert len(findings) == 1
    f = findings[0]
    assert f["resources"] == [{"kind": "Node", "name": MASTERS[1]}]
    assert f["severity"] == "warning"
    assert f["findingType"] == "trend"
    assert f["posture"] is True
    assert "on course for 90% within a day" in f["title"]
    assert "74%" in f["summary"] and "95% by this time tomorrow" in f["summary"]
    assert "forecast" in f["summary"]


def test_prometheus_unreachable_is_a_failure_not_healthy():
    client = MagicMock()
    client.query.side_effect = ConnectionError("connection refused")
    findings, failure = _run(client)
    assert findings == []
    assert failure is not None and "Prometheus unreachable" in failure
    assert SCANNER in trend_scanners._degraded_scanners
    explained = scanner_health.explain_failure(SCANNER, failure)
    assert explained["reason"] == "Pulse cannot query Prometheus"
    assert explained["unwatched"]  # the checks listed in the registry


# ── Edges ─────────────────────────────────────────────────────────────────


def test_one_failed_query_fails_the_scan_but_keeps_other_findings():
    findings, failure = _run(_RecordedPrometheus(_load("warning"), fail={"etcd_fsync"}))
    assert failure is not None and "query failed" in failure
    assert not _by_title(findings, "fsync")
    assert _by_title(findings, "memory at")


def test_master_without_memory_series_is_unknown_not_fine():
    scenario = _load("healthy")
    scenario["memory_used"]["data"]["result"] = scenario["memory_used"]["data"]["result"][:2]
    _, failure = _run(_RecordedPrometheus(scenario))
    assert failure is not None and MASTERS[2] in failure


def test_masters_without_etcd_series_is_a_failure():
    findings, failure = _run(
        _RecordedPrometheus(_load("healthy"), overrides={"etcd_fsync": _EMPTY, "etcd_commit": _EMPTY})
    )
    assert findings == []
    assert failure is not None and "no etcd" in failure


def test_no_control_plane_nodes_means_nothing_to_watch():
    client = _RecordedPrometheus(_load("warning"), overrides={"cp_nodes": _EMPTY})
    findings, failure = _run(client)
    assert findings == [] and failure is None
    assert client.queries == [cpc.CP_NODES_QUERY]


def test_commit_latency_past_the_stall_line_is_left_to_the_stall_scanner():
    scenario = _load("warning")
    for r in scenario["etcd_commit"]["data"]["result"]:
        r["value"][1] = "0.9"
    findings, _ = _run(_RecordedPrometheus(scenario))
    assert not _by_title(findings, "backend commit")


def test_sustained_leader_churn_is_left_to_the_stall_scanner():
    stall = {"status": "success", "data": {"resultType": "vector", "result": [{"metric": {}, "value": [0, "6"]}]}}
    overrides = {"etcd_leader_changes": _load("warning")["etcd_leader_changes"], "etcd_leader_churn_stall": stall}
    findings, _ = _run(_RecordedPrometheus(_load("warning"), overrides=overrides))
    assert not _by_title(findings, "changed leader")


def test_alert_for_another_member_is_not_attributed():
    scenario = _load("warning")
    for r in scenario["etcd_fsync"]["data"]["result"]:
        r["value"][1] = "0.02"  # all three members slow
    findings, _ = _run(_RecordedPrometheus(scenario))
    noted = [
        f["resources"][0]["name"] for f in _by_title(findings, "fsync") if "etcdHighFsyncDurations" in f["summary"]
    ]
    assert noted == [f"etcd-{MASTERS[0]}"]


def test_failing_alert_lookup_does_not_fail_the_scan():
    findings, failure = _run(_RecordedPrometheus(_load("warning"), fail={"related_alerts"}))
    assert failure is None
    assert findings and not any("also firing" in f["summary"] for f in findings)


def test_titles_keep_their_identity_as_numbers_move():
    from sre_agent.monitor.confidence import _finding_key

    a, _ = _run(_RecordedPrometheus(_load("warning")))
    scenario = _load("warning")
    scenario["etcd_fsync"]["data"]["result"][0]["value"][1] = "0.019"
    scenario["memory_used"]["data"]["result"][0]["value"][1] = str(0.87 * 14.6 * 1024**3)
    b, _ = _run(_RecordedPrometheus(scenario))
    assert {_finding_key(f) for f in a} == {_finding_key(f) for f in b}


# ── Registration and routing ──────────────────────────────────────────────


def test_registered_on_every_dispatch_path():
    from sre_agent.api.scanner_rest import _SCANNER_CATEGORIES
    from sre_agent.monitor.registry import SCANNER_REGISTRY
    from sre_agent.monitor.scanners import _get_all_scanners, get_all_scanner_instances

    assert SCANNER in SCANNER_REGISTRY
    assert SCANNER_REGISTRY[SCANNER]["auto_fixable"] is False
    assert SCANNER in {s.meta.name for s in get_all_scanner_instances()}
    assert SCANNER in {name for name, _ in _get_all_scanners()}
    assert any(SCANNER in names for names in _SCANNER_CATEGORIES.values())


def test_master_memory_engages_the_control_plane_pressure_plan():
    from sre_agent.monitor.plan_executor import template_for

    findings, _ = _run(_RecordedPrometheus(_load("warning")))
    memory = _by_title(findings, "memory at")[0]
    template = template_for(memory)
    assert template is not None and template.id == "control-plane-pressure-v1"


def test_etcd_findings_get_a_freeform_investigation_not_the_memory_plan():
    from sre_agent.monitor.plan_executor import template_for

    findings, _ = _run(_RecordedPrometheus(_load("warning")))
    etcd = [f for f in findings if f["resources"][0]["kind"] == "Etcd"]
    assert etcd
    assert all(template_for(f) is None for f in etcd)


def test_no_auto_fix_strategy_maps_to_these_findings():
    from sre_agent.monitor.autofix import AUTO_FIX_HANDLERS
    from sre_agent.monitor.fix_planner import _FAST_PATH_STRATEGIES, default_fix_plan

    findings, _ = _run(_RecordedPrometheus(_load("critical")))
    assert "control_plane" not in AUTO_FIX_HANDLERS
    assert "control_plane" not in _FAST_PATH_STRATEGIES
    for f in findings:
        assert f["autoFixable"] is False
        assert default_fix_plan(f["category"], f) is None


@pytest.mark.asyncio
async def test_auto_fix_at_the_highest_trust_level_does_nothing_with_them():
    """Even when an investigation suggests a fix, these findings never reach it."""
    from sre_agent.monitor.cluster_monitor import ClusterMonitor

    findings, _ = _run(_RecordedPrometheus(_load("critical")))
    assert findings

    async def remediate(batch: list[dict]) -> tuple[list[dict], MagicMock, MagicMock]:
        monitor = ClusterMonitor()
        monitor._subscribers = []
        monitor._broadcast_raw = AsyncMock()
        monitor.broadcast = AsyncMock()
        saved: list[dict] = []
        plan_fix = MagicMock(return_value=None)
        with (
            patch("sre_agent.monitor.cluster_monitor.is_autofix_paused", return_value=False),
            patch("sre_agent.monitor.cluster_monitor.get_settings") as settings,
            patch("sre_agent.monitor.cluster_monitor.get_monitor_repo", return_value=MagicMock()),
            patch("sre_agent.monitor.cluster_monitor.save_action", side_effect=lambda r, **kw: saved.append(dict(r))),
            patch("sre_agent.monitor.fix_planner.get_investigation_for_finding", return_value={"confidence": 0.95}),
            patch("sre_agent.monitor.fix_planner.plan_fix", plan_fix),
            patch("sre_agent.monitor.fix_planner.execute_fix") as execute,
        ):
            settings.return_value.monitor.autofix_enabled = True
            settings.return_value.monitor.max_trust_level = 4
            await monitor.auto_fix(batch)
        return saved, plan_fix, execute

    saved, plan_fix, execute = await remediate([dict(f) for f in findings])
    assert saved == []
    plan_fix.assert_not_called()
    execute.assert_not_called()

    # The harness does reach the planner when a finding is marked fixable, so
    # the silence above is the finding's doing, not a test that never ran.
    _, plan_fix, _ = await remediate([{**findings[0], "autoFixable": True}])
    plan_fix.assert_called_once()
