"""Component Transformation Engine — converts between component types.

Transforms a component spec from one kind to another, mapping data fields
intelligently. Used by widget mutations (change_kind) and live chat
(transform_last_component).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

logger = logging.getLogger("pulse_agent.component_transform")

# Transformation matrix: {(from_kind, to_kind): transform_function}
_TRANSFORMS: dict[tuple[str, str], Callable] = {}


def _register(from_kind: str, to_kind: str):
    """Decorator to register a transformation function."""

    def decorator(fn):
        _TRANSFORMS[(from_kind, to_kind)] = fn
        return fn

    return decorator


def transform(source_spec: dict, target_kind: str, options: dict | None = None) -> dict:
    """Transform a component spec from one kind to another.

    Returns a new spec with kind=target_kind and data mapped.
    Raises ValueError if transformation is not supported.
    """
    source_kind = source_spec.get("kind", "")
    if source_kind == target_kind:
        return dict(source_spec)

    key = (source_kind, target_kind)
    fn = _TRANSFORMS.get(key)
    if not fn:
        raise ValueError(
            f"No transformation from '{source_kind}' to '{target_kind}'. Available: {list_transformations(source_kind)}"
        )

    result = fn(source_spec, options or {})
    result["kind"] = target_kind
    # Preserve title and description
    if "title" not in result and "title" in source_spec:
        result["title"] = source_spec["title"]
    if "description" not in result and "description" in source_spec:
        result["description"] = source_spec["description"]
    return result


def can_transform(source_kind: str, target_kind: str) -> bool:
    """Check if a transformation path exists."""
    return (source_kind, target_kind) in _TRANSFORMS


def list_transformations(source_kind: str) -> list[str]:
    """List valid target kinds for a source kind."""
    return [to_kind for (from_kind, to_kind) in _TRANSFORMS if from_kind == source_kind]


# ---------------------------------------------------------------------------
# Transformation functions
# ---------------------------------------------------------------------------


@_register("data_table", "chart")
def _table_to_chart(spec: dict, options: dict) -> dict:
    """Convert table rows to chart series. First column = labels, numeric columns = series."""
    columns = spec.get("columns", [])
    rows = spec.get("rows", [])

    if not columns or not rows:
        return {"series": [], "chartType": "bar"}

    # Find label column (first non-numeric) and value columns (numeric)
    label_col = options.get("label_column", columns[0]["id"])
    value_cols = options.get("value_columns")

    if not value_cols:
        # Auto-detect numeric columns from first row
        value_cols = []
        for col in columns:
            cid = col["id"]
            if cid == label_col or cid.startswith("_"):
                continue
            sample = rows[0].get(cid)
            if isinstance(sample, (int, float)):
                value_cols.append(cid)

    if not value_cols:
        # Fallback: count rows per label value
        from collections import Counter

        counts = Counter(str(r.get(label_col, "")) for r in rows)
        return {
            "chartType": "bar",
            "series": [{"name": label_col, "data": [{"label": k, "value": v} for k, v in counts.most_common(10)]}],
        }

    # Build series from value columns
    series = []
    for vc in value_cols:
        col_header = next((c["header"] for c in columns if c["id"] == vc), vc)
        data = [{"label": str(r.get(label_col, "")), "value": r.get(vc, 0)} for r in rows]
        series.append({"name": col_header, "data": data})

    return {"chartType": options.get("chart_type", "bar"), "series": series}


@_register("data_table", "bar_list")
def _table_to_bar_list(spec: dict, options: dict) -> dict:
    """Pick label + value columns → ranked bars."""
    columns = spec.get("columns", [])
    rows = spec.get("rows", [])

    label_col = options.get("label_column", columns[0]["id"] if columns else "name")
    value_col = options.get("value_column")

    # Auto-detect first numeric column
    if not value_col and rows:
        for col in columns:
            cid = col["id"]
            if cid == label_col or cid.startswith("_"):
                continue
            if isinstance(rows[0].get(cid), (int, float)):
                value_col = cid
                break

    if not value_col:
        # Count occurrences
        from collections import Counter

        counts = Counter(str(r.get(label_col, "")) for r in rows)
        return {"items": [{"label": k, "value": v} for k, v in counts.most_common(20)]}

    items = [{"label": str(r.get(label_col, "")), "value": r.get(value_col, 0)} for r in rows]
    items.sort(key=lambda x: x["value"], reverse=True)
    return {"items": items[:20]}


@_register("data_table", "metric_card")
def _table_to_metric_card(spec: dict, options: dict) -> dict:
    """Aggregate table to single value (count/sum/avg)."""
    rows = spec.get("rows", [])
    agg = options.get("aggregation", "count")

    if agg == "count":
        return {"value": str(len(rows)), "description": "total rows"}

    value_col = options.get("value_column", "")
    if not value_col:
        return {"value": str(len(rows)), "description": "total rows"}

    values = [r.get(value_col, 0) for r in rows if isinstance(r.get(value_col), (int, float))]
    if not values:
        return {"value": "0", "description": f"no numeric {value_col}"}

    if agg == "sum":
        return {"value": str(sum(values)), "description": f"total {value_col}"}
    elif agg == "avg":
        return {"value": f"{sum(values) / len(values):.1f}", "description": f"avg {value_col}"}
    elif agg == "max":
        return {"value": str(max(values)), "description": f"max {value_col}"}
    else:
        return {"value": str(len(rows)), "description": "total rows"}


@_register("chart", "data_table")
def _chart_to_table(spec: dict, options: dict) -> dict:
    """Convert chart series to table rows."""
    series = spec.get("series", [])
    if not series:
        return {"columns": [], "rows": []}

    # If series have timestamp data (time-series chart)
    first_series = series[0] if series else {}
    data_points = first_series.get("data", [])

    if data_points and "timestamp" in data_points[0]:
        # Time-series → columns = timestamp + one per series
        columns = [{"id": "timestamp", "header": "Time", "type": "timestamp"}]
        for s in series:
            columns.append({"id": s.get("name", s.get("id", "value")), "header": s.get("name", "Value")})

        rows = []
        for i, dp in enumerate(data_points):
            row = {"timestamp": dp.get("timestamp", i)}
            for s in series:
                s_data = s.get("data", [])
                row[s.get("name", s.get("id", "value"))] = s_data[i].get("value", 0) if i < len(s_data) else 0
            rows.append(row)
        return {"columns": columns, "rows": rows}

    # Bar/category chart → label + value columns
    columns = [{"id": "label", "header": "Label"}]
    for s in series:
        columns.append({"id": s.get("name", "value"), "header": s.get("name", "Value")})

    rows = []
    for dp in data_points:
        row = {"label": dp.get("label", "")}
        row[first_series.get("name", "value")] = dp.get("value", 0)
        rows.append(row)
    return {"columns": columns, "rows": rows}


@_register("chart", "metric_card")
def _chart_to_metric_card(spec: dict, options: dict) -> dict:
    """Take latest value from first series."""
    series = spec.get("series", [])
    if not series:
        return {"value": "n/a"}

    data = series[0].get("data", [])
    if not data:
        return {"value": "n/a"}

    latest = data[-1]
    value = latest.get("value", 0)
    return {"value": f"{value:.2f}" if isinstance(value, float) else str(value), "query": spec.get("query", "")}


@_register("metric_card", "chart")
def _metric_card_to_chart(spec: dict, options: dict) -> dict:
    """Use query to render as time-series chart."""
    return {"chartType": "line", "query": spec.get("query", ""), "time_range": options.get("time_range", "1h")}


@_register("status_list", "data_table")
def _status_list_to_table(spec: dict, options: dict) -> dict:
    """Convert status items to table rows."""
    items = spec.get("items", [])
    columns = [
        {"id": "label", "header": "Name"},
        {"id": "status", "header": "Status", "type": "status"},
        {"id": "detail", "header": "Detail"},
    ]
    rows = [
        {"label": it.get("label", ""), "status": it.get("status", ""), "detail": it.get("detail", "")} for it in items
    ]
    return {"columns": columns, "rows": rows}


@_register("bar_list", "data_table")
def _bar_list_to_table(spec: dict, options: dict) -> dict:
    """Convert bar items to table rows."""
    items = spec.get("items", [])
    columns = [
        {"id": "label", "header": "Name"},
        {"id": "value", "header": "Value"},
    ]
    rows = [{"label": it.get("label", ""), "value": it.get("value", 0)} for it in items]
    return {"columns": columns, "rows": rows}


@_register("resource_counts", "data_table")
def _resource_counts_to_table(spec: dict, options: dict) -> dict:
    """Convert resource count items to table rows."""
    items = spec.get("items", [])
    columns = [
        {"id": "resource", "header": "Resource"},
        {"id": "count", "header": "Count"},
        {"id": "status", "header": "Status", "type": "status"},
    ]
    rows = [
        {"resource": it.get("resource", ""), "count": it.get("count", 0), "status": it.get("status", "healthy")}
        for it in items
    ]
    return {"columns": columns, "rows": rows}


@_register("progress_list", "data_table")
def _progress_list_to_table(spec: dict, options: dict) -> dict:
    """Convert progress items to table rows."""
    items = spec.get("items", [])
    columns = [
        {"id": "label", "header": "Name"},
        {"id": "value", "header": "Used"},
        {"id": "max", "header": "Total"},
        {"id": "pct", "header": "%"},
    ]
    rows = []
    for it in items:
        val = it.get("value", 0)
        mx = it.get("max", 100)
        pct = round(val / mx * 100) if mx else 0
        rows.append({"label": it.get("label", ""), "value": val, "max": mx, "pct": f"{pct}%"})
    return {"columns": columns, "rows": rows}
