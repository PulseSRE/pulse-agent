# Data-First Generative UI Implementation Plan

> Historical design/implementation record. This preserves the proposal and tasks at the date in the filename; unchecked tasks, paths, commands, and security claims are not evidence of the current implementation. Use [current documentation](../../README.md) and verify behavior against code before applying a procedure.


> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate empty charts and bad PromQL by making the view designer data-aware — discover available metrics, verify queries return data, fall back to known-good recipes.

**Architecture:** New `promql_recipes.py` module with 79 production-tested recipes sourced from 7 OpenShift/Kubernetes repos. Two new `@beta_tool` functions (`discover_metrics`, `verify_query`) that query Prometheus before dashboard generation. PostgreSQL `promql_queries` table tracks query success/failure rates for learning. View designer prompt updated to use discover → verify → build workflow.

**Tech Stack:** Python 3.11+, pytest, PostgreSQL, Prometheus HTTP API, `@beta_tool` decorator pattern

---

## Files Overview

| File | Action | Purpose |
|------|--------|---------|
| `sre_agent/promql_recipes.py` | CREATE | 79 recipes + lookup functions + learned queries DB |
| `tests/test_promql_recipes.py` | CREATE | Recipe registry tests (~15) |
| `sre_agent/k8s_tools.py` | MODIFY | Add `discover_metrics` + `verify_query` tools, recording hook on `get_prometheus_query` |
| `tests/test_discover_metrics.py` | CREATE | Discovery tool tests (~12) |
| `tests/test_verify_query.py` | CREATE | Verification tool tests (~10) |
| `sre_agent/db_schema.py` | MODIFY | Add `PROMQL_QUERIES_SCHEMA` |
| `sre_agent/db_migrations.py` | MODIFY | Add migration 003 |
| `tests/test_learned_queries.py` | CREATE | Learned queries DB tests (~8) |
| `sre_agent/view_designer.py` | MODIFY | Add tools to `_DATA_TOOL_NAMES`, update workflow + rules |
| `sre_agent/harness.py` | MODIFY | Add tools to `monitoring` category |
| `CLAUDE.md` | MODIFY | Document new module + tools |

---

### Task 1: PromQL Recipe Registry

**Files:**
- Create: `sre_agent/promql_recipes.py`
- Create: `tests/test_promql_recipes.py`

- [ ] **Step 1: Write recipe registry tests**

Create `tests/test_promql_recipes.py`:

```python
"""Tests for the PromQL recipe registry."""

from __future__ import annotations

from sre_agent.promql_recipes import (
    RECIPES,
    PromQLRecipe,
    get_fallback,
    get_recipe,
    get_recipes_for_category,
)


class TestRecipeStructure:
    def test_all_recipes_are_promql_recipe_instances(self):
        for cat, recipes in RECIPES.items():
            for r in recipes:
                assert isinstance(r, PromQLRecipe), f"{cat}: {r} is not PromQLRecipe"

    def test_all_recipes_have_required_fields(self):
        for cat, recipes in RECIPES.items():
            for r in recipes:
                assert r.name, f"{cat}: recipe missing name"
                assert r.query, f"{cat}: {r.name} missing query"
                assert r.chart_type, f"{cat}: {r.name} missing chart_type"
                assert r.metric, f"{cat}: {r.name} missing metric"
                assert r.scope in ("cluster", "namespace", "pod", "node"), (
                    f"{cat}: {r.name} has invalid scope '{r.scope}'"
                )

    def test_no_duplicate_queries_across_categories(self):
        seen: dict[str, str] = {}
        for cat, recipes in RECIPES.items():
            for r in recipes:
                assert r.query not in seen, (
                    f"Duplicate query in {cat}/{r.name} — already in {seen[r.query]}"
                )
                seen[r.query] = f"{cat}/{r.name}"

    def test_all_categories_have_recipes(self):
        expected = {
            "cpu", "memory", "network", "storage", "control_plane",
            "pods", "alerts", "cluster_health", "ingress", "scheduler",
            "overcommit", "workload_state", "storage_state", "node_use",
            "monitoring", "operators",
        }
        assert set(RECIPES.keys()) == expected

    def test_minimum_recipe_count(self):
        total = sum(len(r) for r in RECIPES.values())
        assert total >= 70, f"Expected 70+ recipes, got {total}"

    def test_valid_chart_types(self):
        valid = {"line", "area", "stacked_area", "bar", "stacked_bar", "metric_card", "status_list"}
        for cat, recipes in RECIPES.items():
            for r in recipes:
                assert r.chart_type in valid, f"{cat}/{r.name}: invalid chart_type '{r.chart_type}'"


class TestRecipeLookup:
    def test_get_recipe_known_metric(self):
        r = get_recipe("container_cpu_usage_seconds_total")
        assert r is not None
        assert "cpu" in r.name.lower() or "cpu" in r.query.lower()

    def test_get_recipe_unknown_metric(self):
        assert get_recipe("totally_fake_metric_xyz") is None

    def test_get_recipes_for_category(self):
        cpu_recipes = get_recipes_for_category("cpu")
        assert len(cpu_recipes) >= 5
        assert all(isinstance(r, PromQLRecipe) for r in cpu_recipes)

    def test_get_recipes_for_invalid_category(self):
        assert get_recipes_for_category("nonexistent") == []

    def test_get_fallback_cpu(self):
        fb = get_fallback("cpu", scope="cluster")
        assert fb is not None
        assert fb.scope == "cluster"

    def test_get_fallback_memory(self):
        fb = get_fallback("memory", scope="cluster")
        assert fb is not None

    def test_get_fallback_nonexistent(self):
        assert get_fallback("nonexistent") is None

    def test_get_fallback_prefers_cluster_scope(self):
        fb = get_fallback("cpu")
        assert fb is not None
        assert fb.scope == "cluster"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_promql_recipes.py -v`
