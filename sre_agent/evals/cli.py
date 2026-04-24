"""CLI entrypoint for deterministic eval suites."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger("pulse_agent.evals")

from .runner import evaluate_suite
from .scenarios import load_suite

_BASELINES_DIR = Path(__file__).parent / "baselines"


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
    # Comparison flags
    p.add_argument(
        "--save-baseline",
        action="store_true",
        help="Save results as baseline to sre_agent/evals/baselines/{suite}.json",
    )
    p.add_argument(
        "--compare-baseline",
        action="store_true",
        help="Compare results against saved baseline for this suite.",
    )
    p.add_argument(
        "--compare",
        default="",
        help="Compare results against a specific baseline JSON file path.",
    )
    p.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="Return non-zero exit if regressions detected vs baseline.",
    )
    # Prompt audit
    p.add_argument(
        "--audit-prompt",
        action="store_true",
        help="Print token cost breakdown of each system prompt section.",
    )
    p.add_argument(
        "--mode",
        default="sre",
        choices=("sre", "security", "view_designer", "both"),
        help="Agent mode for --audit-prompt (default: sre).",
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


def _save_baseline(suite_name: str, json_str: str) -> Path:
    """Save JSON results as a baseline for this suite."""
    _BASELINES_DIR.mkdir(parents=True, exist_ok=True)
    path = _BASELINES_DIR / f"{suite_name}.json"
    path.write_text(json_str + "\n", encoding="utf-8")
    return path


def _run_comparison(current_json: str, baseline_path: str, fmt: str) -> tuple[str, bool]:
    """Run comparison against a baseline file. Returns (output, gate_passed)."""
    import tempfile

    from .compare import compare_results, format_comparison

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
        tmp.write(current_json)
        tmp_path = tmp.name

    try:
        result = compare_results(baseline_path, tmp_path)
        return format_comparison(result, fmt), result.gate_passed
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _format_audit(audit: dict, fmt: str) -> str:
    """Format prompt audit results."""
    if fmt == "json":
        return json.dumps(audit, indent=2)
    lines = [f"Prompt Audit — mode: {audit['mode']}"]
    lines.append(f"{'Section':<30} {'Chars':>8} {'~Tokens':>8} {'%':>6}")
    lines.append("-" * 56)
    for s in audit["sections"]:
        lines.append(f"{s['name']:<30} {s['chars']:>8} {s['chars'] // 4:>8} {s['pct']:>5.1f}%")
    lines.append("-" * 56)
    lines.append(f"{'TOTAL':<30} {audit['total_chars']:>8} {audit['estimated_tokens']:>8}")
    return "\n".join(lines)


def main() -> None:
    args = _make_parser().parse_args()

    # Prompt audit mode — runs independently of eval suites
    if args.audit_prompt:
        from ..harness import measure_prompt_sections

        audit = measure_prompt_sections(mode=args.mode)
        print(_format_audit(audit, args.format))
        if args.output:
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            Path(args.output).write_text(_format_audit(audit, args.format) + "\n", encoding="utf-8")
        return

    # Selector suite — deterministic routing accuracy, no fixtures
    if args.suite == "selector":
        from .selector_eval import format_selector_eval, run_selector_eval

        sel_result = run_selector_eval()
        rendered = format_selector_eval(sel_result)
        print(rendered)
        if args.output:
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            Path(args.output).write_text(rendered + "\n", encoding="utf-8")
        if args.fail_on_gate and sel_result.recall_at_5 < 0.90:
            sys.exit(1)
        return

    scenarios = load_suite(args.suite)
    result = evaluate_suite(args.suite, scenarios)

    rendered = _to_json(result) if args.format == "json" else _to_text(result)
    json_str = _to_json(result)  # Always compute JSON for baseline/compare

    print(rendered)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")

    # Save baseline
    if args.save_baseline:
        saved = _save_baseline(args.suite, json_str)
        print(f"\nBaseline saved to {saved}")

    # Run comparison
    comparison_output = ""
    comparison_passed = True

    if args.compare_baseline:
        baseline_path = _BASELINES_DIR / f"{args.suite}.json"
        if not baseline_path.exists():
            print(f"\nNo baseline found at {baseline_path}. Run with --save-baseline first.")
            sys.exit(1)
        comparison_output, comparison_passed = _run_comparison(json_str, str(baseline_path), args.format)
        print(f"\n{comparison_output}")
    elif args.compare:
        comparison_output, comparison_passed = _run_comparison(json_str, args.compare, args.format)
        print(f"\n{comparison_output}")

    # Record to DB (fire-and-forget)
    try:
        from .history import record_eval_run

        record_eval_run(
            suite_name=args.suite,
            source="cli",
            scenario_count=result.scenario_count,
            passed_count=result.passed_count,
            gate_passed=result.gate_passed,
            average_overall=result.average_overall,
            dimensions=result.dimension_averages,
            blocker_counts=result.blocker_counts,
            scenarios=[
                {"scenario_id": s.scenario_id, "overall": s.overall, "passed_gate": s.passed_gate}
                for s in result.scenarios
            ],
        )
    except Exception:
        logger.debug("Failed to record eval run to history", exc_info=True)

    # Exit codes
    if args.fail_on_gate and not result.gate_passed:
        sys.exit(1)
    if args.fail_on_regression and not comparison_passed:
        sys.exit(1)


if __name__ == "__main__":
    main()
