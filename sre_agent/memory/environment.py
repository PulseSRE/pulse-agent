"""Memory about *this* cluster, separate from memory about Kubernetes.

``IncidentStore`` remembers what happened — incidents, learned runbooks, recurring
patterns. It does not remember what is *true here*: who owns the payments
namespace, that Prometheus retains 30 days, that ArgoCD owns production so a
manual edit will be reverted, or that checkout-api normally sits at 600MB.

Without that, every investigation re-derives the same context and reports numbers
with no reference point. "Memory is at 600MB" is not a finding. "Memory is at
600MB, three times this service's normal" is.

Two kinds, deliberately kept apart:

    environment facts   stated or established, changes rarely, no expiry
    workload baselines  measured, changes continuously, recomputed on a window

Facts are asserted by an operator or concluded by the agent. Baselines are
derived from observation. Conflating them would let a measurement overwrite a
statement of intent, or an operator's assertion be silently aged out.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

logger = logging.getLogger("pulse_agent.memory.environment")

CLUSTER_SCOPE = "cluster"

# A baseline computed from a handful of points is noise wearing a number's
# clothes. Below this, report the samples rather than a norm.
MIN_BASELINE_SAMPLES = 20


@dataclass
class EnvironmentFact:
    """Something true about this cluster that the agent should not re-derive."""

    scope: str
    key: str
    value: str
    source: str = ""
    confidence: float = 0.8
    updated_at: int = 0

    def render(self) -> str:
        suffix = f" (source: {self.source})" if self.source else ""
        return f"{self.key}: {self.value}{suffix}"


@dataclass
class WorkloadBaseline:
    """What normal looks like for one workload and metric."""

    namespace: str
    workload: str
    metric: str
    p50: float
    p95: float
    sample_count: int = 0
    window_hours: int = 24
    updated_at: int = 0

    @property
    def is_reliable(self) -> bool:
        return self.sample_count >= MIN_BASELINE_SAMPLES

    def compare(self, observed: float) -> str:
        """Describe an observation against this baseline, in words an operator uses."""
        if not self.is_reliable:
            return f"{observed:g} ({self.metric}) — no reliable baseline yet, {self.sample_count} samples"
        if self.p50 <= 0:
            return f"{observed:g} ({self.metric}) — baseline is zero, cannot compare"

        ratio = observed / self.p50
        if observed > self.p95:
            return f"{observed:g} ({self.metric}) — {ratio:.1f}x this workload's normal ({self.p50:g}), above its p95"
        if ratio >= 1.5:
            return f"{observed:g} ({self.metric}) — {ratio:.1f}x normal ({self.p50:g}) but within its usual range"
        if ratio <= 0.5:
            return f"{observed:g} ({self.metric}) — {ratio:.1f}x normal ({self.p50:g}), unusually low"
        return f"{observed:g} ({self.metric}) — normal for this workload ({self.p50:g})"


def _now_ms() -> int:
    return int(time.time() * 1000)


class ClusterMemory:
    """Store and retrieval for environment facts and workload baselines."""

    def __init__(self, db=None) -> None:
        self._db = db

    @property
    def db(self):
        if self._db is None:
            from ..db import get_database

            self._db = get_database()
        return self._db

    # ----- environment facts -----

    def remember_fact(
        self,
        key: str,
        value: str,
        *,
        scope: str = CLUSTER_SCOPE,
        source: str = "",
        confidence: float = 0.8,
    ) -> bool:
        """Record a fact, replacing any previous value for the same scope and key."""
        key = key.strip()
        value = value.strip()
        if not key or not value:
            return False
        now = _now_ms()
        try:
            self.db.execute(
                """
                INSERT INTO environment_facts
                    (scope, fact_key, fact_value, source, confidence, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (scope, fact_key) DO UPDATE SET
                    fact_value = EXCLUDED.fact_value,
                    source     = EXCLUDED.source,
                    confidence = EXCLUDED.confidence,
                    updated_at = EXCLUDED.updated_at
                """,
                (scope, key, value, source, float(confidence), now, now),
            )
            return True
        except Exception:
            logger.warning("Failed to record environment fact %s/%s", scope, key, exc_info=True)
            return False

    def get_facts(self, scope: str = "") -> list[EnvironmentFact]:
        """Facts for one scope, or every scope when none is given."""
        try:
            if scope:
                rows = self.db.fetchall(
                    "SELECT scope, fact_key, fact_value, source, confidence, updated_at "
                    "FROM environment_facts WHERE scope = ? ORDER BY fact_key",
                    (scope,),
                )
            else:
                rows = self.db.fetchall(
                    "SELECT scope, fact_key, fact_value, source, confidence, updated_at "
                    "FROM environment_facts ORDER BY scope, fact_key"
                )
        except Exception:
            logger.debug("Failed to read environment facts", exc_info=True)
            return []
        return [
            EnvironmentFact(
                scope=r["scope"],
                key=r["fact_key"],
                value=r["fact_value"],
                source=r["source"] or "",
                confidence=float(r["confidence"] or 0.0),
                updated_at=int(r["updated_at"] or 0),
            )
            for r in rows
        ]

    def forget_fact(self, key: str, scope: str = CLUSTER_SCOPE) -> bool:
        """Drop a fact that has stopped being true."""
        try:
            self.db.execute("DELETE FROM environment_facts WHERE scope = ? AND fact_key = ?", (scope, key))
            return True
        except Exception:
            logger.warning("Failed to forget fact %s/%s", scope, key, exc_info=True)
            return False

    # ----- workload baselines -----

    def record_baseline(self, baseline: WorkloadBaseline) -> bool:
        """Store or refresh what normal looks like for one workload and metric."""
        now = _now_ms()
        try:
            self.db.execute(
                """
                INSERT INTO workload_baselines
                    (namespace, workload, metric, p50, p95, sample_count, window_hours, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (namespace, workload, metric) DO UPDATE SET
                    p50          = EXCLUDED.p50,
                    p95          = EXCLUDED.p95,
                    sample_count = EXCLUDED.sample_count,
                    window_hours = EXCLUDED.window_hours,
                    updated_at   = EXCLUDED.updated_at
                """,
                (
                    baseline.namespace,
                    baseline.workload,
                    baseline.metric,
                    float(baseline.p50),
                    float(baseline.p95),
                    int(baseline.sample_count),
                    int(baseline.window_hours),
                    now,
                ),
            )
            return True
        except Exception:
            logger.warning("Failed to record baseline for %s/%s", baseline.namespace, baseline.workload, exc_info=True)
            return False

    def get_baseline(self, namespace: str, workload: str, metric: str) -> WorkloadBaseline | None:
        try:
            row = self.db.fetchone(
                "SELECT namespace, workload, metric, p50, p95, sample_count, window_hours, updated_at "
                "FROM workload_baselines WHERE namespace = ? AND workload = ? AND metric = ?",
                (namespace, workload, metric),
            )
        except Exception:
            logger.debug("Failed to read baseline", exc_info=True)
            return None
        if not row:
            return None
        return WorkloadBaseline(
            namespace=row["namespace"],
            workload=row["workload"],
            metric=row["metric"],
            p50=float(row["p50"]),
            p95=float(row["p95"]),
            sample_count=int(row["sample_count"] or 0),
            window_hours=int(row["window_hours"] or 24),
            updated_at=int(row["updated_at"] or 0),
        )

    def list_baselines(self, namespace: str, workload: str = "") -> list[WorkloadBaseline]:
        try:
            if workload:
                rows = self.db.fetchall(
                    "SELECT namespace, workload, metric, p50, p95, sample_count, window_hours, updated_at "
                    "FROM workload_baselines WHERE namespace = ? AND workload = ? ORDER BY metric",
                    (namespace, workload),
                )
            else:
                rows = self.db.fetchall(
                    "SELECT namespace, workload, metric, p50, p95, sample_count, window_hours, updated_at "
                    "FROM workload_baselines WHERE namespace = ? ORDER BY workload, metric",
                    (namespace,),
                )
        except Exception:
            logger.debug("Failed to list baselines", exc_info=True)
            return []
        return [
            WorkloadBaseline(
                namespace=r["namespace"],
                workload=r["workload"],
                metric=r["metric"],
                p50=float(r["p50"]),
                p95=float(r["p95"]),
                sample_count=int(r["sample_count"] or 0),
                window_hours=int(r["window_hours"] or 24),
                updated_at=int(r["updated_at"] or 0),
            )
            for r in rows
        ]


_memory: ClusterMemory | None = None


def get_cluster_memory() -> ClusterMemory:
    global _memory
    if _memory is None:
        _memory = ClusterMemory()
    return _memory


def reset_cluster_memory() -> None:
    """Drop the cached instance — used by tests."""
    global _memory
    _memory = None