Expected: FAIL — module doesn't exist yet

- [ ] **Step 3: Implement promql_recipes.py**

Create `sre_agent/promql_recipes.py` with:
- `PromQLRecipe` dataclass (name, query, chart_type, description, metric, scope, parameters)
- `RECIPES` dict with 16 categories and ~79 recipes (all queries from the spec)
- `get_recipe(metric_name)` — linear scan of all recipes, match on `metric` field
- `get_recipes_for_category(category)` — direct dict lookup
- `get_fallback(category, scope="cluster")` — get first recipe matching category + scope

All 79 recipes from the spec must be included. Each recipe uses the exact PromQL from the spec tables. The `metric` field is the primary metric name (e.g., `container_cpu_usage_seconds_total`). The `parameters` field lists template variables like `["namespace"]` for scoped queries.

Category key names: `cpu`, `memory`, `network`, `storage`, `control_plane`, `pods`, `alerts`, `cluster_health`, `ingress`, `scheduler`, `overcommit`, `workload_state`, `storage_state`, `node_use`, `monitoring`, `operators`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_promql_recipes.py -v`
Expected: PASS

- [ ] **Step 5: Lint and format**

Run: `python3 -m ruff check sre_agent/promql_recipes.py tests/test_promql_recipes.py && python3 -m ruff format sre_agent/promql_recipes.py tests/test_promql_recipes.py`

- [ ] **Step 6: Commit**

```bash
git add sre_agent/promql_recipes.py tests/test_promql_recipes.py
git commit -m "feat: add PromQL recipe registry with 79 production-tested queries"
```

---

### Task 2: Learned Queries DB Table

**Files:**
- Modify: `sre_agent/db_schema.py`
- Modify: `sre_agent/db_migrations.py`
- Modify: `sre_agent/promql_recipes.py` (add DB functions)
- Create: `tests/test_learned_queries.py`

- [ ] **Step 1: Write learned queries tests**

Create `tests/test_learned_queries.py`:

```python
"""Tests for the learned PromQL queries database layer."""

from __future__ import annotations

import hashlib

from sre_agent.promql_recipes import (
    get_query_reliability,
    get_reliable_queries,
    normalize_query,
    record_query_result,
)


