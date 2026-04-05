"""View Validator — enforces quality rules on dashboard components before save.

Validates component schemas, detects generic titles, deduplicates widgets,
checks PromQL syntax, and enforces structural requirements. Returns a
ValidationResult with errors/warnings and the deduped component list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

VALID_KINDS = frozenset(
    {
        "metric_card",
        "chart",
        "data_table",
        "info_card_grid",
        "status_list",
        "badge_list",
        "key_value",
        "relationship_tree",
        "log_viewer",
        "yaml_viewer",
        "node_map",
        "tabs",
        "grid",
        "section",
    }
)

METRIC_SOURCE_KINDS = frozenset({"metric_card", "info_card_grid", "grid"})

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


@dataclass
class ValidationResult:
    valid: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    deduped_count: int = 0
    components: list[dict] = field(default_factory=list)


def validate_components(
    components: list[dict],
    *,
    max_widgets: int = 8,
    min_widgets: int = 3,
) -> ValidationResult:
    """Validate and deduplicate a list of dashboard components.

    Returns a ValidationResult. ``valid`` is True only when ``errors`` is empty.
    """
    result = ValidationResult()

    if not components:
        result.valid = False
        result.errors.append("Dashboard must have at least 1 component.")
        return result

    # --- Deduplication ---
    deduped = _deduplicate(components)
    result.deduped_count = len(components) - len(deduped)
    result.components = deduped

    # --- Widget count ---
    if len(deduped) < min_widgets:
        result.errors.append(f"Dashboard must have at least {min_widgets} widgets (got {len(deduped)}).")
    if len(deduped) > max_widgets:
        result.errors.append(f"Dashboard must have at most {max_widgets} widgets (got {len(deduped)}).")

    # --- Per-component validation ---
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

        # Track structure coverage (grids count via their items)
        if kind in METRIC_SOURCE_KINDS:
            has_metric_source = True
        if kind == "chart":
            has_chart = True
        if kind == "data_table":
            has_table = True

        # Check nested grid items for metric sources / charts / tables
        if kind == "grid":
            for item in comp.get("items", []):
                ik = item.get("kind", "")
                if ik in METRIC_SOURCE_KINDS:
                    has_metric_source = True
                if ik == "chart":
                    has_chart = True
                if ik == "data_table":
                    has_table = True

    # --- Duplicate titles ---
    seen_titles: set[str] = set()
    for t in all_titles:
        if t in seen_titles:
            result.errors.append(f"Duplicate title '{t}' — each widget must have a unique title.")
        seen_titles.add(t)

    # --- Required structure ---
    if not has_metric_source:
        result.errors.append(
            "Dashboard must include a metric source (metric_card, info_card_grid, or grid with metrics)."
        )
    if not has_chart:
        result.errors.append("Dashboard must include at least one chart.")
    if not has_table:
        result.errors.append("Dashboard must include at least one data_table.")

    # --- PromQL checks (warnings) ---
    _check_promql_all(deduped, result)

    result.valid = len(result.errors) == 0
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _deduplicate(components: list[dict]) -> list[dict]:
    """Remove components with identical query or identical kind+title (case-insensitive)."""
    seen_queries: set[str] = set()
    seen_kind_title: set[tuple[str, str]] = set()
    out: list[dict] = []

    for comp in components:
        query = comp.get("query", "")
        kind = comp.get("kind", "")
        title = (comp.get("title") or "").lower()

        if query and query in seen_queries:
            continue
        key = (kind, title)
        if kind and title and key in seen_kind_title:
            continue

        if query:
            seen_queries.add(query)
        if kind and title:
            seen_kind_title.add(key)
        out.append(comp)

    return out


def _validate_component(comp: dict, result: ValidationResult) -> None:
    """Validate a single component's schema and title."""
    kind = comp.get("kind")
    title = comp.get("title")

    # --- kind ---
    if not kind:
        result.errors.append("Component missing required 'kind' field.")
        return
    if kind not in VALID_KINDS:
        result.errors.append(f"Invalid kind '{kind}' — must be one of: {', '.join(sorted(VALID_KINDS))}.")
        return

    # --- title ---
    # Structural wrappers (grid, tabs, section) don't require titles —
    # they contain other components that have their own titles.
    title_required = kind not in ("grid", "tabs", "section")
    if title_required and (not title or not str(title).strip()):
        result.errors.append(f"Component (kind={kind}) missing required 'title' field.")
        return

    if title and title_required:
        _check_generic_title(str(title), kind, result)

    # --- kind-specific schema ---
    if kind == "chart":
        if not comp.get("series") and not comp.get("query"):
            result.errors.append(f"Chart '{title}' must have 'series' (list) or 'query' (string).")

    elif kind == "metric_card":
        if not comp.get("value") and not comp.get("query"):
            result.errors.append(f"Metric card '{title}' must have 'value' (string) or 'query' (string).")

    elif kind == "data_table":
        if not comp.get("columns"):
            result.errors.append(f"Data table '{title}' must have 'columns' (list).")
        if "rows" not in comp:
            result.errors.append(f"Data table '{title}' must have 'rows' (list).")

    elif kind == "grid":
        items = comp.get("items")
        if items:
            for item in items:
                _validate_component(item, result)


def is_generic_title(title: str, kind: str) -> bool:
    """Return True if *title* is generic or meaningless for the given *kind*."""
    lower = title.strip().lower()

    # Exact match
    if lower in _GENERIC_TITLES:
        return True

    # Numbered generic (e.g. "Chart 1", "Table 2")
    if _NUMBERED_GENERIC_RE.match(lower):
        return True

    # Kind-as-title (e.g. "data table" for kind "data_table")
    kind_as_title = kind.replace("_", " ").lower()
    return lower == kind_as_title


def _check_generic_title(title: str, kind: str, result: ValidationResult) -> None:
    """Reject generic or meaningless titles."""
    if is_generic_title(title, kind):
        lower = title.strip().lower()
        kind_as_title = kind.replace("_", " ")
        if lower == kind_as_title:
            result.errors.append(f"Generic title '{title}' — title matches kind '{kind}', provide a descriptive title.")
        else:
            result.errors.append(f"Generic title '{title}' — provide a descriptive title.")


def _check_promql_all(components: list[dict], result: ValidationResult) -> None:
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


def _check_promql(query: str, result: ValidationResult) -> None:
    """Basic PromQL syntax checks — issues are warnings, not errors."""
    if query.count("{") != query.count("}"):
        result.warnings.append(f"PromQL has unbalanced braces {{}} in: {query}")
    if query.count("(") != query.count(")"):
        result.warnings.append(f"PromQL has unbalanced parens () in: {query}")
    if "}{" in query:
        result.warnings.append(f"PromQL has double label block '}}{{' in: {query}")
