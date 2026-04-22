"""PromQL recipe registry — 73 production-tested queries organized by category."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger("pulse_agent.promql_recipes")

_VALID_CLUSTER_NAME = re.compile(r"^[a-zA-Z0-9._-]+$")

# PromQL function names that should not be treated as metric selectors
_PROMQL_FUNCS = {
    "abs",
    "absent",
    "absent_over_time",
    "avg_over_time",
    "ceil",
    "changes",
    "clamp",
    "clamp_max",
    "clamp_min",
    "count_over_time",
    "day_of_month",
    "day_of_week",
    "days_in_month",
    "delta",
    "deriv",
    "exp",
    "floor",
    "histogram_quantile",
    "holt_winters",
    "hour",
    "idelta",
    "increase",
    "irate",
    "label_join",
    "label_replace",
    "last_over_time",
    "ln",
    "log2",
    "log10",
    "max_over_time",
    "min_over_time",
    "minute",
    "month",
    "predict_linear",
    "quantile_over_time",
    "rate",
    "resets",
    "round",
    "scalar",
    "sgn",
    "sort",
    "sort_desc",
    "sqrt",
    "stddev_over_time",
    "stdvar_over_time",
    "sum_over_time",
    "time",
    "timestamp",
    "vector",
    "year",
    # aggregation operators
    "avg",
    "bottomk",
    "count",
    "count_values",
    "group",
    "max",
    "min",
    "quantile",
    "stddev",
    "stdvar",
    "sum",
    "topk",
}

# Matches a metric selector: metric_name{...} or metric_name (bare)
# Captures: (metric_name)({...}) where group 2 may be absent
_METRIC_RE = re.compile(r"(?<![a-zA-Z_:])([a-zA-Z_:][a-zA-Z0-9_:]*)\{([^}]*)\}")
_BARE_METRIC_RE = re.compile(
    r"(?<![a-zA-Z_:{])([a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?=\s*[\[\(,\)\s]|$)"
)

_THANOS_INCOMPATIBLE = [
    (
        re.compile(r"on\s*\([^)]*\)\s*group_left"),
        "group_left joins may fail on ACM Thanos — use separate queries instead",
    ),
    (re.compile(r"on\s*\([^)]*\)\s*group_right"), "group_right joins may fail on ACM Thanos"),
]


def inject_cluster_label(query: str, cluster_name: str) -> str:
    """Inject cluster='X' into all metric selectors in a PromQL query.

    Handles metric{labels} and bare metric names. Skips if cluster= already present.
    """
    if not _VALID_CLUSTER_NAME.match(cluster_name):
        raise ValueError(f"Invalid cluster name: {cluster_name!r}")
    if f'cluster="{cluster_name}"' in query:
        return query

    cluster_matcher = f'cluster="{cluster_name}"'

    def _inject_into_selector(m: re.Match) -> str:
        name = m.group(1)
        labels = m.group(2)
        if "cluster=" in labels:
            return m.group(0)
        sep = "," if labels.strip() else ""
        return f"{name}{{{cluster_matcher}{sep}{labels}}}"

    result = _METRIC_RE.sub(_inject_into_selector, query)

    # Handle bare metrics (no braces) — only if not a function name
    def _inject_bare(m: re.Match) -> str:
        name = m.group(1)
        if name.lower() in _PROMQL_FUNCS:
            return name
        if name in (
            "by",
            "without",
            "on",
            "ignoring",
            "bool",
            "offset",
            "and",
            "or",
            "unless",
            "inf",
            "nan",
            "NaN",
            "Inf",
        ):
            return name
        # Don't inject if this metric was already handled (has braces after it)
        end = m.end()
        if end < len(result) and result[end] == "{":
            return name
        return f"{name}{{{cluster_matcher}}}"

    return _BARE_METRIC_RE.sub(_inject_bare, result)


def check_thanos_compatibility(query: str) -> str | None:
    """Return a warning if query uses patterns known to fail on Thanos, or None."""
    for pattern, warning in _THANOS_INCOMPATIBLE:
        if pattern.search(query):
            return warning
    return None


@dataclass
class PromQLRecipe:
    """A curated PromQL query with rendering metadata."""

    name: str  # Human title: "CPU by Namespace"
    query: str  # PromQL query string
    chart_type: str  # "line", "area", "stacked_area", "bar", "stacked_bar", "metric_card", "status_list"
    description: str  # What this measures
    metric: str  # Primary metric name: "container_cpu_usage_seconds_total"
    scope: str  # "cluster", "namespace", "pod", "node"
    parameters: list[str] = field(default_factory=list)  # Template variables: ["namespace"]

    def render(self, **params: str) -> str:
        """Substitute parameter placeholders in the query.

        Example: recipe.render(namespace="production", pod="my-pod", cluster="prod-east")
        """
        q = self.query
        mapping = {"NS": "namespace", "POD": "pod", "NODE": "instance", "DEP": "deployment"}
        for placeholder, param_name in mapping.items():
            if param_name in params:
                q = q.replace(f"'{placeholder}'", f"'{params[param_name]}'")
        if "cluster" in params:
            q = inject_cluster_label(q, params["cluster"])
        return q


# ---------------------------------------------------------------------------
# Recipe registry — 16 categories, 79 recipes
# ---------------------------------------------------------------------------

RECIPES: dict[str, list[PromQLRecipe]] = {
    # -- CPU (8) --
    "cpu": [
        PromQLRecipe(
            name="Cluster CPU %",
            query="100 - avg(rate(node_cpu_seconds_total{mode='idle'}[5m])) * 100",
            chart_type="area",
            description="Average CPU utilization across all cluster nodes",
            metric="node_cpu_seconds_total",
            scope="cluster",
        ),
        PromQLRecipe(
            name="CPU by Namespace",
            query='sum by (namespace) (rate(container_cpu_usage_seconds_total{image!=""}[5m]))',
            chart_type="stacked_area",
            description="CPU consumption broken down by Kubernetes namespace",
            metric="container_cpu_usage_seconds_total",
            scope="cluster",
        ),
        PromQLRecipe(
            name="Top 10 CPU Pods",
            query='topk(10, sum by (pod,namespace) (rate(container_cpu_usage_seconds_total{image!=""}[5m])))',
            chart_type="bar",
            description="Top 10 pods consuming the most CPU",
            metric="container_cpu_usage_seconds_total",
            scope="cluster",
        ),
        PromQLRecipe(
            name="Namespace CPU",
            query="namespace:container_cpu_usage:sum{namespace='NS'}",
            chart_type="line",
            description="Total CPU usage for a specific namespace",
            metric="namespace:container_cpu_usage:sum",
            scope="namespace",
            parameters=["namespace"],
        ),
        PromQLRecipe(
            name="Pod CPU",
            query="pod:container_cpu_usage:sum{pod='POD'}",
            chart_type="line",
            description="CPU usage for a specific pod",
            metric="pod:container_cpu_usage:sum",
            scope="pod",
            parameters=["pod"],
        ),
        PromQLRecipe(
            name="CPU Requests vs Usage",
            query='sum(kube_pod_resource_request{resource="cpu"})',
            chart_type="line",
            description="Total CPU requests across the cluster",
            metric="kube_pod_resource_request",
            scope="cluster",
        ),
        PromQLRecipe(
            name="Node CPU",
            query='1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m]))',
            chart_type="line",
            description="Per-node CPU utilization",
            metric="node_cpu_seconds_total",
            scope="node",
        ),
        PromQLRecipe(
            name="Top 25 CPU Pods (OCP)",
            query="topk(25, sort_desc(sum(avg_over_time(pod:container_cpu_usage:sum{namespace='NS'}[5m])) BY (pod, namespace)))",
            chart_type="bar",
            description="Top 25 CPU-consuming pods in a namespace (OpenShift recording rule)",
            metric="pod:container_cpu_usage:sum",
            scope="namespace",
            parameters=["namespace"],
        ),
    ],
    # -- Memory (7) --
    "memory": [
        PromQLRecipe(
            name="Cluster Memory %",
            query="100 - (sum(node_memory_MemAvailable_bytes) / sum(node_memory_MemTotal_bytes)) * 100",
            chart_type="area",
            description="Cluster-wide memory utilization percentage",
            metric="node_memory_MemAvailable_bytes",
            scope="cluster",
        ),
        PromQLRecipe(
            name="Memory by Namespace",
            query='sum by (namespace) (container_memory_working_set_bytes{image!=""})',
            chart_type="stacked_area",
            description="Memory working set broken down by namespace",
            metric="container_memory_working_set_bytes",
            scope="cluster",
        ),
        PromQLRecipe(
            name="Top 10 Memory Pods",
            query='topk(10, sum by (pod,namespace) (container_memory_working_set_bytes{image!=""}))',
            chart_type="bar",
            description="Top 10 pods consuming the most memory",
            metric="container_memory_working_set_bytes",
            scope="cluster",
        ),
        PromQLRecipe(
            name="Namespace Memory",
            query='sum(container_memory_working_set_bytes{namespace=\'NS\',container="",pod!=""})',
            chart_type="line",
            description="Total memory working set for a specific namespace",
            metric="container_memory_working_set_bytes",
            scope="namespace",
            parameters=["namespace"],
        ),
        PromQLRecipe(
            name="Memory Requests",
            query='sum(kube_pod_resource_request{resource="memory"})',
            chart_type="line",
            description="Total memory requests across the cluster",
            metric="kube_pod_resource_request",
            scope="cluster",
        ),
        PromQLRecipe(
            name="Node Memory",
            query="node_memory_MemTotal_bytes{instance='NODE'} - node_memory_MemAvailable_bytes{instance='NODE'}",
            chart_type="line",
            description="Used memory for a specific node",
            metric="node_memory_MemTotal_bytes",
            scope="node",
            parameters=["instance"],
        ),
        PromQLRecipe(
            name="Top 25 Memory Pods (OCP)",
            query="topk(25, sort_desc(sum(avg_over_time(container_memory_working_set_bytes{namespace='NS'}[5m])) BY (pod)))",
            chart_type="bar",
            description="Top 25 memory-consuming pods in a namespace (OpenShift)",
            metric="container_memory_working_set_bytes",
            scope="namespace",
            parameters=["namespace"],
        ),
    ],
    # -- Network (6) --
    "network": [
        PromQLRecipe(
            name="Network Receive by Pod",
            query="sum by (pod) (rate(container_network_receive_bytes_total{namespace='NS'}[5m]))",
            chart_type="stacked_area",
            description="Inbound network traffic per pod in a namespace",
            metric="container_network_receive_bytes_total",
            scope="namespace",
            parameters=["namespace"],
        ),
        PromQLRecipe(
            name="Network Transmit by Pod",
            query="sum by (pod) (rate(container_network_transmit_bytes_total{namespace='NS'}[5m]))",
            chart_type="stacked_area",
            description="Outbound network traffic per pod in a namespace",
            metric="container_network_transmit_bytes_total",
            scope="namespace",
            parameters=["namespace"],
        ),
        PromQLRecipe(
            name="Cluster Network In",
            query="sum(instance:node_network_receive_bytes_excluding_lo:rate1m)",
            chart_type="line",
            description="Total inbound network bandwidth across the cluster",
            metric="instance:node_network_receive_bytes_excluding_lo:rate1m",
            scope="cluster",
        ),
        PromQLRecipe(
            name="Cluster Network Out",
            query="sum(instance:node_network_transmit_bytes_excluding_lo:rate1m)",
            chart_type="line",
            description="Total outbound network bandwidth across the cluster",
            metric="instance:node_network_transmit_bytes_excluding_lo:rate1m",
            scope="cluster",
        ),
        PromQLRecipe(
            name="Dropped Packets In",
            query="sum(irate(container_network_receive_packets_dropped_total{namespace='NS'}[2h])) by (pod)",
            chart_type="line",
            description="Inbound dropped packets per pod in a namespace",
            metric="container_network_receive_packets_dropped_total",
            scope="namespace",
            parameters=["namespace"],
        ),
        PromQLRecipe(
            name="Dropped Packets Out",
            query="sum(irate(container_network_transmit_packets_dropped_total{namespace='NS'}[2h])) by (pod)",
            chart_type="line",
            description="Outbound dropped packets per pod in a namespace",
            metric="container_network_transmit_packets_dropped_total",
            scope="namespace",
            parameters=["namespace"],
        ),
    ],
    # -- Storage (4) --
    "storage": [
        PromQLRecipe(
            name="Cluster Filesystem %",
            query='sum(node_filesystem_size_bytes{device=~"/.*"} - node_filesystem_avail_bytes{device=~"/.*"}) / sum(node_filesystem_size_bytes{device=~"/.*"}) * 100',
            chart_type="area",
            description="Cluster-wide filesystem usage percentage",
            metric="node_filesystem_size_bytes",
            scope="cluster",
        ),
        PromQLRecipe(
            name="Filesystem by Pod",
            query="topk(25, sort_desc(sum(pod:container_fs_usage_bytes:sum{namespace='NS'}) BY (pod)))",
            chart_type="bar",
            description="Top 25 pods by filesystem usage in a namespace",
            metric="pod:container_fs_usage_bytes:sum",
            scope="namespace",
            parameters=["namespace"],
        ),
        PromQLRecipe(
            name="Node Filesystem",
            query="node_filesystem_avail_bytes{instance='NODE'} / node_filesystem_size_bytes{instance='NODE'} * 100",
            chart_type="line",
            description="Available filesystem percentage on a specific node",
            metric="node_filesystem_avail_bytes",
            scope="node",
            parameters=["instance"],
        ),
        PromQLRecipe(
            name="etcd DB Size",
            query="max(etcd_mvcc_db_total_size_in_bytes) / 1024 / 1024",
            chart_type="line",
            description="etcd database size in MiB",
            metric="etcd_mvcc_db_total_size_in_bytes",
            scope="cluster",
        ),
    ],
    # -- Control Plane (5) --
    "control_plane": [
        PromQLRecipe(
            name="API Latency p99",
            query='histogram_quantile(0.99, sum(rate(apiserver_request_duration_seconds_bucket{verb!~"WATCH|CONNECT"}[5m])) by (le))',
            chart_type="line",
            description="99th percentile API server request latency",
            metric="apiserver_request_duration_seconds_bucket",
            scope="cluster",
        ),
        PromQLRecipe(
            name="API Error Rate",
            query='sum(rate(apiserver_request_total{code=~"5.."}[5m])) / sum(rate(apiserver_request_total[5m])) * 100',
            chart_type="line",
            description="Percentage of API server requests returning 5xx errors",
            metric="apiserver_request_total",
            scope="cluster",
        ),
        PromQLRecipe(
            name="API Request Rate",
            query="sum(rate(apiserver_request_total[5m]))",
            chart_type="line",
            description="Total API server request rate",
            metric="apiserver_request_total",
            scope="cluster",
        ),
        PromQLRecipe(
            name="etcd Leader",
            query="max(etcd_server_has_leader)",
            chart_type="metric_card",
            description="Whether etcd cluster has an elected leader",
            metric="etcd_server_has_leader",
            scope="cluster",
        ),
        PromQLRecipe(
            name="etcd WAL Fsync p99",
            query="histogram_quantile(0.99, sum(rate(etcd_disk_wal_fsync_duration_seconds_bucket[5m])) by (le))",
            chart_type="line",
            description="99th percentile etcd WAL fsync duration",
            metric="etcd_disk_wal_fsync_duration_seconds_bucket",
            scope="cluster",
        ),
    ],
    # -- Pods (5) --
    "pods": [
        PromQLRecipe(
            name="Pod Count",
            query="count(kube_running_pod_ready)",
            chart_type="metric_card",
            description="Total number of running and ready pods",
            metric="kube_running_pod_ready",
            scope="cluster",
        ),
        PromQLRecipe(
            name="Pod Restarts",
            query="sum(rate(kube_pod_container_status_restarts_total{namespace='NS'}[5m]))",
            chart_type="line",
            description="Pod container restart rate in a namespace",
            metric="kube_pod_container_status_restarts_total",
            scope="namespace",
            parameters=["namespace"],
        ),
        PromQLRecipe(
            name="Deployment Replicas",
            query="kube_deployment_status_replicas{deployment='DEP',namespace='NS'}",
            chart_type="line",
            description="Current replica count for a deployment",
            metric="kube_deployment_status_replicas",
            scope="namespace",
            parameters=["namespace", "deployment"],
        ),
        PromQLRecipe(
            name="Available Replicas",
            query="kube_deployment_status_replicas_available{deployment='DEP',namespace='NS'}",
            chart_type="line",
            description="Available replicas for a deployment",
            metric="kube_deployment_status_replicas_available",
            scope="namespace",
            parameters=["namespace", "deployment"],
        ),
        PromQLRecipe(
            name="Container Restarts",
            query="kube_pod_container_status_restarts_total{pod='POD',namespace='NS'}",
            chart_type="line",
            description="Total container restarts for a specific pod",
            metric="kube_pod_container_status_restarts_total",
            scope="pod",
            parameters=["namespace", "pod"],
        ),
    ],
    # -- Alerts (3) --
    "alerts": [
        PromQLRecipe(
            name="Firing Alert Count",
            query='count(ALERTS{alertstate="firing"})',
            chart_type="metric_card",
            description="Number of currently firing alerts",
            metric="ALERTS",
            scope="cluster",
        ),
        PromQLRecipe(
            name="Alert Rate",
            query='sum(rate(ALERTS{alertstate="firing"}[1h]))',
            chart_type="line",
            description="Rate of firing alerts over the past hour",
            metric="ALERTS",
            scope="cluster",
        ),
        PromQLRecipe(
            name="Alerts by Severity",
            query='count by (severity) (ALERTS{alertstate="firing"})',
            chart_type="bar",
            description="Firing alerts grouped by severity level",
            metric="ALERTS",
            scope="cluster",
        ),
    ],
    # -- Cluster Health (6) --
    "cluster_health": [
        PromQLRecipe(
            name="CPU Usage:Capacity Ratio",
            query="cluster:node_cpu:ratio",
            chart_type="area",
            description="Ratio of CPU usage to total cluster capacity",
            metric="cluster:node_cpu:ratio",
            scope="cluster",
        ),
        PromQLRecipe(
            name="Cluster CPU Cores Used",
            query="cluster:cpu_usage_cores:sum",
            chart_type="line",
            description="Total CPU cores actively used across the cluster",
            metric="cluster:cpu_usage_cores:sum",
            scope="cluster",
        ),
        PromQLRecipe(
            name="Cluster Memory Used",
            query="cluster:memory_usage_bytes:sum",
            chart_type="line",
            description="Total memory actively used across the cluster",
            metric="cluster:memory_usage_bytes:sum",
            scope="cluster",
        ),
        PromQLRecipe(
            name="Workload vs Platform CPU",
            query="workload:cpu_usage_cores:sum",
            chart_type="stacked_area",
            description="CPU usage split between workloads and platform components",
            metric="workload:cpu_usage_cores:sum",
            scope="cluster",
        ),
        PromQLRecipe(
            name="Node Readiness",
            query='avg_over_time((count(max by (node) (kube_node_status_condition{condition="Ready",status="true"} == 1)) / scalar(count(max by (node) (kube_node_status_condition{condition="Ready",status="true"}))))[5m:1s])',
            chart_type="line",
            description="Fraction of nodes in Ready state over time",
            metric="kube_node_status_condition",
            scope="cluster",
        ),
        PromQLRecipe(
            name="Schedulable Node Availability",
            query="cluster:usage:kube_schedulable_node_ready_reachable:avg5m",
            chart_type="line",
            description="Average availability of schedulable and ready nodes",
            metric="cluster:usage:kube_schedulable_node_ready_reachable:avg5m",
            scope="cluster",
        ),
    ],
    # -- Ingress (4) --
    "ingress": [
        PromQLRecipe(
            name="HTTP Responses by Code",
            query="code:cluster:ingress_http_request_count:rate5m:sum",
            chart_type="stacked_bar",
            description="Ingress HTTP responses grouped by status code",
            metric="code:cluster:ingress_http_request_count:rate5m:sum",
            scope="cluster",
        ),
        PromQLRecipe(
            name="Ingress Bandwidth In",
            query="cluster:usage:ingress_frontend_bytes_in:rate5m:sum",
            chart_type="line",
            description="Inbound bandwidth through ingress frontends",
            metric="cluster:usage:ingress_frontend_bytes_in:rate5m:sum",
            scope="cluster",
        ),
        PromQLRecipe(
            name="Ingress Bandwidth Out",
            query="cluster:usage:ingress_frontend_bytes_out:rate5m:sum",
            chart_type="line",
            description="Outbound bandwidth through ingress frontends",
            metric="cluster:usage:ingress_frontend_bytes_out:rate5m:sum",
            scope="cluster",
        ),
        PromQLRecipe(
            name="Active Connections",
            query="cluster:usage:ingress_frontend_connections:sum",
            chart_type="line",
            description="Current active ingress connections",
            metric="cluster:usage:ingress_frontend_connections:sum",
            scope="cluster",
        ),
    ],
    # -- Scheduler (2) --
    "scheduler": [
        PromQLRecipe(
            name="Scheduling Latency p99",
            query='cluster_quantile:scheduler_scheduling_attempt_duration_seconds:histogram_quantile{quantile="0.99"}',
            chart_type="line",
            description="99th percentile scheduling attempt latency",
            metric="cluster_quantile:scheduler_scheduling_attempt_duration_seconds:histogram_quantile",
            scope="cluster",
        ),
        PromQLRecipe(
            name="Scheduling Attempts",
            query="sum by (result) (rate(scheduler_schedule_attempts_total[5m]))",
            chart_type="stacked_area",
            description="Scheduling attempts by result (scheduled, unschedulable, error)",
            metric="scheduler_schedule_attempts_total",
            scope="cluster",
        ),
    ],
    # -- Overcommit (2) --
    "overcommit": [
        PromQLRecipe(
            name="CPU Overcommit Ratio",
            query='sum(kube_pod_resource_request{resource="cpu"}) / sum(kube_node_status_allocatable{resource="cpu"})',
            chart_type="metric_card",
            description="Ratio of CPU requests to allocatable CPU capacity",
            metric="kube_pod_resource_request",
            scope="cluster",
        ),
        PromQLRecipe(
            name="Memory Overcommit Ratio",
            query='sum(kube_pod_resource_request{resource="memory"}) / sum(kube_node_status_allocatable{resource="memory"})',
            chart_type="metric_card",
            description="Ratio of memory requests to allocatable memory capacity",
            metric="kube_node_status_allocatable",
            scope="cluster",
        ),
    ],
    # -- Workload State (5) --
    "workload_state": [
        PromQLRecipe(
            name="Pods by Phase",
            query="sum by (phase) (kube_pod_status_phase)",
            chart_type="stacked_bar",
            description="Pod distribution across lifecycle phases",
            metric="kube_pod_status_phase",
            scope="cluster",
        ),
        PromQLRecipe(
            name="Pods by Namespace",
            query="count by (namespace) (kube_pod_info)",
            chart_type="bar",
            description="Pod count per namespace",
            metric="kube_pod_info",
            scope="cluster",
        ),
        PromQLRecipe(
            name="Unavailable Deployments",
            query="sum(kube_deployment_status_replicas_unavailable > 0)",
            chart_type="metric_card",
            description="Number of deployments with unavailable replicas",
            metric="kube_deployment_status_replicas_unavailable",
            scope="cluster",
        ),
        PromQLRecipe(
            name="Deployment Replica Mismatch",
            query="kube_deployment_spec_replicas - kube_deployment_status_replicas_available",
            chart_type="bar",
            description="Difference between desired and available replicas per deployment",
            metric="kube_deployment_spec_replicas",
            scope="cluster",
        ),
        PromQLRecipe(
            name="Unschedulable Pods",
            query="count(kube_pod_status_unschedulable == 1)",
            chart_type="metric_card",
            description="Number of pods that cannot be scheduled",
            metric="kube_pod_status_unschedulable",
            scope="cluster",
        ),
    ],
    # -- Storage State (3) --
    "storage_state": [
        PromQLRecipe(
            name="PVC Usage",
            query="kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes * 100",
            chart_type="bar",
            description="Persistent volume claim usage as a percentage",
            metric="kubelet_volume_stats_used_bytes",
            scope="namespace",
        ),
        PromQLRecipe(
            name="PV by Phase",
            query="count by (phase) (kube_persistentvolume_status_phase)",
            chart_type="stacked_bar",
            description="Persistent volumes grouped by lifecycle phase",
            metric="kube_persistentvolume_status_phase",
            scope="cluster",
        ),
        PromQLRecipe(
            name="PVC Count by Namespace",
            query="count by (namespace) (kube_persistentvolumeclaim_info)",
            chart_type="bar",
            description="Number of persistent volume claims per namespace",
            metric="kube_persistentvolumeclaim_info",
            scope="cluster",
        ),
    ],
    # -- Node Use (6) --
    "node_use": [
        PromQLRecipe(
            name="Node CPU Utilization",
            query="instance:node_cpu_utilisation:rate5m{instance='NODE'}",
            chart_type="line",
            description="CPU utilization rate for a specific node",
            metric="instance:node_cpu_utilisation:rate5m",
            scope="node",
            parameters=["instance"],
        ),
        PromQLRecipe(
            name="Node Load per CPU",
            query="instance:node_load1_per_cpu:ratio{instance='NODE'}",
            chart_type="line",
            description="1-minute load average normalized per CPU on a node",
            metric="instance:node_load1_per_cpu:ratio",
            scope="node",
            parameters=["instance"],
        ),
        PromQLRecipe(
            name="Node Memory Utilization",
            query="instance:node_memory_utilisation:ratio{instance='NODE'}",
            chart_type="line",
            description="Memory utilization ratio for a specific node",
            metric="instance:node_memory_utilisation:ratio",
            scope="node",
            parameters=["instance"],
        ),
        PromQLRecipe(
            name="Node Memory Pressure",
            query="instance:node_vmstat_pgmajfault:rate5m{instance='NODE'}",
            chart_type="line",
            description="Major page fault rate indicating memory pressure on a node",
            metric="instance:node_vmstat_pgmajfault:rate5m",
            scope="node",
            parameters=["instance"],
        ),
        PromQLRecipe(
            name="Node Disk IO Utilization",
            query="instance_device:node_disk_io_time_seconds:rate5m{instance='NODE'}",
            chart_type="line",
            description="Disk IO time utilization for a specific node",
            metric="instance_device:node_disk_io_time_seconds:rate5m",
            scope="node",
            parameters=["instance"],
        ),
        PromQLRecipe(
            name="Node Disk IO Saturation",
            query="instance_device:node_disk_io_time_weighted_seconds:rate5m{instance='NODE'}",
            chart_type="line",
            description="Weighted disk IO time indicating saturation on a node",
            metric="instance_device:node_disk_io_time_weighted_seconds:rate5m",
            scope="node",
            parameters=["instance"],
        ),
    ],
    # -- Monitoring (3) --
    "monitoring": [
        PromQLRecipe(
            name="Prometheus Targets Down",
            query="100 * (1 - sum(up) / count(up))",
            chart_type="metric_card",
            description="Percentage of Prometheus scrape targets that are down",
            metric="up",
            scope="cluster",
        ),
        PromQLRecipe(
            name="Config Reload Failures",
            query="max_over_time(reloader_last_reload_successful[5m]) == 0",
            chart_type="metric_card",
            description="Whether config reloader has failed recently",
            metric="reloader_last_reload_successful",
            scope="cluster",
        ),
        PromQLRecipe(
            name="Operator Reconcile Error Rate",
            query="sum(rate(prometheus_operator_reconcile_errors_total[5m])) / sum(rate(prometheus_operator_reconcile_operations_total[5m])) * 100",
            chart_type="line",
            description="Percentage of Prometheus operator reconcile operations that error",
            metric="prometheus_operator_reconcile_errors_total",
            scope="cluster",
        ),
    ],
    # -- Operators (4) --
    "operators": [
        PromQLRecipe(
            name="Operators Available",
            query="sum(cluster_operator_up == 1) / count(cluster_operator_up) * 100",
            chart_type="metric_card",
            description="Percentage of cluster operators in available state",
            metric="cluster_operator_up",
            scope="cluster",
        ),
        PromQLRecipe(
            name="Operator Conditions",
            query='cluster_operator_conditions{condition="Degraded"} == 1',
            chart_type="status_list",
            description="Cluster operators currently in degraded condition",
            metric="cluster_operator_conditions",
            scope="cluster",
        ),
        PromQLRecipe(
            name="Available Updates",
            query="cluster_version_available_updates",
            chart_type="metric_card",
            description="Number of available cluster version updates",
            metric="cluster_version_available_updates",
            scope="cluster",
        ),
        PromQLRecipe(
            name="Operator Condition Transitions",
            query="sum by (name) (cluster_operator_condition_transitions)",
            chart_type="bar",
            description="Count of condition transitions per cluster operator",
            metric="cluster_operator_condition_transitions",
            scope="cluster",
        ),
    ],
}


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------


def get_recipe(metric_name: str) -> PromQLRecipe | None:
    """Find first recipe that uses a given metric name."""
    for recipes in RECIPES.values():
        for r in recipes:
            if r.metric == metric_name:
                return r
    return None


def get_recipes_for_category(category: str) -> list[PromQLRecipe]:
    """Get all recipes in a category."""
    return list(RECIPES.get(category, []))


def get_fallback(category: str, scope: str = "cluster") -> PromQLRecipe | None:
    """Get the best fallback recipe for a category+scope."""
    recipes = RECIPES.get(category, [])
    for r in recipes:
        if r.scope == scope:
            return r
    return recipes[0] if recipes else None


# ---------------------------------------------------------------------------
# Learned-query DB layer
# ---------------------------------------------------------------------------


def normalize_query(query: str) -> str:
    """Normalize a PromQL query for hashing — strip namespace/pod/instance values, lowercase."""
    if not query:
        return ""
    q = str(query).lower()
    q = re.sub(r'namespace\s*=~?\s*"[^"]*"', 'namespace="__NS__"', q)
    q = re.sub(r'pod\s*=~?\s*"[^"]*"', 'pod="__POD__"', q)
    q = re.sub(r'instance\s*=~?\s*"[^"]*"', 'instance="__INSTANCE__"', q)
    q = re.sub(r'deployment\s*=~?\s*"[^"]*"', 'deployment="__DEP__"', q)
    return q.strip()


def _detect_category(query: str) -> str:
    """Best-effort category detection from a PromQL query string."""
    q = query.lower()
    if "cpu" in q:
        return "cpu"
    if "memory" in q or "mem_" in q:
        return "memory"
    if "network" in q or "transmit" in q or "receive" in q:
        return "network"
    if "filesystem" in q or "volume" in q or "pvc" in q or "disk" in q:
        return "storage"
    if "apiserver" in q:
        return "control_plane"
    if "etcd" in q:
        return "control_plane"
    if "alerts" in q or "ALERTS" in query:
        return "alerts"
    if "kube_pod" in q or "pod" in q:
        return "pods"
    if "kube_node" in q or "node_" in q:
        return "nodes"
    return ""


def record_query_result(query: str, *, success: bool, series_count: int = 0, category: str = "") -> None:
    """Record a PromQL query result. Fire-and-forget: swallows all exceptions."""
    try:
        if not query:
            return
        from .db import get_database

        db = get_database()
        normalized = normalize_query(query)
        qhash = hashlib.sha256(normalized.encode()).hexdigest()

        # Auto-detect category from query if not provided
        if not category:
            category = _detect_category(query)

        if success:
            db.execute(
                "INSERT INTO promql_queries (query_hash, query_template, category, success_count, last_success, avg_series_count) "
                "VALUES (%s, %s, %s, 1, NOW(), %s) "
                "ON CONFLICT (query_hash) DO UPDATE SET "
                "category = COALESCE(NULLIF(promql_queries.category, ''), %s), "
                "success_count = promql_queries.success_count + 1, "
                "last_success = NOW(), "
                "avg_series_count = (promql_queries.avg_series_count + %s) / 2",
                (qhash, normalized, category, float(series_count), category, float(series_count)),
            )
        else:
            db.execute(
                "INSERT INTO promql_queries (query_hash, query_template, category, failure_count, last_failure) "
                "VALUES (%s, %s, %s, 1, NOW()) "
                "ON CONFLICT (query_hash) DO UPDATE SET "
                "category = COALESCE(NULLIF(promql_queries.category, ''), %s), "
                "failure_count = promql_queries.failure_count + 1, "
                "last_failure = NOW()",
                (qhash, normalized, category, category),
            )
        db.commit()
    except Exception:
        logger.debug("Failed to record query result", exc_info=True)


def get_query_reliability(query_template: str) -> dict | None:
    """Return success/failure counts for a normalized query template."""
    try:
        from .db import get_database

        db = get_database()
        qhash = hashlib.sha256(query_template.encode()).hexdigest()
        row = db.fetchone(
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
        rows = db.fetchall(
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
