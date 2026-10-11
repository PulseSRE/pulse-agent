"""Small, explicit synthetic memory-view environment; never contacts a service."""

from __future__ import annotations

import copy
import json
import re


def _memory_query(query: str, series: list[dict]) -> list[dict] | None:
    """Support this fixture's metric, simple filters and namespace/total sums."""
    source = query.strip()
    grouping = None
    prefix = re.fullmatch(r"sum\s+by\s*\(\s*namespace\s*\)\s*\((.*)\)", source)
    suffix = re.fullmatch(r"sum\s*\((.*)\)\s*by\s*\(\s*namespace\s*\)", source)
    total = re.fullmatch(r"sum\s*\((.*)\)", source)
    grouped = prefix or suffix
    if grouped is not None:
        source = grouped.group(1).strip()
        grouping = "namespace"
    elif total:
        source = total.group(1).strip()
        grouping = "total"
    metric = re.fullmatch(r"container_memory_working_set_bytes\s*(?:\{(.*)\})?", source)
    if not metric:
        return None
    selectors = metric.group(1) or ""
    matches = list(re.finditer(r'([a-zA-Z_][a-zA-Z_0-9]*)\s*(=~|!~|!=|=)\s*"([^"\\]*)"\s*,?', selectors))
    if re.sub(r'([a-zA-Z_][a-zA-Z_0-9]*)\s*(=~|!~|!=|=)\s*"([^"\\]*)"\s*,?', "", selectors).strip():
        return None
    rows = copy.deepcopy(series)
    for selector in matches:
        label, operator, value = selector.groups()
        if label not in {"namespace", "container", "image", "pod"}:
            return None
        try:

            def keep(row):
                actual = row["labels"].get(label, "")
                hit = bool(re.fullmatch(value, actual)) if operator in ("=~", "!~") else actual == value
                return not hit if operator in ("!=", "!~") else hit

            rows = [row for row in rows if keep(row)]
        except re.error:
            return None
    if grouping == "total":
        return [{"labels": {}, "bytes": sum(row["bytes"] for row in rows)}] if rows else []
    if grouping == "namespace":
        sums: dict[str, float] = {}
        for row in rows:
            ns = row["labels"]["namespace"]
            sums[ns] = sums.get(ns, 0) + row["bytes"]
        return [{"labels": {"namespace": ns}, "bytes": value} for ns, value in sums.items()]
    return rows


def call(name: str, inputs: dict, state: dict) -> str | None:
    """Return None for unsupported requests; mutations need generated evidence."""
    if name in {"get_prometheus_query", "verify_query"}:
        allowed = {"query"} if name == "verify_query" else {"query", "time_range", "title", "description"}
        if set(inputs) - allowed or not isinstance(inputs.get("query"), str):
            return None
        rows = _memory_query(inputs["query"], state["memory_series"])
        if rows is None:
            return None
        if name == "verify_query":
            return f"PASS: query returns {len(rows)} synthetic series" if rows else "FAIL_NO_DATA: no synthetic series"
        time_range = inputs.get("time_range") or "1h"
        if not isinstance(time_range, str) or not re.fullmatch(r"[1-9][0-9]*[smhd]", time_range):
            return None
        title, description = inputs.get("title") or "", inputs.get("description", "")
        if not isinstance(title, str) or not isinstance(description, (str, type(None))):
            return None
        if not rows:
            return "No data returned; no chart component was generated"
        widget = {
            "kind": "chart",
            "title": title
            or (
                "Memory Usage by namespace"
                if re.search(r"by\s*\(\s*namespace\s*\)", inputs["query"])
                else "Memory Usage"
            ),
            "description": description,
            "query": inputs["query"],
            "time_range": time_range,
        }
        state["latest_memory_component"] = widget
        return json.dumps({"query": inputs["query"], "time_range": time_range, "series": rows, "component": widget})
    if name == "discover_metrics":
        if set(inputs) - {"category"} or inputs.get("category", "all") not in {"memory", "all"}:
            return None
        return json.dumps({"metrics": ["container_memory_working_set_bytes"], "source": "synthetic"})
    if name in {"get_view_details", "add_widget_to_view"}:
        if set(inputs) != {"view_id"} or inputs["view_id"] != state["view"]["id"]:
            return None
        if name == "get_view_details":
            return json.dumps(state["view"])
        component = state.get("latest_memory_component")
        if not component or state.get("memory_widget_added"):
            return None
        state["view"]["widgets"].append(copy.deepcopy(component))
        state["memory_widget_added"] = True
        return "Latest memory chart added to SRE Overview (now 6 widgets); synthetic frontend apply acknowledged"
    return None