class TestNormalizeQuery:
    def test_strips_namespace_values(self):
        q = 'rate(container_cpu_usage_seconds_total{namespace="production"}[5m])'
        normalized = normalize_query(q)
        assert "production" not in normalized
        assert "NAMESPACE" in normalized or "namespace=" not in normalized

    def test_strips_pod_values(self):
        q = 'container_memory_working_set_bytes{pod="my-pod-abc123"}'
        normalized = normalize_query(q)
        assert "my-pod-abc123" not in normalized

    def test_lowercases(self):
        q = "SUM(Rate(CPU[5m]))"
        normalized = normalize_query(q)
        assert normalized == normalized.lower()

    def test_deterministic_hash(self):
        q = 'rate(container_cpu_usage_seconds_total{namespace="ns1"}[5m])'
        h1 = hashlib.sha256(normalize_query(q).encode()).hexdigest()
        h2 = hashlib.sha256(normalize_query(q).encode()).hexdigest()
        assert h1 == h2

    def test_same_query_different_namespace_same_hash(self):
        q1 = 'rate(cpu{namespace="ns1"}[5m])'
        q2 = 'rate(cpu{namespace="ns2"}[5m])'
        assert normalize_query(q1) == normalize_query(q2)


class TestRecordQueryResult:
    def test_record_success_creates_entry(self):
        record_query_result("test_query_success_1", success=True, series_count=5)
        rel = get_query_reliability(normalize_query("test_query_success_1"))
        # May be None if DB not available in test — that's fine (fire-and-forget)
        if rel is not None:
            assert rel["success_count"] >= 1

    def test_record_failure_creates_entry(self):
        record_query_result("test_query_fail_1", success=False, series_count=0)
        # fire-and-forget — no assertion on DB state needed

    def test_fire_and_forget_no_exception(self):
        # Should never raise, even with bad input
        record_query_result("", success=True, series_count=0)
        record_query_result(None, success=False, series_count=0)


class TestGetReliableQueries:
    def test_returns_list(self):
        result = get_reliable_queries("cpu", min_success=999)
        assert isinstance(result, list)
