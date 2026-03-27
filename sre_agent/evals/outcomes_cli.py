"""CLI for outcome-based evaluation reports."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .outcomes import analyze_windows, render_text_report


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Generate outcome-based eval report.")
    p.add_argument("--db-path", default="", help="Path to fix history DB.")
    p.add_argument("--policy-file", default="", help="Optional regression policy YAML file.")
    p.add_argument("--current-days", type=int, default=7, help="Current window in days.")
    p.add_argument("--baseline-days", type=int, default=7, help="Baseline window in days.")
    p.add_argument("--format", choices=("text", "json"), default="text")
    p.add_argument("--output", default="", help="Optional output file path.")
    p.add_argument("--fail-on-regression", action="store_true", help="Exit non-zero if regression gate fails.")
    return p


def main() -> None:
    args = _parser().parse_args()
    kwargs = {"current_days": args.current_days, "baseline_days": args.baseline_days}
    if args.db_path:
        kwargs["db_path"] = args.db_path
    if args.policy_file:
        kwargs["policy_path"] = args.policy_file
    report = analyze_windows(**kwargs)

    rendered = json.dumps(report, indent=2, sort_keys=True) if args.format == "json" else render_text_report(report)
    print(rendered)
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rendered + "\n", encoding="utf-8")

    if args.fail_on_regression and not report["gate_passed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
