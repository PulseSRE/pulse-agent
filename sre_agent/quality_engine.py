"""Dashboard quality engine — single source of truth for validation and scoring.

Merges the validation checks from view_validator.py (pre-save) and the
quality scoring rubric from view_critic.py (post-save) into a single
``evaluate_components()`` function that returns a ``QualityResult``.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field

from .decorators import beta_tool

logger = logging.getLogger("pulse_agent.view_critic")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Component registry is the source of truth for valid kinds.
# Call _get_valid_kinds() at validation time (not import time) to support late-registered kinds.
from .component_registry import get_valid_kinds as _get_valid_kinds

VALID_KINDS = _get_valid_kinds()  # Backward-compat export; internal validators use _get_valid_kinds()

METRIC_SOURCE_KINDS = frozenset({"metric_card", "info_card_grid", "grid"})

_RESOLUTION_VALID_STATUSES = frozenset({"done", "running", "pending"})
_BLAST_RADIUS_VALID_STATUSES = frozenset({"degraded", "healthy", "retrying", "paused"})
_ACTION_BUTTON_VALID_STYLES = frozenset({"primary", "danger", "ghost"})

_GENERIC_TITLES = frozenset(
    {
        "chart",
        "table",
        "metric card",
        "metric",
        "card",
        "widget",
        "component",
        "data table",
        "status list",
        "info card",
    }
)

_NUMBERED_GENERIC_RE = re.compile(
    r"^(chart|table|metric card|metric|card|widget|component)\s*\d*$",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class QualityResult:
    valid: bool = True
    score: int = 0
    max_score: int = 10
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    deduped_count: int = 0
    components: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_generic_title(title: str, kind: str) -> bool:
    """Return True if *title* is generic or meaningless for the given *kind*."""
    lower = title.strip().lower()

    if lower in _GENERIC_TITLES:
        return True

    if _NUMBERED_GENERIC_RE.match(lower):
        return True

    kind_as_title = kind.replace("_", " ").lower()
    return lower == kind_as_title


def evaluate_components(
    components: list[dict],
    positions: dict | None = None,
    *,
    max_widgets: int = 8,
    min_widgets: int = 3,
) -> QualityResult:
    """Validate AND score dashboard components.

    Replaces both ``validate_components()`` and ``critique_view()``.
    Returns a QualityResult with validation errors, quality score (0-10),
    and improvement suggestions.
    """
    result = QualityResult()

    if not components:
        result.valid = False
        result.errors.append("Dashboard must have at least 1 component.")
        return result

    # ------------------------------------------------------------------
    # Phase 1: Deduplication (from validator)
    # ------------------------------------------------------------------
    deduped = _deduplicate(components)
    result.deduped_count = len(components) - len(deduped)
    result.components = deduped

    # ------------------------------------------------------------------
    # Phase 2: Per-component schema validation (from validator)
    # ------------------------------------------------------------------
    all_titles: list[str] = []
    has_metric_source = False
    has_chart = False
    has_table = False

    for comp in deduped:
        _validate_component(comp, result)
        title = comp.get("title", "")
        kind = comp.get("kind", "")

        if title:
            all_titles.append(title.lower())

        if kind in METRIC_SOURCE_KINDS:
            has_metric_source = True
        if kind == "chart":
            has_chart = True
        if kind == "data_table":
            has_table = True

        if kind == "grid":
            for item in comp.get("items", []):
                ik = item.get("kind", "")
                if ik in METRIC_SOURCE_KINDS:
                    has_metric_source = True
                if ik == "chart":
                    has_chart = True
                if ik == "data_table":
                    has_table = True

    # ------------------------------------------------------------------
    # Phase 3: Widget count (from both)
    # ------------------------------------------------------------------
    if len(deduped) < min_widgets:
        result.errors.append(f"Dashboard must have at least {min_widgets} widgets (got {len(deduped)}).")
    if len(deduped) > max_widgets:
        result.errors.append(f"Dashboard must have at most {max_widgets} widgets (got {len(deduped)}).")

    # ------------------------------------------------------------------
    # Phase 4: Duplicate titles (from validator)
    # ------------------------------------------------------------------
    seen_titles: set[str] = set()
    for t in all_titles:
        if t in seen_titles:
            result.errors.append(f"Duplicate title '{t}' — each widget must have a unique title.")
        seen_titles.add(t)

    # ------------------------------------------------------------------
    # Phase 5: Required structure (from validator)
    # ------------------------------------------------------------------
    if not has_metric_source:
        result.errors.append(
            "Dashboard must include a metric source (metric_card, info_card_grid, or grid with metrics)."
        )
    if not has_chart:
        result.errors.append("Dashboard must include at least one chart.")
    if not has_table:
        result.errors.append("Dashboard must include at least one data_table.")

    # ------------------------------------------------------------------
    # Phase 6: PromQL checks — warnings only (from validator)
    # ------------------------------------------------------------------
    _check_promql_all(deduped, result)

    # ------------------------------------------------------------------
    # Phase 7: Quality scoring rubric (from critic)
    # ------------------------------------------------------------------
    score = 0

    # R1. Has metric cards or info cards? (2 points)
    if has_metric_source:
        score += 2

    # R2. Has charts with data? (2 points)
    charts = [w for w in deduped if w.get("kind") == "chart"]
    if len(charts) >= 2:
        score += 2
    elif len(charts) == 1:
        score += 1
        result.suggestions.append("Add a second chart (e.g., memory trend alongside CPU)")

    # R3. Has data table? (1 point)
    if has_table:
        score += 1

    # R4. Layout positions computed? (2 points)
    if positions and len(positions) > 0:
        score += 2

    # R5. All widgets have titles? (1 point)
    titled = sum(1 for w in deduped if w.get("title"))
    if titled == len(deduped) and len(deduped) > 0:
        score += 1

    # R6. Charts have descriptions? (1 point)
    if charts:
        described = sum(1 for c in charts if c.get("description"))
        if described == len(charts):
            score += 1
        else:
            result.suggestions.append("Add descriptions to charts explaining what to watch for")

    # R7. Metric cards have PromQL queries? (1 point)
    metric_cards = [w for w in deduped if w.get("kind") == "metric_card"]
    for w in deduped:
        if w.get("kind") == "grid":
            metric_cards.extend(item for item in w.get("items", []) if item.get("kind") == "metric_card")
    cards_with_query = [m for m in metric_cards if m.get("query")]
    if metric_cards and len(cards_with_query) >= len(metric_cards) * 0.5:
        score += 1
    elif metric_cards:
        result.suggestions.append("Add PromQL queries to metric cards for live sparkline charts")

    # ------------------------------------------------------------------
    # Phase 8: Penalty deductions (from critic)
    # ------------------------------------------------------------------

    # Too many widgets penalty
    if len(deduped) > max_widgets:
        score = max(0, score - 2)
    elif len(deduped) >= 6:
        result.suggestions.append("Consider using tabs to organize 6+ widgets into logical groups")

    # Duplicate queries — check the ORIGINAL list so duplicates that were
    # removed by dedup still penalise the score (matches old critic behaviour).
    queries = [w.get("query", "") for w in components if w.get("query")]
    for w in components:
        if w.get("kind") == "grid":
            queries.extend(item.get("query", "") for item in w.get("items", []) if item.get("query"))
    query_counts: Counter[str] = Counter(q for q in queries if q)
    for q, count in query_counts.items():
        if count > 1:
            extras = count - 1
            score -= extras
            result.warnings.append(f"Duplicate query '{q[:60]}' appears {count} times")

    # Empty charts
    for w in deduped:
        if w.get("kind") == "chart":
            series = w.get("series", [])
            total_points = sum(len(s.get("data", [])) for s in series)
            has_query = bool(w.get("query"))
            if total_points == 0 and not has_query:
                chart_title = w.get("title", "untitled")
                result.warnings.append(f"Empty chart '{chart_title}' has no data and no query")
                score -= 1

    # Generic title penalty (affects score too)
    for w in deduped:
        w_title = w.get("title", "")
        w_kind = w.get("kind", "")
        if w_title and w_kind and is_generic_title(w_title, w_kind):
            score -= 1
        if w_kind == "grid":
            for item in w.get("items", []):
                it = item.get("title", "")
                ik = item.get("kind", "")
                if it and ik and is_generic_title(it, ik):
                    score -= 1

    # Component balance
    if len(deduped) >= 3:
        kind_counts = Counter(w.get("kind", "") for w in deduped)
        most_common_kind, most_common_count = kind_counts.most_common(1)[0]
        if most_common_count / len(deduped) > 0.8:
            result.suggestions.append(
                f"{most_common_count}/{len(deduped)} widgets are '{most_common_kind}'"
                " — mix metric cards, charts, and tables"
            )
            score -= 1

    # Duplicate titles penalty (case-insensitive, from critic)
    title_counts = Counter(all_titles)
    dup_titles = [t for t, c in title_counts.items() if c > 1]
    if dup_titles:
        score -= 1

    # ------------------------------------------------------------------
    # Finalize
    # ------------------------------------------------------------------
    score = max(0, min(result.max_score, score))
    result.score = score
    result.valid = len(result.errors) == 0

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _deduplicate(components: list[dict]) -> list[dict]:
    """Remove components with identical query, or identical (kind, title, query) triple."""
    seen_queries: set[str] = set()
    seen_kind_title_query: set[tuple[str, str, str]] = set()
    out: list[dict] = []

    for comp in components:
        query = comp.get("query", "")
        kind = comp.get("kind", "")
        title = (comp.get("title") or "").lower()

        if query and query in seen_queries:
            continue
        key = (kind, title, query)
        if kind and title and key in seen_kind_title_query:
            continue

        if query:
            seen_queries.add(query)
        if kind and title:
            seen_kind_title_query.add(key)
        out.append(comp)

    return out


def _validate_component(comp: dict, result: QualityResult) -> None:
    """Validate a single component's schema and title."""
    kind = comp.get("kind")
    title = comp.get("title")

    if not kind:
        result.errors.append("Component missing required 'kind' field.")
        return
    valid = _get_valid_kinds()
    if kind not in valid:
        result.errors.append(f"Invalid kind '{kind}' — must be one of: {', '.join(sorted(valid))}.")
        return

    from .component_registry import get_component

    comp_def = get_component(kind)
    title_required = comp_def.title_required if comp_def else True
    if title_required and (not title or not str(title).strip()):
        result.errors.append(f"Component (kind={kind}) missing required 'title' field.")
        return

    if title and title_required:
        _check_generic_title(str(title), kind, result)

    if kind == "chart":
        if not comp.get("series") and not comp.get("query"):
            result.errors.append(f"Chart '{title}' must have 'series' (list) or 'query' (string).")

    elif kind == "metric_card":
        if not comp.get("value") and not comp.get("query"):
            result.errors.append(f"Metric card '{title}' must have 'value' (string) or 'query' (string).")

    elif kind == "data_table":
        if not comp.get("columns"):
            result.errors.append(f"Data table '{title}' must have 'columns' (list).")
        if "rows" not in comp and not comp.get("datasources"):
            result.errors.append(f"Data table '{title}' must have 'rows' (list) or 'datasources' (list).")
        if comp.get("datasources"):
            for ds in comp["datasources"]:
                if not ds.get("id"):
                    result.errors.append(f"Data table '{title}': datasource missing 'id'.")
                if not ds.get("type"):
                    result.errors.append(f"Data table '{title}': datasource missing 'type'.")
                ds_type = ds.get("type")
                if ds_type == "k8s" and not ds.get("resource"):
                    result.errors.append(f"Data table '{title}': K8s datasource missing 'resource'.")
                elif ds_type == "promql" and not ds.get("query"):
                    result.errors.append(f"Data table '{title}': PromQL datasource missing 'query'.")
                elif ds_type == "logs" and not ds.get("namespace"):
                    result.errors.append(f"Data table '{title}': Logs datasource missing 'namespace'.")

    elif kind == "grid":
        items = comp.get("items")
        if items:
            for item in items:
                _validate_component(item, result)

    elif kind == "bar_list":
        items = comp.get("items")
        if not items:
            result.errors.append("bar_list must have at least 1 item.")
        else:
            for item in items:
                if not item.get("label"):
                    result.errors.append("bar_list item missing 'label'.")
                if "value" not in item:
                    result.errors.append("bar_list item missing 'value'.")

    elif kind == "progress_list":
        items = comp.get("items")
        if not items:
            result.errors.append("progress_list must have at least 1 item.")
        else:
            for item in items:
                if not item.get("label"):
                    result.errors.append("progress_list item missing 'label'.")
                if "value" not in item:
                    result.errors.append("progress_list item missing 'value'.")
                max_val = item.get("max", 0)
                if max_val <= 0:
                    result.errors.append(f"progress_list item '{item.get('label', '?')}' must have 'max' > 0.")

    elif kind == "stat_card":
        if not comp.get("value"):
            result.errors.append(f"Stat card '{title or 'untitled'}' must have 'value'.")

    elif kind == "timeline":
        lanes = comp.get("lanes")
        if not lanes:
            result.errors.append("timeline must have at least 1 lane.")
        else:
            for lane in lanes:
                if not lane.get("label"):
                    result.errors.append("timeline lane missing 'label'.")
                if not lane.get("events"):
                    result.errors.append(f"timeline lane '{lane.get('label', '?')}' must have at least 1 event.")

    elif kind == "status_list":
        items = comp.get("items", [])
        if items and len(items) >= 3:
            labels = [i.get("label", "") or i.get("name", "") for i in items]
            statuses = [i.get("status", "") for i in items]
            if len(set(labels)) == 1 or (not any(labels) and len(set(statuses)) == 1):
                result.warnings.append(
                    f"status_list '{title or 'untitled'}' has identical items — consider data_table instead."
                )

    elif kind == "resource_counts":
        items = comp.get("items")
        if not items:
            result.errors.append("resource_counts must have 'items'.")
        else:
            for item in items:
                if not item.get("resource"):
                    result.errors.append("resource_counts item missing 'resource'.")
                if "count" not in item:
                    result.errors.append("resource_counts item missing 'count'.")

    elif kind == "confidence_badge":
        score = comp.get("score")
        if score is None:
            result.errors.append("confidence_badge must have 'score'.")
        elif not isinstance(score, (int, float)) or score < 0 or score > 1:
            result.errors.append("confidence_badge 'score' must be a number between 0.0 and 1.0.")

    elif kind == "resolution_tracker":
        steps = comp.get("steps")
        if not steps:
            result.errors.append("resolution_tracker must have at least 1 step.")
        elif isinstance(steps, list):
            for step in steps:
                if not step.get("title"):
                    result.errors.append("resolution_tracker step missing 'title'.")
                status = step.get("status")
                if status not in _RESOLUTION_VALID_STATUSES:
                    result.errors.append(
                        f"resolution_tracker step status must be one of {_RESOLUTION_VALID_STATUSES}, got '{status}'."
                    )

    elif kind == "blast_radius":
        items = comp.get("items")
        if not items:
            result.errors.append("blast_radius must have at least 1 item.")
        elif isinstance(items, list):
            for item in items:
                if not item.get("kind_abbrev"):
                    result.errors.append("blast_radius item missing 'kind_abbrev'.")
                if not item.get("name"):
                    result.errors.append("blast_radius item missing 'name'.")
                status = item.get("status")
                if status and status not in _BLAST_RADIUS_VALID_STATUSES:
                    result.errors.append(
                        f"blast_radius item status must be one of {_BLAST_RADIUS_VALID_STATUSES}, got '{status}'."
                    )

    elif kind == "status_pipeline":
        steps = comp.get("steps")
        current = comp.get("current")
        if not steps or not isinstance(steps, list):
            result.errors.append("status_pipeline must have 'steps' (non-empty list).")
        elif len(steps) < 2:
            result.errors.append("status_pipeline must have at least 2 steps.")
        if current is None or not isinstance(current, int):
            result.errors.append("status_pipeline must have 'current' (int).")
        elif steps and (current < 0 or current >= len(steps)):
            result.errors.append(f"status_pipeline 'current' must be 0..{len(steps) - 1}, got {current}.")

    elif kind == "action_button":
        if not comp.get("label"):
            result.errors.append("action_button must have 'label'.")
        if not comp.get("action"):
            result.errors.append("action_button must have 'action'.")
        if not isinstance(comp.get("action_input"), dict):
            result.errors.append("action_button must have 'action_input' (dict).")
        style = comp.get("style", "primary")
        if style not in _ACTION_BUTTON_VALID_STYLES:
            result.errors.append(f"action_button style must be primary|danger|ghost, got '{style}'.")


