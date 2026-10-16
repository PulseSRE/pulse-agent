"""The dashboard being assembled in the current agent turn, visible to tools.

Widgets reach the session through ``on_component`` as data tools run, and
the view is saved only after the agent's turn ends (``agent_ws`` processes
the ``view_spec`` signal). Until now ``create_dashboard`` could not see that
draft, so it always answered "Here is your dashboard" and the quality
engine's findings went only to the UI, after the fact. The model never
learned a widget was empty or invalid, and could not fix it: by then the turn
was over, and mid-turn the view did not exist yet for the update tools.

``agent_ws`` binds the turn's component list here before running the agent.
Tool threads see it because the agent loop runs tools in a copy of the
calling context, and the list is shared by reference.

Caveat: a widget reaches the list once its tool's batch completes. If the
model calls a data tool and ``create_dashboard`` in the same batch, the check
does not see that widget. It therefore only blocks on problems found in
widgets that are present, never on what might be missing.
"""

from __future__ import annotations

import copy
from contextvars import ContextVar, Token
from dataclasses import dataclass, field


@dataclass
class DashboardDraft:
    components: list[dict]
    # create_dashboard calls that were held back for fixes this turn. The
    # check holds a save back at most once, so it can never loop.
    held: int = 0
    excluded: set[str] = field(default_factory=set)


_DRAFT: ContextVar[DashboardDraft | None] = ContextVar("dashboard_draft", default=None)


def bind(components: list[dict]) -> Token:
    """Expose this turn's component list to tools. Returns a token for ``unbind``."""
    return _DRAFT.set(DashboardDraft(components=components))


def unbind(token: Token) -> None:
    _DRAFT.reset(token)


def current() -> DashboardDraft | None:
    return _DRAFT.get()


def parse_titles(exclude: str) -> set[str]:
    """``"A | B"`` -> ``{"a", "b"}`` (case-insensitive widget titles)."""
    return {t.strip().lower() for t in (exclude or "").split("|") if t.strip()}


def apply_exclusions(components: list[dict], titles: set[str]) -> list[dict]:
    """Drop top-level widgets whose title is in ``titles`` (case-insensitive)."""
    if not titles:
        return list(components)
    return [c for c in components if (c.get("title") or "").strip().lower() not in titles]


# Findings that make a saved page look broken: the widget renders empty or
# not at all. Everything else the engine reports is advisory.
_BLOCKING_WARNING_PREFIXES = ("Empty widget",)
# Engine errors about the dashboard as a whole, not a broken widget. Holding
# a save for these would push the model to pad or trim to hit a count.
_ADVISORY_ERROR_MARKERS = ("must have at least", "must have at most")


def precheck(components: list[dict], check_data: bool = False) -> tuple[list[str], list[str]]:
    """Return ``(blocking, advisory)`` findings for a draft dashboard.

    With ``check_data``, each widget's live query is also run once
    (``check_widget_data``) and a query that fails or returns nothing blocks.
    """
    from .quality_engine import evaluate_components

    if not components:
        return [], []
    # Deep copy: the engine normalizes specs in place; the draft must stay as saved.
    result = evaluate_components(copy.deepcopy(components), positions=None)
    blocking: list[str] = []
    advisory: list[str] = []
    for err in result.errors:
        (advisory if any(m in err for m in _ADVISORY_ERROR_MARKERS) else blocking).append(err)
    for warning in result.warnings:
        (blocking if warning.startswith(_BLOCKING_WARNING_PREFIXES) else advisory).append(warning)
    advisory.extend(result.suggestions)
    if check_data:
        blocking.extend(check_widget_data(components))
    return blocking, advisory


# --- Live data check -------------------------------------------------------
#
# The structural check above sees a widget's spec, not what it will show.
# Charts and metric cards with a ``query`` are re-run by the UI on every
# refresh, so a query that errors or matches no series renders as an error
# box or "No data" no matter how good the spec looks. Running each query once
# before saving lets the model fix it while it still can.

_MAX_QUERIES = 16
_QUERY_TIMEOUT_S = 5
_CONTAINER_KEYS = ("items", "components")


def widget_queries(components: list[dict]) -> list[tuple[str, str]]:
    """``(widget title, PromQL)`` for every widget with a live query, nested ones included."""
    found: list[tuple[str, str]] = []

    def walk(items: list, parent: str) -> None:
        for comp in items or []:
            if not isinstance(comp, dict):
                continue
            title = (comp.get("title") or comp.get("label") or parent or comp.get("kind") or "widget").strip()
            query = comp.get("query")
            if isinstance(query, str) and query.strip():
                found.append((title, query.strip()))
            for key in _CONTAINER_KEYS:
                if isinstance(comp.get(key), list):
                    walk(comp[key], title)
            for tab in comp.get("tabs") or []:
                if isinstance(tab, dict):
                    walk(tab.get("components") or [], tab.get("label") or title)

    walk(components, "")
    return found


def _run_query(query: str) -> tuple[str, str]:
    """``(outcome, detail)``: outcome is ok, empty, invalid, error or unreachable."""
    import math
    import urllib.error

    from .prometheus import PrometheusConfigError, prometheus_request

    try:
        data = prometheus_request("api/v1/query", {"query": query}, timeout=_QUERY_TIMEOUT_S)
    except PrometheusConfigError as e:
        return "unreachable", str(e)
    except urllib.error.HTTPError as e:
        # Prometheus answers a bad query with 400 and a JSON error body.
        try:
            import json

            detail = json.loads(e.read() or b"{}").get("error", "")
        except Exception:
            detail = ""
        return ("error", detail or f"HTTP {e.code}") if e.code in (400, 422) else ("unreachable", f"HTTP {e.code}")
    except Exception as e:  # connection refused, DNS, timeout: the backend, not the query
        return "unreachable", type(e).__name__
    if data.get("status") != "success":
        return "error", str(data.get("error") or "query failed")
    result = (data.get("data") or {}).get("result")
    kind = (data.get("data") or {}).get("resultType")
    if kind == "scalar" or kind == "string":
        return "ok", ""
    if not result:
        return "empty", ""
    values = []
    for series in result:
        try:
            values.append(float((series.get("value") or [0, "nan"])[1]))
        except (TypeError, ValueError):
            values.append(math.nan)
    if not any(math.isfinite(v) for v in values):
        return "invalid", ""
    return "ok", ""


def check_widget_data(components: list[dict]) -> list[str]:
    """Blocking findings for widgets whose live query would render broken or empty.

    Best effort: when Prometheus cannot be reached (or is not configured) the
    check reports nothing, so a monitoring outage never holds a save.
    """
    from concurrent.futures import ThreadPoolExecutor

    pairs = widget_queries(components)
    if not pairs:
        return []
    unique = list(dict.fromkeys(q for _, q in pairs))[:_MAX_QUERIES]
    with ThreadPoolExecutor(max_workers=min(6, len(unique))) as pool:
        outcomes = dict(zip(unique, pool.map(_run_query, unique), strict=True))
    if any(outcome == "unreachable" for outcome, _ in outcomes.values()):
        return []

    findings: list[str] = []
    for title, query in pairs:
        outcome, detail = outcomes.get(query, ("ok", ""))
        if outcome == "error":
            findings.append(f"Query failed for '{title}': {detail[:200]} — fix the PromQL or leave the widget out")
        elif outcome == "empty":
            findings.append(
                f"No data for '{title}': its query matches no series right now "
                "(check the metric name and label values against get_prometheus_query results)"
            )
        elif outcome == "invalid":
            findings.append(f"No usable data for '{title}': its query returns only NaN/Inf (e.g. division by zero)")
    return findings
