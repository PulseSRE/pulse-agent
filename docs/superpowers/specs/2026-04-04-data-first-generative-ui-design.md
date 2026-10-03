# Data-First Generative UI — Design Spec

> Historical design/implementation record. This preserves the proposal and tasks at the date in the filename; unchecked tasks, paths, commands, and security claims are not evidence of the current implementation. Use [current documentation](../../README.md) and verify behavior against code before applying a procedure.


**Goal:** Eliminate empty charts and bad PromQL by making the view designer data-aware. The agent discovers what metrics exist on the cluster, selects relevant ones, verifies queries return data, and falls back to known-good recipes when generated queries fail.

**Problem:** The agent generates PromQL queries blind — it doesn't know what metrics the cluster has. This produces empty charts, invalid queries, and unusable dashboards. The validation layer we just built catches structural issues but can't fix "no data" problems.

**Approach:** Two new agent tools (`discover_metrics`, `verify_query`) + a comprehensive known-good PromQL recipe registry sourced from OpenShift console dashboards + a PostgreSQL table to learn which queries succeed on each cluster over time.

---

## 1. `discover_metrics` Tool

New `@beta_tool` in `sre_agent/k8s_tools.py`.

### Signature

```python
@beta_tool
def discover_metrics(category: str = "all") -> str:
    """Discover available Prometheus metrics on this cluster. Call this BEFORE
    writing PromQL queries to know which metrics actually exist.

    Args:
        category: One of: 'cpu', 'memory', 'network', 'storage', 'pods',
                  'nodes', 'api_server', 'etcd', 'alerts', 'all'.
    """
```

### Behavior

1. Query Prometheus `/api/v1/label/__name__/values` (single API call, returns all metric names)
2. Cache result in-memory for 5 minutes (`_metric_names_cache` with TTL)
3. Filter by category using prefix maps:
   - `cpu`: `container_cpu_*`, `node_cpu_*`, `process_cpu_*`, `pod:container_cpu_*`
   - `memory`: `container_memory_*`, `node_memory_*`, `machine_memory_*`
   - `network`: `container_network_*`, `node_network_*`
   - `storage`: `node_filesystem_*`, `kubelet_volume_*`, `container_fs_*`
   - `pods`: `kube_pod_*`, `kube_running_pod_*`, `kubelet_running_*`
   - `nodes`: `kube_node_*`, `machine_*`, `node_*`
   - `api_server`: `apiserver_*`
   - `etcd`: `etcd_*`
   - `alerts`: `ALERTS*`
4. For each matched metric, look up a known-good recipe from `PROMQL_RECIPES` (if one exists)
5. Return categorized list with metric names + recipes

### Return Format (text for Claude)

```
Available CPU metrics (5 found):
  container_cpu_usage_seconds_total
    Recipe: sum by (namespace) (rate(container_cpu_usage_seconds_total{image!=""}[5m]))
    Chart: stacked_area | Title: "CPU by Namespace"
  node_cpu_seconds_total
    Recipe: 100 - avg(rate(node_cpu_seconds_total{mode='idle'}[5m])) * 100
    Chart: area | Title: "Cluster CPU Utilization"
  pod:container_cpu_usage:sum (recording rule)
    Recipe: pod:container_cpu_usage:sum{namespace='NAMESPACE'}
    Chart: line | Title: "Pod CPU Usage"
```

### Error Handling
- Prometheus unreachable: return "Cannot reach Prometheus. Use hardcoded recipes from PROMQL_RECIPES."
- No metrics in category: return "No metrics found for category '{category}'. Available categories: ..."

---

## 2. `verify_query` Tool

New `@beta_tool` in `sre_agent/k8s_tools.py`.

### Signature

```python
@beta_tool
def verify_query(query: str) -> str:
    """Test a PromQL query against Prometheus to verify it returns data.
    Call this BEFORE using a query in a dashboard to ensure it works.

    Args:
        query: PromQL query to test.
    """
```

### Behavior

1. Execute an instant query (`/api/v1/query`) — fast, single-point evaluation
2. Return one of:
   - `"PASS: Query returns data (N series, sample: {metric}={value})"` — query works
   - `"FAIL_NO_DATA: Query returned 0 results. Metric may not exist or labels may be wrong."` — valid syntax but no data
   - `"FAIL_SYNTAX: {prometheus_error_message}"` — invalid PromQL
   - `"FAIL_UNREACHABLE: Cannot reach Prometheus at {url}"` — connectivity issue
