"""View Critic — evaluates dashboard design quality against a rubric.

Called by the view designer agent after creating a view to auto-detect
design issues and score quality before showing to the user.
"""

from __future__ import annotations

import logging

from anthropic import beta_tool

logger = logging.getLogger("pulse_agent.view_critic")


@beta_tool
def critique_view(view_id: str) -> str:
    """Critique a view's design quality against best practices. Returns a score
    (0-10) and specific improvement suggestions. Call this AFTER create_dashboard
    to verify the view meets quality standards before showing to the user.

    Args:
        view_id: The view ID to critique (e.g. 'cv-abc123').
    """
    from . import db

    view = db.get_view(view_id)
    if not view:
        return f"View {view_id} not found."

    layout = view.get("layout", [])
    positions = view.get("positions", {})
    title = view.get("title", "")

    score = 0
    max_score = 10
    issues: list[str] = []
    suggestions: list[str] = []

    # --- Rubric checks ---

    # 1. Has metric cards or info cards? (2 points)
    has_metrics = any(w.get("kind") in ("metric_card", "info_card_grid", "grid") for w in layout)
    if has_metrics:
        score += 2
    else:
        issues.append("NO METRIC CARDS: Add cluster_metrics() or namespace_summary() for KPI row at top")

    # 2. Has charts with time_range? (2 points)
    charts = [w for w in layout if w.get("kind") == "chart"]
    if len(charts) >= 2:
        score += 2
    elif len(charts) == 1:
        score += 1
        issues.append("ONLY 1 CHART: Add a second chart (e.g., memory trend alongside CPU)")
    else:
        issues.append("NO CHARTS: Call get_prometheus_query(query, time_range='1h') for trend visualizations")

    # 3. Has data table? (1 point)
    has_table = any(w.get("kind") == "data_table" for w in layout)
    if has_table:
        score += 1
    else:
        issues.append("NO TABLE: Add a data_table for drill-down (list_pods, list_nodes, etc.)")

    # 4. Template applied (positions not empty)? (2 points)
    if positions and len(positions) > 0:
        score += 2
    else:
        issues.append("NO TEMPLATE: Use create_dashboard(template='...') for professional layout")

    # 5. All widgets have titles? (1 point)
    titled = sum(1 for w in layout if w.get("title"))
    if titled == len(layout) and len(layout) > 0:
        score += 1
    else:
        untitled = len(layout) - titled
        issues.append(f"UNTITLED WIDGETS: {untitled} widget(s) missing titles — add descriptive names")

    # 6. Charts have descriptions? (1 point)
    if charts:
        described = sum(1 for c in charts if c.get("description"))
        if described == len(charts):
            score += 1
        else:
            suggestions.append("Add descriptions to charts explaining what to watch for")

    # 7. Metric cards have PromQL queries? (1 point)
    metric_cards = [w for w in layout if w.get("kind") == "metric_card"]
    # Also check inside grid items
    for w in layout:
        if w.get("kind") == "grid":
            metric_cards.extend(item for item in w.get("items", []) if item.get("kind") == "metric_card")
    cards_with_query = [m for m in metric_cards if m.get("query")]
    if metric_cards and len(cards_with_query) >= len(metric_cards) * 0.5:
        score += 1
    elif metric_cards:
        suggestions.append("Add PromQL queries to metric cards for live sparkline charts")

    # --- Widget count check ---
    if len(layout) < 3:
        issues.append(f"TOO FEW WIDGETS: Only {len(layout)} widgets. Minimum 3 (metrics + chart + table)")
    elif len(layout) >= 5:
        suggestions.append("Consider using tabs to organize 5+ widgets into logical groups")

    # 8. Duplicate widget detection
    queries = [w.get("query", "") for w in layout if w.get("query")]
    # Also check inside grids
    for w in layout:
        if w.get("kind") == "grid":
            queries.extend(item.get("query", "") for item in w.get("items", []) if item.get("query"))
    seen_queries: set[str] = set()
    for q in queries:
        if q and q in seen_queries:
            issues.append(f"DUPLICATE QUERY: '{q[:60]}' appears multiple times — use different metrics")
            break
        if q:
            seen_queries.add(q)

    # 9. Data quality — charts with no data
    for w in layout:
        if w.get("kind") == "chart":
            series = w.get("series", [])
            total_points = sum(len(s.get("data", [])) for s in series)
            if total_points == 0 and w.get("title"):
                suggestions.append(f"Chart '{w['title']}' has no data — verify Prometheus connectivity")

    # --- Build result ---
    result_lines = [
        f"## View Quality Score: {score}/{max_score}",
        f"Title: {title}",
        f"Widgets: {len(layout)}",
        f"Template: {'applied' if positions else 'NONE'}",
    ]

    if issues:
        result_lines.append(f"\n### Issues ({len(issues)}):")
        for issue in issues:
            result_lines.append(f"- ❌ {issue}")

    if suggestions:
        result_lines.append(f"\n### Suggestions ({len(suggestions)}):")
        for s in suggestions:
            result_lines.append(f"- 💡 {s}")

    if score >= 7:
        result_lines.append("\n✅ View passes quality check. Ready to show to user.")
    elif score >= 5:
        result_lines.append("\n⚠️ View needs improvements. Fix the issues above, then critique again.")
    else:
        result_lines.append(
            "\n❌ View quality is low. Add missing components (metrics, charts, table) and re-critique."
        )

    return "\n".join(result_lines)
