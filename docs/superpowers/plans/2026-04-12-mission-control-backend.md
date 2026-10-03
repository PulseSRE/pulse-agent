# Mission Control — Backend Analytics APIs Implementation Plan

> Historical design/implementation record. This preserves the proposal and tasks at the date in the filename; unchecked tasks, paths, commands, and security claims are not evidence of the current implementation. Use [current documentation](../../README.md) and verify behavior against code before applying a procedure.


> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build 9 new REST endpoints and 1 WebSocket event that surface agent analytics data to the UI — trust-building metrics for operators (Mission Control) and tuning metrics for developers (Toolbox).

**Architecture:** New `api/analytics_rest.py` router handles all analytics endpoints. Aggregation logic lives in new helper functions within existing modules (`tool_usage.py`, `intelligence.py`, `prompt_log.py`, `memory/store.py`) rather than raw SQL in endpoints. Monitor endpoints (`fix-history/summary`, `coverage`) extend `api/monitor_rest.py`. WebSocket skill activity event extends the existing `/ws/monitor` handler.

**Tech Stack:** Python 3.11, PostgreSQL, FastAPI, psycopg2, pytest

**Spec:** `docs/superpowers/specs/2026-04-12-mission-control-redesign-design.md`

**Out of scope (separate plan):** All frontend/UI changes (OpenshiftPulse repo).

---

## File Structure

| File | Responsibility |
|------|---------------|
| `sre_agent/api/analytics_rest.py` (create) | Analytics router: `/analytics/confidence`, `/analytics/accuracy`, `/analytics/cost`, `/analytics/intelligence`, `/analytics/prompt`, `/recommendations`, `/readiness/summary` |
| `sre_agent/api/monitor_rest.py` (modify) | Add `/fix-history/summary` and `/monitor/coverage` endpoints |
| `sre_agent/api/app.py` (modify) | Register analytics router |
| `sre_agent/intelligence.py` (modify) | Add `get_intelligence_sections()` returning structured dicts instead of markdown |
| `sre_agent/prompt_log.py` (modify) | Expose `get_prompt_stats()` and `get_prompt_versions()` (already exist, just need REST wiring) |
| `sre_agent/memory/store.py` (modify) | Add `get_accuracy_stats()` for anti-patterns, learning stats, override rate |
| `sre_agent/tool_usage.py` (modify) | Add `get_cost_per_incident()` and `get_satisfaction_stats()` |
| `sre_agent/api/ws_monitor.py` or equivalent (modify) | Add `skill_activity` WebSocket event |
| `tests/test_analytics_rest.py` (create) | All analytics endpoint tests |
| `tests/test_analytics_helpers.py` (create) | Tests for new aggregation helper functions |

---

### Task 1: Create analytics router scaffold + fix-history/summary endpoint

**Files:**
- Create: `sre_agent/api/analytics_rest.py`
- Modify: `sre_agent/api/app.py`
- Modify: `sre_agent/api/monitor_rest.py`
- Create: `tests/test_analytics_rest.py`

- [ ] **Step 1: Write failing test for fix-history/summary**

Create `tests/test_analytics_rest.py`:

```python
"""Tests for Mission Control analytics endpoints."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def api_client(pulse_token, monkeypatch, tmp_path):
    monkeypatch.setenv("PULSE_AGENT_WS_TOKEN", pulse_token)
    monkeypatch.setenv("PULSE_AGENT_MEMORY", "0")
    monkeypatch.setenv("PULSE_AGENT_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")

    with (
        patch("sre_agent.k8s_client._initialized", True),
        patch("sre_agent.k8s_client._load_k8s"),
        patch("sre_agent.k8s_client.get_core_client") as core,
        patch("sre_agent.k8s_client.get_apps_client") as apps,
        patch("sre_agent.k8s_client.get_custom_client") as custom,
    ):
        core.return_value = MagicMock()
        apps.return_value = MagicMock()
        custom.return_value = MagicMock()
        from sre_agent.api.app import app

        client = TestClient(app, raise_server_exceptions=False)
        yield client


@pytest.fixture
def api_headers(pulse_token):
    return {"Authorization": f"Bearer {pulse_token}"}


class TestFixHistorySummary:
    def test_returns_aggregated_stats(self, api_client, api_headers):
        with patch("sre_agent.api.monitor_rest.get_fix_history_summary") as mock:
            mock.return_value = {
                "total_actions": 20,
                "completed": 16,
                "failed": 2,
                "rolled_back": 2,
                "success_rate": 0.8,
                "rollback_rate": 0.1,
                "avg_resolution_ms": 245000,
                "by_category": [
                    {"category": "crashloop", "count": 10, "success_count": 9, "auto_fixed": 7, "confirmation_required": 3},
                    {"category": "workloads", "count": 6, "success_count": 5, "auto_fixed": 3, "confirmation_required": 3},
                ],
                "trend": {"current_week": 12, "previous_week": 8, "delta": 4},
            }
            r = api_client.get("/fix-history/summary", headers=api_headers)

        assert r.status_code == 200
        data = r.json()
        assert data["total_actions"] == 20
        assert data["success_rate"] == 0.8
        assert len(data["by_category"]) == 2
        assert "trend" in data

    def test_empty_history(self, api_client, api_headers):
        with patch("sre_agent.api.monitor_rest.get_fix_history_summary") as mock:
            mock.return_value = {
                "total_actions": 0,
                "completed": 0,
                "failed": 0,
                "rolled_back": 0,
                "success_rate": 0.0,
                "rollback_rate": 0.0,
                "avg_resolution_ms": 0,
                "by_category": [],
                "trend": {"current_week": 0, "previous_week": 0, "delta": 0},
            }
            r = api_client.get("/fix-history/summary", headers=api_headers)

        assert r.status_code == 200
        assert r.json()["total_actions"] == 0

    def test_requires_auth(self, api_client):
        r = api_client.get("/fix-history/summary")
        assert r.status_code == 401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_analytics_rest.py -v -k "TestFixHistorySummary" 2>&1 | tail -20`
Expected: FAIL — `get_fix_history_summary` not found or endpoint 404

- [ ] **Step 3: Implement fix-history/summary endpoint and helper**

Add to `sre_agent/api/monitor_rest.py`:

```python
@router.get("/fix-history/summary")
async def rest_fix_history_summary(
    days: int = Query(7, ge=1, le=90),
    _auth=Depends(verify_token),
):
    """Aggregated fix history stats for Mission Control outcomes card."""
    return get_fix_history_summary(days=days)


def get_fix_history_summary(days: int = 7) -> dict:
    """Compute aggregated fix history statistics."""
    from .. import db

    database = db.get_database()

    cutoff_sql = f"NOW() - INTERVAL '1 day' * {days}"
    half_days = days // 2 or 1
    prev_cutoff_sql = f"NOW() - INTERVAL '1 day' * {days + half_days}"

    # Overall counts
    overall = database.fetchone(
        f"SELECT "
        f"  COUNT(*) AS total, "
        f"  COUNT(*) FILTER (WHERE status = 'completed') AS completed, "
        f"  COUNT(*) FILTER (WHERE status = 'failed') AS failed, "
        f"  COUNT(*) FILTER (WHERE status = 'rolled_back') AS rolled_back, "
        f"  COALESCE(ROUND(AVG(duration_ms)), 0) AS avg_resolution_ms "
        f"FROM actions WHERE timestamp > EXTRACT(EPOCH FROM {cutoff_sql}) * 1000"
    )

    total = overall["total"] if overall else 0
    completed = overall["completed"] if overall else 0
    failed = overall["failed"] if overall else 0
    rolled_back = overall["rolled_back"] if overall else 0

    # By category
    by_cat = database.fetchall(
        f"SELECT category, COUNT(*) AS count, "
        f"  COUNT(*) FILTER (WHERE status = 'completed') AS success_count, "
        f"  COUNT(*) FILTER (WHERE was_confirmed = false AND status = 'completed') AS auto_fixed, "
        f"  COUNT(*) FILTER (WHERE was_confirmed = true) AS confirmation_required "
        f"FROM actions WHERE timestamp > EXTRACT(EPOCH FROM {cutoff_sql}) * 1000 "
        f"GROUP BY category ORDER BY count DESC"
    )

    # Trend: current period vs previous period
    prev_total = database.fetchone(
        f"SELECT COUNT(*) AS cnt FROM actions "
        f"WHERE timestamp > EXTRACT(EPOCH FROM {prev_cutoff_sql}) * 1000 "
        f"  AND timestamp <= EXTRACT(EPOCH FROM {cutoff_sql}) * 1000"
    )
    prev_count = prev_total["cnt"] if prev_total else 0

    return {
        "total_actions": total,
        "completed": completed,
        "failed": failed,
        "rolled_back": rolled_back,
        "success_rate": round(completed / total, 3) if total > 0 else 0.0,
        "rollback_rate": round(rolled_back / total, 3) if total > 0 else 0.0,
        "avg_resolution_ms": int(overall["avg_resolution_ms"]) if overall else 0,
        "by_category": [dict(r) for r in by_cat],
        "trend": {
            "current_week": total,
            "previous_week": prev_count,
            "delta": total - prev_count,
        },
    }
```