3. Record result in `promql_queries` table (success/failure count)

### Side Effects
- On PASS: upsert into `promql_queries` with `success_count += 1`, `last_success = now()`, `avg_series_count` updated
- On FAIL_NO_DATA or FAIL_SYNTAX: upsert with `failure_count += 1`, `last_failure = now()`
- Recording is fire-and-forget (same pattern as tool_usage recording)

---

## 3. PromQL Recipe Registry (`sre_agent/promql_recipes.py`)

New module containing production-tested PromQL queries organized by category. Sourced from:
- OpenShift console `cluster-dashboard.ts` (28 queries)
- OpenShift console `project-dashboard.ts` (13 queries)
- OpenShift console `resource-metrics.ts` (10 queries)
- OpenShift console `node-dashboard/queries.ts` (25 queries)
- OpenShift `cluster-monitoring-operator` prometheus-rule.yaml (40+ recording rules)
- OpenShift `cluster-monitoring-operator` control-plane prometheus-rule.yaml (alerts + recording rules)
- Pulse Agent existing recipes (view_designer.py, view_tools.py)
- Pulse Agent ControlPlaneMetrics.tsx + AutoMetrics.ts

### Structure

```python
@dataclass
class PromQLRecipe:
    name: str           # Human title: "CPU by Namespace"
    query: str          # PromQL: "sum by (namespace) (rate(container_cpu_usage_seconds_total{image!=''}[5m]))"
    chart_type: str     # "line", "area", "stacked_area", "bar", "stacked_bar"
    description: str    # "Shows CPU consumption broken down by namespace"
    metric: str         # Primary metric name this uses: "container_cpu_usage_seconds_total"
    scope: str          # "cluster", "namespace", "pod", "node"
    parameters: list[str]  # Template variables: ["namespace"] or []

RECIPES: dict[str, list[PromQLRecipe]] = { ... }
```

### Categories and Recipes

**CPU (8 recipes)**
| Name | Query | Chart | Scope |
|------|-------|-------|-------|
| Cluster CPU % | `100 - avg(rate(node_cpu_seconds_total{mode='idle'}[5m])) * 100` | area | cluster |
| CPU by Namespace | `sum by (namespace) (rate(container_cpu_usage_seconds_total{image!=""}[5m]))` | stacked_area | cluster |
| Top 10 CPU Pods | `topk(10, sum by (pod,namespace) (rate(container_cpu_usage_seconds_total{image!=""}[5m])))` | bar | cluster |
| Namespace CPU | `namespace:container_cpu_usage:sum{namespace='NS'}` | line | namespace |
| Pod CPU | `pod:container_cpu_usage:sum{pod='POD'}` | line | pod |
| CPU Requests vs Usage | `sum(kube_pod_resource_request{resource="cpu"})` | line | cluster |
| Node CPU | `1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m]))` | line | node |
| Top 25 CPU Pods (OCP) | `topk(25, sort_desc(sum(avg_over_time(pod:container_cpu_usage:sum{namespace='NS'}[5m])) BY (pod, namespace)))` | bar | namespace |

**Memory (7 recipes)**
| Name | Query | Chart | Scope |
|------|-------|-------|-------|
| Cluster Memory % | `100 - (sum(node_memory_MemAvailable_bytes) / sum(node_memory_MemTotal_bytes)) * 100` | area | cluster |
| Memory by Namespace | `sum by (namespace) (container_memory_working_set_bytes{image!=""})` | stacked_area | cluster |
| Top 10 Memory Pods | `topk(10, sum by (pod,namespace) (container_memory_working_set_bytes{image!=""}))` | bar | cluster |
| Namespace Memory | `sum(container_memory_working_set_bytes{namespace='NS',container="",pod!=""})` | line | namespace |
| Memory Requests | `sum(kube_pod_resource_request{resource="memory"})` | line | cluster |
| Node Memory | `node_memory_MemTotal_bytes{instance='NODE'} - node_memory_MemAvailable_bytes{instance='NODE'}` | line | node |
| Top 25 Memory Pods (OCP) | `topk(25, sort_desc(sum(avg_over_time(container_memory_working_set_bytes{namespace='NS'}[5m])) BY (pod)))` | bar | namespace |

