"""View Critic — thin wrapper around quality_engine for backward compatibility.

The scoring rubric now lives in ``quality_engine.evaluate_components()``.
This module keeps the ``critique_view`` @beta_tool so existing tool
registrations and tests continue to work.
"""

from __future__ import annotations

import logging

from .decorators import beta_tool

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

    from .quality_engine import evaluate_components

    layout = view.get("layout", [])
    positions = view.get("positions", {})
    title = view.get("title", "")

    result = evaluate_components(layout, positions)

    # --- Build text result for the agent ---
    lines = [
        f"## View Quality Score: {result.score}/{result.max_score}",
        f"Title: {title}",
        f"Widgets: {len(layout)}",
        f"Template: {'applied' if positions else 'NONE'}",
    ]

    # Combine errors + warnings into issues for display
    issues: list[str] = list(result.errors)

    # Add critic-style issue labels for missing structure
    if not any(w.get("kind") in ("metric_card", "info_card_grid", "grid") for w in layout):
        issues.append("NO METRIC CARDS: Add cluster_metrics() or namespace_summary() for KPI row at top")
    charts = [w for w in layout if w.get("kind") == "chart"]
    if len(charts) == 1:
        issues.append("ONLY 1 CHART: Add a second chart (e.g., memory trend alongside CPU)")
    elif len(charts) == 0:
        issues.append("NO CHARTS: Call get_prometheus_query(query, time_range='1h') for trend visualizations")
    if not any(w.get("kind") == "data_table" for w in layout):
        issues.append("NO TABLE: Add a data_table for drill-down (list_pods, list_nodes, etc.)")
    if not positions:
        issues.append("NO LAYOUT: Positions not computed — this may indicate a save error")

    # Untitled widgets
    titled = sum(1 for w in layout if w.get("title"))
    if titled < len(layout) and len(layout) > 0:
        untitled = len(layout) - titled
        issues.append(f"UNTITLED WIDGETS: {untitled} widget(s) missing titles — add descriptive names")

    # Too few / too many
    if len(layout) < 3:
        issues.append(f"TOO FEW WIDGETS: Only {len(layout)} widgets. Minimum 3 (metrics + chart + table)")
    elif len(layout) > 8:
        issues.append(f"TOO MANY WIDGETS: {len(layout)} widgets — reorganize into tabs or remove duplicates")

    # Duplicate queries (from warnings)
    for w in result.warnings:
        if w.startswith("Duplicate query"):
            issues.append(f"DUPLICATE QUERY: {w}")

    # Empty charts (from warnings)
    for w in result.warnings:
        if w.startswith("Empty chart"):
            issues.append(f"EMPTY CHART: {w}")

    # Generic titles
    from .quality_engine import is_generic_title

    for w in layout:
        w_title = w.get("title", "")
        w_kind = w.get("kind", "")
        if w_title and w_kind and is_generic_title(w_title, w_kind):
            issues.append(f"GENERIC TITLE: '{w_title}' — provide a descriptive, specific title")
        if w_kind == "grid":
            for item in w.get("items", []):
                it = item.get("title", "")
                ik = item.get("kind", "")
                if it and ik and is_generic_title(it, ik):
                    issues.append(f"GENERIC TITLE: '{it}' — provide a descriptive, specific title")

    # Component balance
    if len(layout) >= 3:
        from collections import Counter

        kind_counts = Counter(w.get("kind", "") for w in layout)
        most_common_kind, most_common_count = kind_counts.most_common(1)[0]
        if most_common_count / len(layout) > 0.8:
            issues.append(
                f"IMBALANCED: {most_common_count}/{len(layout)} widgets are '{most_common_kind}'"
                " — mix metric cards, charts, and tables"
            )

    # Duplicate titles
    from collections import Counter as _Counter

    all_titles = [w.get("title", "").lower() for w in layout if w.get("title")]
    title_counts = _Counter(all_titles)
    dup_titles = [t for t, c in title_counts.items() if c > 1]
    if dup_titles:
        issues.append(
            f"DUPLICATE TITLES: {', '.join(repr(t) for t in dup_titles)} — each widget must have a unique title"
        )

    # Deduplicate issues
    seen: set[str] = set()
    unique_issues: list[str] = []
    for issue in issues:
        if issue not in seen:
            seen.add(issue)
            unique_issues.append(issue)
    issues = unique_issues

    if issues:
        lines.append(f"\n### Issues ({len(issues)}):")
        for issue in issues:
            lines.append(f"- \u274c {issue}")

    suggestions = list(result.suggestions)
    if suggestions:
        lines.append(f"\n### Suggestions ({len(suggestions)}):")
        for s in suggestions:
            lines.append(f"- \U0001f4a1 {s}")

    if result.score >= 7:
        lines.append("\n\u2705 View passes quality check. Ready to show to user.")
    elif result.score >= 5:
        lines.append("\n\u26a0\ufe0f View needs improvements. Fix the issues above, then critique again.")
    else:
        lines.append("\n\u274c View quality is low. Add missing components (metrics, charts, table) and re-critique.")

    return "\n".join(lines)
