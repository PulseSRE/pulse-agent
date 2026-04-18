"""Monitoring and Prometheus/Alertmanager tools."""

from __future__ import annotations

import json
from typing import Any

from kubernetes.client.rest import ApiException

from .. import k8s_client as _kc
from ..decorators import beta_tool
from ..errors import ToolError
from ..prometheus import prometheus_request
from .validators import MAX_RESULTS

# Metrics API uses the CustomObjectsApi to query metrics.k8s.io
_METRICS_GROUP = "metrics.k8s.io"
_METRICS_VERSION = "v1beta1"


@beta_tool
def get_firing_alerts():
    """Get all currently firing alerts from Alertmanager. Returns alert name, severity, namespace, summary, and duration."""

    core = _kc.get_core_client()
    # Try to use the service proxy
    try:
        result = core.connect_get_namespaced_service_proxy_with_path(
            "alertmanager-main:web",
            "openshift-monitoring",
            path="api/v2/alerts",
            _preload_content=False,
        )
        data = json.loads(result.data)
    except Exception:
        # Fallback: try via custom API
        try:
            result = _kc.get_custom_client().get_cluster_custom_object(
                "monitoring.coreos.com", "v1", "alertmanagers", "main"
            )
            return "Alertmanager found but cannot query alerts via this method. Configure ALERTMANAGER_URL."
        except Exception:
            return "Cannot reach Alertmanager. It may not be installed or accessible."

    if not isinstance(data, list):
        return "Unexpected response format from Alertmanager."

    firing = [a for a in data if a.get("status", {}).get("state") == "active"]
    if not firing:
        return "No alerts currently firing."

    lines = []
    items = []
    _severity_to_status = {"critical": "error", "warning": "warning", "info": "info"}
    for alert in sorted(firing, key=lambda a: a.get("labels", {}).get("severity", ""), reverse=True):
        labels = alert.get("labels", {})
        annotations = alert.get("annotations", {})
        name = labels.get("alertname", "unknown")
        severity = labels.get("severity", "?")
        ns = labels.get("namespace", "cluster-wide")
        summary = annotations.get("summary", annotations.get("message", annotations.get("description", "")))[:200]
        starts = alert.get("startsAt", "?")[:19]

        lines.append(f"[{severity.upper()}] {name}  namespace={ns}  since={starts}\n  {summary}")
        items.append(
            {
                "name": f"[{severity.upper()}] {name}",
                "status": _severity_to_status.get(severity.lower(), "warning"),
                "detail": f"{ns} — {summary[:100]}" if summary else ns,
            }
        )

    text = f"Firing alerts ({len(firing)}):\n\n" + "\n\n".join(lines)
    component = (
        {
            "kind": "status_list",
            "title": f"Firing Alerts ({len(items)})",
            "items": items,
        }
        if items
        else None
    )
    return (text, component)


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
def discover_metrics(category: str = "all"):
    """Discover available Prometheus metrics on this cluster. Call this BEFORE
    writing PromQL queries to know which metrics actually exist.

    Args:
        category: One of: 'cpu', 'memory', 'network', 'storage', 'pods',
                  'nodes', 'api_server', 'etcd', 'alerts', 'all'.
    """
    import time as _time

    from ..promql_recipes import RECIPES, get_recipe

    valid_cats = set(_CATEGORY_PREFIXES.keys()) | {"all"}
    if category not in valid_cats:
        return f"Invalid category '{category}'. Available categories: {', '.join(sorted(valid_cats))}"

    now = _time.time()
    if _metric_names_cache["data"] is not None and now - _metric_names_cache["ts"] < 300:
        all_metrics = _metric_names_cache["data"]
    else:
        try:
            data = prometheus_request("api/v1/label/__name__/values", timeout=15)

            if data.get("status") != "success":
                return f"Prometheus error: {data.get('error', 'unknown')}"

            all_metrics = sorted(data.get("data", []))
            _metric_names_cache["data"] = all_metrics
            _metric_names_cache["ts"] = now

        except Exception as e:
            lines = [f"Cannot reach Prometheus ({e}). Using hardcoded recipes:"]
            cat_recipes = RECIPES.get(category, []) if category != "all" else [r for rs in RECIPES.values() for r in rs]
            for r in cat_recipes[:15]:
                lines.append(f"  {r.metric}")
                lines.append(f"    Recipe: {r.query}")
                lines.append(f'    Chart: {r.chart_type} | Title: "{r.name}"')
            return "\n".join(lines)

    if category == "all":
        filtered = all_metrics
    else:
        prefixes = _CATEGORY_PREFIXES[category]
        filtered = [m for m in all_metrics if any(m.startswith(p) or m.startswith(p.rstrip("_")) for p in prefixes)]

    if not filtered:
        return f"No metrics found for category '{category}' (0 of {len(all_metrics)} total metrics matched)."

    lines = [f"Available {category} metrics ({len(filtered)} found):"]
    for metric_name in filtered[:30]:
        recipe = get_recipe(metric_name)
        lines.append(f"  {metric_name}")
        if recipe:
            lines.append(f"    Recipe: {recipe.query}")
            lines.append(f'    Chart: {recipe.chart_type} | Title: "{recipe.name}"')

    if len(filtered) > 30:
        lines.append(f"  ... and {len(filtered) - 30} more")

    return "\n".join(lines)


