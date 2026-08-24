"""get_etcd_status — structured etcd health from already-scraped metrics.

One read-only tool so etcd_defrag and control-plane plans get structured
state instead of composing four PromQL calls each time.
"""

from __future__ import annotations

from unittest.mock import patch

from sre_agent.k8s_tools.monitoring import _ETCD_QUERIES, get_etcd_status


def _prom_response(value: float) -> dict:
    return {"status": "success", "data": {"result": [{"value": [1700000000, str(value)]}]}}


_HEALTHY = {
    "has_leader": 1.0,
    "leader_changes_1h": 0.0,
    "members": 3.0,
    "db_size_mib": 512.0,
    "db_in_use_mib": 300.0,
    "quota_mib": 8192.0,
    "wal_fsync_p99_s": 0.004,
    "backend_commit_p99_s": 0.010,
    "proposal_failures_1h": 0.0,
}


def _fake_request(values: dict[str, float]):
    by_query = {q: values[name] for name, q in _ETCD_QUERIES.items() if name in values}

    def fake(endpoint, params=None, timeout=15):
        q = (params or {}).get("query", "")
        if q in by_query:
            return _prom_response(by_query[q])
        return {"status": "success", "data": {"result": []}}

    return fake


class TestGetEtcdStatus:
    def test_healthy_cluster_summary(self):
        with patch("sre_agent.k8s_tools.monitoring.prometheus_request", side_effect=_fake_request(_HEALTHY)):
            out = get_etcd_status.func()
        assert "elected leader" in out
        assert "3 members" in out
        assert "512 MiB of 8192 MiB quota" in out
        assert "4.0ms" in out  # WAL fsync p99
        assert "⚠" not in out

    def test_reclaimable_space_surfaces_defrag_hint(self):
        with patch("sre_agent.k8s_tools.monitoring.prometheus_request", side_effect=_fake_request(_HEALTHY)):
            out = get_etcd_status.func()
        assert "212 MiB reclaimable by defrag" in out

    def test_no_leader_is_flagged(self):
        values = dict(_HEALTHY, has_leader=0.0)
        with patch("sre_agent.k8s_tools.monitoring.prometheus_request", side_effect=_fake_request(values)):
            out = get_etcd_status.func()
        assert "NO LEADER" in out

    def test_slow_fsync_warns(self):
        values = dict(_HEALTHY, wal_fsync_p99_s=0.05)
        with patch("sre_agent.k8s_tools.monitoring.prometheus_request", side_effect=_fake_request(values)):
            out = get_etcd_status.func()
        assert "above 10ms target" in out

    def test_quota_pressure_warns(self):
        values = dict(_HEALTHY, db_size_mib=7000.0, db_in_use_mib=6800.0)
        with patch("sre_agent.k8s_tools.monitoring.prometheus_request", side_effect=_fake_request(values)):
            out = get_etcd_status.func()
        assert "approaching quota" in out

    def test_unreachable_prometheus_degrades_cleanly(self):
        with patch(
            "sre_agent.k8s_tools.monitoring.prometheus_request",
            side_effect=ConnectionError("no route to host"),
        ):
            out = get_etcd_status.func()
        assert "Cannot read etcd metrics" in out

    def test_registered_in_all_tools_and_a_category(self):
        """A tool absent from ALL_TOOLS or every category is unreachable —
        the stranded-tool failure the contract suite exists to catch."""
        from sre_agent.k8s_tools import ALL_TOOLS
        from sre_agent.tool_categories import get_tool_category

        assert any(getattr(t, "name", "") == "get_etcd_status" for t in ALL_TOOLS)
        assert get_tool_category("get_etcd_status") is not None
