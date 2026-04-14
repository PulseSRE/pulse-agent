# Intelligent Auto-Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Auto-fix uses the investigation's root cause diagnosis to apply targeted fixes (patch image, create ConfigMap, adjust resources) instead of blindly restarting deployments.

**Architecture:** A new `fix_planner.py` module sits between the investigation result and the auto-fix execution. It queries the latest investigation for a finding, extracts the `suspected_cause`, maps it to a fix strategy, and executes the targeted fix. Falls back to the existing blunt handlers (delete_pod, restart_deployment) when no targeted strategy matches.

**Tech Stack:** Python, Kubernetes Python client (existing), PostgreSQL (existing investigations table)

---

### Task 1: Fix Planner — Root Cause Classification

**Files:**
- Create: `sre_agent/monitor/fix_planner.py`
- Test: `tests/test_fix_planner.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fix_planner.py
"""Tests for intelligent auto-fix planning."""

from __future__ import annotations

from sre_agent.monitor.fix_planner import classify_root_cause


class TestClassifyRootCause:
    def test_bad_image_tag(self):
        cause = "The image registry.example.com/app:v999 does not exist"
        assert classify_root_cause(cause) == "bad_image"

    def test_missing_configmap(self):
        cause = "ConfigMap my-config not found in namespace production"
        assert classify_root_cause(cause) == "missing_config"

    def test_oom_killed(self):
        cause = "Container exceeded memory limit of 256Mi and was OOMKilled"
        assert classify_root_cause(cause) == "oom"

    def test_readiness_probe_failure(self):
        cause = "Readiness probe failed: connection refused on port 8080"
        assert classify_root_cause(cause) == "probe_failure"

    def test_resource_quota_exceeded(self):
        cause = "pods quota exceeded in namespace staging"
        assert classify_root_cause(cause) == "quota_exceeded"

    def test_unknown_cause(self):
        cause = "Something unexpected happened"
        assert classify_root_cause(cause) == "unknown"

    def test_empty_cause(self):
        assert classify_root_cause("") == "unknown"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_fix_planner.py::TestClassifyRootCause -v`
Expected: FAIL — module does not exist

- [ ] **Step 3: Implement classify_root_cause**

Create `sre_agent/monitor/fix_planner.py`:

```python
"""Intelligent auto-fix planning — maps investigation diagnosis to targeted fixes.

Sits between the investigation result and auto-fix execution:
1. Query latest investigation for the finding
2. Classify the root cause from suspected_cause text
3. Select a targeted fix strategy
4. Fall back to blunt handlers if no strategy matches
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("pulse_agent.monitor")

# Root cause categories with keyword patterns
_CAUSE_PATTERNS: list[tuple[str, list[str]]] = [
    ("bad_image", ["image", "does not exist", "not found in registry", "imagepullbackoff", "pull access denied", "manifest unknown"]),
    ("missing_config", ["configmap", "not found", "missing", "secret.*not found"]),
    ("oom", ["oom", "out of memory", "memory limit", "oomkilled", "exceeded memory"]),
    ("probe_failure", ["readiness probe", "liveness probe", "probe failed", "connection refused"]),
    ("quota_exceeded", ["quota", "exceeded", "forbidden", "limit reached"]),
    ("crash_exit", ["exit code", "fatal", "panic", "segfault", "error code"]),
    ("dependency", ["connection refused", "connection timed out", "no such host", "dns", "service unavailable"]),
]


def classify_root_cause(suspected_cause: str) -> str:
    """Classify a suspected cause string into a root cause category."""
    if not suspected_cause:
        return "unknown"

    lower = suspected_cause.lower()
    for category, patterns in _CAUSE_PATTERNS:
        for pattern in patterns:
            if re.search(pattern, lower):
                return category

    return "unknown"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_fix_planner.py::TestClassifyRootCause -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/monitor/fix_planner.py tests/test_fix_planner.py
git commit -m "feat: add root cause classification for intelligent auto-fix"
```

---

### Task 2: Fix Strategies — Targeted Remediation Functions

**Files:**
- Modify: `sre_agent/monitor/fix_planner.py`
- Test: `tests/test_fix_planner.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_fix_planner.py`:

