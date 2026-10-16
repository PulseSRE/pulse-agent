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


def normalize_component_spec(spec: dict) -> dict:
    """Normalize field name aliases in a component spec.

    Fixes known mismatches between what tools produce and what the frontend
    expects, so specs are consistent before validation or persistence.
    """
    kind = spec.get("kind", "")

    if kind == "status_list":
        items = spec.get("items")
        if isinstance(items, list):
            for item in items:
                if "label" in item and "name" not in item:
                    item["name"] = item.pop("label")
                elif "text" in item and "name" not in item:
                    item["name"] = item["text"]  # copy, keep text for description
                elif "title" in item and "name" not in item:
                    item["name"] = item.pop("title")
                status = item.get("status")
                if status == "info":
                    item["status"] = "unknown"

    elif kind == "badge_list":
        badges = spec.get("badges")
        if isinstance(badges, list):
            for badge in badges:
                if "label" in badge and "text" not in badge:
                    badge["text"] = badge.pop("label")

    elif kind == "log_viewer":
        lines = spec.get("lines")
        if isinstance(lines, list):
            for line in lines:
                if line.get("level") == "warning":
                    line["level"] = "warn"

    elif kind == "blast_radius":
        items = spec.get("items")
        if isinstance(items, list):
            for item in items:
                status = item.get("status")
                if status == "info":
                    item["status"] = "healthy"

    elif kind == "chart":
        series = spec.get("series")
        if isinstance(series, list):
            for s in series:
                if "values" in s and "data" not in s:
                    s["data"] = s.pop("values")
                if "label" in s and "name" not in s:
                    s["name"] = s.pop("label")

    elif kind == "data_table":
        rows = spec.get("rows")
        title = (spec.get("title") or "").lower()
        # Cap "Top N" tables at 20 rows — 200-row dumps are not useful for top-consumers views
        if isinstance(rows, list) and len(rows) > 20 and ("top" in title or "consumer" in title):
            spec["rows"] = rows[:20]

    elif kind == "yaml_viewer":
        if "yaml" in spec and "content" not in spec:
            spec["content"] = spec.pop("yaml")

    elif kind == "metric_card":
        val = spec.get("value")
        if isinstance(val, float) and val == int(val):
            spec["value"] = str(int(val))
        elif isinstance(val, str):
            try:
                f = float(val)
                if f == int(f) and "." in val:
                    spec["value"] = str(int(f))
            except (ValueError, OverflowError):
                pass

    elif kind == "stat_card":
        if "label" in spec and "title" not in spec:
            spec["title"] = spec.pop("label")

    elif kind == "info_card_grid":
        cards = spec.get("cards")
        if isinstance(cards, list):
            for card in cards:
                if "title" in card and "label" not in card:
                    card["label"] = card.pop("title")
                if "text" in card and "value" not in card:
                    card["value"] = card.pop("text")

    # Flatten props wrapper: {kind, props: {data}} → {kind, ...data}
    if "props" in spec and isinstance(spec["props"], dict):
        props = spec.pop("props")
        for k, v in props.items():
            if k not in spec:
                spec[k] = v

    return spec


def normalize_layout(layout: list[dict]) -> list[dict]:
    """Normalize all component specs in a layout list.

    Empty tabs are stripped structurally: tab children with no components are
    removed; if a tabs widget has no populated tabs remaining it is dropped.
    """
    result = []
    for spec in layout:
        normalize_component_spec(spec)
        kind = spec.get("kind")
        if kind == "grid":
            for item in spec.get("items", []):
                normalize_component_spec(item)
        elif kind == "tabs":
            for tab in spec.get("tabs", []):
                for comp in tab.get("components", []):
                    normalize_component_spec(comp)
            spec["tabs"] = [t for t in spec.get("tabs", []) if t.get("components")]
            if not spec["tabs"]:
                continue  # drop widget if no populated tabs remain
        elif kind == "section":
            for comp in spec.get("components", []):
                normalize_component_spec(comp)
        result.append(spec)
    return result


