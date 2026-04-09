"""A/B comparison of eval suite results.

Compares two JSON result files (baseline vs current) and reports
per-scenario score deltas, regressions, and improvements.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ScenarioDelta:
    scenario_id: str
    category: str
    baseline_overall: float
    current_overall: float
    delta: float
    baseline_gate: bool
    current_gate: bool
    dimension_deltas: dict[str, float] = field(default_factory=dict)


@dataclass
class CompareResult:
    baseline_name: str
    current_name: str
    baseline_average: float
    current_average: float
    overall_delta: float
    regression_count: int
    improvement_count: int
    unchanged_count: int
    new_scenarios: list[str]
    removed_scenarios: list[str]
    deltas: list[ScenarioDelta]
    has_regressions: bool

    @property
    def gate_passed(self) -> bool:
        return not self.has_regressions


# Thresholds
_SCENARIO_REGRESSION_THRESHOLD = 0.05  # single scenario drop
_OVERALL_REGRESSION_THRESHOLD = 0.02  # average drop


def _load_result(path: str | Path) -> dict:
    """Load a JSON eval result file."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Result file not found: {p}")
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def compare_results(baseline_path: str | Path, current_path: str | Path) -> CompareResult:
    """Compare two eval suite result JSON files.

    Returns a CompareResult with per-scenario deltas and regression detection.
    """
    baseline = _load_result(baseline_path)
    current = _load_result(current_path)

    baseline_scenarios = {s["scenario_id"]: s for s in baseline.get("scenarios", [])}
    current_scenarios = {s["scenario_id"]: s for s in current.get("scenarios", [])}

    set(baseline_scenarios) | set(current_scenarios)
    new_ids = set(current_scenarios) - set(baseline_scenarios)
    removed_ids = set(baseline_scenarios) - set(current_scenarios)
    common_ids = set(baseline_scenarios) & set(current_scenarios)

    deltas: list[ScenarioDelta] = []
    regression_count = 0
    improvement_count = 0
    unchanged_count = 0

    for sid in sorted(common_ids):
        b = baseline_scenarios[sid]
        c = current_scenarios[sid]
        delta = round(c["overall"] - b["overall"], 4)

        dim_deltas = {}
        b_dims = b.get("dimensions", {})
        c_dims = c.get("dimensions", {})
        for dim in set(b_dims) | set(c_dims):
            dim_deltas[dim] = round(c_dims.get(dim, 0) - b_dims.get(dim, 0), 4)

        deltas.append(
            ScenarioDelta(
                scenario_id=sid,
                category=c.get("category", b.get("category", "")),
                baseline_overall=b["overall"],
                current_overall=c["overall"],
                delta=delta,
                baseline_gate=b.get("passed_gate", True),
                current_gate=c.get("passed_gate", True),
                dimension_deltas=dim_deltas,
            )
        )

        if delta < -_SCENARIO_REGRESSION_THRESHOLD:
            regression_count += 1
        elif delta > _SCENARIO_REGRESSION_THRESHOLD:
            improvement_count += 1
        else:
            unchanged_count += 1

    baseline_avg = baseline.get("average_overall", 0)
    current_avg = current.get("average_overall", 0)
    overall_delta = round(current_avg - baseline_avg, 4)

    has_regressions = regression_count > 0 or overall_delta < -_OVERALL_REGRESSION_THRESHOLD

    return CompareResult(
        baseline_name=baseline.get("suite_name", "baseline"),
        current_name=current.get("suite_name", "current"),
        baseline_average=baseline_avg,
        current_average=current_avg,
        overall_delta=overall_delta,
        regression_count=regression_count,
        improvement_count=improvement_count,
        unchanged_count=unchanged_count,
        new_scenarios=sorted(new_ids),
        removed_scenarios=sorted(removed_ids),
        deltas=deltas,
        has_regressions=has_regressions,
    )


def format_comparison(result: CompareResult, fmt: str = "text") -> str:
    """Render a CompareResult as text or JSON."""
    if fmt == "json":
        return _to_json(result)
    return _to_text(result)


def _to_text(r: CompareResult) -> str:
    lines: list[str] = []
    lines.append(f"Comparison: {r.baseline_name} → {r.current_name}")
    lines.append(f"Gate: {'PASS' if r.gate_passed else 'FAIL — regressions detected'}")
    lines.append("")

    # Overall
    arrow = "+" if r.overall_delta >= 0 else ""
    lines.append(f"Overall average: {r.baseline_average:.4f} → {r.current_average:.4f} ({arrow}{r.overall_delta:.4f})")
    lines.append(
        f"Regressions: {r.regression_count} | Improvements: {r.improvement_count} | Unchanged: {r.unchanged_count}"
    )
    lines.append("")

    # Per-scenario table
    lines.append(f"{'Scenario':<40} {'Baseline':>8} {'Current':>8} {'Delta':>8} {'Gate':>6}")
    lines.append("-" * 72)
    for d in r.deltas:
        arrow = "+" if d.delta >= 0 else ""
        gate = "PASS" if d.current_gate else "FAIL"
        marker = " !!!" if d.delta < -_SCENARIO_REGRESSION_THRESHOLD else ""
        lines.append(
            f"{d.scenario_id:<40} {d.baseline_overall:>8.4f} {d.current_overall:>8.4f} "
            f"{arrow}{d.delta:>7.4f} {gate:>6}{marker}"
        )

    if r.new_scenarios:
        lines.append("")
        lines.append(f"New scenarios (no baseline): {', '.join(r.new_scenarios)}")
    if r.removed_scenarios:
        lines.append(f"Removed scenarios: {', '.join(r.removed_scenarios)}")

    return "\n".join(lines)


def _to_json(r: CompareResult) -> str:
    payload = {
        "baseline_name": r.baseline_name,
        "current_name": r.current_name,
        "baseline_average": r.baseline_average,
        "current_average": r.current_average,
        "overall_delta": r.overall_delta,
        "regression_count": r.regression_count,
        "improvement_count": r.improvement_count,
        "unchanged_count": r.unchanged_count,
        "gate_passed": r.gate_passed,
        "new_scenarios": r.new_scenarios,
        "removed_scenarios": r.removed_scenarios,
        "deltas": [
            {
                "scenario_id": d.scenario_id,
                "category": d.category,
                "baseline_overall": d.baseline_overall,
                "current_overall": d.current_overall,
                "delta": d.delta,
                "baseline_gate": d.baseline_gate,
                "current_gate": d.current_gate,
                "dimension_deltas": d.dimension_deltas,
            }
            for d in r.deltas
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True)
