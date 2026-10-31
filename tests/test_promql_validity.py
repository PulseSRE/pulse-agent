"""Every PromQL query the scanners ship must actually parse.

The scanner tests mock the Prometheus client and assert on how a canned
response is formatted, so the query string itself is an argument nobody ever
examines. They pass just as happily with `"this is not promql at all ((("` or
with an empty string — verified. Nothing in CI has ever parsed these queries.

That gap shipped a real bug: the error-rate-acceleration query applied `offset`
to a finished ratio, which PromQL rejects. Prometheus answered 400 on every
pass, the scanner returned nothing, and "no results" is indistinguishable from
"nothing is wrong". It was invisible for as long as it existed, and surfaced
only as a vague "Pulse cannot query Prometheus" that pointed at reachability.

`promql_parser.parse()` (the `promql-parser` package, already in the `test`
extra for the mock cluster) is a full PromQL parse with no Prometheus to talk
to and no binary to install, so this runs on every `pytest` invocation, local
or CI, and cannot be skipped.
"""

from __future__ import annotations

import importlib
import pkgutil
import re

import promql_parser
import pytest

# Anything that looks like PromQL rather than prose: a known function call or a
# label matcher. Deliberately broad — a false positive costs one parse, while a
# false negative is a query that ships unvalidated.
_PROMQL_HINT = re.compile(
    r"\b(rate|irate|sum|avg|max|min|count|histogram_quantile|predict_linear"
    r"|deriv|increase|delta|absent|topk|avg_over_time|max_over_time)\s*\(|\{[a-z_]+="
)


def _collect_scanner_queries() -> dict[str, str]:
    """Module-level PromQL constants across sre_agent.monitor.

    Discovery is by inspection rather than an explicit list so a new scanner is
    covered the moment it defines its query, instead of when someone remembers
    to register it here.
    """
    import sre_agent.monitor as monitor_pkg

    found: dict[str, str] = {}
    for mod in pkgutil.iter_modules(monitor_pkg.__path__):
        # No try/except: a module that fails to import would otherwise take its
        # queries out of the check without anyone noticing.
        m = importlib.import_module(f"sre_agent.monitor.{mod.name}")
        for attr in dir(m):
            if attr.startswith("__"):
                continue
            value = getattr(m, attr, None)
            if isinstance(value, str) and len(value) > 25 and _PROMQL_HINT.search(value):
                found[f"{mod.name}.{attr}"] = value
    return found


def test_promql_hint_finds_the_known_scanner_queries():
    """Guard the guard: if discovery silently matches nothing, this suite is theatre."""
    queries = _collect_scanner_queries()
    assert len(queries) >= 5, f"expected to discover scanner queries, found {sorted(queries)}"
    assert any("ERROR_ACCELERATION" in k for k in queries), (
        "the error-acceleration query must be discovered — it is the one that shipped broken"
    )


def test_every_scanner_query_parses():
    queries = _collect_scanner_queries()

    failures = []
    for name, query in sorted(queries.items()):
        try:
            promql_parser.parse(query)
        except ValueError as exc:
            failures.append(f"{name}: {exc}\n    {query}")

    if failures:
        pytest.fail("PromQL failed to parse:\n" + "\n".join(failures))


def test_parser_rejects_an_offset_on_a_finished_expression():
    """The parser must catch the exact shape that shipped, or the suite proves nothing."""
    from sre_agent.monitor import trend_scanners

    share = trend_scanners._ERROR_SHARE
    pre_fix = f"({share} > 0.01) and ({share} > 2 * ({share} offset 1d))"
    with pytest.raises(ValueError, match="offset"):
        promql_parser.parse(pre_fix)
