"""Findings measured against each workload's own normal, not one threshold for all."""

from __future__ import annotations

from unittest.mock import MagicMock

from sre_agent.memory.environment import WorkloadBaseline
from sre_agent.monitor import baselines
from sre_agent.monitor.baselines import (
    _BY_NAME,
    baseline_context,
    is_anomalous,
    refresh_baselines,
    scan_workload_anomalies,
)

MEM = _BY_NAME["memory_bytes"]
GiB = 1 << 30


def _series(rows):
    return {
        "status": "success",
        "data": {"result": [{"metric": {"namespace": ns, "pod": pod}, "value": [0, str(v)]} for ns, pod, v in rows]},
    }


class FakeMemory:
    def __init__(self):
        self.rows: dict = {}

    def record_baseline(self, b):
        self.rows[(b.namespace, b.workload, b.metric)] = b
        return True

    def get_baseline(self, ns, workload, metric):
        return self.rows.get((ns, workload, metric))

    def list_baselines(self, ns, workload=""):
        return [b for (n, w, _), b in self.rows.items() if n == ns and (not workload or w == workload)]


def test_replicas_and_recreated_pods_fold_into_one_workload():
    client = MagicMock()

    def query(promql, timeout=60):
        pods = [("shop", "api-5f58f69bd6-w4x22", 1 * GiB), ("shop", "api-5f58f69bd6-q9z2m", 3 * GiB)]
        if promql.startswith("count_over_time"):
            return _series([(ns, p, 168) for ns, p, _ in pods])
        if "0.95" in promql:
            return _series([(ns, p, v * 1.5) for ns, p, v in pods])
        return _series(pods)

    client.query.side_effect = query
    mem = FakeMemory()
    refresh_baselines(client=client, memory=mem)
    b = mem.get_baseline("shop", "api", "memory_bytes")
    assert b.p50 == 2 * GiB, "the typical replica's median"
    assert b.p95 == 4.5 * GiB, "the highest p95 any replica reached"
    assert b.sample_count == 168 and b.is_reliable


def _baseline(p50, p95, samples=168):
    return WorkloadBaseline("shop", "api", "memory_bytes", p50=p50, p95=p95, sample_count=samples)


def test_well_above_its_own_normal_is_anomalous():
    assert is_anomalous(_baseline(1 * GiB, 1.5 * GiB), 3 * GiB, MEM)


def test_a_routine_peak_is_not():
    """Above p95 but under 2x the median: a busy hour, not an incident."""
    assert not is_anomalous(_baseline(1 * GiB, 1.5 * GiB), 1.8 * GiB, MEM)


def test_a_big_workload_at_its_usual_size_is_not():
    """8 GiB is normal for this JVM. A fixed threshold would page on it forever."""
    assert not is_anomalous(_baseline(8 * GiB, 10 * GiB), 8.5 * GiB, MEM)


def test_no_verdict_without_enough_history():
    assert not is_anomalous(_baseline(1 * GiB, 1.5 * GiB, samples=5), 5 * GiB, MEM)


def test_tiny_values_never_page():
    assert not is_anomalous(_baseline(1 << 20, 2 << 20), 20 << 20, MEM)


def test_the_finding_says_how_far_from_normal(monkeypatch):
    monkeypatch.setattr(baselines, "maybe_refresh", lambda: False)
    mem = FakeMemory()
    mem.record_baseline(_baseline(1 * GiB, 1.5 * GiB))
    client = MagicMock()
    client.query.side_effect = lambda q, timeout=30: (
        _series([("shop", "api-5f58f69bd6-w4x22", 3.2 * GiB)]) if "memory" in q else _series([])
    )
    findings = scan_workload_anomalies(client=client, memory=mem)
    assert len(findings) == 1
    f = findings[0]
    assert f["category"] == "anomaly"
    assert "3.2x this workload's normal" in f["summary"]
    assert "1.0 GiB" in f["summary"]


def test_investigations_are_told_what_normal_is():
    mem = FakeMemory()
    mem.record_baseline(_baseline(1 * GiB, 1.5 * GiB))
    text = baseline_context([{"kind": "Pod", "name": "api-5f58f69bd6-w4x22", "namespace": "shop"}], memory=mem)
    assert text == "shop/api memory_bytes: normally 1.0 GiB, usual peak 1.5 GiB"


def test_refresh_runs_off_the_scan_path_and_not_too_often(monkeypatch):
    started = []
    monkeypatch.setattr(baselines.threading, "Thread", lambda **kw: MagicMock(start=lambda: started.append(kw)))
    monkeypatch.setattr(baselines, "_last_refresh", 0.0)
    assert baselines.maybe_refresh(now=10**9)
    monkeypatch.setattr(baselines, "_last_refresh", 10**9)
    assert not baselines.maybe_refresh(now=10**9 + 60)
    assert len(started) == 1
