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

`promtool check rules` parses expressions without needing a running
Prometheus, which makes this cheap enough to run on every push.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

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
        try:
            m = importlib.import_module(f"sre_agent.monitor.{mod.name}")
        except Exception:  # pragma: no cover - import side effects are not this test's problem
            continue
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


@pytest.mark.skipif(shutil.which("promtool") is None, reason="promtool not installed")
def test_every_scanner_query_parses():
    queries = _collect_scanner_queries()

    # promtool validates recording-rule expressions, which is a full PromQL
    # parse without needing a Prometheus to talk to.
    rules = {
        "groups": [
            {
                "name": "pulse-promql-validation",
                "rules": [{"record": f"pulse:validate:{i}", "expr": q} for i, q in enumerate(queries.values())],
            }
        ]
    }

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "rules.json"  # promtool accepts JSON; YAML is a superset
        path.write_text(json.dumps(rules))
        proc = subprocess.run(
            ["promtool", "check", "rules", str(path)],
            capture_output=True,
            text=True,
        )

    if proc.returncode != 0:
        names = list(queries)
        detail = proc.stdout + proc.stderr
        for i, name in enumerate(names):
            detail = detail.replace(f"pulse:validate:{i}", name)
        pytest.fail(f"PromQL failed to parse:\n{detail}")