- [ ] **Step 4: Create analytics router scaffold**

Create `sre_agent/api/analytics_rest.py`:

```python
"""Analytics REST endpoints for Mission Control and Toolbox."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from .auth import verify_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent/analytics", tags=["analytics"])
```

Register in `sre_agent/api/app.py` — add alongside existing router imports:

```python
from .analytics_rest import router as analytics_router
# ...
app.include_router(analytics_router)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_analytics_rest.py -v -k "TestFixHistorySummary" 2>&1 | tail -20`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add sre_agent/api/analytics_rest.py sre_agent/api/monitor_rest.py sre_agent/api/app.py tests/test_analytics_rest.py
git commit -m "feat: add fix-history/summary endpoint + analytics router scaffold"
```

---

### Task 2: Scanner coverage endpoint

**Files:**
- Modify: `sre_agent/api/monitor_rest.py`
- Modify: `tests/test_analytics_rest.py`

- [ ] **Step 1: Write failing test for coverage endpoint**

Add to `tests/test_analytics_rest.py`:

```python
class TestScannerCoverage:
    def test_returns_coverage_stats(self, api_client, api_headers):
        with patch("sre_agent.api.monitor_rest.get_scanner_coverage") as mock:
            mock.return_value = {
                "active_scanners": 12,
                "total_scanners": 17,
                "coverage_pct": 78.0,
                "categories": [
                    {"name": "pod_health", "covered": True, "scanners": ["crashloop", "pending", "oom"]},
                    {"name": "node_pressure", "covered": True, "scanners": ["node_pressure"]},
                    {"name": "storage", "covered": False, "scanners": []},
                ],
                "per_scanner": [
                    {
                        "name": "scan_crashlooping_pods",
                        "enabled": True,
                        "finding_count": 12,
                        "actionable_count": 8,
                        "noise_pct": 15.0,
                    },
                ],
            }
            r = api_client.get("/monitor/coverage", headers=api_headers)

        assert r.status_code == 200
        data = r.json()
        assert data["active_scanners"] == 12
        assert data["coverage_pct"] == 78.0
        assert len(data["categories"]) == 3
        assert data["per_scanner"][0]["noise_pct"] == 15.0

    def test_requires_auth(self, api_client):
        r = api_client.get("/monitor/coverage")
        assert r.status_code == 401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_analytics_rest.py::TestScannerCoverage -v 2>&1 | tail -10`
Expected: FAIL — endpoint 404

- [ ] **Step 3: Implement coverage endpoint**

Add to `sre_agent/api/monitor_rest.py`:

```python
# Scanner category mapping — which failure modes each scanner covers
_SCANNER_CATEGORIES = {
    "pod_health": ["scan_crashlooping_pods", "scan_pending_pods", "scan_oom_killed_pods", "scan_image_pull_errors"],
    "node_pressure": ["scan_node_pressure"],
    "workload_health": ["scan_failed_deployments", "scan_daemonset_gaps", "scan_hpa_saturation"],
    "security_audit": ["scan_rbac_changes", "scan_auth_events", "scan_config_changes"],
    "certificate_expiry": ["scan_expiring_certs"],
    "alerts": ["scan_firing_alerts"],
    "deployment_audit": ["scan_deployment_changes", "scan_warning_events"],
    "operator_health": ["scan_degraded_operators"],
}


@router.get("/monitor/coverage")
async def rest_scanner_coverage(
    days: int = Query(7, ge=1, le=90),
    _auth=Depends(verify_token),
):
    """Scanner coverage stats for Mission Control coverage card."""
    return get_scanner_coverage(days=days)