def validate_layout_kinds(layout: list[dict]) -> list[str]:
    """Validate that all components in a layout have valid kinds.

    Returns a list of error strings (empty if all valid).
    Lighter than evaluate_components — suitable for REST save paths.
    """
    valid = _get_valid_kinds()
    errors: list[str] = []
    for i, spec in enumerate(layout):
        kind = spec.get("kind")
        if not kind:
            errors.append(f"Widget {i}: missing 'kind' field")
        elif kind not in valid:
            errors.append(f"Widget {i}: invalid kind '{kind}'")
    return errors


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
    # Phase 0: Normalize field aliases
    # ------------------------------------------------------------------
    components = normalize_layout(components)

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
    for comp in deduped:
        _validate_component(comp, result)
        title = comp.get("title", "")
        if title:
            all_titles.append(title.lower())

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

    # No required mix of widget kinds. This used to reject any dashboard
    # without a metric card, a chart and a data_table, which forced one
    # template onto every topic and contradicted the skill's "shaped by its
    # topic" rule. A dashboard is judged on whether each widget fits its data.

    # ------------------------------------------------------------------
    # Phase 5: PromQL checks — warnings only (from validator)
    # ------------------------------------------------------------------
    _check_promql_all(deduped, result)

    # ------------------------------------------------------------------
    # Phase 6: Score — start from full marks, deduct for real problems
    # ------------------------------------------------------------------
    score = result.max_score
    flat = _flatten_widgets(deduped)

    if len(deduped) > max_widgets:
        score -= 2

    # Empty widgets render as blank boxes.
    for w in flat:
        reason = _empty_reason(w)
        if reason:
            result.warnings.append(f"Empty widget '{w.get('title') or w.get('kind')}': {reason}")
            score -= 2

    # Widgets whose kind does not fit the shape of their data.
    for w in flat:
        misfit = _shape_misfit(w)
        if misfit:
            result.suggestions.append(misfit)
            score -= 1
    loose_cards = sum(1 for w in deduped if w.get("kind") == "metric_card")
    if loose_cards >= 5:
        result.suggestions.append(
            f"{loose_cards} separate metric cards — group related ones in a grid, or use a data_table "
            "when they are the same measure per item"
        )
        score -= 1

    # Titles: every widget that should have one, and none generic.
    untitled = [w for w in deduped if _title_required(w) and not w.get("title")]
    if untitled:
        result.suggestions.append(f"{len(untitled)} widget(s) have no title")
        score -= 1
    for w in flat:
        if w.get("title") and is_generic_title(w["title"], w.get("kind", "")):
            score -= 1
    if any(c > 1 for c in Counter(all_titles).values()):
        score -= 1

    # Duplicate queries — check the ORIGINAL list so duplicates that were
    # removed by dedup still count (matches old critic behaviour).
    queries = [w.get("query", "") for w in _flatten_widgets(components) if w.get("query")]
    for q, count in Counter(queries).items():
        if count > 1:
            score -= count - 1
            result.warnings.append(f"Duplicate query '{q[:60]}' appears {count} times")

    # Live data: time-sensitive widgets without a query freeze at save time.
    static = [w for w in flat if w.get("kind") in _LIVE_KINDS and not w.get("query")]
    if static:
        names = ", ".join(f"'{w.get('title') or w.get('kind')}'" for w in static[:4])
        result.warnings.append(
            f"{len(static)} time-sensitive widget(s) have no PromQL query and will not refresh: {names}"
        )
        score -= 1

    # Advice that costs nothing when ignored.
    charts = [w for w in flat if w.get("kind") == "chart"]
    if charts and not all(c.get("description") for c in charts):
        result.suggestions.append("Add descriptions to charts saying what to watch for")
    if len(deduped) >= 6:
        result.suggestions.append("Consider using tabs to organize 6+ widgets into logical groups")

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


# Kinds whose value goes stale when saved without a query.
_LIVE_KINDS = frozenset({"chart", "metric_card", "stat_card"})

_LIST_FIELDS = {
    "status_list": "items",
    "bar_list": "items",
    "progress_list": "items",
    "badge_list": "badges",
    "key_value": "pairs",
    "info_card_grid": "cards",
    "node_map": "nodes",
}


def _flatten_widgets(components: list[dict]) -> list[dict]:
    """Leaf widgets, including those nested in grids, sections and tabs."""
    out: list[dict] = []
    for c in components:
        kind = c.get("kind", "")
        if kind == "grid":
            out.extend(_flatten_widgets(c.get("items", [])))
        elif kind == "section":
            out.extend(_flatten_widgets(c.get("components", [])))
        elif kind == "tabs":
            for tab in c.get("tabs", []):
                out.extend(_flatten_widgets(tab.get("components", [])))
            out.append(c)
        else:
            out.append(c)
    return out


def _empty_reason(w: dict) -> str:
    kind = w.get("kind", "")
    if kind == "chart":
        points = sum(len(s.get("data", [])) for s in w.get("series", []) or [])
        if points == 0 and not w.get("query"):
            return "no data and no query"
    elif kind == "data_table":
        if not w.get("rows") and not w.get("datasources"):
            return "no rows and no datasource"
    elif kind in _LIST_FIELDS:
        if not w.get(_LIST_FIELDS[kind]):
            return f"no {_LIST_FIELDS[kind]}"
    return ""


