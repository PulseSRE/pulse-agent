#!/usr/bin/env python3
"""Mechanical guardrails against bug classes this repo has already hit.

Both exist because the same mistake happened more than once and was invisible
until someone went looking. They are cheap to run and impossible to forget,
which is the whole point — a rule that lives in a reviewer's head is a rule
that holds until the reviewer is busy.

  1. no-silent-skip
     A test that skips instead of failing reports success while asserting
     nothing. This repo shipped a test named "Route is created" that called
     Skip() when the Route was missing — the exact regression it existed to
     catch made it skip rather than fail. Nine more dead CRD guards sat in
     front of assertions in the operator, and 22 in the UI's E2E suite.

  2. no-by-value-global-import
     `from .mod import FLAG` binds the *value* at import time. Rebinding
     FLAG inside mod.py afterwards is invisible to the importer. The auto-fix
     kill switch was inert for four months this way: /health read the live
     value and reported "paused" while the monitor loop read a stale False and
     carried on deleting pods. The same shape appeared again in agent_ws's
     client factory.

Usage:  python3 scripts/check_discipline.py
Exit 1 on any violation.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Skips that are legitimate: a genuinely absent optional dependency or backend,
# where the alternative is not a better test but no test run at all. Each entry
# needs a reason so the list stays honest rather than becoming a dumping ground.
SKIP_ALLOWLIST: dict[str, str] = {}

SKIP_RE = re.compile(r"(?:pytest\.mark\.(?:skip|xfail)|(?<![\w.])Skip\(|pytest\.skip\()")


def check_no_silent_skip() -> list[str]:
    violations = []
    for path in sorted((ROOT / "tests").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if SKIP_RE.search(line):
                key = f"{rel}:{lineno}"
                if key in SKIP_ALLOWLIST:
                    continue
                violations.append(
                    f"{key}: test skip found. A guard in front of an assertion must fail, "
                    f"not skip — a skipped test reports success while checking nothing. "
                    f"If this is genuinely unavoidable, add '{key}' to SKIP_ALLOWLIST with a reason."
                )
    return violations


def _globals_rebound_per_module() -> dict[str, set[str]]:
    """Module path -> names it rebinds under a `global` statement."""
    out: dict[str, set[str]] = {}
    for path in sorted((ROOT / "sre_agent").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        names = {n for node in ast.walk(tree) if isinstance(node, ast.Global) for n in node.names}
        if names:
            out[path.stem] = names
    return out


def check_no_by_value_global_import() -> list[str]:
    rebound = _globals_rebound_per_module()
    violations = []
    for path in sorted((ROOT / "sre_agent").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        # Only module-level imports bind once. A deferred `from x import y`
        # inside a function body re-executes on every call and therefore reads
        # the current value — that form is safe and must not be flagged, or the
        # check cries wolf and gets ignored, which is the failure mode this
        # whole file exists to prevent.
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            source = node.module.rsplit(".", 1)[-1]
            if source == path.stem:
                continue
            for alias in node.names:
                if alias.name in rebound.get(source, set()):
                    violations.append(
                        f"{rel}:{node.lineno}: `from {node.module} import {alias.name}` binds the value "
                        f"at import time, but {source}.py rebinds {alias.name} under `global`. This "
                        f"importer will never see the change. Import the module and read "
                        f"{source}.{alias.name}, or use an accessor function."
                    )
    return violations


def check_no_silent_scanner_failure() -> list[str]:
    """A scanner that swallows an exception must say so.

    Scanners return a list. An empty list is what a healthy scan of a healthy
    cluster returns, so a scanner that catches its own error and returns []
    is indistinguishable from one that worked and found nothing — the
    dispatcher records "clean" either way, and whatever that scanner watches
    is silently unwatched.

    Twenty-two scanners were in that state. The fix is one line in the handler:
    report_failure(e) next to the logging that was already there.
    """
    violations: list[str] = []
    for path in sorted((ROOT / "sre_agent").rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not node.name.startswith(("scan_", "get_trend")):
                continue
            for handler in [h for n in ast.walk(node) if isinstance(n, ast.Try) for h in n.handlers]:
                broad = isinstance(handler.type, ast.Name) and handler.type.id == "Exception"
                if not broad:
                    continue
                calls = {
                    n.func.id for n in ast.walk(handler) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                }
                logs_it = any(
                    isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr in ("error", "exception")
                    for n in ast.walk(handler)
                )
                if logs_it and "report_failure" not in calls:
                    rel = path.relative_to(ROOT)
                    violations.append(
                        f"{rel}:{handler.lineno} {node.name}() logs a swallowed exception "
                        f"without report_failure(e) — the run will be recorded as clean and "
                        f"whatever it watches will be silently unwatched."
                    )
    return violations


SCHEMA_DECL_RE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)


def check_no_test_owned_core_schema() -> list[str]:
    """A test may not declare a table that db_schema.py owns.

    CREATE TABLE IF NOT EXISTS is a no-op whenever the table is already there,
    so a test's private, narrower copy of a real table looks harmless for as
    long as the real one survives. On the run where it does not — the suite
    shares one long-lived PostgreSQL, and an interrupted run can leave a table
    dropped — the test's version is the one the session gets, and every later
    statement that touches a column it omits fails somewhere else entirely.

    That is the whole of the intermittent "column category does not exist" at
    migration 001: a five-column `actions` in tests/test_eval_outcomes.py, and
    a failure reported against a scanner test in another file.

    Tests populate the schema. db_schema.py defines it.
    """
    schema_src = (ROOT / "sre_agent" / "db_schema.py").read_text()
    owned = set(SCHEMA_DECL_RE.findall(schema_src))
    violations: list[str] = []
    for path in sorted((ROOT / "tests").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            for name in SCHEMA_DECL_RE.findall(line):
                if name in owned:
                    violations.append(
                        f"{rel}:{lineno}: this test declares `{name}`, a table db_schema.py owns. "
                        f"CREATE TABLE IF NOT EXISTS hides the divergence until a run starts with "
                        f"the table missing, and then the whole session gets this shape. Import the "
                        f"schema (db_schema / run_migrations) and TRUNCATE for isolation instead."
                    )
    return violations


def check_one_unreleased_section() -> list[str]:
    """CHANGELOG.md must have at most one "## [Unreleased]" heading.

    Several branches each adding their own Unreleased section merge without a
    conflict, because they land at different offsets in the file. The result is
    two sections that both look current, and a release promotes only one of
    them — so half the entries silently ship under the wrong version, or none.

    This happened twice in one day across two repos before anyone noticed,
    which is the argument for checking it rather than watching for it.
    """
    changelog = ROOT / "CHANGELOG.md"
    if not changelog.exists():
        return []
    count = sum(1 for line in changelog.read_text().splitlines() if line.strip() == "## [Unreleased]")
    if count > 1:
        return [
            f"CHANGELOG.md has {count} '## [Unreleased]' sections. A release promotes one "
            f"and the rest ship under the wrong version or not at all — merge them into one."
        ]
    return []


def main() -> int:
    failed = False
    for name, check in (
        ("no-silent-skip", check_no_silent_skip),
        ("no-by-value-global-import", check_no_by_value_global_import),
        ("no-silent-scanner-failure", check_no_silent_scanner_failure),
        ("one-unreleased-section", check_one_unreleased_section),
        ("no-test-owned-core-schema", check_no_test_owned_core_schema),
    ):
        violations = check()
        if violations:
            failed = True
            print(f"\n{name}: {len(violations)} violation(s)")
            for v in violations:
                print(f"  {v}")
        else:
            print(f"{name}: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
