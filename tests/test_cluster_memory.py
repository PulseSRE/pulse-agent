"""Tests for cluster memory — environment facts and workload baselines."""

from __future__ import annotations

import pytest

from sre_agent.memory.environment import (
    CLUSTER_SCOPE,
    MIN_BASELINE_SAMPLES,
    ClusterMemory,
    EnvironmentFact,
    WorkloadBaseline,
)


class _FakeDB:
    """Minimal stand-in that keeps rows in dicts, mirroring upsert semantics."""

    def __init__(self) -> None:
        self.facts: dict[tuple[str, str], dict] = {}
        self.baselines: dict[tuple[str, str, str], dict] = {}
        # Database.execute() runs with autocommit=False and keeps the connection
        # checked out until commit(). The original fake persisted immediately, so
        # a missing commit was invisible — every write "succeeded" and nothing
        # was durable. Staging mirrors the real semantics.
        self._pending: list = []
        self.commits = 0

    def commit(self):
        for fn in self._pending:
            fn()
        self._pending.clear()
        self.commits += 1

    def execute(self, sql: str, params: tuple):
        s = " ".join(sql.split())
        if "INSERT INTO environment_facts" in s:
            scope, key, value, source, conf, _created, updated = params
            row = {
                "scope": scope,
                "fact_key": key,
                "fact_value": value,
                "source": source,
                "confidence": conf,
                "updated_at": updated,
            }
            self._pending.append(lambda: self.facts.__setitem__((scope, key), row))
        elif "DELETE FROM environment_facts" in s:
            self._pending.append(lambda: self.facts.pop((params[0], params[1]), None))
        elif "INSERT INTO workload_baselines" in s:
            ns, wl, metric, p50, p95, n, window, updated = params
            brow = {
                "namespace": ns,
                "workload": wl,
                "metric": metric,
                "p50": p50,
                "p95": p95,
                "sample_count": n,
                "window_hours": window,
                "updated_at": updated,
            }
            self._pending.append(lambda: self.baselines.__setitem__((ns, wl, metric), brow))

    def fetchall(self, sql: str, params: tuple = ()):
        s = " ".join(sql.split())
        if "FROM environment_facts" in s:
            rows = list(self.facts.values())
            if "WHERE scope = ?" in s:
                rows = [r for r in rows if r["scope"] == params[0]]
            return sorted(rows, key=lambda r: (r["scope"], r["fact_key"]))
        rows = list(self.baselines.values())
        if "AND workload = ?" in s:
            return [r for r in rows if r["namespace"] == params[0] and r["workload"] == params[1]]
        return [r for r in rows if r["namespace"] == params[0]]

    def fetchone(self, sql: str, params: tuple = ()):
        return self.baselines.get((params[0], params[1], params[2]))


@pytest.fixture
def memory() -> ClusterMemory:
    return ClusterMemory(db=_FakeDB())


class TestEnvironmentFacts:
    def test_records_and_recalls(self, memory):
        assert memory.remember_fact("prometheus_retention", "30 days", source="operator")
        facts = memory.get_facts()
        assert len(facts) == 1
        assert facts[0].key == "prometheus_retention"
        assert facts[0].source == "operator"

    def test_rerecording_replaces_rather_than_duplicates(self, memory):
        memory.remember_fact("owner", "team-a")
        memory.remember_fact("owner", "team-b")
        facts = memory.get_facts()
        assert len(facts) == 1
        assert facts[0].value == "team-b"

    def test_scopes_are_independent(self, memory):
        memory.remember_fact("owner", "commerce", scope="payments")
        memory.remember_fact("owner", "platform", scope=CLUSTER_SCOPE)
        assert len(memory.get_facts()) == 2
        assert memory.get_facts("payments")[0].value == "commerce"

    def test_empty_key_or_value_is_rejected(self, memory):
        assert memory.remember_fact("", "something") is False
        assert memory.remember_fact("key", "   ") is False
        assert memory.get_facts() == []

    def test_a_fact_can_stop_being_true(self, memory):
        memory.remember_fact("gitops", "argocd owns prod")
        memory.forget_fact("gitops")
        assert memory.get_facts() == []

    def test_render_includes_source_when_known(self):
        assert "source: operator" in EnvironmentFact("cluster", "k", "v", source="operator").render()
        assert "source" not in EnvironmentFact("cluster", "k", "v").render()


