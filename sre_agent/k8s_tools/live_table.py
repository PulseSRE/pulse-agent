"""Live table tool — creates multi-datasource auto-refreshing tables.

The frontend uses K8s watches for real-time row updates and polls
Prometheus / log endpoints for enrichment columns.
"""

from __future__ import annotations

import json
from typing import Any

from ..decorators import beta_tool
from ..tool_registry import register_tool
from .generic import _fetch_table_rows


@beta_tool
def create_live_table(
    title: str,
    datasources_json: str,
    description: str = "",
    deduplicate_by: str = "name",
):
    """Create a live auto-refreshing table from multiple data sources.

    The table stays current via K8s watches (real-time) and optional
    Prometheus / log enrichment (polled).  It uses the same rendering
    engine as the resource browser.

    Args:
        title: Table title (e.g. 'Troubled Pods Across Namespaces').
        datasources_json: JSON array of datasource objects.  Each must have
            ``type`` (k8s | promql | logs) and ``id``.  Types:

            K8s — provides base rows (real-time watch):
              {"type": "k8s", "id": "prod", "label": "Production",
               "resource": "pods", "namespace": "production",
               "labelSelector": "app=api", "fieldSelector": ""}

            PromQL — enriches rows with a metric column (polled):
              {"type": "promql", "id": "cpu", "label": "CPU",
               "query": "sum(rate(container_cpu_usage_seconds_total{namespace='production'}[5m])) by (pod)",
               "columnId": "cpu_usage", "columnHeader": "CPU",
               "unit": "cores", "joinLabel": "pod", "joinColumn": "name"}

            Logs — enriches rows with a log-count column (polled):
              {"type": "logs", "id": "errors", "label": "Errors",
               "namespace": "production", "pattern": "error|Error|ERROR",
               "columnId": "error_count", "columnHeader": "Recent Errors"}
        description: What this table monitors.
        deduplicate_by: Column for row deduplication (default 'name').
    """
    try:
        datasources = json.loads(datasources_json)
    except json.JSONDecodeError as e:
        return f"Invalid JSON in datasources_json: {e}"

    if not isinstance(datasources, list) or not datasources:
        return "datasources_json must be a non-empty JSON array."

    if len(datasources) > 10:
        return "Maximum 10 datasources per table."

    k8s_sources: list[dict[str, Any]] = []
    for ds in datasources:
        if not ds.get("id"):
            return f"Datasource missing required 'id': {ds}"
        ds_type = ds.get("type")
        if ds_type not in ("k8s", "promql", "logs"):
            return f"Datasource '{ds['id']}' has invalid type '{ds_type}'. Must be k8s, promql, or logs."
        if ds_type == "k8s":
            if not ds.get("resource"):
                return f"K8s datasource '{ds['id']}' missing required 'resource'."
            if not ds.get("label"):
                ds["label"] = ds["id"]
            k8s_sources.append(ds)
        elif ds_type == "promql":
            if not ds.get("query"):
                return f"PromQL datasource '{ds['id']}' missing required 'query'."
            if not ds.get("columnId"):
                return f"PromQL datasource '{ds['id']}' missing required 'columnId'."
            if not ds.get("joinLabel") or not ds.get("joinColumn"):
                return f"PromQL datasource '{ds['id']}' missing 'joinLabel' and/or 'joinColumn'."
        elif ds_type == "logs":
            if not ds.get("namespace"):
                return f"Logs datasource '{ds['id']}' missing required 'namespace'."
            if not ds.get("columnId"):
                return f"Logs datasource '{ds['id']}' missing required 'columnId'."

    if not k8s_sources:
        return "At least one K8s datasource is required to provide base table rows."

    if len(k8s_sources) > 5:
        return "Maximum 5 K8s datasources per table."

    # Fetch initial snapshot from the first K8s datasource for the text summary.
    # The frontend will set up watches for all K8s datasources independently.
    first = k8s_sources[0]
    initial = _fetch_table_rows(
        resource=first["resource"],
        namespace=first.get("namespace", ""),
        group=first.get("group", ""),
        version=first.get("version", "v1"),
        label_selector=first.get("labelSelector", ""),
        field_selector=first.get("fieldSelector", ""),
    )

    if isinstance(initial, str):
        # Error fetching — still return the spec so the frontend can try watches
        initial_cols: list[dict[str, str]] = [{"id": "name", "header": "Name", "type": "resource_name"}]
        initial_rows: list[dict[str, Any]] = []
        source_summary = f"{first.get('label', first['id'])}: error ({initial})"
    else:
        initial_cols = initial["columns"]
        initial_rows = initial["rows"]
        source_summary = f"{first.get('label', first['id'])}: {len(initial_rows)} rows"

    # Add enrichment column placeholders to the initial column set
    for ds in datasources:
        if ds["type"] in ("promql", "logs"):
            col_id = ds.get("columnId", ds["id"])
            col_header = ds.get("columnHeader", col_id)
            if not any(c["id"] == col_id for c in initial_cols):
                initial_cols.append({"id": col_id, "header": col_header, "type": "text"})

    text = (
        f"Live table '{title}' with {len(datasources)} datasources ({source_summary}). "
        f"The table updates in real-time via K8s watches"
    )
    enrichment_count = sum(1 for ds in datasources if ds["type"] in ("promql", "logs"))
    if enrichment_count:
        text += f" with {enrichment_count} enrichment column{'s' if enrichment_count > 1 else ''} polled every 30s"
    text += "."

    component: dict[str, Any] = {
        "kind": "data_table",
        "title": title,
        "description": description or f"Live data from {len(datasources)} sources",
        "columns": initial_cols,
        "rows": initial_rows,
        "datasources": datasources,
        "deduplicateBy": deduplicate_by,
    }

    return (text, component)


register_tool(create_live_table)
