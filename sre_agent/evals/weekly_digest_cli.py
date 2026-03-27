"""CLI for generating weekly eval markdown digest."""

from __future__ import annotations

import argparse
from pathlib import Path

from .weekly_digest import render_weekly_digest


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Generate weekly eval markdown digest.")
    p.add_argument("--db-path", default="", help="Optional fix history database path.")
    p.add_argument("--current-days", type=int, default=7, help="Current window in days.")
    p.add_argument("--baseline-days", type=int, default=7, help="Baseline window in days.")
    p.add_argument("--output", default="", help="Optional output markdown file path.")
    return p


def main() -> None:
    args = _parser().parse_args()
    digest = render_weekly_digest(
        current_days=args.current_days,
        baseline_days=args.baseline_days,
        db_path=args.db_path,
    )
    print(digest)
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(digest + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
