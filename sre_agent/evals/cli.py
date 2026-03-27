"""CLI entrypoint for deterministic eval suites."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .runner import evaluate_suite
from .scenarios import load_suite


def _make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run pulse-agent eval suites.")
    p.add_argument("--suite", default="core", help="Suite fixture name (default: core)")
    p.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format",
    )
    p.add_argument(
        "--fail-on-gate",
        action="store_true",
        help="Return non-zero exit if release gate fails.",
    )
    p.add_argument(
        "--output",
        default="",
        help="Optional file path to write output.",
    )
    return p


def _to_json(result) -> str:
    payload = {
        "suite_name": result.suite_name,
        "scenario_count": result.scenario_count,
        "passed_count": result.passed_count,
        "gate_passed": result.gate_passed,
        "average_overall": result.average_overall,
        "dimension_averages": result.dimension_averages,
        "blocker_counts": result.blocker_counts,
        "scenarios": [
            {
                "scenario_id": s.scenario_id,
                "category": s.category,
                "overall": s.overall,
                "dimensions": s.dimensions,
                "blockers": s.blockers,
                "passed_gate": s.passed_gate,
            }
            for s in result.scenarios
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _to_text(result) -> str:
    lines: list[str] = []
    lines.append(f"Suite: {result.suite_name}")
    lines.append(
        f"Scenarios: {result.scenario_count} | Passed: {result.passed_count} | Gate: {'PASS' if result.gate_passed else 'FAIL'}"
    )
    lines.append(f"Average overall score: {result.average_overall:.3f}")
    lines.append("Dimension averages:")
    for k, v in result.dimension_averages.items():
        lines.append(f"  - {k}: {v:.3f}")
    if result.blocker_counts:
        lines.append("Blockers:")
        for k, v in sorted(result.blocker_counts.items()):
            lines.append(f"  - {k}: {v}")
    lines.append("Scenario results:")
    for s in result.scenarios:
        lines.append(
            f"  - {s.scenario_id} ({s.category}) overall={s.overall:.3f} gate={'PASS' if s.passed_gate else 'FAIL'}"
        )
        if s.blockers:
            lines.append(f"      blockers={','.join(s.blockers)}")
    return "\n".join(lines)


def main() -> None:
    args = _make_parser().parse_args()
    scenarios = load_suite(args.suite)
    result = evaluate_suite(args.suite, scenarios)

    rendered = _to_json(result) if args.format == "json" else _to_text(result)
    print(rendered)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")

    if args.fail_on_gate and not result.gate_passed:
        sys.exit(1)


if __name__ == "__main__":
    main()