def get_scanner_coverage(days: int = 7) -> dict:
    """Compute scanner coverage percentage and per-scanner stats."""
    from ..monitor.scanners import SCANNER_REGISTRY

    # Get enabled scanners from registry
    all_scanners = list(SCANNER_REGISTRY.keys()) if hasattr(SCANNER_REGISTRY, "keys") else []
    enabled = [s for s in all_scanners if SCANNER_REGISTRY.get(s, {}).get("enabled", True)]

    total = len(all_scanners) or 1
    active = len(enabled)

    # Category coverage
    categories = []
    for cat_name, cat_scanners in _SCANNER_CATEGORIES.items():
        covered_scanners = [s for s in cat_scanners if s in enabled]
        categories.append({
            "name": cat_name,
            "covered": len(covered_scanners) > 0,
            "scanners": covered_scanners,
        })

    covered_cats = sum(1 for c in categories if c["covered"])
    coverage_pct = round(covered_cats / len(categories) * 100, 1) if categories else 0.0

    # Per-scanner finding stats from DB
    per_scanner = []
    try:
        from .. import db

        database = db.get_database()
        # Count findings per scanner from scan_runs
        for scanner_name in all_scanners:
            finding_count = 0
            actionable_count = 0
            noise_count = 0
            try:
                row = database.fetchone(
                    "SELECT COUNT(*) AS cnt FROM actions WHERE category = %s "
                    "AND timestamp > EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * %s) * 1000",
                    (scanner_name.replace("scan_", ""), days),
                )
                actionable_count = row["cnt"] if row else 0
            except Exception:
                pass

            per_scanner.append({
                "name": scanner_name,
                "enabled": scanner_name in enabled,
                "finding_count": finding_count,
                "actionable_count": actionable_count,
                "noise_pct": 0.0,
            })
    except Exception:
        logger.debug("Failed to compute per-scanner stats", exc_info=True)

    return {
        "active_scanners": active,
        "total_scanners": total,
        "coverage_pct": coverage_pct,
        "categories": categories,
        "per_scanner": per_scanner,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_analytics_rest.py::TestScannerCoverage -v 2>&1 | tail -10`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/api/monitor_rest.py tests/test_analytics_rest.py
git commit -m "feat: add scanner coverage endpoint with category breakdown"
```

---

### Task 3: Confidence calibration endpoint

**Files:**
- Modify: `sre_agent/api/analytics_rest.py`
- Modify: `tests/test_analytics_rest.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_analytics_rest.py`:

```python
class TestConfidenceCalibration:
    def test_returns_calibration_stats(self, api_client, api_headers):
        with patch("sre_agent.api.analytics_rest._compute_confidence_calibration") as mock:
            mock.return_value = {
                "brier_score": 0.08,
                "accuracy_pct": 92.0,
                "rating": "good",
                "total_predictions": 45,
                "buckets": [
                    {"range": "0.0-0.2", "predicted": 0.1, "actual": 0.05, "count": 5},
                    {"range": "0.8-1.0", "predicted": 0.9, "actual": 0.88, "count": 20},
                ],
            }
            r = api_client.get("/api/agent/analytics/confidence", headers=api_headers)

        assert r.status_code == 200
        data = r.json()
        assert data["accuracy_pct"] == 92.0
        assert data["rating"] == "good"
        assert len(data["buckets"]) == 2

    def test_no_data(self, api_client, api_headers):
        with patch("sre_agent.api.analytics_rest._compute_confidence_calibration") as mock:
            mock.return_value = {
                "brier_score": 0.0,
                "accuracy_pct": 0.0,
                "rating": "insufficient_data",
                "total_predictions": 0,
                "buckets": [],
            }
            r = api_client.get("/api/agent/analytics/confidence", headers=api_headers)

        assert r.status_code == 200
        assert r.json()["rating"] == "insufficient_data"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_analytics_rest.py::TestConfidenceCalibration -v 2>&1 | tail -10`
Expected: FAIL — endpoint 404

- [ ] **Step 3: Implement confidence calibration endpoint**

Add to `sre_agent/api/analytics_rest.py`:

```python
@router.get("/confidence")
async def analytics_confidence(
    days: int = Query(30, ge=1, le=365),
    _auth=Depends(verify_token),
):
    """Confidence calibration stats for Mission Control quality card."""
    return _compute_confidence_calibration(days=days)


def _compute_confidence_calibration(days: int = 30) -> dict:
    """Compute Brier score from action confidence vs. verification outcome."""
    try:
        from .. import db

        database = db.get_database()
        rows = database.fetchall(
            "SELECT confidence, verification_status FROM actions "
            "WHERE confidence IS NOT NULL "
            "  AND verification_status IS NOT NULL "
            "  AND timestamp > EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * %s) * 1000",
            (days,),
        )

        if not rows or len(rows) < 5:
            return {
                "brier_score": 0.0,
                "accuracy_pct": 0.0,
                "rating": "insufficient_data",
                "total_predictions": len(rows) if rows else 0,
                "buckets": [],
            }

        # Brier score: mean squared error of predicted vs actual
        total_se = 0.0
        bucket_data: dict[str, list[tuple[float, float]]] = {}
        for row in rows:
            conf = float(row["confidence"])
            actual = 1.0 if row["verification_status"] == "verified" else 0.0
            total_se += (conf - actual) ** 2

            # Bucket by 0.2 intervals
            bucket_idx = min(int(conf / 0.2), 4)
            bucket_key = f"{bucket_idx * 0.2:.1f}-{(bucket_idx + 1) * 0.2:.1f}"
            bucket_data.setdefault(bucket_key, []).append((conf, actual))

        brier = total_se / len(rows)
        accuracy_pct = round((1 - brier) * 100, 1)

        # Rating thresholds
        if accuracy_pct >= 85:
            rating = "good"
        elif accuracy_pct >= 70:
            rating = "fair"
        else:
            rating = "poor"

        buckets = []
        for range_key in sorted(bucket_data.keys()):
            pairs = bucket_data[range_key]
            avg_pred = sum(p[0] for p in pairs) / len(pairs)
            avg_actual = sum(p[1] for p in pairs) / len(pairs)
            buckets.append({
                "range": range_key,
                "predicted": round(avg_pred, 3),
                "actual": round(avg_actual, 3),
                "count": len(pairs),
            })

        return {
            "brier_score": round(brier, 4),
            "accuracy_pct": accuracy_pct,
            "rating": rating,
            "total_predictions": len(rows),
            "buckets": buckets,
        }
    except Exception:
        logger.debug("Failed to compute confidence calibration", exc_info=True)
        return {
            "brier_score": 0.0,
            "accuracy_pct": 0.0,
            "rating": "insufficient_data",
            "total_predictions": 0,
            "buckets": [],
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_analytics_rest.py::TestConfidenceCalibration -v 2>&1 | tail -10`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/api/analytics_rest.py tests/test_analytics_rest.py
git commit -m "feat: add confidence calibration endpoint (Brier score)"
```

---

### Task 4: Accuracy analytics endpoint

**Files:**
- Modify: `sre_agent/api/analytics_rest.py`
- Modify: `sre_agent/memory/store.py`
- Modify: `tests/test_analytics_rest.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_analytics_rest.py`:

```python
class TestAccuracyAnalytics:
    def test_returns_accuracy_stats(self, api_client, api_headers):
        with patch("sre_agent.api.analytics_rest._compute_accuracy_stats") as mock:
            mock.return_value = {
                "avg_quality_score": 0.82,
                "quality_trend": {"current": 0.82, "previous": 0.74, "delta": 0.08},
                "dimensions": {
                    "resolution": 0.85,
                    "efficiency": 0.78,
                    "safety": 0.95,
                    "speed": 0.70,
                },
                "anti_patterns": [
                    {
                        "error_type": "image_pull",
                        "namespace": "prod",
                        "count": 3,
                        "description": "Agent struggled with image pull errors in prod namespace",
                    }
                ],
                "learning": {
                    "total_runbooks": 12,
                    "new_this_month": 3,
                    "runbook_success_rate": 0.87,
                    "total_patterns": 4,
                    "pattern_types": {"recurring": 2, "time_based": 1, "correlation": 1},
                },
                "override_rate": {
                    "overrides": 2,
                    "total_proposed": 14,
                    "rate": 0.143,
                },
            }
            r = api_client.get("/api/agent/analytics/accuracy", headers=api_headers)

        assert r.status_code == 200
        data = r.json()
        assert data["avg_quality_score"] == 0.82
        assert data["quality_trend"]["delta"] == 0.08
        assert len(data["anti_patterns"]) == 1
        assert data["learning"]["runbook_success_rate"] == 0.87
        assert data["override_rate"]["rate"] == 0.143
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_analytics_rest.py::TestAccuracyAnalytics -v 2>&1 | tail -10`
Expected: FAIL

- [ ] **Step 3: Add get_accuracy_stats to memory store**

Add to `sre_agent/memory/store.py`:

```python
def get_accuracy_stats(self, days: int = 30) -> dict:
    """Compute accuracy analytics: quality scores, anti-patterns, learning stats."""
    # Quality scores from incidents table
    all_incidents = self.db.fetchall(
        "SELECT score, query_keywords, error_type, namespace, timestamp "
        "FROM incidents WHERE score > 0 AND timestamp > datetime('now', ?)",
        (f"-{days} days",),
    )

    scores = [r["score"] for r in all_incidents if r["score"]]
    avg_score = sum(scores) / len(scores) if scores else 0.0

    # Previous period for trend
    prev_incidents = self.db.fetchall(
        "SELECT score FROM incidents WHERE score > 0 "
        "AND timestamp > datetime('now', ?) AND timestamp <= datetime('now', ?)",
        (f"-{days * 2} days", f"-{days} days"),
    )
    prev_scores = [r["score"] for r in prev_incidents if r["score"]]
    prev_avg = sum(prev_scores) / len(prev_scores) if prev_scores else 0.0

    # Anti-patterns: low-score incidents grouped by error_type
    low_score = self.db.fetchall(
        "SELECT error_type, namespace, COUNT(*) AS cnt "
        "FROM incidents WHERE score < 0.4 AND score > 0 "
        "AND timestamp > datetime('now', ?) "
        "GROUP BY error_type, namespace "
        "HAVING COUNT(*) >= 2 "
        "ORDER BY cnt DESC LIMIT 3",
        (f"-{days} days",),
    )
    anti_patterns = [
        {
            "error_type": r["error_type"] or "unknown",
            "namespace": r["namespace"] or "cluster-wide",
            "count": r["cnt"],
            "description": f"Agent struggled with {r['error_type'] or 'unknown'} errors"
            + (f" in {r['namespace']}" if r["namespace"] else ""),
        }
        for r in low_score
    ]

    # Learning stats
    runbooks = self.db.fetchall("SELECT success_count, failure_count FROM runbooks")
    total_success = sum(r["success_count"] for r in runbooks)
    total_failure = sum(r["failure_count"] for r in runbooks)
    runbook_total = total_success + total_failure

    patterns = self.db.fetchall("SELECT pattern_type, COUNT(*) AS cnt FROM patterns GROUP BY pattern_type")
    pattern_types = {r["pattern_type"]: r["cnt"] for r in patterns}

    new_runbooks = self.db.fetchall(
        "SELECT COUNT(*) AS cnt FROM runbooks WHERE created_at > datetime('now', ?)",
        (f"-{days} days",),
    )

    return {
        "avg_quality_score": round(avg_score, 3),
        "quality_trend": {
            "current": round(avg_score, 3),
            "previous": round(prev_avg, 3),
            "delta": round(avg_score - prev_avg, 3),
        },
        "anti_patterns": anti_patterns,
        "learning": {
            "total_runbooks": len(runbooks),
            "new_this_month": new_runbooks[0]["cnt"] if new_runbooks else 0,
            "runbook_success_rate": round(total_success / runbook_total, 3) if runbook_total > 0 else 0.0,
            "total_patterns": sum(pattern_types.values()),
            "pattern_types": pattern_types,
        },
    }
```

- [ ] **Step 4: Implement accuracy endpoint**

Add to `sre_agent/api/analytics_rest.py`:

```python
@router.get("/accuracy")
async def analytics_accuracy(
    days: int = Query(30, ge=1, le=365),
    _auth=Depends(verify_token),
):
    """Accuracy analytics for Mission Control agent accuracy section."""
    return _compute_accuracy_stats(days=days)


def _compute_accuracy_stats(days: int = 30) -> dict:
    """Aggregate accuracy stats from memory store and actions table."""
    result: dict = {
        "avg_quality_score": 0.0,
        "quality_trend": {"current": 0.0, "previous": 0.0, "delta": 0.0},
        "dimensions": {"resolution": 0.0, "efficiency": 0.0, "safety": 0.0, "speed": 0.0},
        "anti_patterns": [],
        "learning": {
            "total_runbooks": 0,
            "new_this_month": 0,
            "runbook_success_rate": 0.0,
            "total_patterns": 0,
            "pattern_types": {},
        },
        "override_rate": {"overrides": 0, "total_proposed": 0, "rate": 0.0},
    }

    # Memory store stats (SQLite)
    try:
        from ..memory.store import IncidentStore

        store = IncidentStore()
        memory_stats = store.get_accuracy_stats(days=days)
        result.update({
            "avg_quality_score": memory_stats["avg_quality_score"],
            "quality_trend": memory_stats["quality_trend"],
            "anti_patterns": memory_stats["anti_patterns"],
            "learning": memory_stats["learning"],
        })
    except Exception:
        logger.debug("Failed to get memory accuracy stats", exc_info=True)

    # Override rate from actions table (PostgreSQL)
    try:
        from .. import db

        database = db.get_database()
        override_row = database.fetchone(
            "SELECT "
            "  COUNT(*) FILTER (WHERE was_confirmed = false) AS overrides, "
            "  COUNT(*) AS total "
            "FROM tool_usage "
            "WHERE requires_confirmation = true "
            "  AND timestamp > NOW() - INTERVAL '1 day' * %s",
            (days,),
        )
        if override_row and override_row["total"] > 0:
            overrides = override_row["overrides"]
            total = override_row["total"]
            result["override_rate"] = {
                "overrides": overrides,
                "total_proposed": total,
                "rate": round(overrides / total, 3),
            }
    except Exception:
        logger.debug("Failed to compute override rate", exc_info=True)

    return result
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_analytics_rest.py::TestAccuracyAnalytics -v 2>&1 | tail -10`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add sre_agent/api/analytics_rest.py sre_agent/memory/store.py tests/test_analytics_rest.py
git commit -m "feat: add accuracy analytics endpoint with anti-patterns and override rate"
```

---

### Task 5: Cost analytics endpoint

**Files:**
- Modify: `sre_agent/api/analytics_rest.py`
- Modify: `tests/test_analytics_rest.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_analytics_rest.py`:

```python
class TestCostAnalytics:
    def test_returns_cost_stats(self, api_client, api_headers):
        with patch("sre_agent.api.analytics_rest._compute_cost_stats") as mock:
            mock.return_value = {
                "avg_tokens_per_incident": 12400,
                "trend": {"current": 12400, "previous": 15100, "delta_pct": -17.9},
                "by_mode": [
                    {"mode": "sre", "avg_tokens": 11200, "count": 30},
                    {"mode": "security", "avg_tokens": 18500, "count": 8},
                ],
                "total_tokens": 496000,
                "total_incidents": 40,
            }
            r = api_client.get("/api/agent/analytics/cost", headers=api_headers)

        assert r.status_code == 200
        data = r.json()
        assert data["avg_tokens_per_incident"] == 12400
        assert data["trend"]["delta_pct"] == -17.9
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_analytics_rest.py::TestCostAnalytics -v 2>&1 | tail -10`
Expected: FAIL

- [ ] **Step 3: Implement cost endpoint**

Add to `sre_agent/api/analytics_rest.py`:

```python
@router.get("/cost")
async def analytics_cost(
    days: int = Query(30, ge=1, le=365),
    _auth=Depends(verify_token),
):
    """Token cost per incident for Mission Control outcomes card."""
    return _compute_cost_stats(days=days)


def _compute_cost_stats(days: int = 30) -> dict:
    """Compute average tokens per incident, trending."""
    try:
        from .. import db

        database = db.get_database()
        half = days // 2 or 1

        # Current period: avg tokens per session
        current = database.fetchone(
            "SELECT COALESCE(ROUND(AVG(session_tokens)), 0) AS avg_tokens, "
            "  COUNT(DISTINCT session_id) AS session_count, "
            "  COALESCE(SUM(input_tokens + output_tokens), 0) AS total_tokens "
            "FROM tool_turns "
            "WHERE timestamp > NOW() - INTERVAL '1 day' * %s",
            (days,),
        )

        # Previous period
        prev = database.fetchone(
            "SELECT COALESCE(ROUND(AVG(session_tokens)), 0) AS avg_tokens, "
            "  COUNT(DISTINCT session_id) AS session_count "
            "FROM (SELECT session_id, SUM(input_tokens + output_tokens) AS session_tokens "
            "      FROM tool_turns "
            "      WHERE timestamp > NOW() - INTERVAL '1 day' * %s "
            "        AND timestamp <= NOW() - INTERVAL '1 day' * %s "
            "      GROUP BY session_id) sub",
            (days + half, days),
        )

        # Per-session token sums for current period
        per_session = database.fetchone(
            "SELECT COALESCE(ROUND(AVG(session_tokens)), 0) AS avg_tokens "
            "FROM (SELECT session_id, SUM(input_tokens + output_tokens) AS session_tokens "
            "      FROM tool_turns "
            "      WHERE timestamp > NOW() - INTERVAL '1 day' * %s "
            "      GROUP BY session_id) sub",
            (days,),
        )

        cur_avg = int(per_session["avg_tokens"]) if per_session else 0
        prev_avg = int(prev["avg_tokens"]) if prev else 0
        delta_pct = round((cur_avg - prev_avg) / prev_avg * 100, 1) if prev_avg > 0 else 0.0

        # By mode
        by_mode = database.fetchall(
            "SELECT agent_mode AS mode, "
            "  COALESCE(ROUND(AVG(input_tokens + output_tokens)), 0) AS avg_tokens, "
            "  COUNT(*) AS count "
            "FROM tool_turns "
            "WHERE timestamp > NOW() - INTERVAL '1 day' * %s "
            "  AND agent_mode IS NOT NULL "
            "GROUP BY agent_mode ORDER BY count DESC",
            (days,),
        )

        return {
            "avg_tokens_per_incident": cur_avg,
            "trend": {
                "current": cur_avg,
                "previous": prev_avg,
                "delta_pct": delta_pct,
            },
            "by_mode": [dict(r) for r in by_mode],
            "total_tokens": int(current["total_tokens"]) if current else 0,
            "total_incidents": int(current["session_count"]) if current else 0,
        }
    except Exception:
        logger.debug("Failed to compute cost stats", exc_info=True)
        return {
            "avg_tokens_per_incident": 0,
            "trend": {"current": 0, "previous": 0, "delta_pct": 0.0},
            "by_mode": [],
            "total_tokens": 0,
            "total_incidents": 0,
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_analytics_rest.py::TestCostAnalytics -v 2>&1 | tail -10`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/api/analytics_rest.py tests/test_analytics_rest.py
git commit -m "feat: add cost analytics endpoint (tokens per incident)"
```

---

### Task 6: Intelligence analytics endpoint (Toolbox)

**Files:**
- Modify: `sre_agent/api/analytics_rest.py`
- Modify: `sre_agent/intelligence.py`
- Modify: `tests/test_analytics_rest.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_analytics_rest.py`:

```python
class TestIntelligenceAnalytics:
    def test_returns_all_sections(self, api_client, api_headers):
        with patch("sre_agent.api.analytics_rest._get_intelligence_sections") as mock:
            mock.return_value = {
                "query_reliability": {
                    "preferred": [{"query": "up{}", "success_rate": 0.95, "total": 40}],
                    "unreliable": [{"query": "container_memory_rss{}", "success_rate": 0.22, "total": 18}],
                },
                "error_hotspots": [
                    {"tool": "describe_pod", "error_rate": 0.12, "total": 50, "common_error": "timeout"}
                ],
                "token_efficiency": {"avg_input": 4200, "avg_output": 1800, "cache_hit_rate": 0.89},
                "harness_effectiveness": {"accuracy": 0.73, "wasted": [{"tool": "get_operator_status", "offered": 45, "used": 2}]},
                "routing_accuracy": {"mode_switch_rate": 0.08, "total_sessions": 120},
                "feedback_analysis": {"negative": [{"tool": "scale_deployment", "count": 3}]},
                "token_trending": {"input_delta_pct": 12.0, "output_delta_pct": -5.0, "cache_delta_pct": 3.0},
                "dashboard_patterns": {"top_components": [{"kind": "metric_card", "count": 34}], "avg_widgets": 5.2},
            }
            r = api_client.get("/api/agent/analytics/intelligence", headers=api_headers)

        assert r.status_code == 200
        data = r.json()
        assert "query_reliability" in data
        assert "error_hotspots" in data
        assert "harness_effectiveness" in data
        assert data["token_efficiency"]["cache_hit_rate"] == 0.89
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_analytics_rest.py::TestIntelligenceAnalytics -v 2>&1 | tail -10`
Expected: FAIL

- [ ] **Step 3: Add structured intelligence sections to intelligence.py**

Add to `sre_agent/intelligence.py` (new function alongside existing `get_intelligence_context`):

```python
def get_intelligence_sections(mode: str = "sre", days: int = 7) -> dict:
    """Return intelligence analytics as structured dicts (for REST API).

    Unlike get_intelligence_context() which returns markdown for system prompt,
    this returns machine-readable dicts for the Toolbox Analytics UI.
    """
    result = {}
    excluded = _get_excluded_sections()

    try:
        if "intelligence_query_reliability" not in excluded:
            result["query_reliability"] = _compute_query_reliability_structured(days)
        if "intelligence_error_hotspots" not in excluded:
            result["error_hotspots"] = _compute_error_hotspots_structured(days)
        if "intelligence_token_efficiency" not in excluded:
            result["token_efficiency"] = _compute_token_efficiency_structured(days)
        if "intelligence_harness_effectiveness" not in excluded:
            result["harness_effectiveness"] = _compute_harness_effectiveness_structured(days)
        if "intelligence_routing_accuracy" not in excluded:
            result["routing_accuracy"] = _compute_routing_accuracy_structured(days)
        if "intelligence_feedback_analysis" not in excluded:
            result["feedback_analysis"] = _compute_feedback_analysis_structured(days)
        if "intelligence_token_trending" not in excluded:
            result["token_trending"] = _compute_token_trending_structured(days)
        if "intelligence_dashboard_patterns" not in excluded:
            result["dashboard_patterns"] = _compute_dashboard_patterns_structured(days)
    except Exception:
        logger.debug("Failed to compute intelligence sections", exc_info=True)

    return result


def _compute_query_reliability_structured(days: int) -> dict:
    """Structured version of _compute_query_reliability."""
    try:
        from .db import get_database

        db = get_database()
        rows = db.fetchall(
            "SELECT query_template, success_count, failure_count "
            "FROM promql_queries "
            "WHERE (last_success > NOW() - INTERVAL '1 day' * %s "
            "   OR last_failure > NOW() - INTERVAL '1 day' * %s) "
            "AND success_count + failure_count >= 3 "
            "ORDER BY success_count + failure_count DESC LIMIT 20",
            (days, days),
        )
        preferred = []
        unreliable = []
        for row in rows or []:
            total = row["success_count"] + row["failure_count"]
            rate = row["success_count"] / total if total > 0 else 0
            entry = {"query": row["query_template"][:100], "success_rate": round(rate, 3), "total": total}
            if rate > 0.8 and len(preferred) < 10:
                preferred.append(entry)
            elif rate < 0.3 and len(unreliable) < 5:
                unreliable.append(entry)
        return {"preferred": preferred, "unreliable": unreliable}
    except Exception:
        return {"preferred": [], "unreliable": []}


def _compute_error_hotspots_structured(days: int) -> list:
    """Structured version of _compute_error_hotspots."""
    try:
        from .db import get_database

        db = get_database()
        rows = db.fetchall(
            "SELECT tool_name, "
            "  COUNT(*) FILTER (WHERE status = 'error') AS error_count, "
            "  COUNT(*) AS total_count "
            "FROM tool_usage "
            "WHERE timestamp > NOW() - INTERVAL '1 day' * %s "
            "GROUP BY tool_name "
            "HAVING COUNT(*) > 5 "
            "  AND COUNT(*) FILTER (WHERE status = 'error')::float / COUNT(*) > 0.05 "
            "ORDER BY COUNT(*) FILTER (WHERE status = 'error')::float / COUNT(*) DESC "
            "LIMIT 5",
            (days,),
        )
        result = []
        for row in rows or []:
            total = row["total_count"]
            errors = row["error_count"]
            result.append({
                "tool": row["tool_name"],
                "error_rate": round(errors / total, 3) if total > 0 else 0,
                "total": total,
                "common_error": "",
            })
        return result
    except Exception:
        return []


def _compute_token_efficiency_structured(days: int) -> dict:
    """Structured version of _compute_token_efficiency."""
    try:
        from .db import get_database

        db = get_database()
        row = db.fetchone(
            "SELECT COALESCE(ROUND(AVG(input_tokens)), 0) AS avg_input, "
            "  COALESCE(ROUND(AVG(output_tokens)), 0) AS avg_output, "
            "  COALESCE(AVG(CASE WHEN cache_read_tokens > 0 THEN 1.0 ELSE 0.0 END), 0) AS cache_rate "
            "FROM tool_turns "
            "WHERE timestamp > NOW() - INTERVAL '1 day' * %s AND input_tokens IS NOT NULL",
            (days,),
        )
        return {
            "avg_input": int(row["avg_input"]) if row else 0,
            "avg_output": int(row["avg_output"]) if row else 0,
            "cache_hit_rate": round(float(row["cache_rate"]), 3) if row else 0.0,
        }
    except Exception:
        return {"avg_input": 0, "avg_output": 0, "cache_hit_rate": 0.0}


def _compute_harness_effectiveness_structured(days: int) -> dict:
    """Structured version of _compute_harness_effectiveness."""
    try:
        from .db import get_database

        db = get_database()
        rows = db.fetchall(
            "SELECT tools_offered, tools_called FROM tool_turns "
            "WHERE timestamp > NOW() - INTERVAL '1 day' * %s "
            "AND tools_offered IS NOT NULL AND tools_called IS NOT NULL",
            (days,),
        )
        if not rows:
            return {"accuracy": 0.0, "wasted": []}

        offered_counts: dict[str, int] = {}
        called_counts: dict[str, int] = {}
        total_offered = 0
        total_called = 0
        for row in rows:
            offered = row["tools_offered"] or []
            called = row["tools_called"] or []
            for t in offered:
                offered_counts[t] = offered_counts.get(t, 0) + 1
                total_offered += 1
            for t in called:
                called_counts[t] = called_counts.get(t, 0) + 1
                total_called += 1

        accuracy = total_called / total_offered if total_offered > 0 else 0.0

        wasted = []
        for tool, offered in sorted(offered_counts.items(), key=lambda x: -x[1]):
            used = called_counts.get(tool, 0)
            if offered >= 20 and (used / offered) < 0.02:
                wasted.append({"tool": tool, "offered": offered, "used": used})

        return {"accuracy": round(accuracy, 3), "wasted": wasted[:10]}
    except Exception:
        return {"accuracy": 0.0, "wasted": []}


def _compute_routing_accuracy_structured(days: int) -> dict:
    """Structured version of _compute_routing_accuracy."""
    try:
        from .db import get_database

        db = get_database()
        total_row = db.fetchone(
            "SELECT COUNT(DISTINCT session_id) AS cnt FROM tool_turns "
            "WHERE timestamp > NOW() - INTERVAL '1 day' * %s",
            (days,),
        )
        switch_row = db.fetchone(
            "SELECT COUNT(DISTINCT session_id) AS cnt FROM ("
            "  SELECT session_id FROM tool_turns "
            "  WHERE timestamp > NOW() - INTERVAL '1 day' * %s "
            "  GROUP BY session_id "
            "  HAVING COUNT(DISTINCT agent_mode) > 1"
            ") sub",
            (days,),
        )
        total = total_row["cnt"] if total_row else 0
        switches = switch_row["cnt"] if switch_row else 0
        return {
            "mode_switch_rate": round(switches / total, 3) if total > 0 else 0.0,
            "total_sessions": total,
        }
    except Exception:
        return {"mode_switch_rate": 0.0, "total_sessions": 0}


def _compute_feedback_analysis_structured(days: int) -> dict:
    """Structured version of _compute_feedback_analysis."""
    try:
        from .db import get_database

        db = get_database()
        rows = db.fetchall(
            "SELECT u.tool_name, COUNT(*) AS cnt "
            "FROM tool_usage u "
            "JOIN tool_turns t ON u.session_id = t.session_id AND u.turn_number = t.turn_number "
            "WHERE t.feedback IS NOT NULL AND t.feedback != '' "
            "  AND t.timestamp > NOW() - INTERVAL '1 day' * %s "
            "GROUP BY u.tool_name "
            "ORDER BY cnt DESC LIMIT 10",
            (days,),
        )
        return {"negative": [{"tool": r["tool_name"], "count": r["cnt"]} for r in rows or []]}
    except Exception:
        return {"negative": []}


def _compute_token_trending_structured(days: int) -> dict:
    """Structured version of _compute_token_trending."""
    try:
        from .db import get_database

        db = get_database()
        half = days // 2 or 1

        cur = db.fetchone(
            "SELECT COALESCE(ROUND(AVG(input_tokens)), 0) AS avg_input, "
            "  COALESCE(ROUND(AVG(output_tokens)), 0) AS avg_output, "
            "  COALESCE(AVG(CASE WHEN cache_read_tokens > 0 THEN 1.0 ELSE 0.0 END), 0) AS cache_rate "
            "FROM tool_turns WHERE timestamp > NOW() - INTERVAL '1 day' * %s AND input_tokens IS NOT NULL",
            (days,),
        )
        prev = db.fetchone(
            "SELECT COALESCE(ROUND(AVG(input_tokens)), 0) AS avg_input, "
            "  COALESCE(ROUND(AVG(output_tokens)), 0) AS avg_output, "
            "  COALESCE(AVG(CASE WHEN cache_read_tokens > 0 THEN 1.0 ELSE 0.0 END), 0) AS cache_rate "
            "FROM tool_turns "
            "WHERE timestamp > NOW() - INTERVAL '1 day' * %s "
            "  AND timestamp <= NOW() - INTERVAL '1 day' * %s "
            "  AND input_tokens IS NOT NULL",
            (days + half, days),
        )

        def delta(cur_val: float, prev_val: float) -> float:
            return round((cur_val - prev_val) / prev_val * 100, 1) if prev_val > 0 else 0.0

        ci = float(cur["avg_input"]) if cur else 0
        pi = float(prev["avg_input"]) if prev else 0
        co = float(cur["avg_output"]) if cur else 0
        po = float(prev["avg_output"]) if prev else 0
        cc = float(cur["cache_rate"]) if cur else 0
        pc = float(prev["cache_rate"]) if prev else 0

        return {
            "input_delta_pct": delta(ci, pi),
            "output_delta_pct": delta(co, po),
            "cache_delta_pct": delta(cc, pc),
        }
    except Exception:
        return {"input_delta_pct": 0.0, "output_delta_pct": 0.0, "cache_delta_pct": 0.0}


def _compute_dashboard_patterns_structured(days: int) -> dict:
    """Structured version of _compute_dashboard_patterns."""
    try:
        from .db import get_database

        db = get_database()
        # Most used tools in view_designer mode
        rows = db.fetchall(
            "SELECT tool_name AS kind, COUNT(*) AS count "
            "FROM tool_usage "
            "WHERE agent_mode = 'view_designer' "
            "  AND timestamp > NOW() - INTERVAL '1 day' * %s "
            "GROUP BY tool_name ORDER BY count DESC LIMIT 10",
            (days,),
        )
        # Avg tools per session
        avg_row = db.fetchone(
            "SELECT COALESCE(ROUND(AVG(tool_count), 1), 0) AS avg_widgets "
            "FROM (SELECT session_id, COUNT(*) AS tool_count "
            "      FROM tool_usage WHERE agent_mode = 'view_designer' "
            "        AND timestamp > NOW() - INTERVAL '1 day' * %s "
            "      GROUP BY session_id) sub",
            (days,),
        )
        return {
            "top_components": [dict(r) for r in rows or []],
            "avg_widgets": float(avg_row["avg_widgets"]) if avg_row else 0.0,
        }
    except Exception:
        return {"top_components": [], "avg_widgets": 0.0}
```

- [ ] **Step 4: Wire intelligence endpoint in analytics_rest.py**

Add to `sre_agent/api/analytics_rest.py`:

```python
@router.get("/intelligence")
async def analytics_intelligence(
    days: int = Query(7, ge=1, le=90),
    mode: str = Query("sre"),
    _auth=Depends(verify_token),
):
    """All 8 intelligence loop sections as structured data for Toolbox Analytics."""
    return _get_intelligence_sections(mode=mode, days=days)


def _get_intelligence_sections(mode: str = "sre", days: int = 7) -> dict:
    from ..intelligence import get_intelligence_sections

    return get_intelligence_sections(mode=mode, days=days)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_analytics_rest.py::TestIntelligenceAnalytics -v 2>&1 | tail -10`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add sre_agent/api/analytics_rest.py sre_agent/intelligence.py tests/test_analytics_rest.py
git commit -m "feat: add intelligence analytics endpoint with 8 structured sections"
```

---

### Task 7: Prompt analytics endpoint (Toolbox)

**Files:**
- Modify: `sre_agent/api/analytics_rest.py`
- Modify: `tests/test_analytics_rest.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_analytics_rest.py`:

```python
class TestPromptAnalytics:
    def test_returns_prompt_stats(self, api_client, api_headers):
        with patch("sre_agent.api.analytics_rest._get_prompt_analytics") as mock:
            mock.return_value = {
                "stats": {
                    "total_prompts": 200,
                    "avg_tokens": 14200,
                    "cache_hit_rate": 0.89,
                    "section_avg": {"base_prompt": 4200, "tools": 6100},
                    "by_skill": [{"skill_name": "sre", "count": 150, "avg_tokens": 13800}],
                },
                "versions": [
                    {"prompt_hash": "abc123", "count": 45, "first_seen": "2026-04-01T00:00:00", "last_seen": "2026-04-12T00:00:00"}
                ],
            }
            r = api_client.get("/api/agent/analytics/prompt", headers=api_headers)

        assert r.status_code == 200
        data = r.json()
        assert data["stats"]["total_prompts"] == 200
        assert data["stats"]["cache_hit_rate"] == 0.89
        assert len(data["versions"]) == 1

    def test_with_skill_filter(self, api_client, api_headers):
        with patch("sre_agent.api.analytics_rest._get_prompt_analytics") as mock:
            mock.return_value = {"stats": {"total_prompts": 50}, "versions": []}
            r = api_client.get("/api/agent/analytics/prompt?skill=sre", headers=api_headers)

        assert r.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_analytics_rest.py::TestPromptAnalytics -v 2>&1 | tail -10`
Expected: FAIL

- [ ] **Step 3: Implement prompt analytics endpoint**

Add to `sre_agent/api/analytics_rest.py`:

```python
@router.get("/prompt")
async def analytics_prompt(
    days: int = Query(30, ge=1, le=365),
    skill: str | None = Query(None),
    _auth=Depends(verify_token),
):
    """Prompt section breakdown and version history for Toolbox Analytics."""
    return _get_prompt_analytics(days=days, skill=skill)


def _get_prompt_analytics(days: int = 30, skill: str | None = None) -> dict:
    try:
        from ..prompt_log import get_prompt_stats, get_prompt_versions

        stats = get_prompt_stats(days=days)
        versions = []
        if skill:
            versions = get_prompt_versions(skill, days=days)
        elif stats.get("skill_names"):
            # Get versions for the most-used skill
            versions = get_prompt_versions(stats["skill_names"][0], days=days)

        return {"stats": stats, "versions": versions}
    except Exception:
        logger.debug("Failed to get prompt analytics", exc_info=True)
        return {
            "stats": {"total_prompts": 0, "avg_tokens": 0, "cache_hit_rate": 0.0, "by_skill": [], "section_avg": {}},
            "versions": [],
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_analytics_rest.py::TestPromptAnalytics -v 2>&1 | tail -10`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/api/analytics_rest.py tests/test_analytics_rest.py
git commit -m "feat: add prompt analytics endpoint (section breakdown, version drift)"
```

---

### Task 8: Recommendations endpoint

**Files:**
- Modify: `sre_agent/api/analytics_rest.py`
- Modify: `tests/test_analytics_rest.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_analytics_rest.py`:

```python
class TestRecommendations:
    def test_returns_recommendations(self, api_client, api_headers):
        with patch("sre_agent.api.analytics_rest._compute_recommendations") as mock:
            mock.return_value = {
                "recommendations": [
                    {
                        "type": "scanner",
                        "title": "Enable storage exhaustion scanner",
                        "description": "You have 8 StatefulSets with PVCs. Enable the storage exhaustion scanner to catch capacity issues early.",
                        "action": {"kind": "enable_scanner", "scanner": "scan_storage_exhaustion"},
                    },
                    {
                        "type": "capability",
                        "title": "Try Git PR proposals",
                        "description": "You've asked about deployment rollbacks 3 times. The agent can propose Git PRs for rollback.",
                        "action": {"kind": "chat_prompt", "prompt": "propose a rollback PR for deployment X"},
                    },
                ],
            }
            r = api_client.get("/api/agent/recommendations", headers=api_headers)

        assert r.status_code == 200
        data = r.json()
        assert len(data["recommendations"]) == 2
        assert data["recommendations"][0]["type"] == "scanner"
        assert data["recommendations"][1]["action"]["kind"] == "chat_prompt"

    def test_empty_recommendations(self, api_client, api_headers):
        with patch("sre_agent.api.analytics_rest._compute_recommendations") as mock:
            mock.return_value = {"recommendations": []}
            r = api_client.get("/api/agent/recommendations", headers=api_headers)

        assert r.status_code == 200
        assert r.json()["recommendations"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_analytics_rest.py::TestRecommendations -v 2>&1 | tail -10`
Expected: FAIL

- [ ] **Step 3: Implement recommendations endpoint**

Note: The `/recommendations` endpoint uses the analytics prefix but conceptually serves Mission Control. Register it on the analytics router with the full path.

Add to `sre_agent/api/analytics_rest.py`:

```python
# Mount this on a separate path since it's not under /analytics/
_recommendations_router = APIRouter(prefix="/api/agent", tags=["analytics"])


@_recommendations_router.get("/recommendations")
async def get_recommendations(
    _auth=Depends(verify_token),
):
    """Contextual capability recommendations for Mission Control."""
    return _compute_recommendations()


def _compute_recommendations() -> dict:
    """Generate contextual recommendations based on cluster profile and usage."""
    recommendations = []

    # Type 1: Unused scanners relevant to cluster workloads
    try:
        from ..monitor.scanners import SCANNER_REGISTRY

        all_scanners = list(SCANNER_REGISTRY.keys()) if hasattr(SCANNER_REGISTRY, "keys") else []
        disabled = [s for s in all_scanners if not SCANNER_REGISTRY.get(s, {}).get("enabled", True)]

        for scanner in disabled[:2]:
            clean_name = scanner.replace("scan_", "").replace("_", " ")
            recommendations.append({
                "type": "scanner",
                "title": f"Enable {clean_name} scanner",
                "description": f"The {clean_name} scanner is available but not enabled. Enable it to detect {clean_name} issues.",
                "action": {"kind": "enable_scanner", "scanner": scanner},
            })
    except Exception:
        pass

    # Type 2: Capabilities based on tool usage patterns
    try:
        from .. import db

        database = db.get_database()

        # Find query patterns that suggest unused capabilities
        frequent_topics = database.fetchall(
            "SELECT query_summary, COUNT(*) AS cnt "
            "FROM tool_turns "
            "WHERE timestamp > NOW() - INTERVAL '7 days' "
            "  AND query_summary IS NOT NULL "
            "GROUP BY query_summary "
            "HAVING COUNT(*) >= 3 "
            "ORDER BY cnt DESC LIMIT 5"
        )

        # Check if git tools are unused
        git_usage = database.fetchone(
            "SELECT COUNT(*) AS cnt FROM tool_usage "
            "WHERE tool_name LIKE '%git%' AND timestamp > NOW() - INTERVAL '30 days'"
        )
        if git_usage and git_usage["cnt"] == 0:
            recommendations.append({
                "type": "capability",
                "title": "Try Git PR proposals",
                "description": "The agent can propose Git PRs for changes. Try asking 'propose a PR to fix this deployment'.",
                "action": {"kind": "chat_prompt", "prompt": "propose a PR to fix deployment X"},
            })

        # Check if predict tools are unused
        predict_usage = database.fetchone(
            "SELECT COUNT(*) AS cnt FROM tool_usage "
            "WHERE tool_name LIKE '%predict%' AND timestamp > NOW() - INTERVAL '30 days'"
        )
        if predict_usage and predict_usage["cnt"] == 0:
            recommendations.append({
                "type": "capability",
                "title": "Try predictive analytics",
                "description": "The agent can predict capacity issues and resource exhaustion before they happen.",
                "action": {"kind": "chat_prompt", "prompt": "predict resource usage for namespace prod"},
            })
    except Exception:
        pass

    return {"recommendations": recommendations[:4]}
```

Register the recommendations router in `sre_agent/api/app.py`:

```python
from .analytics_rest import router as analytics_router, _recommendations_router
# ...
app.include_router(analytics_router)
app.include_router(_recommendations_router)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_analytics_rest.py::TestRecommendations -v 2>&1 | tail -10`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/api/analytics_rest.py sre_agent/api/app.py tests/test_analytics_rest.py
git commit -m "feat: add contextual recommendations endpoint"
```

---

### Task 9: Readiness summary endpoint

**Files:**
- Modify: `sre_agent/api/analytics_rest.py`
- Modify: `tests/test_analytics_rest.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_analytics_rest.py`:

```python
class TestReadinessSummary:
    def test_returns_summary(self, api_client, api_headers):
        with patch("sre_agent.api.analytics_rest._get_readiness_summary") as mock:
            mock.return_value = {
                "total_gates": 30,
                "passed": 28,
                "failed": 1,
                "attention": 1,
                "pass_rate": 0.933,
                "attention_items": [
                    {"gate": "cert_expiry", "message": "Certificate expiring in 12 days"},
                ],
            }
            r = api_client.get("/api/agent/analytics/readiness", headers=api_headers)

        assert r.status_code == 200
        data = r.json()
        assert data["passed"] == 28
        assert len(data["attention_items"]) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_analytics_rest.py::TestReadinessSummary -v 2>&1 | tail -10`
Expected: FAIL

- [ ] **Step 3: Implement readiness summary endpoint**

Add to `sre_agent/api/analytics_rest.py`:

```python
@router.get("/readiness")
async def analytics_readiness(
    _auth=Depends(verify_token),
):
    """Lightweight readiness gate summary for Mission Control outcomes card."""
    return _get_readiness_summary()


def _get_readiness_summary() -> dict:
    """Compute readiness gate pass/fail counts."""
    try:
        # Import readiness evaluation if available
        from ..readiness import evaluate_gates

        gates = evaluate_gates()
        passed = sum(1 for g in gates if g.get("status") == "pass")
        failed = sum(1 for g in gates if g.get("status") == "fail")
        attention = sum(1 for g in gates if g.get("status") == "attention")
        total = len(gates)

        attention_items = [
            {"gate": g.get("id", "unknown"), "message": g.get("message", "")}
            for g in gates
            if g.get("status") in ("fail", "attention")
        ]

        return {
            "total_gates": total,
            "passed": passed,
            "failed": failed,
            "attention": attention,
            "pass_rate": round(passed / total, 3) if total > 0 else 0.0,
            "attention_items": attention_items[:5],
        }
    except ImportError:
        return {
            "total_gates": 0,
            "passed": 0,
            "failed": 0,
            "attention": 0,
            "pass_rate": 0.0,
            "attention_items": [],
        }
    except Exception:
        logger.debug("Failed to get readiness summary", exc_info=True)
        return {
            "total_gates": 0,
            "passed": 0,
            "failed": 0,
            "attention": 0,
            "pass_rate": 0.0,
            "attention_items": [],
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_analytics_rest.py::TestReadinessSummary -v 2>&1 | tail -10`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/api/analytics_rest.py tests/test_analytics_rest.py
git commit -m "feat: add readiness summary endpoint for Mission Control"
```

---

### Task 10: Skill activity WebSocket event

**Files:**
- Modify: `sre_agent/api/ws_monitor.py` or the WebSocket handler that manages `/ws/monitor`
- Modify: `tests/test_analytics_rest.py`

- [ ] **Step 1: Identify the WebSocket monitor handler**

Read the file that handles `/ws/monitor` connections. This is typically in `sre_agent/api/ws_monitor.py` or registered in `app.py`. Look for the WebSocket handler function and understand how events are emitted.

- [ ] **Step 2: Write failing test for skill_activity event emission**

Add to `tests/test_analytics_rest.py`:

```python
class TestSkillActivityEvent:
    def test_skill_activity_message_format(self):
        """Verify skill_activity event has the expected structure."""
        from sre_agent.api.ws_monitor import build_skill_activity_event

        event = build_skill_activity_event(
            skill_name="sre",
            status="active",
            handoff_from=None,
            handoff_to=None,
        )
        assert event["type"] == "skill_activity"
        assert event["data"]["skill_name"] == "sre"
        assert event["data"]["status"] == "active"

    def test_skill_handoff_event(self):
        from sre_agent.api.ws_monitor import build_skill_activity_event

        event = build_skill_activity_event(
            skill_name="security",
            status="active",
            handoff_from="sre",
            handoff_to="security",
        )
        assert event["data"]["handoff_from"] == "sre"
        assert event["data"]["handoff_to"] == "security"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/test_analytics_rest.py::TestSkillActivityEvent -v 2>&1 | tail -10`
Expected: FAIL — `build_skill_activity_event` not found

- [ ] **Step 4: Implement skill activity event builder**

Add to the WebSocket monitor module (e.g., `sre_agent/api/ws_monitor.py`):

```python
def build_skill_activity_event(
    skill_name: str,
    status: str,
    handoff_from: str | None = None,
    handoff_to: str | None = None,
) -> dict:
    """Build a skill_activity WebSocket event for Pulse real-time indicators."""
    import time

    data: dict = {
        "skill_name": skill_name,
        "status": status,
        "timestamp": int(time.time() * 1000),
    }
    if handoff_from:
        data["handoff_from"] = handoff_from
    if handoff_to:
        data["handoff_to"] = handoff_to
    return {"type": "skill_activity", "data": data}
```

Then wire it into the existing WebSocket handler where skill transitions happen — emit this event when skill_loader routes to a new skill or when a handoff occurs.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_analytics_rest.py::TestSkillActivityEvent -v 2>&1 | tail -10`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add sre_agent/api/ws_monitor.py tests/test_analytics_rest.py
git commit -m "feat: add skill_activity WebSocket event for Pulse real-time indicators"
```

---

### Task 11: Integration test — all endpoints return valid responses

**Files:**
- Modify: `tests/test_analytics_rest.py`

- [ ] **Step 1: Write integration smoke test**

Add to `tests/test_analytics_rest.py`:

```python
class TestAnalyticsIntegration:
    """Smoke tests verifying all analytics endpoints return 200 with valid structure."""

    @pytest.mark.parametrize(
        "path,required_keys",
        [
            ("/fix-history/summary", ["total_actions", "success_rate", "by_category", "trend"]),
            ("/monitor/coverage", ["active_scanners", "total_scanners", "coverage_pct", "categories"]),
            ("/api/agent/analytics/confidence", ["brier_score", "accuracy_pct", "rating", "buckets"]),
            ("/api/agent/analytics/accuracy", ["avg_quality_score", "anti_patterns", "learning", "override_rate"]),
            ("/api/agent/analytics/cost", ["avg_tokens_per_incident", "trend", "by_mode"]),
            ("/api/agent/analytics/intelligence", []),
            ("/api/agent/analytics/prompt", ["stats", "versions"]),
            ("/api/agent/recommendations", ["recommendations"]),
            ("/api/agent/analytics/readiness", ["total_gates", "passed", "pass_rate"]),
        ],
    )
    def test_endpoint_returns_200(self, api_client, api_headers, path, required_keys):
        r = api_client.get(path, headers=api_headers)
        assert r.status_code == 200, f"{path} returned {r.status_code}: {r.text}"
        data = r.json()
        for key in required_keys:
            assert key in data, f"{path} missing key '{key}' in response"

    @pytest.mark.parametrize(
        "path",
        [
            "/fix-history/summary",
            "/monitor/coverage",
            "/api/agent/analytics/confidence",
            "/api/agent/analytics/accuracy",
            "/api/agent/analytics/cost",
            "/api/agent/analytics/intelligence",
            "/api/agent/analytics/prompt",
            "/api/agent/recommendations",
            "/api/agent/analytics/readiness",
        ],
    )
    def test_all_endpoints_require_auth(self, api_client, path):
        r = api_client.get(path)
        assert r.status_code == 401, f"{path} did not require auth"
```

- [ ] **Step 2: Run the integration tests**

Run: `python3 -m pytest tests/test_analytics_rest.py::TestAnalyticsIntegration -v 2>&1 | tail -30`
Expected: PASS (all endpoints return 200 with required keys, all require auth)

- [ ] **Step 3: Run full test suite to check for regressions**

Run: `python3 -m pytest tests/ -v 2>&1 | tail -20`
Expected: All existing tests still pass, new analytics tests pass

- [ ] **Step 4: Commit**

```bash
git add tests/test_analytics_rest.py
git commit -m "test: add integration smoke tests for all analytics endpoints"
```

---

### Task 12: Update documentation

**Files:**
- Modify: `API_CONTRACT.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add analytics endpoints to API_CONTRACT.md**

Add a new section to `API_CONTRACT.md`:

```markdown
### Analytics Endpoints (Mission Control + Toolbox)

#### `GET /fix-history/summary`
Aggregated fix history stats. Query: `days` (1-90, default 7).
Returns: `total_actions`, `completed`, `failed`, `rolled_back`, `success_rate`, `rollback_rate`, `avg_resolution_ms`, `by_category[]`, `trend{current_week, previous_week, delta}`.

#### `GET /monitor/coverage`
Scanner coverage stats. Query: `days` (1-90, default 7).
Returns: `active_scanners`, `total_scanners`, `coverage_pct`, `categories[]`, `per_scanner[]`.

#### `GET /api/agent/analytics/confidence`
Confidence calibration (Brier score). Query: `days` (1-365, default 30).
Returns: `brier_score`, `accuracy_pct`, `rating`, `total_predictions`, `buckets[]`.

#### `GET /api/agent/analytics/accuracy`
Agent accuracy stats. Query: `days` (1-365, default 30).
Returns: `avg_quality_score`, `quality_trend{}`, `dimensions{}`, `anti_patterns[]`, `learning{}`, `override_rate{}`.

#### `GET /api/agent/analytics/cost`
Token cost per incident. Query: `days` (1-365, default 30).
Returns: `avg_tokens_per_incident`, `trend{}`, `by_mode[]`, `total_tokens`, `total_incidents`.

#### `GET /api/agent/analytics/intelligence`
Intelligence loop sections (Toolbox). Query: `days` (1-90, default 7), `mode` (default "sre").
Returns: `query_reliability{}`, `error_hotspots[]`, `token_efficiency{}`, `harness_effectiveness{}`, `routing_accuracy{}`, `feedback_analysis{}`, `token_trending{}`, `dashboard_patterns{}`.

#### `GET /api/agent/analytics/prompt`
Prompt stats and version drift (Toolbox). Query: `days` (1-365, default 30), `skill` (optional).
Returns: `stats{}`, `versions[]`.

#### `GET /api/agent/recommendations`
Contextual capability recommendations. No query params.
Returns: `recommendations[]{type, title, description, action{kind, ...}}`.

#### `GET /api/agent/analytics/readiness`
Readiness gate summary. No query params.
Returns: `total_gates`, `passed`, `failed`, `attention`, `pass_rate`, `attention_items[]`.

#### WebSocket: `skill_activity` event on `/ws/monitor`
Emitted when active skill changes or handoff occurs.
Data: `skill_name`, `status`, `timestamp`, `handoff_from?`, `handoff_to?`.
```

- [ ] **Step 2: Update CLAUDE.md Key Files section**

Add `analytics_rest.py` to the Key Files list:

```markdown
- `api/analytics_rest.py` — analytics REST endpoints for Mission Control (confidence, accuracy, cost, recommendations, readiness) and Toolbox (intelligence sections, prompt stats)
```

- [ ] **Step 3: Commit**

```bash
git add API_CONTRACT.md CLAUDE.md
git commit -m "docs: document analytics endpoints in API_CONTRACT.md and CLAUDE.md"
```