**Network (6 recipes)**
| Name | Query | Chart | Scope |
|------|-------|-------|-------|
| Network Receive by Pod | `sum by (pod) (rate(container_network_receive_bytes_total{namespace='NS'}[5m]))` | stacked_area | namespace |
| Network Transmit by Pod | `sum by (pod) (rate(container_network_transmit_bytes_total{namespace='NS'}[5m]))` | stacked_area | namespace |
| Cluster Network In | `sum(instance:node_network_receive_bytes_excluding_lo:rate1m)` | line | cluster |
| Cluster Network Out | `sum(instance:node_network_transmit_bytes_excluding_lo:rate1m)` | line | cluster |
| Dropped Packets In | `sum(irate(container_network_receive_packets_dropped_total{namespace='NS'}[2h])) by (pod)` | line | namespace |
| Dropped Packets Out | `sum(irate(container_network_transmit_packets_dropped_total{namespace='NS'}[2h])) by (pod)` | line | namespace |

**Storage (4 recipes)**
| Name | Query | Chart | Scope |
|------|-------|-------|-------|
| Cluster Filesystem % | `sum(node_filesystem_size_bytes{device=~"/.*"} - node_filesystem_avail_bytes{device=~"/.*"}) / sum(node_filesystem_size_bytes{device=~"/.*"}) * 100` | area | cluster |
| Filesystem by Pod | `topk(25, sort_desc(sum(pod:container_fs_usage_bytes:sum{namespace='NS'}) BY (pod)))` | bar | namespace |
| Node Filesystem | `node_filesystem_avail_bytes{instance='NODE'} / node_filesystem_size_bytes{instance='NODE'} * 100` | line | node |
| etcd DB Size | `max(etcd_mvcc_db_total_size_in_bytes) / 1024 / 1024` | line | cluster |

**Control Plane (5 recipes)**
| Name | Query | Chart | Scope |
|------|-------|-------|-------|
| API Latency p99 | `histogram_quantile(0.99, sum(rate(apiserver_request_duration_seconds_bucket{verb!~"WATCH\|CONNECT"}[5m])) by (le))` | line | cluster |
| API Error Rate | `sum(rate(apiserver_request_total{code=~"5.."}[5m])) / sum(rate(apiserver_request_total[5m])) * 100` | line | cluster |
| API Request Rate | `sum(rate(apiserver_request_total[5m]))` | line | cluster |
| etcd Leader | `max(etcd_server_has_leader)` | metric_card | cluster |
| etcd WAL Fsync p99 | `histogram_quantile(0.99, sum(rate(etcd_disk_wal_fsync_duration_seconds_bucket[5m])) by (le))` | line | cluster |

**Pods & Workloads (5 recipes)**
| Name | Query | Chart | Scope |
|------|-------|-------|-------|
| Pod Count | `count(kube_running_pod_ready)` | metric_card | cluster |
| Pod Restarts | `sum(rate(kube_pod_container_status_restarts_total{namespace='NS'}[5m]))` | line | namespace |
| Deployment Replicas | `kube_deployment_status_replicas{deployment='DEP',namespace='NS'}` | line | namespace |
| Available Replicas | `kube_deployment_status_replicas_available{deployment='DEP',namespace='NS'}` | line | namespace |
| Container Restarts | `kube_pod_container_status_restarts_total{pod='POD',namespace='NS'}` | line | pod |

**Alerts (3 recipes)**
| Name | Query | Chart | Scope |
|------|-------|-------|-------|
| Firing Alert Count | `count(ALERTS{alertstate="firing"})` | metric_card | cluster |
| Alert Rate | `sum(rate(ALERTS{alertstate="firing"}[1h]))` | line | cluster |
| Alerts by Severity | `count by (severity) (ALERTS{alertstate="firing"})` | bar | cluster |

**Cluster Health (6 recipes)** — from cluster-monitoring-operator recording rules
| Name | Query | Chart | Scope |
|------|-------|-------|-------|
| CPU Usage:Capacity Ratio | `cluster:node_cpu:ratio` | area | cluster |
| Cluster CPU Cores Used | `cluster:cpu_usage_cores:sum` | line | cluster |
| Cluster Memory Used | `cluster:memory_usage_bytes:sum` | line | cluster |
| Workload vs Platform CPU | `workload:cpu_usage_cores:sum` (+ `openshift:cpu_usage_cores:sum` as second series) | stacked_area | cluster |
| Node Readiness | `avg_over_time((count(max by (node) (kube_node_status_condition{condition="Ready",status="true"} == 1)) / scalar(count(max by (node) (kube_node_status_condition{condition="Ready",status="true"}))))[5m:1s])` | line | cluster |
| Schedulable Node Availability | `cluster:usage:kube_schedulable_node_ready_reachable:avg5m` | line | cluster |