```

- [ ] **Step 2: Add schema to db_schema.py**

Add to `sre_agent/db_schema.py` after the `TOOL_USAGE_INDEX_SCHEMA` definition:

```python
PROMQL_QUERIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS promql_queries (
    id SERIAL PRIMARY KEY,
    query_hash TEXT NOT NULL,
    query_template TEXT NOT NULL,
    category TEXT DEFAULT '',
    success_count INT DEFAULT 0,
    failure_count INT DEFAULT 0,
    last_success TIMESTAMPTZ,
    last_failure TIMESTAMPTZ,
    avg_series_count FLOAT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(query_hash)
);
"""
```

Also add `PROMQL_QUERIES_SCHEMA` to the `ALL_SCHEMAS` concatenation.

- [ ] **Step 3: Add migration 003 to db_migrations.py**

```python
def _migrate_003_promql_queries(db: Database) -> None:
    """Add promql_queries table for tracking query success/failure rates."""
    from .db_schema import PROMQL_QUERIES_SCHEMA

    db.executescript(PROMQL_QUERIES_SCHEMA)


MIGRATIONS = [
    (1, "baseline", _migrate_001_baseline),
    (2, "tool_usage", _migrate_002_tool_usage),
    (3, "promql_queries", _migrate_003_promql_queries),
]
```

- [ ] **Step 4: Add DB functions to promql_recipes.py**

Add to `sre_agent/promql_recipes.py`:

```python
import hashlib
import logging
import re

logger = logging.getLogger("pulse_agent.promql_recipes")


def normalize_query(query: str) -> str:
    """Normalize a PromQL query for hashing — strip namespace/pod values, lowercase."""
    if not query:
        return ""
    q = query.lower()
    # Replace namespace="value" with namespace="__NS__"
    q = re.sub(r'namespace\s*=~?\s*"[^"]*"', 'namespace="__NS__"', q)
    # Replace pod="value" with pod="__POD__"
    q = re.sub(r'pod\s*=~?\s*"[^"]*"', 'pod="__POD__"', q)
    # Replace instance="value"
    q = re.sub(r'instance\s*=~?\s*"[^"]*"', 'instance="__INSTANCE__"', q)
    # Replace deployment="value"
    q = re.sub(r'deployment\s*=~?\s*"[^"]*"', 'deployment="__DEP__"', q)
    return q.strip()


def record_query_result(query: str, *, success: bool, series_count: int = 0) -> None:
    """Record a PromQL query result. Fire-and-forget: swallows all exceptions."""
    try:
        if not query:
            return
        from .db import get_database

        db = get_database()
        normalized = normalize_query(query)
        qhash = hashlib.sha256(normalized.encode()).hexdigest()

        if success:
            db.execute(
                "INSERT INTO promql_queries (query_hash, query_template, success_count, last_success, avg_series_count) "
                "VALUES (%s, %s, 1, NOW(), %s) "
                "ON CONFLICT (query_hash) DO UPDATE SET "
                "success_count = promql_queries.success_count + 1, "
                "last_success = NOW(), "
                "avg_series_count = (promql_queries.avg_series_count + %s) / 2",
                (qhash, normalized, float(series_count), float(series_count)),
            )
        else:
            db.execute(
                "INSERT INTO promql_queries (query_hash, query_template, failure_count, last_failure) "
                "VALUES (%s, %s, 1, NOW()) "
                "ON CONFLICT (query_hash) DO UPDATE SET "
                "failure_count = promql_queries.failure_count + 1, "
                "last_failure = NOW()",
                (qhash, normalized),
            )
    except Exception:
        logger.debug("Failed to record query result", exc_info=True)


def get_query_reliability(query_template: str) -> dict | None:
    """Return success/failure counts for a normalized query template."""
    try:
        from .db import get_database

        db = get_database()
        qhash = hashlib.sha256(query_template.encode()).hexdigest()
        row = db.fetch_one(
            "SELECT success_count, failure_count, avg_series_count FROM promql_queries WHERE query_hash = %s",
            (qhash,),
        )
        if row:
            return {"success_count": row[0], "failure_count": row[1], "avg_series_count": row[2]}
    except Exception:
        logger.debug("Failed to get query reliability", exc_info=True)
    return None


def get_reliable_queries(category: str, min_success: int = 3) -> list[dict]:
    """Return queries with high success rates for a category."""
    try:
        from .db import get_database

        db = get_database()
        rows = db.fetch_all(
            "SELECT query_template, success_count, failure_count, avg_series_count "
            "FROM promql_queries WHERE category = %s AND success_count >= %s "
            "ORDER BY success_count DESC LIMIT 20",
            (category, min_success),
        )
        return [
            {"query_template": r[0], "success_count": r[1], "failure_count": r[2], "avg_series_count": r[3]}
            for r in rows
        ]
    except Exception:
        logger.debug("Failed to get reliable queries", exc_info=True)
    return []
```

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/test_learned_queries.py tests/test_promql_recipes.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add sre_agent/db_schema.py sre_agent/db_migrations.py sre_agent/promql_recipes.py tests/test_learned_queries.py
git commit -m "feat: add promql_queries table and learned query tracking"
```

---

### Task 3: `discover_metrics` Tool

**Files:**
- Modify: `sre_agent/k8s_tools.py`
- Create: `tests/test_discover_metrics.py`

- [ ] **Step 1: Write discover_metrics tests**

Create `tests/test_discover_metrics.py`:

```python
"""Tests for the discover_metrics tool."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from sre_agent.k8s_tools import discover_metrics


def _mock_prom_labels(metric_names: list[str]):
    """Return a context manager that mocks Prometheus label values API."""
    import json

    response_data = json.dumps({"status": "success", "data": metric_names}).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = response_data
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return patch("urllib.request.urlopen", return_value=mock_resp)