@beta_tool
def verify_query(query: str):
    """Test a PromQL query against Prometheus to verify it returns data.
    Call this BEFORE using a query in a dashboard to ensure it works.

    Args:
        query: PromQL query to test.
    """
    if not query or not query.strip():
        return "Error: query is empty."

    if any(c in query for c in [";", "\\", "\n", "\r"]):
        return "Error: Invalid characters in query."

    try:
        data = prometheus_request("api/v1/query", params={"query": query}, timeout=15)
    except Exception as e:
        return f"FAIL_UNREACHABLE: Cannot reach Prometheus: {e}"

    if data.get("status") != "success":
        error_msg = data.get("error", "unknown error")
        try:
            from ..promql_recipes import record_query_result

            record_query_result(query, success=False, series_count=0)
        except Exception:
            pass
        return f"FAIL_SYNTAX: {error_msg}"

    results = data.get("data", {}).get("result", [])

    if not results:
        try:
            from ..promql_recipes import record_query_result

            record_query_result(query, success=False, series_count=0)
        except Exception:
            pass
        return "FAIL_NO_DATA: Query returned 0 results. Metric may not exist or labels may be wrong."

    sample = results[0]
    metric_name = sample.get("metric", {}).get("__name__", "")
    value = sample.get("value", [None, ""])[1] if sample.get("value") else ""
    sample_info = f"{metric_name}={value}" if metric_name else f"value={value}"

    try:
        from ..promql_recipes import record_query_result

        record_query_result(query, success=True, series_count=len(results))
    except Exception:
        pass

    return f"PASS: Query returns data ({len(results)} series, sample: {sample_info})"


