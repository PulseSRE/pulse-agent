# Autonomous Remediation & Proactive Intelligence — Design Spec

**Date:** 2026-04-17
**Status:** Approved
**Priority:** High
**Owner:** Ali

## Problem Statement

The SRE agent scores 72% on chaos tests — detection is perfect but auto-fix doesn't execute reliably. Fixes are blunt (delete pod, restart deployment), there's no outcome verification, and no learning from past fixes. The agent also lacks proactive intelligence — it reacts to failures instead of predicting them.

## Goals

1. **Reliable auto-fix** — chaos test score ≥ 85%
2. **Targeted fixes** — root-cause-specific remediation instead of blunt restarts
3. **Outcome learning** — agent gets smarter from past fix results
4. **Proactive detection** — predict failures before they happen
5. **Earned trust** — agent demonstrates reliability to earn higher autonomy

---

## Initiative 1: Fix the Fix Pipeline

**Impact:** Chaos 72% → 85%+ | **Effort:** Small (2-3 hours)

### Current Issues
- Fix planner waits for investigation results (20s timeout) before acting
- Fast-path fallback exists but doesn't bypass investigation check cleanly
- 5-minute cooldown prevents first fix attempt
- No verification that the fix actually worked

### Changes

**`sre_agent/monitor/session.py`:**
1. Fast-path should execute immediately for known categories without waiting for investigation
2. Remove cooldown for first fix attempt on a resource (keep it for retries)
3. Add fix verification: after fix execution, wait 30s, re-scan the specific resource, emit `fix_verified` or `fix_failed` event
4. Emit `fix_trace` WebSocket event with full decision tree

**`sre_agent/monitor/fix_planner.py`:**
1. `plan_fix()` should try fast-path FIRST, then investigation-backed if fast-path confidence is low
2. Return confidence score with every plan

### Verification
```bash
make chaos-test  # Target: ≥85% (375/440)
```

---

## Initiative 2: Smarter Fix Strategies

**Impact:** Better fix quality | **Effort:** Medium (1 day)

### Root Cause → Fix Mapping

| Root Cause | Current Fix | Better Fix |
|-----------|-------------|------------|
| CrashLoop (config error) | Delete pod | Check ConfigMap/Secret, rollback deployment |
| CrashLoop (code bug) | Delete pod | Rollback to previous revision |
| OOM | Delete pod | Patch memory limit (2x), add VPA recommendation |
| ImagePullBackOff | Restart | Rollback deployment to last good revision |
| Failed deployment | Restart | Check rollout history, rollback if available |
| Pending pod | None | Check resource quotas, node capacity, PVC binding |

### New File: `sre_agent/monitor/fix_strategies.py`

```python
class FixStrategy:
    name: str           # e.g., "rollback_deployment"
    category: str       # e.g., "crashloop"
    confidence: float   # 0-1
    pre_checks: list    # conditions that must be true
    actions: list       # K8s operations to perform
    verification: str   # how to check if fix worked

STRATEGIES = {
    "crashloop": [
        FixStrategy("rollback_deployment", confidence=0.8, 
                    pre_checks=["has_previous_revision", "not_first_deployment"]),
        FixStrategy("restart_pod", confidence=0.5,
                    pre_checks=["has_owner_reference"]),
    ],
    "oom": [
        FixStrategy("increase_memory_limit", confidence=0.7,
                    pre_checks=["is_deployment_or_statefulset"]),
        FixStrategy("restart_pod", confidence=0.4,
                    pre_checks=["has_owner_reference"]),
    ],
    "image_pull": [
        FixStrategy("rollback_deployment", confidence=0.9,
                    pre_checks=["has_previous_revision"]),
    ],
}
```

### Pre-Fix Analysis
Before executing any fix:
1. Check deployment rollout history — is there a previous good revision?
2. Check recent changes — what changed in the last 10 minutes?
3. Check if this is transient (first occurrence) vs persistent (3+ occurrences)

---

## Initiative 3: Outcome Learning

**Impact:** Long-term quality improvement | **Effort:** Medium (1 day)

### Database Schema

```sql
CREATE TABLE fix_outcomes (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    category TEXT NOT NULL,          -- crashloop, oom, image_pull
    strategy TEXT NOT NULL,          -- rollback_deployment, restart_pod
    resource_key TEXT NOT NULL,      -- Deployment/ns/name
    namespace TEXT,
    result TEXT NOT NULL,            -- fixed, failed, regressed, timed_out
    duration_seconds INTEGER,
    finding_id TEXT,
    confidence REAL,
    notes TEXT
);
```