class TestBaselineComparison:
    """A number on its own is not a finding."""

    @staticmethod
    def _baseline(**kw) -> WorkloadBaseline:
        base = {
            "namespace": "production",
            "workload": "checkout-api",
            "metric": "memory_bytes",
            "p50": 600.0,
            "p95": 800.0,
            "sample_count": 100,
        }
        base.update(kw)
        return WorkloadBaseline(**base)

    def test_normal_reads_as_normal(self):
        assert "normal for this workload" in self._baseline().compare(620)

    def test_above_p95_is_called_out_with_a_multiple(self):
        out = self._baseline().compare(1800)
        assert "3.0x" in out
        assert "above its p95" in out

    def test_elevated_but_within_range_is_distinguished(self):
        # needs a workload whose p95 is more than 1.5x its p50, otherwise anything
        # elevated is already above p95 and this branch cannot be reached
        out = self._baseline(p95=1200.0).compare(900)
        assert "within its usual range" in out
        assert "1.5x" in out

    def test_mildly_elevated_still_reads_as_normal(self):
        assert "normal for this workload" in self._baseline().compare(780)

    def test_unusually_low_is_reported(self):
        assert "unusually low" in self._baseline().compare(100)

    def test_thin_sample_refuses_to_claim_a_norm(self):
        out = self._baseline(sample_count=MIN_BASELINE_SAMPLES - 1).compare(1800)
        assert "no reliable baseline" in out
        assert "3.0x" not in out

    def test_zero_baseline_does_not_divide_by_zero(self):
        assert "cannot compare" in self._baseline(p50=0.0).compare(5)

    def test_reliability_threshold(self):
        assert self._baseline(sample_count=MIN_BASELINE_SAMPLES).is_reliable
        assert not self._baseline(sample_count=MIN_BASELINE_SAMPLES - 1).is_reliable


class TestBaselineStorage:
    def test_round_trip(self, memory):
        assert memory.record_baseline(WorkloadBaseline("production", "checkout-api", "memory_bytes", 600.0, 800.0, 120))
        got = memory.get_baseline("production", "checkout-api", "memory_bytes")
        assert got is not None
        assert got.p50 == 600.0
        assert got.sample_count == 120

    def test_missing_baseline_returns_none(self, memory):
        assert memory.get_baseline("production", "nope", "memory_bytes") is None

    def test_list_by_namespace_and_workload(self, memory):
        memory.record_baseline(WorkloadBaseline("prod", "a", "memory_bytes", 1, 2, 50))
        memory.record_baseline(WorkloadBaseline("prod", "a", "cpu_cores", 1, 2, 50))
        memory.record_baseline(WorkloadBaseline("prod", "b", "memory_bytes", 1, 2, 50))
        assert len(memory.list_baselines("prod")) == 3
        assert len(memory.list_baselines("prod", "a")) == 2


class TestRealDatabaseAccessor:
    """The 18 tests above inject a fake db, so none of them touch this path.

    ClusterMemory.db imported `get_db`, which does not exist — the accessor is
    `get_database`. Every call raised ImportError, was swallowed by the caller's
    own except, and returned False. Feature 3 was inert in production with the
    tables present and no error anywhere.
    """

    def test_the_accessor_it_imports_actually_exists(self):
        import sre_agent.db as db_module
        from sre_agent.memory.environment import ClusterMemory

        source = ClusterMemory.db.fget.__code__.co_names
        imported = [n for n in source if n.startswith("get_")]
        assert imported, "db property should import an accessor"
        for name in imported:
            assert hasattr(db_module, name), f"sre_agent.db has no {name!r}"


class TestReachability:
    def test_memory_tools_are_categorised(self):
        from sre_agent.tool_categories import TOOL_CATEGORIES

        categorised: set[str] = set()
        for cat in TOOL_CATEGORIES.values():
            categorised.update(cat.get("tools", []))
        for tool in (
            "remember_environment_fact",
            "get_environment_facts",
            "compare_to_baseline",
            "search_conversations",
        ):
            assert tool in categorised, f"{tool} would never be offered"


class TestWritesAreCommitted:
    """Database.execute keeps the connection checked out until commit().

    Without it the write is rolled back when the connection returns to the pool.
    remember_fact returned True and get_facts returned [] — a write that reports
    success and does nothing durable.
    """

    def test_recording_a_fact_commits(self, memory):
        memory.remember_fact("k", "v")
        assert memory.db.commits >= 1
        assert [f.key for f in memory.get_facts()] == ["k"]

    def test_forgetting_a_fact_commits(self, memory):
        memory.remember_fact("k", "v")
        before = memory.db.commits
        memory.forget_fact("k")
        assert memory.db.commits > before
        assert memory.get_facts() == []

    def test_recording_a_baseline_commits(self, memory):
        memory.record_baseline(WorkloadBaseline("prod", "api", "memory_bytes", 1.0, 2.0, 50))
        assert memory.db.commits >= 1
        assert memory.get_baseline("prod", "api", "memory_bytes") is not None
