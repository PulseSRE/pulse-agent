"""View Planner — generates a structured dashboard plan for user approval.

The view designer agent calls plan_dashboard BEFORE building. The plan
describes what will be built: template, rows, widgets, data sources.
The user approves or adjusts, then the agent executes.
"""

from __future__ import annotations

from .decorators import beta_tool


@beta_tool
def plan_dashboard(
    title: str,
    rows: str,
    template: str = "",
) -> str:
    """Present a dashboard plan to the user for approval BEFORE building it.
    Call this INSTEAD of create_dashboard. After user approves, then call the
    data tools and create_dashboard.

    Args:
        title: Proposed dashboard title.
        rows: A structured description of each row, formatted as:
              "Row 1 — Metric Cards: CPU Usage (sparkline), Memory Usage (sparkline), Nodes Ready, Pods Running
               Row 2 — Charts: CPU by Namespace (stacked_area, 1h), Memory by Namespace (stacked_area, 1h)
               Row 3 — Table: Pod Status (name, status, restarts, node, age)"
        template: Deprecated — layout is now automatic. Ignored if provided.
    """
    plan_lines = [
        f"## Dashboard Plan: {title}",
        "",
        "**Layout:** automatic (computed from component types)",
        "",
    ]

    # Parse and format rows
    for row_line in rows.strip().split("\n"):
        row_line = row_line.strip()
        if row_line.startswith("Row"):
            plan_lines.append(f"**{row_line}**")
        elif row_line.startswith("-"):
            plan_lines.append(row_line)
        else:
            plan_lines.append(f"- {row_line}")

    # Validate plan structure
    rows_lower = rows.lower()
    warnings = []
    if not any(kw in rows_lower for kw in ["metric", "kpi", "card", "summary", "cluster_metrics", "namespace_summary"]):
        warnings.append("Missing metric cards — add cluster_metrics() or namespace_summary() for KPI row")
    if not any(kw in rows_lower for kw in ["chart", "trend", "graph", "prometheus", "cpu", "memory"]):
        warnings.append("Missing charts — add get_prometheus_query() calls for trend visualization")
    if not any(kw in rows_lower for kw in ["table", "list", "pods", "nodes", "events"]):
        warnings.append("Missing data table — add list_pods() or list_nodes() for drill-down")

    if warnings:
        plan_lines.append("")
        plan_lines.append("⚠️ **Plan issues:**")
        for w in warnings:
            plan_lines.append(f"- {w}")
        plan_lines.append("")
        plan_lines.append("Fix these before building. A good dashboard needs: metric cards → charts → table.")

    plan_lines.append("")
    plan_lines.append("Shall I build this? You can ask me to add/remove widgets or adjust the layout.")

    return "\n".join(plan_lines)