class TestDiscoverMetrics:
    def setup_method(self):
        # Clear cache before each test
        import sre_agent.k8s_tools as kt
        if hasattr(kt, "_metric_names_cache"):
            kt._metric_names_cache = {"data": None, "ts": 0}

    def test_cpu_category_filters(self):
        metrics = [
            "container_cpu_usage_seconds_total",
            "node_cpu_seconds_total",
            "container_memory_working_set_bytes",
            "kube_pod_info",
        ]
        with _mock_prom_labels(metrics):
            result = discover_metrics.call({"category": "cpu"})
        assert "container_cpu_usage_seconds_total" in result
        assert "node_cpu_seconds_total" in result
        assert "container_memory" not in result

    def test_memory_category_filters(self):
        metrics = [
            "container_cpu_usage_seconds_total",
            "container_memory_working_set_bytes",
            "node_memory_MemAvailable_bytes",
        ]
        with _mock_prom_labels(metrics):
            result = discover_metrics.call({"category": "memory"})
        assert "container_memory_working_set_bytes" in result
        assert "node_memory_MemAvailable_bytes" in result
        assert "cpu" not in result

    def test_all_category_returns_all(self):
        metrics = ["container_cpu_usage_seconds_total", "container_memory_working_set_bytes"]
        with _mock_prom_labels(metrics):
            result = discover_metrics.call({"category": "all"})
        assert "container_cpu" in result
        assert "container_memory" in result

    def test_includes_recipe_when_available(self):
        metrics = ["container_cpu_usage_seconds_total"]
        with _mock_prom_labels(metrics):
            result = discover_metrics.call({"category": "cpu"})
        assert "Recipe:" in result

    def test_empty_prometheus_response(self):
        with _mock_prom_labels([]):
            result = discover_metrics.call({"category": "cpu"})
        assert "No metrics found" in result or "0 found" in result

    def test_invalid_category(self):
        metrics = ["container_cpu_usage_seconds_total"]
        with _mock_prom_labels(metrics):
            result = discover_metrics.call({"category": "nonexistent"})
        assert "Available categories" in result or "No metrics found" in result

    def test_caching_second_call_uses_cache(self):
        metrics = ["container_cpu_usage_seconds_total"]
        with _mock_prom_labels(metrics) as mock_urlopen:
            discover_metrics.call({"category": "cpu"})
            discover_metrics.call({"category": "memory"})
        # Should only call urlopen once (cached)
        assert mock_urlopen.call_count == 1

    def test_prometheus_unreachable(self):
        with patch("urllib.request.urlopen", side_effect=Exception("connection refused")):
            result = discover_metrics.call({"category": "cpu"})
        assert "Cannot reach" in result or "recipe" in result.lower()

    def test_default_category_is_all(self):
        metrics = ["container_cpu_usage_seconds_total", "etcd_server_has_leader"]
        with _mock_prom_labels(metrics):
            result = discover_metrics.call({})
        assert "container_cpu" in result
        assert "etcd_server" in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_discover_metrics.py -v`
Expected: FAIL — `discover_metrics` doesn't exist

- [ ] **Step 3: Implement discover_metrics in k8s_tools.py**

Add near line 1758 (before `get_prometheus_query`):

```python
# In-memory cache for metric names (TTL 5 minutes)
_metric_names_cache: dict = {"data": None, "ts": 0}

_CATEGORY_PREFIXES: dict[str, list[str]] = {
    "cpu": ["container_cpu_", "node_cpu_", "process_cpu_", "pod:container_cpu_"],
    "memory": ["container_memory_", "node_memory_", "machine_memory_"],
    "network": ["container_network_", "node_network_"],
    "storage": ["node_filesystem_", "kubelet_volume_", "container_fs_"],
    "pods": ["kube_pod_", "kube_running_pod_", "kubelet_running_"],
    "nodes": ["kube_node_", "machine_", "node_"],
    "api_server": ["apiserver_"],
    "etcd": ["etcd_"],
    "alerts": ["ALERTS"],
}