### Learning Logic

```python
def get_best_strategy(category: str) -> FixStrategy:
    """Pick the strategy with the highest success rate for this category."""
    outcomes = db.fetchall(
        "SELECT strategy, COUNT(*) as total, "
        "SUM(CASE WHEN result='fixed' THEN 1 ELSE 0 END) as successes "
        "FROM fix_outcomes WHERE category=? GROUP BY strategy",
        (category,)
    )
    # Rank by success rate, prefer strategies with more data
    # Fall back to default ordering if no history
```

### Negative Learning
- If a strategy fails 3x consecutively for a category → demote it
- If a strategy causes a regression (finding got worse) → blacklist for that resource
- Weekly digest: "Strategy X failed 5/8 times for OOM — consider reviewing"

---

## Initiative 4: Proactive Anomaly Detection

**Impact:** Prevent incidents | **Effort:** Medium (1 day)

### New Scanner: `scan_predictive_risks`

Runs every 3rd scan cycle. Queries Prometheus for:

```python
PREDICTIVE_QUERIES = {
    "memory_oom_risk": {
        "query": "predict_linear(container_memory_working_set_bytes[1h], 3600)",
        "threshold": "container_spec_memory_limit_bytes",
        "message": "Container {pod} predicted to OOM in {time}",
    },
    "disk_pressure": {
        "query": "predict_linear(kubelet_volume_stats_used_bytes[6h], 86400)",
        "threshold": "kubelet_volume_stats_capacity_bytes * 0.95",
        "message": "PVC {pvc} predicted full in {time}",
    },
    "restart_acceleration": {
        "query": "deriv(kube_pod_container_status_restarts_total[30m])",
        "threshold": 0.01,  # >0.01 restarts/sec = accelerating
        "message": "Pod {pod} restart rate accelerating",
    },
    "cpu_saturation": {
        "query": "avg_over_time(rate(container_cpu_usage_seconds_total[5m])[30m:5m])",
        "threshold": 0.9,  # 90% sustained
        "message": "Container {pod} CPU saturated at {value}%",
    },
}
```

### Finding Format
```json
{
    "type": "finding",
    "severity": "warning",
    "category": "predictive",
    "title": "OOM Risk: api-server predicted to exceed memory limit",
    "summary": "Memory usage trending toward limit. Predicted OOM in 45 minutes based on 1h trend.",
    "autoFixable": true,
    "predictedTimeToFailure": "45m",
    "recommendation": "Increase memory limit or investigate memory leak"
}
```

---

## Initiative 5: Confidence-Driven Autonomy

**Impact:** Earned trust | **Effort:** Large (2-3 days)

### Trust Score Engine

```python
class TrustEngine:
    """Per-category trust scoring based on fix outcomes."""
    
    def get_trust_score(self, category: str) -> float:
        """0.0 = no trust, 1.0 = full trust"""
        outcomes = get_recent_outcomes(category, days=30)
        if len(outcomes) < 5:
            return 0.0  # Not enough data
        success_rate = successes / total
        recency_weight = decay_by_age(outcomes)
        return success_rate * recency_weight
    
    def should_auto_fix(self, category: str, trust_level: int) -> bool:
        score = self.get_trust_score(category)
        if trust_level >= 4: return True
        if trust_level == 3: return score >= 0.7
        if trust_level == 2: return False  # always ask
        return False
    
    def get_recommendations(self) -> list:
        """Suggest trust level changes based on track record."""
        # e.g., "Crashloop: 94% success over 16 fixes → recommend trust 3"
```

### UI: Trust Dashboard
- Per-category reliability scores
- Fix history timeline
- Recommendations for trust level changes
- "Promote" / "Demote" buttons per category

---

## Implementation Order

| Week | Initiative | Deliverable |
|------|-----------|-------------|
| 1 | Fix the fix pipeline | Chaos ≥85%, fix verification loop |
| 1 | Smarter strategies | Root-cause fixes, pre-fix analysis |
| 2 | Outcome learning | fix_outcomes table, strategy ranking |
| 2 | Proactive detection | predict_linear scanner, trend alerts |
| 3 | Trust engine | Per-category scoring, UI dashboard |

## Success Metrics

| Metric | Current | Target |
|--------|---------|--------|
| Chaos test score | 72% | ≥90% |
| Auto-fix success rate | Unknown | ≥80% |
| Mean time to remediation | N/A | <5 minutes |
| Proactive warnings | 0 | ≥3 per day (on active cluster) |
| False positive rate | N/A | <10% |