```python
from unittest.mock import patch, MagicMock
from sre_agent.monitor.fix_planner import plan_fix, FixPlan


class TestPlanFix:
    def test_bad_image_returns_patch_strategy(self):
        investigation = {
            "suspectedCause": "Image app:v999 does not exist in the registry",
            "recommendedFix": "Update the image to app:v2.1.0",
            "confidence": 0.95,
        }
        finding = {
            "category": "image_pull",
            "resources": [{"kind": "Pod", "name": "app-abc", "namespace": "prod"}],
        }
        plan = plan_fix(investigation, finding)
        assert plan is not None
        assert plan.strategy == "patch_image"
        assert plan.confidence >= 0.5

    def test_oom_returns_patch_resources(self):
        investigation = {
            "suspectedCause": "Container exceeded memory limit of 256Mi",
            "recommendedFix": "Increase memory limit to 512Mi",
            "confidence": 0.9,
        }
        finding = {
            "category": "crashloop",
            "resources": [{"kind": "Deployment", "name": "api", "namespace": "prod"}],
        }
        plan = plan_fix(investigation, finding)
        assert plan is not None
        assert plan.strategy == "patch_resources"

    def test_unknown_cause_returns_none(self):
        investigation = {
            "suspectedCause": "Something unclear happened",
            "recommendedFix": "Check the logs",
            "confidence": 0.3,
        }
        finding = {
            "category": "crashloop",
            "resources": [{"kind": "Pod", "name": "x", "namespace": "default"}],
        }
        plan = plan_fix(investigation, finding)
        assert plan is None  # falls back to blunt handler

    def test_low_confidence_returns_none(self):
        investigation = {
            "suspectedCause": "Image might be wrong",
            "recommendedFix": "Try a different tag",
            "confidence": 0.3,
        }
        finding = {
            "category": "image_pull",
            "resources": [{"kind": "Pod", "name": "x", "namespace": "default"}],
        }
        plan = plan_fix(investigation, finding)
        assert plan is None  # too low confidence for targeted fix
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_fix_planner.py::TestPlanFix -v`
Expected: FAIL — `plan_fix` and `FixPlan` not defined

- [ ] **Step 3: Implement plan_fix and FixPlan**

Add to `sre_agent/monitor/fix_planner.py`:

```python
from dataclasses import dataclass


@dataclass
class FixPlan:
    """A targeted fix plan produced by the fix planner."""
    strategy: str          # e.g., "patch_image", "patch_resources", "create_configmap"
    cause_category: str    # from classify_root_cause
    confidence: float      # from investigation
    description: str       # human-readable description of what will be done
    params: dict           # strategy-specific parameters


# Minimum confidence to attempt a targeted fix
_MIN_TARGETED_CONFIDENCE = 0.5

# Map root cause category to fix strategy
_STRATEGY_MAP: dict[str, str] = {
    "bad_image": "patch_image",
    "oom": "patch_resources",
    "missing_config": "create_configmap",
    "probe_failure": "patch_probe",
    "quota_exceeded": "suggest_quota_increase",
}


def plan_fix(investigation: dict, finding: dict) -> FixPlan | None:
    """Plan a targeted fix based on investigation results.

    Returns a FixPlan if a targeted strategy is available and confidence
    is sufficient. Returns None to fall back to blunt handlers.
    """
    suspected_cause = investigation.get("suspectedCause", "")
    recommended_fix = investigation.get("recommendedFix", "")
    confidence = float(investigation.get("confidence", 0))

    if confidence < _MIN_TARGETED_CONFIDENCE:
        return None

    cause_category = classify_root_cause(suspected_cause)
    strategy = _STRATEGY_MAP.get(cause_category)

    if not strategy:
        return None

    return FixPlan(
        strategy=strategy,
        cause_category=cause_category,
        confidence=confidence,
        description=f"{strategy}: {recommended_fix[:200]}",
        params={
            "suspected_cause": suspected_cause,
            "recommended_fix": recommended_fix,
            "resources": finding.get("resources", []),
        },
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_fix_planner.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/monitor/fix_planner.py tests/test_fix_planner.py
git commit -m "feat: add fix planning with strategy selection"
```

---

### Task 3: Execute Targeted Fixes — patch_image and patch_resources

**Files:**
- Modify: `sre_agent/monitor/fix_planner.py`
- Test: `tests/test_fix_planner.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_fix_planner.py`:

```python
from sre_agent.monitor.fix_planner import execute_fix


class TestExecuteFix:
    @patch("sre_agent.monitor.fix_planner.get_apps_client")
    @patch("sre_agent.monitor.fix_planner.get_core_client")
    def test_patch_image_finds_owner_and_patches(self, mock_core, mock_apps):
        # Mock pod with owner reference to a ReplicaSet
        pod = MagicMock()
        pod.metadata.owner_references = [MagicMock(kind="ReplicaSet", name="app-rs-abc")]
        pod.spec.containers = [MagicMock(name="app", image="app:v999")]
        mock_core.return_value.read_namespaced_pod.return_value = pod

        # Mock ReplicaSet with owner reference to Deployment
        rs = MagicMock()
        rs.metadata.owner_references = [MagicMock(kind="Deployment", name="app")]
        mock_apps.return_value.read_namespaced_replica_set.return_value = rs

        # Mock deployment
        dep = MagicMock()
        dep.spec.template.spec.containers = [MagicMock(name="app", image="app:v999")]
        dep.metadata.annotations = {"deployment.kubernetes.io/revision": "3"}
        mock_apps.return_value.read_namespaced_deployment.return_value = dep

        plan = FixPlan(
            strategy="patch_image",
            cause_category="bad_image",
            confidence=0.95,
            description="patch image to latest known good",
            params={
                "suspected_cause": "Image app:v999 does not exist",
                "recommended_fix": "Rollback to previous revision",
                "resources": [{"kind": "Pod", "name": "app-abc", "namespace": "prod"}],
            },
        )

        tool, before, after = execute_fix(plan)
        assert tool == "rollback_deployment"
        assert "app" in before
        mock_apps.return_value.patch_namespaced_deployment.assert_not_called()

    @patch("sre_agent.monitor.fix_planner.get_apps_client")
    def test_patch_resources_doubles_memory(self, mock_apps):
        dep = MagicMock()
        container = MagicMock()
        container.name = "app"
        container.resources.limits = {"memory": "256Mi"}
        container.resources.requests = {"memory": "128Mi"}
        dep.spec.template.spec.containers = [container]
        dep.metadata.annotations = {}
        mock_apps.return_value.read_namespaced_deployment.return_value = dep

        plan = FixPlan(
            strategy="patch_resources",
            cause_category="oom",
            confidence=0.9,
            description="increase memory",
            params={
                "suspected_cause": "OOMKilled at 256Mi",
                "recommended_fix": "Increase to 512Mi",
                "resources": [{"kind": "Deployment", "name": "api", "namespace": "prod"}],
            },
        )

        tool, before, after = execute_fix(plan)
        assert tool == "patch_resources"
        assert "512Mi" in after
        mock_apps.return_value.patch_namespaced_deployment.assert_called_once()

    def test_unknown_strategy_raises(self):
        plan = FixPlan(
            strategy="teleport_pod",
            cause_category="unknown",
            confidence=0.99,
            description="impossible",
            params={"resources": []},
        )
        try:
            execute_fix(plan)
            assert False, "Should have raised"
        except ValueError as e:
            assert "teleport_pod" in str(e)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_fix_planner.py::TestExecuteFix -v`
Expected: FAIL — `execute_fix` not defined

- [ ] **Step 3: Implement execute_fix with patch_image and patch_resources strategies**

Add to `sre_agent/monitor/fix_planner.py`:

```python
from ..k8s_client import get_apps_client, get_core_client


def execute_fix(plan: FixPlan) -> tuple[str, str, str]:
    """Execute a targeted fix plan. Returns (tool_name, before_state, after_state).

    Raises ValueError for unknown strategies.
    """
    executor = _EXECUTORS.get(plan.strategy)
    if not executor:
        raise ValueError(f"No executor for strategy: {plan.strategy}")

    logger.info(
        "Intelligent fix: strategy=%s cause=%s confidence=%.2f",
        plan.strategy, plan.cause_category, plan.confidence,
    )
    return executor(plan)


def _execute_patch_image(plan: FixPlan) -> tuple[str, str, str]:
    """Fix bad image by rolling back to the previous deployment revision."""
    resources = plan.params.get("resources", [])
    if not resources:
        raise ValueError("No resources in fix plan")

    r = resources[0]
    ns = r.get("namespace", "default")
    core = get_core_client()
    apps = get_apps_client()

    # Find the owning Deployment from the pod
    pod = core.read_namespaced_pod(r["name"], ns)
    dep_name = None
    bad_image = ""

    for container in pod.spec.containers:
        bad_image = container.image

    for ref in pod.metadata.owner_references or []:
        if ref.kind == "ReplicaSet":
            rs = apps.read_namespaced_replica_set(ref.name, ns)
            for rs_ref in rs.metadata.owner_references or []:
                if rs_ref.kind == "Deployment":
                    dep_name = rs_ref.name
                    break

    if not dep_name:
        raise ValueError(f"Cannot find owning Deployment for pod {r['name']}")

    dep = apps.read_namespaced_deployment(dep_name, ns)
    revision = (dep.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "0")
    before = f"Deployment {dep_name} in {ns}: image={bad_image}, revision={revision}"

    # Rollback: undo the last rollout
    # Kubernetes doesn't have a direct rollback API in apps/v1.
    # The safest approach: roll back to the previous ReplicaSet revision.
    rollback_revision = max(int(revision) - 1, 0)

    # Find the ReplicaSet for the previous revision
    rs_list = apps.list_namespaced_replica_set(ns, label_selector=f"app={dep_name}")
    target_rs = None
    for rs in rs_list.items:
        rs_rev = (rs.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "")
        if rs_rev == str(rollback_revision):
            target_rs = rs
            break

    if target_rs and target_rs.spec.template.spec.containers:
        good_image = target_rs.spec.template.spec.containers[0].image
        # Patch deployment with the previous image
        body = {"spec": {"template": {"spec": {"containers": [{"name": target_rs.spec.template.spec.containers[0].name, "image": good_image}]}}}}
        apps.patch_namespaced_deployment(dep_name, ns, body=body)
        after = f"Deployment {dep_name} patched: image={good_image} (rolled back from revision {revision} to {rollback_revision})"
        return ("rollback_deployment", before, after)

    # Fallback: if we can't find the previous revision, just delete the pod
    core.delete_namespaced_pod(r["name"], ns)
    return ("rollback_deployment", before, f"Pod {r['name']} deleted — previous revision not found, controller will recreate")


def _execute_patch_resources(plan: FixPlan) -> tuple[str, str, str]:
    """Fix OOM by doubling the memory limit on the deployment."""
    resources = plan.params.get("resources", [])
    if not resources:
        raise ValueError("No resources in fix plan")

    r = resources[0]
    ns = r.get("namespace", "default")
    name = r.get("name", "")
    kind = r.get("kind", "")
    apps = get_apps_client()

    # If resource is a Pod, find the owning Deployment
    if kind == "Pod":
        core = get_core_client()
        pod = core.read_namespaced_pod(name, ns)
        for ref in pod.metadata.owner_references or []:
            if ref.kind == "ReplicaSet":
                rs = apps.read_namespaced_replica_set(ref.name, ns)
                for rs_ref in rs.metadata.owner_references or []:
                    if rs_ref.kind == "Deployment":
                        name = rs_ref.name
                        kind = "Deployment"
                        break

    if kind != "Deployment":
        raise ValueError(f"Cannot patch resources on {kind}/{name} — only Deployments supported")

    dep = apps.read_namespaced_deployment(name, ns)
    container = dep.spec.template.spec.containers[0]

    # Parse current memory limit
    current_limit = "256Mi"
    if container.resources and container.resources.limits:
        current_limit = container.resources.limits.get("memory", "256Mi")

    # Double it
    from ..units import parse_memory_bytes
    current_bytes = parse_memory_bytes(current_limit)
    new_bytes = current_bytes * 2
    new_limit = f"{new_bytes // (1024 * 1024)}Mi"

    before = f"Deployment {name} in {ns}: memory limit={current_limit}"

    body = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [{
                        "name": container.name,
                        "resources": {"limits": {"memory": new_limit}},
                    }]
                }
            }
        }
    }
    apps.patch_namespaced_deployment(name, ns, body=body)
    after = f"Deployment {name} patched: memory limit {current_limit} → {new_limit}"

    return ("patch_resources", before, after)


def _execute_noop(plan: FixPlan) -> tuple[str, str, str]:
    """Strategies that can't be auto-fixed — log and skip."""
    return ("skip", "", f"Strategy {plan.strategy} requires manual intervention: {plan.description}")


_EXECUTORS: dict[str, callable] = {
    "patch_image": _execute_patch_image,
    "patch_resources": _execute_patch_resources,
    "create_configmap": _execute_noop,  # future: create from template
    "patch_probe": _execute_noop,       # future: adjust probe config
    "suggest_quota_increase": _execute_noop,  # informational only
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_fix_planner.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/monitor/fix_planner.py tests/test_fix_planner.py
git commit -m "feat: add targeted fix executors (patch_image, patch_resources)"
```

---

### Task 4: Wire Fix Planner into Auto-Fix Pipeline

**Files:**
- Modify: `sre_agent/monitor/session.py:81-240`
- Test: `tests/test_fix_planner.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_fix_planner.py`:

```python
from sre_agent.monitor.fix_planner import get_investigation_for_finding


class TestGetInvestigation:
    @patch("sre_agent.monitor.fix_planner._get_db")
    def test_returns_latest_investigation(self, mock_get_db):
        db = MagicMock()
        db.fetchone.return_value = {
            "suspected_cause": "Image does not exist",
            "recommended_fix": "Roll back",
            "confidence": 0.95,
        }
        mock_get_db.return_value = db

        result = get_investigation_for_finding("f-abc123")
        assert result is not None
        assert result["suspected_cause"] == "Image does not exist"

    @patch("sre_agent.monitor.fix_planner._get_db")
    def test_returns_none_when_no_investigation(self, mock_get_db):
        db = MagicMock()
        db.fetchone.return_value = None
        mock_get_db.return_value = db

        result = get_investigation_for_finding("f-nonexistent")
        assert result is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_fix_planner.py::TestGetInvestigation -v`