**Ingress (4 recipes)** — from cluster-monitoring-operator recording rules
| Name | Query | Chart | Scope |
|------|-------|-------|-------|
| HTTP Responses by Code | `code:cluster:ingress_http_request_count:rate5m:sum` | stacked_bar | cluster |
| Ingress Bandwidth In | `cluster:usage:ingress_frontend_bytes_in:rate5m:sum` | line | cluster |
| Ingress Bandwidth Out | `cluster:usage:ingress_frontend_bytes_out:rate5m:sum` | line | cluster |
| Active Connections | `cluster:usage:ingress_frontend_connections:sum` | line | cluster |

**Scheduler (2 recipes)** — from control-plane prometheus-rule
| Name | Query | Chart | Scope |
|------|-------|-------|-------|
| Scheduling Latency p99 | `cluster_quantile:scheduler_scheduling_attempt_duration_seconds:histogram_quantile{quantile="0.99"}` | line | cluster |
| Scheduling Attempts | `sum by (result) (rate(scheduler_schedule_attempts_total[5m]))` | stacked_area | cluster |

**Resource Overcommit (2 recipes)** — KPI cards for capacity planning
| Name | Query | Chart | Scope |
|------|-------|-------|-------|
| CPU Overcommit Ratio | `sum(kube_pod_resource_request{resource="cpu"}) / sum(kube_node_status_allocatable{resource="cpu"})` | metric_card | cluster |
| Memory Overcommit Ratio | `sum(kube_pod_resource_request{resource="memory"}) / sum(kube_node_status_allocatable{resource="memory"})` | metric_card | cluster |

**Workload State (5 recipes)** — from kube-state-metrics
| Name | Query | Chart | Scope |
|------|-------|-------|-------|
| Pods by Phase | `sum by (phase) (kube_pod_status_phase)` | stacked_bar | cluster |
| Pods by Namespace | `count by (namespace) (kube_pod_info)` | bar | cluster |
| Unavailable Deployments | `sum(kube_deployment_status_replicas_unavailable > 0)` | metric_card | cluster |
| Deployment Replica Mismatch | `kube_deployment_spec_replicas - kube_deployment_status_replicas_available` | bar | cluster |
| Unschedulable Pods | `count(kube_pod_status_unschedulable == 1)` | metric_card | cluster |

**Storage State (3 recipes)** — from kube-state-metrics
| Name | Query | Chart | Scope |
|------|-------|-------|-------|
| PVC Usage | `kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes * 100` | bar | namespace |
| PV by Phase | `count by (phase) (kube_persistentvolume_status_phase)` | stacked_bar | cluster |
| PVC Count by Namespace | `count by (namespace) (kube_persistentvolumeclaim_info)` | bar | cluster |

**Node USE Method (6 recipes)** — from prometheus/node_exporter mixin recording rules (pre-aggregated, always available)
| Name | Query | Chart | Scope |
|------|-------|-------|-------|
| Node CPU Utilization | `instance:node_cpu_utilisation:rate5m{instance='NODE'}` | line | node |
| Node Load per CPU | `instance:node_load1_per_cpu:ratio{instance='NODE'}` | line | node |
| Node Memory Utilization | `instance:node_memory_utilisation:ratio{instance='NODE'}` | line | node |
| Node Memory Pressure | `instance:node_vmstat_pgmajfault:rate5m{instance='NODE'}` | line | node |
| Node Disk IO Utilization | `instance_device:node_disk_io_time_seconds:rate5m{instance='NODE'}` | line | node |
| Node Disk IO Saturation | `instance_device:node_disk_io_time_weighted_seconds:rate5m{instance='NODE'}` | line | node |

**Monitoring Health (3 recipes)** — from prometheus-operator self-monitoring
| Name | Query | Chart | Scope |
|------|-------|-------|-------|
| Prometheus Targets Down | `100 * (1 - sum(up) / count(up))` | metric_card | cluster |
| Config Reload Failures | `max_over_time(reloader_last_reload_successful[5m]) == 0` | metric_card | cluster |
| Operator Reconcile Error Rate | `sum(rate(prometheus_operator_reconcile_errors_total[5m])) / sum(rate(prometheus_operator_reconcile_operations_total[5m])) * 100` | line | cluster |