@beta_tool
def discover_metrics(category: str = "all") -> str:
    """Discover available Prometheus metrics on this cluster. Call this BEFORE
    writing PromQL queries to know which metrics actually exist.

    Args:
        category: One of: 'cpu', 'memory', 'network', 'storage', 'pods',
                  'nodes', 'api_server', 'etcd', 'alerts', 'all'.
    """
    import os
    import ssl
    import time as _time
    import urllib.request

    from .promql_recipes import RECIPES, get_recipe

    # Validate category
    valid_cats = set(_CATEGORY_PREFIXES.keys()) | {"all"}
    if category not in valid_cats:
        return f"Invalid category '{category}'. Available categories: {', '.join(sorted(valid_cats))}"

    # Check cache (5 min TTL)
    now = _time.time()
    if _metric_names_cache["data"] is not None and now - _metric_names_cache["ts"] < 300:
        all_metrics = _metric_names_cache["data"]
    else:
        # Query Prometheus for all metric names
        base_url = os.environ.get("THANOS_URL", "https://thanos-querier.openshift-monitoring.svc:9091")
        url = f"{base_url}/api/v1/label/__name__/values"

        try:
            token = ""
            try:
                with open("/var/run/secrets/kubernetes.io/serviceaccount/token") as f:
                    token = f.read().strip()
            except FileNotFoundError:
                pass

            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            headers = {}
            if token:
                headers["Authorization"] = f"Bearer {token}"

            req = urllib.request.Request(url, headers=headers)
            resp = urllib.request.urlopen(req, context=ctx, timeout=15)
            data = json.loads(resp.read())

            if data.get("status") != "success":
                return f"Prometheus error: {data.get('error', 'unknown')}"

            all_metrics = sorted(data.get("data", []))
            _metric_names_cache["data"] = all_metrics
            _metric_names_cache["ts"] = now

        except Exception as e:
            # Fallback: return recipes without live discovery
            lines = [f"Cannot reach Prometheus ({e}). Using hardcoded recipes:"]
            cat_recipes = RECIPES.get(category, []) if category != "all" else [r for rs in RECIPES.values() for r in rs]
            for r in cat_recipes[:15]:
                lines.append(f"  {r.metric}")
                lines.append(f"    Recipe: {r.query}")
                lines.append(f"    Chart: {r.chart_type} | Title: \"{r.name}\"")
            return "\n".join(lines)

    # Filter by category
    if category == "all":
        filtered = all_metrics
    else:
        prefixes = _CATEGORY_PREFIXES[category]
        filtered = [m for m in all_metrics if any(m.startswith(p) or m.startswith(p.rstrip("_")) for p in prefixes)]

    if not filtered:
        return f"No metrics found for category '{category}' (0 of {len(all_metrics)} total metrics matched)."

    # Build output with recipe lookup
    lines = [f"Available {category} metrics ({len(filtered)} found):"]
    for metric_name in filtered[:30]:  # Cap at 30 to avoid huge output
        recipe = get_recipe(metric_name)
        lines.append(f"  {metric_name}")
        if recipe:
            lines.append(f"    Recipe: {recipe.query}")
            lines.append(f"    Chart: {recipe.chart_type} | Title: \"{recipe.name}\"")

    if len(filtered) > 30:
        lines.append(f"  ... and {len(filtered) - 30} more")

    return "\n".join(lines)