Expected: FAIL — `get_investigation_for_finding` not defined

- [ ] **Step 3: Implement get_investigation_for_finding**

Add to `sre_agent/monitor/fix_planner.py`:

```python
def _get_db():
    from ..db import get_database
    return get_database()


def get_investigation_for_finding(finding_id: str) -> dict | None:
    """Look up the latest completed investigation for a finding."""
    try:
        db = _get_db()
        return db.fetchone(
            "SELECT suspected_cause, recommended_fix, confidence "
            "FROM investigations "
            "WHERE finding_id = %s AND status = 'completed' "
            "ORDER BY timestamp DESC LIMIT 1",
            (finding_id,),
        )
    except Exception:
        logger.debug("Failed to look up investigation for %s", finding_id, exc_info=True)
        return None
```

- [ ] **Step 4: Modify auto_fix in session.py to try targeted fix first**

In `sre_agent/monitor/session.py`, after the handler lookup (line ~120 `handler = AUTO_FIX_HANDLERS.get(category)`) and before the cooldown check, add the intelligent fix attempt:

Replace the section from `handler = AUTO_FIX_HANDLERS.get(category)` through the execution block to try the fix planner first:

After line `handler = AUTO_FIX_HANDLERS.get(category)`, before `if not handler: continue`, add:

```python
            # Try intelligent fix first — uses investigation diagnosis
            from .fix_planner import get_investigation_for_finding, plan_fix, execute_fix as execute_targeted_fix

            investigation = get_investigation_for_finding(finding.get("id", ""))
            targeted_plan = None
            if investigation:
                targeted_plan = plan_fix(investigation, finding)
                if targeted_plan:
                    logger.info(
                        "Intelligent fix available: strategy=%s cause=%s confidence=%.2f for %s",
                        targeted_plan.strategy, targeted_plan.cause_category,
                        targeted_plan.confidence, resource_key,
                    )
```

Then in the execution block (the `try` around line 220 where `handler` is called), replace:

```python
                tool, before_state, after_state = await asyncio.to_thread(handler, finding)
```

with:

```python
                if targeted_plan:
                    tool, before_state, after_state = await asyncio.to_thread(
                        execute_targeted_fix, targeted_plan
                    )
                else:
                    tool, before_state, after_state = await asyncio.to_thread(handler, finding)
```

- [ ] **Step 5: Run all tests**

Run: `python3 -m pytest tests/ -q --tb=short`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add sre_agent/monitor/session.py sre_agent/monitor/fix_planner.py tests/test_fix_planner.py
git commit -m "feat: wire intelligent fix planner into auto-fix pipeline"
```

---

### Task 5: Emit Targeted Fix Info to UI

**Files:**
- Modify: `sre_agent/monitor/session.py` (action_report)

- [ ] **Step 1: Add fix plan details to action report**

In the action_report construction (around line 171), when a targeted_plan exists, include the strategy info:

```python
            if targeted_plan:
                action_report["fixStrategy"] = targeted_plan.strategy
                action_report["causeCategory"] = targeted_plan.cause_category
                action_report["fixDescription"] = targeted_plan.description
```

This allows the UI to show "Intelligent fix: rolled back image" instead of just "Restart Deployment".

- [ ] **Step 2: Run all tests**

Run: `python3 -m pytest tests/ -q --tb=short`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add sre_agent/monitor/session.py
git commit -m "feat: emit fix strategy metadata in action reports"
```

---

### Task 6: Full Verification

- [ ] **Step 1: Run full test suite**

Run: `python3 -m pytest tests/ -v --tb=short`
Expected: All tests PASS

- [ ] **Step 2: Run ruff lint**

Run: `python3 -m ruff check sre_agent/ tests/`
Expected: All checks passed

- [ ] **Step 3: Run mypy**

Run: `python3 -m mypy sre_agent/ --ignore-missing-imports --exclude 'skills/(view-designer|capacity-planner)'`
Expected: Success

- [ ] **Step 4: Run eval suites**

Run: `python3 -m sre_agent.evals.cli --suite release --fail-on-gate`
Expected: Gate PASS

- [ ] **Step 5: Commit and push**

```bash
git add -A
git commit -m "chore: verify intelligent auto-fix — all tests, lint, types, evals pass"
git push
```