**Cluster Version & Operators (4 recipes)** — from openshift/cluster-version-operator
| Name | Query | Chart | Scope |
|------|-------|-------|-------|
| Operators Available | `sum(cluster_operator_up == 1) / count(cluster_operator_up) * 100` | metric_card | cluster |
| Operator Conditions | `cluster_operator_conditions{condition="Degraded"} == 1` | status_list | cluster |
| Available Updates | `cluster_version_available_updates` | metric_card | cluster |
| Operator Condition Transitions | `sum by (name) (cluster_operator_condition_transitions)` | bar | cluster |

**Multi-Cluster Health (6 recipes)** — from stolostron/multicluster-observability-operator ACM dashboards
| Name | Query | Chart | Scope |
|------|-------|-------|-------|
| Node Ready Ratio | `sum(kube_node_status_condition{condition="Ready",status="true"} == 1) / count(kube_node_status_condition{condition="Ready",status="true"})` | metric_card | cluster |
| System Pod Health | `count(kube_pod_status_phase{namespace=~"openshift-.+\|kube-.+",phase=~"Failed\|Pending"} == 0) / count(kube_pod_status_phase{namespace=~"openshift-.+\|kube-.+",phase=~"Failed\|Pending"})` | metric_card | cluster |
| etcd Leader Changes | `sum(changes(etcd_server_leader_changes_seen_total{job="etcd"}[1h]))` | metric_card | cluster |
| etcd Proposals Pending | `sum(etcd_server_proposals_pending)` | line | cluster |
| Operator Health Ratio | `count(cluster_operator_conditions{condition=~"Degraded\|Progressing"} == 0) / count(cluster_operator_conditions{condition=~"Degraded\|Progressing"})` | metric_card | cluster |
| PVCs Over 80% Used | `count(((kubelet_volume_stats_capacity_bytes - kubelet_volume_stats_available_bytes) / kubelet_volume_stats_capacity_bytes * 100) > 80)` | metric_card | cluster |

**Total: ~79 production-tested recipes** across 16 categories.

### Sources
- `openshift/console` — cluster-dashboard.ts, project-dashboard.ts, resource-metrics.ts, node-dashboard/queries.ts
- `openshift/cluster-monitoring-operator` — prometheus-rule.yaml, control-plane/prometheus-rule.yaml
- `kubernetes/kube-state-metrics` — pod, deployment, node, PV/PVC metric documentation
- `prometheus/node_exporter` — node-mixin USE method recording rules + alerts
- `prometheus-operator/prometheus-operator` — operator self-monitoring alerts
- `openshift/cluster-version-operator` — CVO metrics (cluster_operator_up, cluster_version, operator conditions)
- `stolostron/multicluster-observability-operator` — ACM cluster overview dashboard, PVC dashboard, metrics allowlist
- Pulse Agent existing codebase — view_tools.py, view_designer.py, ControlPlaneMetrics.tsx, AutoMetrics.ts

### Recipe Lookup

```python
def get_recipe(metric_name: str) -> PromQLRecipe | None:
    """Find a recipe that uses a given metric."""

def get_recipes_for_category(category: str) -> list[PromQLRecipe]:
    """Get all recipes in a category."""

def get_fallback(category: str, scope: str = "cluster") -> PromQLRecipe | None:
    """Get the best fallback recipe for a category+scope when a query fails."""
```

---

## 4. Learned Queries Table (`promql_queries`)

New PostgreSQL table for tracking query success/failure rates per cluster.

### Schema

```sql
CREATE TABLE IF NOT EXISTS promql_queries (
    id SERIAL PRIMARY KEY,
    query_hash TEXT NOT NULL,         -- SHA256 of normalized query (labels stripped)
    query_template TEXT NOT NULL,     -- Query with namespace/pod replaced by placeholder
    category TEXT DEFAULT '',
    success_count INT DEFAULT 0,
    failure_count INT DEFAULT 0,
    last_success TIMESTAMPTZ,
    last_failure TIMESTAMPTZ,
    avg_series_count FLOAT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(query_hash)
);
```

### Recording

Called by `verify_query` and `get_prometheus_query` (existing tool — add recording hook):
- Normalize query: strip namespace/pod values, lowercase, hash
- Upsert on conflict: increment success_count or failure_count
- Fire-and-forget (same pattern as `record_tool_call`)

