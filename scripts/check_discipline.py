#!/usr/bin/env python3
"""Two mechanical guardrails against bug classes this repo has already hit.

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


def main() -> int:
    failed = False
    for name, check in (
        ("no-silent-skip", check_no_silent_skip),
        ("no-by-value-global-import", check_no_by_value_global_import),
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