def _check_generic_title(title: str, kind: str, result: QualityResult) -> None:
    """Reject generic or meaningless titles."""
    if is_generic_title(title, kind):
        lower = title.strip().lower()
        kind_as_title = kind.replace("_", " ")
        if lower == kind_as_title:
            result.errors.append(f"Generic title '{title}' — title matches kind '{kind}', provide a descriptive title.")
        else:
            result.errors.append(f"Generic title '{title}' — provide a descriptive title.")


def _check_promql_all(components: list[dict], result: QualityResult) -> None:
    """Check PromQL in all components (including nested grid items)."""
    for comp in components:
        query = comp.get("query", "")
        if query:
            _check_promql(query, result)
        if comp.get("kind") == "grid":
            for item in comp.get("items", []):
                q = item.get("query", "")
                if q:
                    _check_promql(q, result)


def _check_promql(query: str, result: QualityResult) -> None:
    """Basic PromQL syntax checks — issues are warnings, not errors."""
    if query.count("{") != query.count("}"):
        result.warnings.append(f"PromQL has unbalanced braces {{}} in: {query}")
    if query.count("(") != query.count(")"):
        result.warnings.append(f"PromQL has unbalanced parens () in: {query}")
    if "}{" in query:
        result.warnings.append(f"PromQL has double label block '}}{{' in: {query}")


# ---------------------------------------------------------------------------
# critique_view tool — backward-compatible @beta_tool wrapper
# ---------------------------------------------------------------------------


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
        kind_counts = Counter(w.get("kind", "") for w in layout)
        most_common_kind, most_common_count = kind_counts.most_common(1)[0]
        if most_common_count / len(layout) > 0.8:
            issues.append(
                f"IMBALANCED: {most_common_count}/{len(layout)} widgets are '{most_common_kind}'"
                " — mix metric cards, charts, and tables"
            )

    # Duplicate titles
    all_titles = [w.get("title", "").lower() for w in layout if w.get("title")]
    title_counts = Counter(all_titles)
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
            lines.append(f"- ❌ {issue}")

    suggestions = list(result.suggestions)
    if suggestions:
        lines.append(f"\n### Suggestions ({len(suggestions)}):")
        for s in suggestions:
            lines.append(f"- 💡 {s}")

    if result.score >= 7:
        lines.append("\n✅ View passes quality check. Ready to show to user.")
    elif result.score >= 5:
        lines.append("\n⚠️ View needs improvements. Fix the issues above, then critique again.")
    else:
        lines.append("\n❌ View quality is low. Add missing components (metrics, charts, table) and re-critique.")

    return "\n".join(lines)
