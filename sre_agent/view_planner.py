"""View Planner — generates a structured dashboard plan for user approval.

The view designer agent calls plan_dashboard BEFORE building. The plan
describes what will be built: template, rows, widgets, data sources.
The user approves or adjusts, then the agent executes.
"""

from __future__ import annotations

from anthropic import beta_tool


@beta_tool
def plan_dashboard(
    title: str,
    template: str,
    rows: str,
) -> str:
    """Present a dashboard plan to the user for approval BEFORE building it.
    Call this INSTEAD of create_dashboard. After user approves, then call the
    data tools and create_dashboard.

    Args:
        title: Proposed dashboard title.
        template: Layout template ID (sre_dashboard, namespace_overview, incident_report, monitoring_panel, resource_detail).
        rows: A structured description of each row, formatted as:
              "Row 1 — Metric Cards: CPU Usage (sparkline), Memory Usage (sparkline), Nodes Ready, Pods Running
               Row 2 — Charts: CPU by Namespace (stacked_area, 1h), Memory by Namespace (stacked_area, 1h)
               Row 3 — Table: Pod Status (name, status, restarts, node, age)"
    """
    plan_lines = [
        f"## Dashboard Plan: {title}",
        "",
        f"**Template:** `{template}`",
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

    plan_lines.append("")
    plan_lines.append(
        "Shall I build this? You can ask me to change the template, add/remove widgets, or adjust the layout."
    )

    return "\n".join(plan_lines)