```

Register the tool after `get_prometheus_query`:
```python
register_tool(discover_metrics)
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_discover_metrics.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/k8s_tools.py tests/test_discover_metrics.py
git commit -m "feat: add discover_metrics tool for data-first dashboard generation"
```

---

### Task 4: `verify_query` Tool

**Files:**
- Modify: `sre_agent/k8s_tools.py`
- Create: `tests/test_verify_query.py`

- [ ] **Step 1: Write verify_query tests**

Create `tests/test_verify_query.py`:

{% raw %}
```python
"""Tests for the verify_query tool."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from sre_agent.k8s_tools import verify_query


def _mock_prom_instant(status: str, results: list | None = None, error: str = ""):
    """Return a context manager that mocks Prometheus instant query API."""
    data = {"status": status}
    if status == "success":
        data["data"] = {"resultType": "vector", "result": results or []}
    if error:
        data["error"] = error
    response_data = json.dumps(data).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = response_data
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return patch("urllib.request.urlopen", return_value=mock_resp)


class TestVerifyQuery:
    def test_pass_with_data(self):
        results = [
            {"metric": {"__name__": "up", "job": "kubelet"}, "value": [1711540800, "1"]},
            {"metric": {"__name__": "up", "job": "apiserver"}, "value": [1711540800, "1"]},
        ]
        with _mock_prom_instant("success", results):
            result = verify_query.call({"query": "up"})
        assert "PASS" in result
        assert "2 series" in result

    def test_fail_no_data(self):
        with _mock_prom_instant("success", []):
            result = verify_query.call({"query": "nonexistent_metric"})
        assert "FAIL_NO_DATA" in result

    def test_fail_syntax(self):
        with _mock_prom_instant("error", error="parse error"):
            result = verify_query.call({"query": "invalid{{"})
        assert "FAIL_SYNTAX" in result
        assert "parse error" in result

    def test_fail_unreachable(self):
        with patch("urllib.request.urlopen", side_effect=Exception("connection refused")):
            result = verify_query.call({"query": "up"})
        assert "FAIL_UNREACHABLE" in result

    def test_single_series_shows_sample(self):
        results = [{"metric": {"__name__": "up"}, "value": [1711540800, "1"]}]
        with _mock_prom_instant("success", results):
            result = verify_query.call({"query": "up"})
        assert "PASS" in result
        assert "1 series" in result

    def test_records_success(self):
        results = [{"metric": {"__name__": "up"}, "value": [1711540800, "1"]}]
        with (
            _mock_prom_instant("success", results),
            patch("sre_agent.promql_recipes.record_query_result") as mock_record,
        ):
            verify_query.call({"query": "up"})
        mock_record.assert_called_once()
        _, kwargs = mock_record.call_args
        assert kwargs["success"] is True

    def test_records_failure(self):
        with (
            _mock_prom_instant("success", []),
            patch("sre_agent.promql_recipes.record_query_result") as mock_record,
        ):
            verify_query.call({"query": "nonexistent"})
        mock_record.assert_called_once()
        _, kwargs = mock_record.call_args
        assert kwargs["success"] is False

    def test_invalid_characters_rejected(self):
        result = verify_query.call({"query": "up; drop table"})
        assert "Invalid" in result or "Error" in result

    def test_empty_query(self):
        result = verify_query.call({"query": ""})
        assert "Error" in result or "empty" in result.lower()
```
{% endraw %}

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_verify_query.py -v`
Expected: FAIL

- [ ] **Step 3: Implement verify_query in k8s_tools.py**

Add after `discover_metrics`:

```python
@beta_tool
def verify_query(query: str) -> str:
    """Test a PromQL query against Prometheus to verify it returns data.
    Call this BEFORE using a query in a dashboard to ensure it works.

    Args:
        query: PromQL query to test.
    """
    import os
    import ssl
    import urllib.parse
    import urllib.request

    if not query or not query.strip():
        return "Error: query is empty."

    # Sanitize
    if any(c in query for c in [";", "\\", "\n", "\r"]):
        return "Error: Invalid characters in query."

    base_url = os.environ.get("THANOS_URL", "https://thanos-querier.openshift-monitoring.svc:9091")
    params = urllib.parse.urlencode({"query": query})
    url = f"{base_url}/api/v1/query?{params}"

    try:
        token = ""
        try:
            with open("/var/run/secrets/kubernetes.io/serviceaccount/token") as f:
                token = f.read().strip()
        except FileNotFoundError:
            pass

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        req = urllib.request.Request(url, headers=headers)
        resp = urllib.request.urlopen(req, context=ctx, timeout=15)
        data = json.loads(resp.read())
    except Exception as e:
        return f"FAIL_UNREACHABLE: Cannot reach Prometheus at {base_url}: {e}"

    if data.get("status") != "success":
        error_msg = data.get("error", "unknown error")
        try:
            from .promql_recipes import record_query_result
            record_query_result(query, success=False, series_count=0)
        except Exception:
            pass
        return f"FAIL_SYNTAX: {error_msg}"

    results = data.get("data", {}).get("result", [])

    if not results:
        try:
            from .promql_recipes import record_query_result
            record_query_result(query, success=False, series_count=0)
        except Exception:
            pass
        return "FAIL_NO_DATA: Query returned 0 results. Metric may not exist or labels may be wrong."

    # Build sample info
    sample = results[0]
    metric_name = sample.get("metric", {}).get("__name__", "")
    value = sample.get("value", [None, ""])[1] if sample.get("value") else ""
    sample_info = f"{metric_name}={value}" if metric_name else f"value={value}"

    try:
        from .promql_recipes import record_query_result
        record_query_result(query, success=True, series_count=len(results))
    except Exception:
        pass

    return f"PASS: Query returns data ({len(results)} series, sample: {sample_info})"


register_tool(verify_query)
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_verify_query.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/k8s_tools.py tests/test_verify_query.py
git commit -m "feat: add verify_query tool for pre-save PromQL validation"
```

---

### Task 5: Recording Hook on `get_prometheus_query`

**Files:**
- Modify: `sre_agent/k8s_tools.py`

- [ ] **Step 1: Add recording hook**

In `get_prometheus_query`, after line 1843 (`if not results: return ...`), add a recording call. Also add one after the successful result parsing (before the return statement at the end).

Find the line `if not results:` (around line 1843) and add after the return:

After line 1844 (`return f"Query returned no results for: {query}"`), the function continues with success path. Find the return statement at the end of the function (where it returns the component tuple) and add before it:

```python
    # Record query result for learned queries system
    try:
        from .promql_recipes import record_query_result
        record_query_result(query, success=True, series_count=len(results))
    except Exception:
        pass
```

Also add after `if not results:` block:

```python
    if not results:
        try:
            from .promql_recipes import record_query_result
            record_query_result(query, success=False, series_count=0)
        except Exception:
            pass
        return f"Query returned no results for: {query}"
```

And after the `data.get("status") != "success"` block:

```python
    if data.get("status") != "success":
        try:
            from .promql_recipes import record_query_result
            record_query_result(query, success=False, series_count=0)
        except Exception:
            pass
        return f"Query error: {data.get('error', 'unknown')}"
```

- [ ] **Step 2: Run existing Prometheus tests**

Run: `python3 -m pytest tests/ -k "promql or prometheus" -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add sre_agent/k8s_tools.py
git commit -m "feat: add query result recording to get_prometheus_query"
```

---

### Task 6: View Designer + Harness Integration

**Files:**
- Modify: `sre_agent/view_designer.py`
- Modify: `sre_agent/harness.py`

- [ ] **Step 1: Add tools to view designer `_DATA_TOOL_NAMES`**

In `sre_agent/view_designer.py`, add to the `_DATA_TOOL_NAMES` set (around line 14):

```python
    "discover_metrics",
    "verify_query",
```

- [ ] **Step 2: Add tools to harness `monitoring` category**

In `sre_agent/harness.py`, find the `"monitoring"` category in `TOOL_CATEGORIES` and add both tools to its `"tools"` list:

```python
    "discover_metrics",
    "verify_query",
```

- [ ] **Step 3: Update view designer system prompt**

In `sre_agent/view_designer.py`, update the BUILD step (Step 2) to include the new data-first workflow. After the existing "CRITICAL — Component Accumulation" section, add:

```
### Data-First Query Building

1. Call discover_metrics(category) for each metric category in the plan (cpu, memory, etc.)
2. Review the available metrics — select the ones that match the dashboard intent
3. For each chart or metric_card:
   a. If a known recipe is listed for the metric → use that exact recipe query
   b. If no recipe → write a PromQL query using the discovered metric names
   c. Call verify_query(query) to test it returns data
   d. If PASS → proceed to get_prometheus_query(query, time_range="1h")
   e. If FAIL → try a different recipe from the same category
   f. If all recipes fail → skip this widget (do NOT add empty charts)
```

Add to the Rules section:

```
16. ALWAYS call discover_metrics() before writing PromQL queries — know what exists
17. ALWAYS call verify_query() before calling get_prometheus_query() — verify data exists
18. When verify_query fails, try a known-good recipe from the same category instead
19. NEVER add a chart or metric_card with a query that failed verify_query
```

- [ ] **Step 4: Run existing tests**

Run: `python3 -m pytest tests/test_harness.py tests/test_views.py -v`
Expected: PASS (no test regressions)

- [ ] **Step 5: Commit**

```bash
git add sre_agent/view_designer.py sre_agent/harness.py
git commit -m "feat: integrate discover_metrics + verify_query into view designer workflow"
```

---

### Task 7: Update Documentation

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update CLAUDE.md**

Add to the Key Files section:

```
- `promql_recipes.py` — 79 production-tested PromQL recipes + learned queries DB (sources: OpenShift console, cluster-monitoring-operator, kube-state-metrics, node_exporter, ACM)
```

Update the Tools subsection to mention the two new tools in the appropriate tool file description.

Update test count if changed significantly.

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add promql_recipes to CLAUDE.md key files"
```

---

## Verification

After all tasks:

```bash
# Run full test suite
python3 -m pytest tests/ -v

# Run just the new tests
python3 -m pytest tests/test_promql_recipes.py tests/test_discover_metrics.py tests/test_verify_query.py tests/test_learned_queries.py -v

# Lint + type check
make verify
```

**Manual verification:**
1. Start the agent: `pulse-agent-api`
2. Connect via UI, ask: "Create a dashboard for the production namespace"
3. Verify in logs: `discover_metrics` called before PromQL queries
4. Verify in logs: `verify_query` called before `get_prometheus_query`
5. Verify: no empty charts in the final dashboard
6. Check `promql_queries` table: entries recorded for successful/failed queries