@beta_tool
def get_prometheus_query(query: str, time_range: str = "1h", title: str = "", description: str = ""):
    """Execute a PromQL query against Prometheus/Thanos and return the results as an interactive chart.

    Args:
        query: PromQL query string, e.g. 'up', 'node_memory_MemAvailable_bytes', 'rate(container_cpu_usage_seconds_total[5m])'.
        time_range: Time range for the query (e.g. '5m', '1h', '24h'). Defaults to '1h'.
        title: Human-readable title for the chart (e.g. 'CPU Usage by Namespace'). If empty, auto-generated from the query.
        description: Description of what to watch for (e.g. 'Spikes above 80% indicate resource pressure').
    """
    if any(c in query for c in [";", "\\", "\n", "\r"]):
        return "Error: Invalid characters in query."

    if not time_range:
        time_range = "1h"

    import time as _time

    _UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    try:
        unit = time_range[-1]
        amount = int(time_range[:-1])
        seconds = amount * _UNITS.get(unit, 3600)
    except (ValueError, IndexError):
        seconds = 3600

    now = int(_time.time())
    step = max(60, seconds // 120)
    query_params: dict[str, str] = {
        "query": query,
        "start": str(now - seconds),
        "end": str(now),
        "step": str(step),
    }

    endpoint = "api/v1/query_range"

    try:
        data = prometheus_request(endpoint, params=query_params, timeout=15)
    except Exception as e:
        return f"Cannot reach Prometheus/Thanos: {e}"

    if data.get("status") != "success":
        try:
            from ..promql_recipes import record_query_result

            record_query_result(query, success=False, series_count=0)
        except Exception:
            pass
        return f"Query error: {data.get('error', 'unknown')}"

    result_type = data.get("data", {}).get("resultType", "")
    results = data.get("data", {}).get("result", [])

    if not results:
        try:
            from ..promql_recipes import record_query_result

            record_query_result(query, success=False, series_count=0)
        except Exception:
            pass
        # Suggest verified recipe alternatives
        try:
            from ..promql_recipes import _detect_category, get_recipes_for_category

            cat = _detect_category(query)
            if cat:
                alternatives = get_recipes_for_category(cat)[:3]
                if alternatives:
                    alt_text = "\n".join(f"  - {r.name}: {r.query}" for r in alternatives)
                    return (
                        f"Query returned no results for: {query}\n\n"
                        f"Try these verified alternatives for '{cat}':\n{alt_text}"
                    )
        except Exception:
            pass
        return f"Query returned no results for: {query}"

    # Default color palette for chart series
    _CHART_COLORS = ["#60a5fa", "#34d399", "#fbbf24", "#f87171", "#a78bfa", "#38bdf8", "#fb923c", "#e879f9"]

    # Generate a human-readable title from PromQL
    def _title_from_query(q: str) -> str:
        q = q.strip()
        # Extract the metric name and grouping
        import re as _re

        # Common patterns -> friendly names
        if "cpu_usage_seconds_total" in q:
            group = _re.search(r"by\s*\(([^)]+)\)", q)
            by = group.group(1) if group else ""
            ns = _re.search(r'namespace="([^"]+)"', q)
            prefix = f"{ns.group(1)} — " if ns else ""
            return f"{prefix}CPU Usage" + (f" by {by}" if by else "")
        if "memory" in q.lower():
            group = _re.search(r"by\s*\(([^)]+)\)", q)
            by = group.group(1) if group else ""
            return "Memory Usage" + (f" by {by}" if by else "")
        if "node_cpu_seconds_total" in q:
            return "Node CPU Utilization"
        if "node_memory" in q:
            return "Node Memory Pressure"
        if "network_receive" in q:
            group = _re.search(r"by\s*\(([^)]+)\)", q)
            by = group.group(1) if group else ""
            return "Network Receive" + (f" by {by}" if by else "")
        if "network_transmit" in q:
            group = _re.search(r"by\s*\(([^)]+)\)", q)
            by = group.group(1) if group else ""
            return "Network Transmit" + (f" by {by}" if by else "")
        if "restart" in q.lower():
            return "Pod Restarts"
        if "ALERTS" in q:
            return "Firing Alerts"
        if "kube_event" in q:
            return "Warning Events"
        if "filesystem" in q or "volume_stats" in q:
            return "Disk Usage"
        if "predict_linear" in q:
            return "Capacity Projection"
        # Fallback: extract the actual metric name (not function wrappers like sum/rate/topk)
        _PROMQL_FUNCS = {
            "sum",
            "avg",
            "min",
            "max",
            "count",
            "rate",
            "irate",
            "increase",
            "topk",
            "bottomk",
            "histogram_quantile",
            "predict_linear",
            "sort",
            "sort_desc",
            "abs",
            "ceil",
            "floor",
            "round",
            "deriv",
            "delta",
            "idelta",
            "changes",
            "resets",
            "vector",
            "scalar",
            "time",
            "label_replace",
            "label_join",
            "avg_over_time",
            "sum_over_time",
            "quantile_over_time",
            "min_over_time",
            "max_over_time",
            "count_over_time",
            "last_over_time",
        }
        # Find metric names (contain underscores, not just function names)
        candidates = _re.findall(r"([a-z][a-z0-9_]{4,})\b", q.lower())
        metric_name = ""
        for c in candidates:
            if c not in _PROMQL_FUNCS and "_" in c:
                metric_name = c
                break
        if metric_name:
            # Clean up metric name into a title
            group = _re.search(r"by\s*\(([^)]+)\)", q)
            by = f" by {group.group(1)}" if group else ""
            title_str = (
                metric_name.replace("_total", "")
                .replace("_seconds", "")
                .replace("kube_", "")
                .replace("container_", "")
                .replace("node_", "Node ")
            )
            return title_str.replace("_", " ").strip().title() + by
        return q[:60]

    def _desc_from_query(q: str, tr: str, count: int) -> str:
        """Generate a useful description explaining why this data matters."""
        if "cpu_usage_seconds_total" in q:
            return "Identifies which workloads consume the most CPU — helps optimize resource requests and spot runaway processes"
        if "memory" in q.lower():
            return (
                "Shows actual memory consumption — useful for right-sizing resource limits and detecting memory leaks"
            )
        if "node_cpu_seconds_total" in q:
            return "Tracks node-level CPU saturation — high utilization may require scaling or workload rebalancing"
        if "node_memory" in q.lower():
            return "Monitors available memory per node — low availability risks OOM kills and pod evictions"
        if "up" in q and "up{" not in q:
            return "Service availability — 1 means up, 0 means the target is down or unreachable"
        if "network_receive" in q:
            return "Inbound network traffic — spikes may indicate unexpected load or DDoS"
        if "network_transmit" in q:
            return "Outbound network traffic — useful for identifying high-bandwidth workloads"
        if "restart" in q.lower():
            return "Container restarts over time — sustained restarts indicate crashlooping or resource issues"
        if "ALERTS" in q:
            return "Firing alert count over time — rising trend indicates degrading cluster health"
        if "filesystem" in q or "volume_stats" in q:
            return "Storage utilization — watch for volumes approaching capacity"
        if "predict_linear" in q:
            return "Linear projection based on recent trends — shows estimated future values"
        return f"{'Time series' if tr else 'Snapshot'} with {count} {'series' if tr else 'results'}"

    def _pick_chart_type(q: str, chart_series: list, raw_results: list, *, is_instant: bool = False) -> str:
        """Pick the best chart type based on query pattern and data shape."""
        q_lower = q.lower()
        num_series = len(chart_series)

        # Pie/donut: categorical data with few items (distribution/proportion queries)
        pie_signals = (
            "distribution",
            "breakdown",
            "proportion",
            "share",
            "by phase",
            "by status",
            "by type",
            "by reason",
            "by severity",
            "by kind",
            "pie",
            "donut",
        )
        if any(s in q_lower for s in pie_signals) and 2 <= num_series <= 10:
            return "donut"
        # Instant queries with "count by" or "sum by" and few results -> donut
        if is_instant and num_series <= 8 and any(p in q_lower for p in ("count by", "sum by", "group by")):
            return "donut"

        # Treemap: hierarchical breakdown with many categories
        if num_series > 10 and any(w in q_lower for w in ("by namespace", "by pod", "by container")):
            return "treemap"

        # Radar: multi-dimensional comparison (e.g., comparing metrics across nodes)
        radar_signals = ("compare", "radar", "spider", "score", "rating")
        if any(s in q_lower for s in radar_signals) and 3 <= num_series <= 8:
            return "radar"

        # Scatter: correlation between two values
        if any(s in q_lower for s in ("scatter", "correlation", "vs ", " vs.")):
            return "scatter"

        # Stacked area: "sum by" queries showing namespace/pod breakdown
        if "sum by" in q_lower and num_series >= 3:
            if "cpu" in q_lower or "memory" in q_lower or "network" in q_lower:
                return "stacked_area"

        # Bar chart: topk queries, comparison across items, or few data points
        if "topk" in q_lower or num_series >= 5:
            if chart_series:
                data = chart_series[0].get("data", [])
                if len(data) <= 5:
                    return "bar"
        # Instant queries with ranked data -> bar
        if is_instant and num_series >= 3:
            return "bar"

        # Area chart: single series utilization/percentage metrics
        if num_series == 1:
            if any(w in q_lower for w in ("percent", "ratio", "utilization", "usage", "100 -")):
                return "area"

        # Stacked bar: count/sum by category (e.g., pod status, alert severity)
        if "count" in q_lower and "by" in q_lower and num_series <= 5:
            return "stacked_bar"

        # Default: line chart for time-series trends
        return "line"

    def _build_chart_component(chart_type: str, chart_series: list, tr: str = "") -> dict:
        """Build a chart component dict. Shared by range and instant query paths."""
        comp: dict = {
            "kind": "chart",
            "chartType": chart_type,
            "title": title or _title_from_query(query),
            "description": description or _desc_from_query(query, tr, len(chart_series)),
            "series": chart_series,
            "height": 300,
            "query": query,
        }
        if tr:
            comp["timeRange"] = tr
        return comp

    def _record_success(series_count: int) -> None:
        """Fire-and-forget query result recording."""
        try:
            from ..promql_recipes import record_query_result

            record_query_result(query, success=True, series_count=series_count)
        except Exception:
            pass

    def _extract_label(metric: dict, index: int) -> str:
        """Extract a display label from a Prometheus metric dict."""
        label_parts = [f"{v}" for k, v in metric.items() if k != "__name__"]
        return (", ".join(label_parts) or metric.get("__name__", f"series-{index}"))[:60]

    lines = []
    if result_type == "matrix":
        # Range query -> build a ChartSpec
        import math

        series = []
        for i, r in enumerate(results[:10]):
            metric = r.get("metric", {})
            label = _extract_label(metric, i)
            values = r.get("values", [])
            points = [[int(float(ts) * 1000), float(val)] for ts, val in values if not math.isnan(float(val))]
            latest = values[-1][1] if values else "?"
            lines.append(f"{label} = {latest} (latest of {len(values)} samples)")
            series.append({"label": label, "data": points, "color": _CHART_COLORS[i % len(_CHART_COLORS)]})

        if len(results) > 10:
            lines.append(f"... and {len(results) - 10} more series (truncated to top 10 for chart)")

        text = "\n".join(lines)
        chart_type = _pick_chart_type(query, series, results)
        component = _build_chart_component(chart_type, series, tr=time_range)
        _record_success(len(series))
        return (text, component)

    else:
        # Instant query (vector) -> build a DataTableSpec or chart
        rows = []
        label_keys = []
        if results:
            first_metric = results[0].get("metric", {})
            label_keys = [k for k in first_metric if k != "__name__"]

        for r in results[:50]:
            metric = r.get("metric", {})
            label_str = ", ".join(f"{k}={v}" for k, v in metric.items() if k != "__name__")
            name = metric.get("__name__", "")
            _ts, val = r.get("value", [0, "?"])
            lines.append(f"{name}{{{label_str}}} = {val}" if label_str else f"{name} = {val}")

            row: dict = {}
            if label_keys:
                for k in label_keys:
                    row[k] = metric.get(k, "")
            else:
                row["metric"] = name or query
            row["value"] = str(val)
            rows.append(row)

        if len(results) > 50:
            lines.append(f"... and {len(results) - 50} more results (truncated)")

        text = "\n".join(lines)

        # Try chart for instant queries with categorical data (pie/donut/bar)
        chart_type_instant: str | None = (
            _pick_chart_type(query, [], results, is_instant=True) if 2 <= len(results) <= 20 else None
        )
        if chart_type_instant and chart_type_instant in ("donut", "pie", "bar", "treemap", "radar"):
            import math

            chart_series = []
            for i, r in enumerate(results[:10]):
                metric = r.get("metric", {})
                label = _extract_label(metric, i)
                _ts, val = r.get("value", [0, "0"])
                try:
                    fval = float(val)
                    if math.isnan(fval):
                        continue
                except (ValueError, TypeError):
                    continue
                chart_series.append(
                    {"label": label, "data": [[0, fval]], "color": _CHART_COLORS[i % len(_CHART_COLORS)]}
                )

            if chart_series:
                component_chart = _build_chart_component(chart_type_instant, chart_series)
                _record_success(len(chart_series))
                return (text, component_chart)

        # Build columns from label keys
        if label_keys:
            columns = [{"id": k, "header": k.replace("_", " ").title()} for k in label_keys]
        else:
            columns = [{"id": "metric", "header": "Metric"}]
        columns.append({"id": "value", "header": "Value"})

        component_table: dict[str, Any] | None = (
            {
                "kind": "data_table",
                "title": title or _title_from_query(query),
                "description": description or _desc_from_query(query, "", len(rows)),
                "columns": columns,
                "rows": rows,
                "query": query,
            }
            if rows
            else None
        )
        try:
            from ..promql_recipes import record_query_result

            record_query_result(query, success=True, series_count=len(rows))
        except Exception:
            pass
        return (text, component_table)


@beta_tool
def get_node_metrics():
    """Get actual CPU and memory usage for all nodes from the metrics API. Requires metrics-server to be installed."""
    from ..units import format_cpu, format_memory, parse_cpu_millicores, parse_memory_bytes

    try:
        result = _kc.get_custom_client().list_cluster_custom_object(_METRICS_GROUP, _METRICS_VERSION, "nodes")
    except ApiException as e:
        if e.status == 404:
            return "Error: Metrics API not available. Is metrics-server installed?"
        return f"Error ({e.status}): {e.reason}"

    # Get node capacity for utilization %
    nodes_result = _kc.safe(lambda: _kc.get_core_client().list_node())
    capacity_map = {}
    if not isinstance(nodes_result, ToolError):
        for node in nodes_result.items:
            alloc = node.status.allocatable or {}
            capacity_map[node.metadata.name] = {
                "cpu_m": parse_cpu_millicores(alloc.get("cpu", "0")),
                "mem_bytes": parse_memory_bytes(alloc.get("memory", "0")),
            }

    lines = []
    rows = []
    for item in result.get("items", []):
        name = item["metadata"]["name"]
        usage = item.get("usage", {})
        cpu_m = parse_cpu_millicores(usage.get("cpu", "0"))
        mem_bytes = parse_memory_bytes(usage.get("memory", "0"))

        cpu_pct_val: float = 0
        mem_pct_val: float = 0
        pct = ""
        if name in capacity_map:
            cap = capacity_map[name]
            cpu_pct_val = (cpu_m / cap["cpu_m"] * 100) if cap["cpu_m"] > 0 else 0
            mem_pct_val = (mem_bytes / cap["mem_bytes"] * 100) if cap["mem_bytes"] > 0 else 0
            pct = f"  CPU%={cpu_pct_val:.0f}%  Mem%={mem_pct_val:.0f}%"

        lines.append(f"{name}  CPU={format_cpu(cpu_m)}  Memory={format_memory(mem_bytes)}{pct}")
        rows.append(
            {
                "name": name,
                "cpu": format_cpu(cpu_m),
                "memory": format_memory(mem_bytes),
                "cpu_pct": f"{cpu_pct_val:.0f}%",
                "mem_pct": f"{mem_pct_val:.0f}%",
            }
        )

    text = "\n".join(lines) or "No node metrics found."
    component = (
        {
            "kind": "data_table",
            "title": f"Node Metrics ({len(rows)})",
            "columns": [
                {"id": "name", "header": "Node"},
                {"id": "cpu", "header": "CPU Usage"},
                {"id": "cpu_pct", "header": "CPU %"},
                {"id": "memory", "header": "Memory Usage"},
                {"id": "mem_pct", "header": "Memory %"},
            ],
            "rows": rows,
        }
        if rows
        else None
    )
    return (text, component)


@beta_tool
def get_pod_metrics(namespace: str = "default", sort_by: str = "cpu"):
    """Get actual CPU and memory usage for pods from the metrics API. Requires metrics-server.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' for all namespaces.
        sort_by: Sort results by 'cpu' or 'memory'. Shows top consumers first.
    """
    from ..units import format_cpu, format_memory, parse_cpu_millicores, parse_memory_bytes

    try:
        if namespace.upper() == "ALL":
            result = _kc.get_custom_client().list_cluster_custom_object(_METRICS_GROUP, _METRICS_VERSION, "pods")
        else:
            result = _kc.get_custom_client().list_namespaced_custom_object(
                _METRICS_GROUP, _METRICS_VERSION, namespace, "pods"
            )
    except ApiException as e:
        if e.status == 404:
            return "Error: Metrics API not available. Is metrics-server installed?"
        return f"Error ({e.status}): {e.reason}"

    pods = []
    for item in result.get("items", []):
        ns = item["metadata"]["namespace"]
        name = item["metadata"]["name"]
        total_cpu_m = 0
        total_mem_bytes = 0
        for container in item.get("containers", []):
            usage = container.get("usage", {})
            total_cpu_m += parse_cpu_millicores(usage.get("cpu", "0"))
            total_mem_bytes += parse_memory_bytes(usage.get("memory", "0"))

        pods.append(
            {
                "ns": ns,
                "name": name,
                "cpu_m": total_cpu_m,
                "mem_bytes": total_mem_bytes,
                "cpu_str": format_cpu(total_cpu_m),
                "mem_str": format_memory(total_mem_bytes),
            }
        )

    if sort_by == "memory":
        pods.sort(key=lambda p: p["mem_bytes"], reverse=True)
    else:
        pods.sort(key=lambda p: p["cpu_m"], reverse=True)

    lines = []
    rows = []
    for p in pods[:MAX_RESULTS]:
        lines.append(f"{p['ns']}/{p['name']}  CPU={p['cpu_str']}  Memory={p['mem_str']}")
        rows.append(
            {
                "namespace": p["ns"],
                "name": p["name"],
                "cpu": p["cpu_str"],
                "memory": p["mem_str"],
            }
        )
    total = len(pods)
    if total > MAX_RESULTS:
        lines.append(f"... and {total - MAX_RESULTS} more pods (truncated)")

    text = "\n".join(lines) or "No pod metrics found."
    sort_label = "CPU" if sort_by != "memory" else "Memory"
    component = (
        {
            "kind": "data_table",
            "title": f"Pod Metrics — Top by {sort_label} ({len(rows)})",
            "columns": [
                {"id": "namespace", "header": "Namespace"},
                {"id": "name", "header": "Pod"},
                {"id": "cpu", "header": "CPU"},
                {"id": "memory", "header": "Memory"},
            ],
            "rows": rows,
        }
        if rows
        else None
    )
    return (text, component)
