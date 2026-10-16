"""Evaluate PromQL over series derived from the scenario.

The view designer writes its own queries, and the UI re-runs them for every
chart. Canned answers keyed on query text would only work for queries someone
anticipated, so this evaluates the expression: selectors, ``rate``/``increase``
and the ``*_over_time`` family, aggregations with ``by``/``without``,
``topk``/``bottomk``, arithmetic, comparisons and set operators, with
``on``/``ignoring`` matching. A metric the scenario does not model returns no
series, as a real Prometheus would for a metric that does not exist.

Counters are modelled by their per-second rate; a bare counter selector returns
rate x uptime. Gauges drift gently over time (``scenario.wave``) so trend charts
are not flat lines.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Callable
from typing import Any

import promql_parser

from . import scenario as sc

Labels = dict[str, str]
Sample = tuple[Labels, float, float]  # labels, instant value, per-second rate (0 for gauges)
Vector = list[tuple[Labels, float]]

UPTIME_SECONDS = 15 * 86400
GIB = 1024**3
MIB = 1024**2


# ---------------------------------------------------------------------------
# Series model
# ---------------------------------------------------------------------------


def _drift(base: float, t: float, *key: object, pct: float = 0.08) -> float:
    return sc.wave(base, base * pct, t, *key)


def _running_pods() -> list[tuple]:
    return [p for p in sc.PODS if p[4] == "Running"]


def _pod_labels(p: tuple, **extra: str) -> Labels:
    container = "redis" if p[1] == "cache" else p[1]
    return {"namespace": p[2], "pod": p[0], "container": container, "node": p[3], **extra}


def _limits(p: tuple) -> tuple[float, float]:
    dep = next((d for d in sc.DEPLOYMENTS if d[0] == p[1] and d[1] == p[2]), None)
    return (dep[4], dep[5]) if dep else (1.0, 1024)


def _restart_rate(p: tuple) -> float:
    # The crash-looping cache restarted 14 times in about three hours.
    return p[6] / (3 * 3600) if p[5] == "CrashLoopBackOff" else p[6] / UPTIME_SECONDS


def _counter(labels: Labels, rate: float) -> Sample:
    return labels, rate * UPTIME_SECONDS, rate


def _gauge(labels: Labels, value: float) -> Sample:
    return labels, value, 0.0


def _series_for(name: str, t: float) -> list[Sample]:
    nodes = sc.NODES
    pods = _running_pods()
    if name == "container_cpu_usage_seconds_total":
        return [_counter(_pod_labels(p, cpu="total"), _drift(p[7], t, name, p[0])) for p in pods]
    if name == "container_memory_working_set_bytes" or name == "container_memory_usage_bytes":
        return [_gauge(_pod_labels(p), _drift(p[8] * MIB, t, name, p[0], pct=0.04)) for p in pods]
    if name in ("container_spec_memory_limit_bytes",):
        return [_gauge(_pod_labels(p), _limits(p)[1] * MIB) for p in pods]
    if name in ("kube_pod_container_resource_limits", "kube_pod_container_resource_requests"):
        share = 1.0 if name.endswith("limits") else 0.5
        out = []
        for p in pods:
            cpu, mem = _limits(p)
            out.append(_gauge(_pod_labels(p, resource="cpu", unit="core"), cpu * share))
            out.append(_gauge(_pod_labels(p, resource="memory", unit="byte"), mem * MIB * share))
        return out
    if name == "container_cpu_cfs_throttled_periods_total" or name == "container_cpu_cfs_periods_total":
        # checkout-api is throttled ~38% of periods; others barely.
        periods = 10.0
        out = []
        for p in pods:
            frac = 0.38 if p[1] == "checkout-api" and p[2] == "production" else 0.02
            out.append(_counter(_pod_labels(p), periods * (frac if "throttled" in name else 1.0)))
        return out
    if name in ("container_network_receive_bytes_total", "container_network_transmit_bytes_total"):
        factor = 2.4e6 if "receive" in name else 1.6e6
        return [_counter(_pod_labels(p, interface="eth0"), _drift(p[7] * factor, t, name, p[0])) for p in pods]
    if name.startswith("container_network_") and name.endswith("_total"):
        return [_counter(_pod_labels(p, interface="eth0"), 0.0) for p in pods]
    if name == "kube_pod_container_status_restarts_total":
        return [_counter(_pod_labels(p), _restart_rate(p)) for p in sc.PODS]
    if name == "kube_pod_container_status_waiting_reason":
        return [_gauge(_pod_labels(p, reason=p[5]), 1.0) for p in sc.PODS if p[5] == "CrashLoopBackOff"]
    if name == "kube_pod_status_phase":
        phases = ("Pending", "Running", "Succeeded", "Failed", "Unknown")
        return [
            _gauge({"namespace": p[2], "pod": p[0], "phase": ph}, 1.0 if p[4] == ph else 0.0)
            for p in sc.PODS
            for ph in phases
        ]
    if name == "kube_pod_status_ready":
        out = []
        for p in sc.PODS:
            ready = p[4] == "Running" and not p[5]
            for cond in ("true", "false"):
                out.append(
                    _gauge(
                        {"namespace": p[2], "pod": p[0], "condition": cond}, 1.0 if (cond == "true") == ready else 0.0
                    )
                )
        return out
    if name == "kube_pod_info":
        return [
            _gauge(
                {"namespace": p[2], "pod": p[0], "node": p[3] if p[4] != "Pending" else "", "created_by_name": p[1]},
                1.0,
            )
            for p in sc.PODS
        ]
    if name == "kube_node_status_condition":
        out = []
        for n in nodes:
            for cond in ("Ready", "MemoryPressure", "DiskPressure", "PIDPressure"):
                on = n[6] if cond == "Ready" else n[7] == cond
                for status in ("true", "false", "unknown"):
                    value = 1.0 if (status == "true" and on) or (status == "false" and not on) else 0.0
                    out.append(_gauge({"node": n[0], "condition": cond, "status": status}, value))
        return out
    if name in ("kube_node_status_allocatable", "kube_node_status_capacity"):
        out = []
        for n in nodes:
            out.append(_gauge({"node": n[0], "resource": "cpu", "unit": "core"}, float(n[2])))
            out.append(_gauge({"node": n[0], "resource": "memory", "unit": "byte"}, n[3] * GIB))
            out.append(_gauge({"node": n[0], "resource": "pods", "unit": "integer"}, 250.0))
        return out
    if name == "kube_node_info":
        return [
            _gauge({"node": n[0], "kubelet_version": "v1.31.6", "internal_ip": f"10.0.0.{i + 10}"}, 1.0)
            for i, n in enumerate(nodes)
        ]
    if name == "node_cpu_seconds_total":
        out = []
        for n in nodes:
            busy = _drift(n[4] / 100, t, name, n[0], pct=0.06)
            for cpu in range(n[2]):
                inst = {"instance": n[0], "node": n[0], "cpu": str(cpu)}
                out.append(_counter({**inst, "mode": "idle"}, max(0.0, 1 - busy)))
                out.append(_counter({**inst, "mode": "user"}, busy * 0.7))
                out.append(_counter({**inst, "mode": "system"}, busy * 0.3))
        return out
    if name in ("node_memory_MemTotal_bytes", "node_memory_MemAvailable_bytes"):
        out = []
        for n in nodes:
            total = n[3] * GIB
            used = _drift(n[5] / 100, t, name, n[0], pct=0.03) * total
            out.append(_gauge({"instance": n[0], "node": n[0]}, total if "Total" in name else total - used))
        return out
    if name in ("node_filesystem_size_bytes", "node_filesystem_avail_bytes", "node_filesystem_free_bytes"):
        out = []
        for i, n in enumerate(nodes):
            size = 120 * GIB
            free = size * (0.62 - 0.08 * i)
            labels = {"instance": n[0], "node": n[0], "device": "/dev/nvme0n1p4", "mountpoint": "/", "fstype": "xfs"}
            out.append(_gauge(labels, size if "size" in name else free))
        return out
    if name == "node_load1":
        return [_gauge({"instance": n[0], "node": n[0]}, n[2] * n[4] / 100) for n in nodes]
    if name in (
        "kube_deployment_status_replicas",
        "kube_deployment_spec_replicas",
        "kube_deployment_status_replicas_available",
        "kube_deployment_status_replicas_ready",
        "kube_deployment_status_replicas_unavailable",
        "kube_deployment_status_replicas_updated",
    ):
        out = []
        for d in sc.DEPLOYMENTS:
            pods_of = [p for p in sc.PODS if p[1] == d[0] and p[2] == d[1]]
            n_ready = sum(1 for p in pods_of if p[4] == "Running" and not p[5])
            value = {
                "kube_deployment_spec_replicas": d[2],
                "kube_deployment_status_replicas": len(pods_of),
                "kube_deployment_status_replicas_updated": len(pods_of),
                "kube_deployment_status_replicas_available": n_ready,
                "kube_deployment_status_replicas_ready": n_ready,
                "kube_deployment_status_replicas_unavailable": max(0, d[2] - n_ready),
            }[name]
            out.append(_gauge({"namespace": d[1], "deployment": d[0]}, float(value)))
        return out
    if name in (
        "kube_horizontalpodautoscaler_status_current_replicas",
        "kube_horizontalpodautoscaler_spec_max_replicas",
        "kube_horizontalpodautoscaler_spec_min_replicas",
        "kube_horizontalpodautoscaler_status_desired_replicas",
    ):
        value = {"current": 2, "desired": 2, "max": 4, "min": 2}[name.split("_")[-2]]
        return [_gauge({"namespace": "production", "horizontalpodautoscaler": "checkout-api"}, float(value))]
    if name == "kube_namespace_status_phase":
        return [_gauge({"namespace": ns, "phase": "Active"}, 1.0) for ns in sc.NAMESPACES]
    if name in ("kube_persistentvolumeclaim_status_phase", "kube_persistentvolume_status_phase"):
        claims = [
            ("production", "cache-data"),
            ("production", "search-index"),
            ("openshift-monitoring", "prometheus-db"),
        ]
        out = []
        for ns, claim in claims:
            for phase in ("Bound", "Pending", "Lost"):
                labels = {"namespace": ns, "persistentvolumeclaim": claim, "phase": phase}
                if name.startswith("kube_persistentvolume_status"):
                    labels = {"persistentvolume": f"pv-{claim}", "phase": "Bound" if phase == "Bound" else phase}
                out.append(_gauge(labels, 1.0 if phase == "Bound" else 0.0))
        return out
    if name in ("kubelet_volume_stats_used_bytes", "kubelet_volume_stats_capacity_bytes"):
        vols = [
            ("production", "cache-data", 10, 4.1),
            ("production", "search-index", 50, 31.0),
            ("openshift-monitoring", "prometheus-db", 100, 87.0),
        ]
        return [
            _gauge({"namespace": ns, "persistentvolumeclaim": c}, (cap if "capacity" in name else used) * GIB)
            for ns, c, cap, used in vols
        ]
    if name == "ALERTS":
        return [
            _gauge(
                {
                    "alertname": a[0],
                    "alertstate": "firing",
                    "severity": a[1],
                    **({"namespace": a[2], "pod": a[3]} if a[2] else {"node": a[3]}),
                },
                1.0,
            )
            for a in sc.ALERTS
        ]
    if name == "kube_event_count":
        return [_gauge({"namespace": e[0], "reason": e[3], "type": e[2]}, float(e[5])) for e in sc.EVENTS]
    if name == "cluster_operator_up":
        ops = ["authentication", "console", "dns", "ingress", "monitoring", "network", "storage"]
        return [_gauge({"name": op}, 1.0) for op in ops]
    if name == "cluster_operator_conditions":
        return [_gauge({"name": "monitoring", "condition": "Degraded"}, 1.0)]
    if name == "up":
        jobs = ["apiserver", "kubelet", "node-exporter", "kube-state-metrics", "etcd", "prometheus-k8s"]
        out = [_gauge({"job": j, "instance": n[0]}, 1.0) for j in jobs for n in nodes]
        out.append(_gauge({"job": "cache-exporter", "instance": "cache-7f9d8b-h3jxw", "namespace": "production"}, 0.0))
        return out
    if name == "apiserver_request_total":
        mix = {
            ("GET", "200"): 140.0,
            ("LIST", "200"): 22.0,
            ("WATCH", "200"): 6.0,
            ("POST", "201"): 9.0,
            ("PATCH", "200"): 4.0,
            ("GET", "404"): 2.5,
            ("GET", "500"): 0.04,
        }
        return [
            _counter({"verb": v, "code": c, "resource": "pods"}, _drift(r, t, name, v, c)) for (v, c), r in mix.items()
        ]
    if name == "etcd_server_has_leader":
        return [_gauge({"instance": n[0]}, 1.0) for n in nodes if "master" in n[1]]
    if name.endswith("_bucket"):
        # Histograms are answered at the histogram_quantile call; expose one bucket set.
        return [_counter({"le": le}, 1.0) for le in ("0.05", "0.1", "0.25", "0.5", "1", "+Inf")]
    return []


# Representative p50-p99 latencies for histogram_quantile, by histogram name.
_QUANTILES = {
    "apiserver_request_duration_seconds_bucket": 0.21,
    "scheduler_e2e_scheduling_duration_seconds_bucket": 0.014,
    "etcd_disk_wal_fsync_duration_seconds_bucket": 0.006,
    "http_request_duration_seconds_bucket": 0.32,
}


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


class _Matrix:
    """A range selector: samples plus the window, consumed by a range function."""

    def __init__(self, samples: list[Sample], seconds: float):
        self.samples = samples
        self.seconds = seconds


def _match(op: str, actual: str, wanted: str) -> bool:
    if op.endswith("Equal") and not op.endswith("NotEqual"):
        return actual == wanted
    if op.endswith("NotEqual"):
        return actual != wanted
    if op.endswith("NotRe"):
        return re.fullmatch(wanted, actual) is None
    if op.endswith("Re"):
        return re.fullmatch(wanted, actual) is not None
    return True


def _select(node: Any, t: float) -> list[Sample]:
    matchers = [(m.name, str(m.op), m.value) for m in node.matchers.matchers]
    names = [node.name] if node.name else []
    name_matchers = [m for m in matchers if m[0] == "__name__"]
    if not names:
        # {__name__=~"..."}: expand over the metrics the model knows.
        names = [n for n in KNOWN_METRICS if all(_match(op, n, v) for _, op, v in name_matchers)]
    out: list[Sample] = []
    for name in names:
        for labels, value, rate in _series_for(name, t):
            if all(_match(op, labels.get(k, ""), v) for k, op, v in matchers if k != "__name__"):
                out.append(({"__name__": name, **labels}, value, rate))
    return out


def _strip_name(labels: Labels) -> Labels:
    return {k: v for k, v in labels.items() if k != "__name__"}


def _num(x: Any) -> float:
    return float(getattr(x, "val", x))


def _group_key(labels: Labels, modifier: Any) -> tuple:
    if modifier is None:
        return ()
    names = list(modifier.labels)
    if str(modifier.type).endswith("Without"):
        keep = {k: v for k, v in labels.items() if k not in names and k != "__name__"}
    else:
        keep = {k: labels[k] for k in names if k in labels}
    return tuple(sorted(keep.items()))


def _aggregate(node: Any, t: float) -> Vector:
    op = str(node.op)
    vec = _as_vector(_eval(node.expr, t))
    groups: dict[tuple, list[tuple[Labels, float]]] = defaultdict(list)
    for labels, value in vec:
        groups[_group_key(labels, node.modifier)].append((labels, value))
    if op in ("topk", "bottomk"):
        k = int(_num(_eval(node.param, t)) if not isinstance(node.param, (int, float)) else node.param)
        out: Vector = []
        for members in groups.values():
            ranked = sorted(members, key=lambda m: m[1], reverse=op == "topk")
            out.extend((_strip_name(lbl), v) for lbl, v in ranked[:k])
        return out
    result: Vector = []
    for key, members in groups.items():
        values = [v for _, v in members if not math.isnan(v)]
        if not values:
            continue
        if op == "sum":
            agg = sum(values)
        elif op == "avg":
            agg = sum(values) / len(values)
        elif op == "max":
            agg = max(values)
        elif op == "min":
            agg = min(values)
        elif op == "count":
            agg = float(len(values))
        elif op == "quantile":
            q = _num(_eval(node.param, t))
            ordered = sorted(values)
            agg = ordered[min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))]
        elif op in ("stddev", "stdvar"):
            mean = sum(values) / len(values)
            var = sum((v - mean) ** 2 for v in values) / len(values)
            agg = var if op == "stdvar" else math.sqrt(var)
        elif op == "group":
            agg = 1.0
        else:
            agg = sum(values)
        result.append((dict(key), agg))
    return result


_RANGE_FUNCS = {
    "rate",
    "irate",
    "increase",
    "delta",
    "idelta",
    "deriv",
    "avg_over_time",
    "max_over_time",
    "min_over_time",
    "sum_over_time",
    "count_over_time",
    "last_over_time",
    "quantile_over_time",
    "stddev_over_time",
    "predict_linear",
    "changes",
    "resets",
    "present_over_time",
}


def _range_func(name: str, matrix: _Matrix, args: list[Any], t: float) -> Vector:
    out: Vector = []
    for labels, value, rate in matrix.samples:
        lbl = _strip_name(labels) if name not in ("last_over_time",) else labels
        if name in ("rate", "irate"):
            v = rate
        elif name == "increase":
            v = rate * matrix.seconds
        elif name in ("delta", "idelta", "deriv", "changes", "resets"):
            v = 0.0
        elif name == "count_over_time":
            v = matrix.seconds / 30
        elif name == "sum_over_time":
            v = value * matrix.seconds / 30
        elif name == "present_over_time":
            v = 1.0
        elif name == "predict_linear":
            horizon = _num(args[1]) if len(args) > 1 else 0.0
            # A slight upward drift so capacity forecasts have something to show.
            v = value * (1 + 0.02 * horizon / 86400)
        else:
            v = value
        out.append((lbl, v))
    return out


def _call(node: Any, t: float) -> Any:
    name = node.func.name
    args = list(node.args)
    if name in _RANGE_FUNCS:
        if name in ("quantile_over_time",):
            return _range_func(name, _eval(args[1], t), args, t)
        matrix = _eval(args[0], t)
        if isinstance(matrix, _Matrix):
            # Positional like the PromQL call, with the matrix itself at index 0.
            return _range_func(name, matrix, [None, *(_eval(a, t) for a in args[1:])], t)
        return _as_vector(matrix)
    if name == "vector":
        return [({}, _num(_eval(args[0], t)))]
    if name == "scalar":
        vec = _as_vector(_eval(args[0], t))
        return vec[0][1] if len(vec) == 1 else float("nan")
    if name == "time":
        return t
    if name == "histogram_quantile":
        inner = args[1]
        hist = _histogram_name(inner) or ""
        base = _QUANTILES.get(hist, 0.1) * (0.6 + _num(_eval(args[0], t)) * 0.5)
        vec = _as_vector(_eval(inner, t))
        groups = {tuple(sorted((k, v) for k, v in lbl.items() if k not in ("le", "__name__"))) for lbl, _ in vec}
        return [(dict(g), _drift(base, t, hist, g)) for g in groups] if groups else []
    if name == "absent":
        return [] if _as_vector(_eval(args[0], t)) else [({}, 1.0)]
    vec_funcs: dict[str, Callable[[float], float]] = {
        "abs": abs,
        "ceil": math.ceil,
        "floor": math.floor,
        "exp": math.exp,
        "sqrt": math.sqrt,
        "ln": lambda v: math.log(v) if v > 0 else float("nan"),
        "log2": lambda v: math.log2(v) if v > 0 else float("nan"),
        "log10": lambda v: math.log10(v) if v > 0 else float("nan"),
    }
    if name in vec_funcs:
        return [(_strip_name(lbl), vec_funcs[name](v)) for lbl, v in _as_vector(_eval(args[0], t))]
    if name == "round":
        to = _num(_eval(args[1], t)) if len(args) > 1 else 1.0
        return [(_strip_name(lbl), round(v / to) * to) for lbl, v in _as_vector(_eval(args[0], t))]
    if name in ("clamp_min", "clamp_max", "clamp"):
        vec = _as_vector(_eval(args[0], t))
        lo = _num(_eval(args[1], t)) if name in ("clamp_min", "clamp") else -math.inf
        hi = _num(_eval(args[-1], t)) if name in ("clamp_max", "clamp") else math.inf
        return [(_strip_name(lbl), min(hi, max(lo, v))) for lbl, v in vec]
    if name in ("sort", "sort_desc"):
        return sorted(_as_vector(_eval(args[0], t)), key=lambda s: s[1], reverse=name == "sort_desc")
    if name == "label_replace" and len(args) == 5:
        dst, repl, src, regex = (_eval(a, t) for a in args[1:])
        out = []
        for lbl, v in _as_vector(_eval(args[0], t)):
            m = re.fullmatch(regex, lbl.get(src, ""))
            new = dict(lbl)
            if m:
                new[dst] = m.expand(repl.replace("$", "\\"))
            out.append((new, v))
        return out
    # Anything else: pass the first vector argument through rather than fail the query.
    for a in args:
        value = _eval(a, t)
        if isinstance(value, list):
            return value
    return []


def _histogram_name(node: Any) -> str | None:
    kind = type(node).__name__
    if kind == "VectorSelector":
        return node.name
    for attr in ("expr", "lhs", "rhs", "vector_selector"):
        child = getattr(node, attr, None)
        if child is not None:
            found = _histogram_name(child)
            if found:
                return found
    for child in getattr(node, "args", []) or []:
        found = _histogram_name(child)
        if found:
            return found
    return None


def _arith(op: str, a: float, b: float) -> float:
    try:
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op == "/":
            return a / b if b else (math.nan if a == 0 else math.copysign(math.inf, a))
        if op == "%":
            return math.fmod(a, b) if b else math.nan
        if op == "^":
            return a**b
        if op == "atan2":
            return math.atan2(a, b)
    except (OverflowError, ValueError):
        return math.nan
    raise ValueError(op)


_COMPARE = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
}


def _signature(labels: Labels, matching: Any) -> tuple:
    clean = _strip_name(labels)
    if matching is None:
        return tuple(sorted(clean.items()))
    names = set(matching.labels)
    if str(matching.type).endswith("Include"):
        return tuple(sorted((k, v) for k, v in clean.items() if k in names))
    return tuple(sorted((k, v) for k, v in clean.items() if k not in names))


def _binary(node: Any, t: float) -> Any:
    op = str(node.op)
    modifier = node.modifier
    return_bool = bool(modifier and modifier.return_bool)
    matching = modifier.matching if modifier else None
    lhs, rhs = _eval(node.lhs, t), _eval(node.rhs, t)
    lhs_vec, rhs_vec = isinstance(lhs, list), isinstance(rhs, list)

    if op in ("and", "or", "unless"):
        lv, rv = _as_vector(lhs), _as_vector(rhs)
        rsigs = {_signature(lbl, matching) for lbl, _ in rv}
        if op == "and":
            return [s for s in lv if _signature(s[0], matching) in rsigs]
        if op == "unless":
            return [s for s in lv if _signature(s[0], matching) not in rsigs]
        lsigs = {_signature(lbl, matching) for lbl, _ in lv}
        return lv + [s for s in rv if _signature(s[0], matching) not in lsigs]

    def combine(a: float, b: float, keep: float) -> float | None:
        if op in _COMPARE:
            hit = _COMPARE[op](a, b)
            if return_bool:
                return 1.0 if hit else 0.0
            return keep if hit else None
        return _arith(op, a, b)

    if not lhs_vec and not rhs_vec:
        value = combine(float(lhs), float(rhs), float(lhs))
        return 0.0 if value is None else value
    if lhs_vec and not rhs_vec:
        out = []
        for lbl, v in lhs:
            r = combine(v, float(rhs), v)
            if r is not None:
                out.append((_strip_name(lbl) if op not in _COMPARE or return_bool else lbl, r))
        return out
    if rhs_vec and not lhs_vec:
        out = []
        for lbl, v in rhs:
            r = combine(float(lhs), v, v)
            if r is not None:
                out.append((_strip_name(lbl) if op not in _COMPARE or return_bool else lbl, r))
        return out
    # vector op vector: match on signature; the "one" side may be either.
    right: dict[tuple, list[tuple[Labels, float]]] = defaultdict(list)
    for lbl, v in rhs:
        right[_signature(lbl, matching)].append((lbl, v))
    group_right = modifier is not None and str(modifier.card).endswith("OneToMany")
    out = []
    if group_right:
        left = {_signature(lbl, matching): (lbl, v) for lbl, v in lhs}
        for sig, members in right.items():
            if sig in left:
                for lbl, v in members:
                    r = combine(left[sig][1], v, left[sig][1])
                    if r is not None:
                        out.append((_strip_name(lbl), r))
        return out
    for lbl, v in lhs:
        matches = right.get(_signature(lbl, matching))
        if not matches:
            continue
        r = combine(v, matches[0][1], v)
        if r is not None:
            out.append((_strip_name(lbl) if op not in _COMPARE or return_bool else lbl, r))
    return out


def _as_vector(value: Any) -> Vector:
    if isinstance(value, _Matrix):
        return [(lbl, v) for lbl, v, _ in value.samples]
    if isinstance(value, list):
        return value
    return [({}, float(value))]


def _eval(node: Any, t: float) -> Any:
    kind = type(node).__name__
    if kind == "NumberLiteral":
        return float(node.val)
    if kind == "StringLiteral":
        return node.val
    if kind == "ParenExpr":
        return _eval(node.expr, t)
    if kind == "UnaryExpr":
        value = _eval(node.expr, t)
        return [(_strip_name(lbl), -v) for lbl, v in value] if isinstance(value, list) else -value
    if kind == "VectorSelector":
        return [(lbl, v) for lbl, v, _ in _select(node, t)]
    if kind == "MatrixSelector":
        return _Matrix(_select(node.vector_selector, t), node.range.total_seconds())
    if kind == "SubqueryExpr":
        return _Matrix([(lbl, v, 0.0) for lbl, v in _as_vector(_eval(node.expr, t))], node.range.total_seconds())
    if kind == "AggregateExpr":
        return _aggregate(node, t)
    if kind == "Call":
        return _call(node, t)
    if kind == "BinaryExpr":
        return _binary(node, t)
    raise ValueError(f"unsupported PromQL node {kind}")


KNOWN_METRICS = [
    "ALERTS",
    "apiserver_request_total",
    "cluster_operator_conditions",
    "cluster_operator_up",
    "container_cpu_cfs_periods_total",
    "container_cpu_cfs_throttled_periods_total",
    "container_cpu_usage_seconds_total",
    "container_memory_usage_bytes",
    "container_memory_working_set_bytes",
    "container_network_receive_bytes_total",
    "container_network_transmit_bytes_total",
    "container_spec_memory_limit_bytes",
    "etcd_server_has_leader",
    "kube_deployment_spec_replicas",
    "kube_deployment_status_replicas",
    "kube_deployment_status_replicas_available",
    "kube_deployment_status_replicas_unavailable",
    "kube_event_count",
    "kube_horizontalpodautoscaler_spec_max_replicas",
    "kube_horizontalpodautoscaler_status_current_replicas",
    "kube_namespace_status_phase",
    "kube_node_info",
    "kube_node_status_allocatable",
    "kube_node_status_capacity",
    "kube_node_status_condition",
    "kube_persistentvolumeclaim_status_phase",
    "kube_pod_container_resource_limits",
    "kube_pod_container_resource_requests",
    "kube_pod_container_status_restarts_total",
    "kube_pod_container_status_waiting_reason",
    "kube_pod_info",
    "kube_pod_status_phase",
    "kube_pod_status_ready",
    "kubelet_volume_stats_capacity_bytes",
    "kubelet_volume_stats_used_bytes",
    "node_cpu_seconds_total",
    "node_filesystem_avail_bytes",
    "node_filesystem_size_bytes",
    "node_load1",
    "node_memory_MemAvailable_bytes",
    "node_memory_MemTotal_bytes",
    "up",
]


def evaluate(query: str, t: float) -> tuple[str, Any]:
    """Return (resultType, result) in the shape of Prometheus's instant query API."""
    value = _eval(promql_parser.parse(query), t)
    if isinstance(value, _Matrix):
        value = _as_vector(value)
    if isinstance(value, list):
        return "vector", [(lbl, v) for lbl, v in value if not math.isnan(v) and not math.isinf(v)]
    if isinstance(value, str):
        return "string", value
    return "scalar", float(value)