def _shape_misfit(w: dict) -> str:
    """A suggestion when the widget kind does not suit its data, else ''."""
    kind = w.get("kind", "")
    title = w.get("title") or kind
    if kind == "chart" and w.get("chartType", "line") in ("line", "area"):
        series = w.get("series", []) or []
        # Points may be [timestamp, value] pairs or bare values; only pairs carry time.
        stamps = {pt[0] for s in series for pt in s.get("data", []) if isinstance(pt, (list, tuple)) and pt}
        if series and all(len(s.get("data", [])) <= 1 for s in series) and len(stamps) == 1:
            return f"'{title}' plots a single instant as a {w.get('chartType', 'line')} chart — use a bar_list for a ranking"
    if kind == "status_list":
        statuses = {i.get("status") for i in w.get("items", [])}
        if len(w.get("items", [])) >= 3 and len(statuses) == 1:
            return f"'{title}': every item has the same status — a data_table shows the differences better"
    if kind == "data_table" and "top" in title.lower() and len(w.get("rows", [])) > 20:
        return f"'{title}' is a Top-N table with {len(w.get('rows', []))} rows — limit it to 20"
    if kind == "tabs" and any(not t.get("components") for t in w.get("tabs", [])):
        return "tabs widget has an empty tab — drop tabs with nothing in them"
    return ""


def _title_required(w: dict) -> bool:
    from .component_registry import get_component

    comp = get_component(w.get("kind", ""))
    return comp.title_required if comp else True


def _deduplicate(components: list[dict]) -> list[dict]:
    """Remove duplicate components.

    Dedup criteria applied in order:
    1. Identical non-empty query string — keeps the first occurrence (earlier
       component wins; later one with same PromQL is redundant).
    2. Identical (kind, title) pair — keeps the LAST occurrence so that the
       most-recent agent pass (build phase) wins over earlier investigation-phase
       components, preventing double-layout when the agent runs two passes.

    Query is intentionally excluded from the (kind, title) key so that two
    containers of the same type (e.g. two "tabs" blocks) are collapsed even
    when their internal PromQL differs between passes.
    """
    # Pre-scan: record the last index of each (kind, title) pair so we can
    # keep only that occurrence while preserving original list order.
    last_kt: dict[tuple[str, str], int] = {}
    for i, comp in enumerate(components):
        k = comp.get("kind", "")
        t = (comp.get("title") or "").lower()
        if k and t:
            last_kt[(k, t)] = i

    seen_queries: set[str] = set()
    out: list[dict] = []

    for i, comp in enumerate(components):
        query = comp.get("query", "")
        kind = comp.get("kind", "")
        title = (comp.get("title") or "").lower()

        # Drop if a later component has the same query (keep first query match)
        if query and query in seen_queries:
            continue
        # Drop if a later occurrence of the same (kind, title) exists (keep last)
        if kind and title and last_kt.get((kind, title)) != i:
            continue

        if query:
            seen_queries.add(query)
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


def _warning_kind(warning: str, layout: list[dict]) -> str:
    """Kind of the widget an "Empty widget '<title>'" warning names."""
    name = warning.split("'", 2)[1] if warning.count("'") >= 2 else ""
    for w in _flatten_widgets(layout):
        if (w.get("title") or w.get("kind")) == name:
            return w.get("kind", "")
    return ""


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

    # Issues come from the engine's own result, so critique and save-time
    # validation apply one rubric. (This used to add its own checks that
    # demanded metric cards, two charts and a table on every dashboard.)
    issues: list[str] = list(result.errors)
    if not positions:
        issues.append("NO LAYOUT: Positions not computed — this may indicate a save error")
    for warning in result.warnings:
        if warning.startswith("Duplicate query"):
            issues.append(f"DUPLICATE QUERY: {warning}")
        elif warning.startswith("Empty widget"):
            label = "EMPTY CHART" if _warning_kind(warning, layout) == "chart" else "EMPTY WIDGET"
            issues.append(f"{label}: {warning}")
        else:
            issues.append(f"STALE DATA: {warning}" if "will not refresh" in warning else warning)
    for widget in _flatten_widgets(layout):
        w_title = widget.get("title", "")
        if w_title and is_generic_title(w_title, widget.get("kind", "")):
            issues.append(f"GENERIC TITLE: '{w_title}' — provide a descriptive, specific title")
    untitled = sum(1 for w in layout if _title_required(w) and not w.get("title"))
    if untitled:
        issues.append(f"UNTITLED WIDGETS: {untitled} widget(s) missing titles — add descriptive names")
    if len(layout) > 8:
        issues.append(f"TOO MANY WIDGETS: {len(layout)} widgets — reorganize into tabs or remove duplicates")
    dup_titles = [t for t, c in Counter(w.get("title", "").lower() for w in layout if w.get("title")).items() if c > 1]
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
        lines.append("\n❌ View quality is low. Fix the issues above so each widget fits its data, then re-critique.")

    return "\n".join(lines)