### Query Functions

```python
def get_query_reliability(query_hash: str) -> dict | None:
    """Return success_count, failure_count, avg_series_count for a query."""

def get_reliable_queries(category: str, min_success: int = 3) -> list[dict]:
    """Return queries with high success rates for a category."""
```

---

## 5. View Designer Prompt Updates (`view_designer.py`)

### Updated Workflow

Replace Step 2 (BUILD) with:

```
### Step 2: BUILD (after user approves)

1. Call discover_metrics(category) for each metric category in the plan
2. Review available metrics — select the ones that match the dashboard intent
3. For each chart/metric_card:
   a. If a known recipe exists for the metric → use it
   b. If no recipe → write a PromQL query using discovered metric names
   c. Call verify_query(query) to test it
   d. If PASS → proceed to get_prometheus_query(query, time_range="1h")
   e. If FAIL → try the category fallback recipe
   f. If fallback also fails → skip this widget
4. Call create_dashboard(title, template=...)
```

### New Rule

Add to Rules section:
```
16. ALWAYS call discover_metrics() before writing PromQL queries
17. ALWAYS call verify_query() before calling get_prometheus_query()
18. When verify_query fails, use get_fallback() for a known-good alternative
```

---

## 6. Integration Points

### `get_prometheus_query` Enhancement

Add recording hook to existing function (after line where results are parsed):
```python
# Record query success/failure for the learned queries system
try:
    from .promql_recipes import record_query_result
    record_query_result(query, success=bool(results), series_count=len(results))
except Exception:
    pass  # fire-and-forget
```

### View Designer Tool List

Add `discover_metrics` and `verify_query` to `_DATA_TOOL_NAMES` in `view_designer.py`.

### Harness Categories

Add both tools to the `monitoring` category in `harness.py` TOOL_CATEGORIES.

---

## 7. Test Framework

### `tests/test_promql_recipes.py` (~15 tests)
- All recipes have required fields (name, query, chart_type, metric, scope)
- No duplicate queries across categories
- All categories have at least 1 recipe
- get_recipe returns correct recipe for known metric
- get_fallback returns recipe for each category

### `tests/test_discover_metrics.py` (~12 tests)
- Mock Prometheus label values API, verify category filtering
- Test caching (second call uses cache)
- Test cache expiry after TTL
- Test with empty Prometheus response
- Test invalid category returns error
- Test recipe lookup for discovered metrics
- Test Prometheus unreachable fallback

### `tests/test_verify_query.py` (~10 tests)
- Mock Prometheus instant query API
- Test PASS with data
- Test FAIL_NO_DATA with empty result
- Test FAIL_SYNTAX with error response
- Test FAIL_UNREACHABLE with connection error
- Test recording to promql_queries table (mock DB)
- Test fire-and-forget doesn't propagate errors

### `tests/test_learned_queries.py` (~8 tests)
- Record success increments count
- Record failure increments count
- get_query_reliability returns correct stats
- get_reliable_queries filters by min_success
- Query normalization strips namespace/pod values
- Upsert on conflict works correctly

---

## 8. Files Created/Modified

| File | Action | Purpose |
|------|--------|---------|
| `sre_agent/promql_recipes.py` | CREATE | Recipe registry + learned queries DB functions |
| `sre_agent/k8s_tools.py` | MODIFY | Add discover_metrics + verify_query tools, add recording hook to get_prometheus_query |
| `sre_agent/view_designer.py` | MODIFY | Updated workflow + new rules |
| `sre_agent/db_schema.py` | MODIFY | Add promql_queries table schema |
| `sre_agent/db_migrations.py` | MODIFY | Add migration for promql_queries |
| `sre_agent/harness.py` | MODIFY | Add tools to categories |
| `tests/test_promql_recipes.py` | CREATE | Recipe registry tests |
| `tests/test_discover_metrics.py` | CREATE | Discovery tool tests |
| `tests/test_verify_query.py` | CREATE | Verification tool tests |
| `tests/test_learned_queries.py` | CREATE | Learned queries DB tests |

---

## Out of Scope
- Progressive streaming rendering (Phase 2 — frontend)
- Semantic layout engine (Phase 3)
- Cross-user dashboard intelligence (Phase 4)
- Natural language refinement (Phase 5)
