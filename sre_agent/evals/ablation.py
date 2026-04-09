"""Prompt section ablation framework.

Tests whether removing specific prompt sections affects eval scores.
Each section is excluded via PULSE_PROMPT_EXCLUDE_SECTIONS env var and
the eval suite is re-run to measure the impact.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .runner import evaluate_suite
from .scenarios import load_suite

# All sections that can be ablated
ALL_SECTIONS = [
    "chain_hints",
    "intelligence_query_reliability",
    "intelligence_dashboard_patterns",
    "intelligence_error_hotspots",
    "intelligence_token_efficiency",
    "intelligence_harness_effectiveness",
    "intelligence_routing_accuracy",
    "intelligence_feedback_analysis",
    "intelligence_token_trending",
    "component_schemas",
    "component_hint_ops",
    "component_hint_core",
]


@dataclass
class SectionResult:
    section: str
    baseline_average: float
    ablated_average: float
    delta: float
    chars_saved: int


@dataclass
class AblationResult:
    suite: str
    mode: str
    baseline_average: float
    results: list[SectionResult]

    @property
    def sorted_by_impact(self) -> list[SectionResult]:
        """Sections sorted by score impact (most harmful removal first)."""
        return sorted(self.results, key=lambda r: r.delta)

    @property
    def trim_candidates(self) -> list[SectionResult]:
        """Sections where removal doesn't hurt (delta >= -0.01)."""
        return [r for r in self.results if r.delta >= -0.01]


def run_ablation(
    suite: str = "release",
    sections: list[str] | None = None,
    mode: str = "sre",
) -> AblationResult:
    """Run ablation tests on prompt sections.

    For each section, excludes it via env var, re-runs the eval suite,
    and measures the score delta vs baseline.
    """
    sections_to_test = sections or ALL_SECTIONS

    # Get token costs for each section
    from ..harness import measure_prompt_sections

    audit = measure_prompt_sections(mode=mode)
    section_chars = {s["name"]: s["chars"] for s in audit["sections"]}

    # Baseline: run with all sections enabled
    old_env = os.environ.pop("PULSE_PROMPT_EXCLUDE_SECTIONS", None)
    try:
        scenarios = load_suite(suite)
        baseline_result = evaluate_suite(suite, scenarios)
        baseline_avg = baseline_result.average_overall

        results: list[SectionResult] = []
        for section in sections_to_test:
            os.environ["PULSE_PROMPT_EXCLUDE_SECTIONS"] = section
            ablated_result = evaluate_suite(suite, load_suite(suite))
            ablated_avg = ablated_result.average_overall
            delta = round(ablated_avg - baseline_avg, 4)

            results.append(
                SectionResult(
                    section=section,
                    baseline_average=baseline_avg,
                    ablated_average=ablated_avg,
                    delta=delta,
                    chars_saved=section_chars.get(section, 0),
                )
            )

        return AblationResult(
            suite=suite,
            mode=mode,
            baseline_average=baseline_avg,
            results=results,
        )
    finally:
        os.environ.pop("PULSE_PROMPT_EXCLUDE_SECTIONS", None)
        if old_env is not None:
            os.environ["PULSE_PROMPT_EXCLUDE_SECTIONS"] = old_env


def format_ablation(result: AblationResult, fmt: str = "text") -> str:
    """Format ablation results."""
    if fmt == "json":
        return json.dumps(
            {
                "suite": result.suite,
                "mode": result.mode,
                "baseline_average": result.baseline_average,
                "results": [
                    {
                        "section": r.section,
                        "baseline_average": r.baseline_average,
                        "ablated_average": r.ablated_average,
                        "delta": r.delta,
                        "chars_saved": r.chars_saved,
                    }
                    for r in result.sorted_by_impact
                ],
                "trim_candidates": [r.section for r in result.trim_candidates],
            },
            indent=2,
        )

    lines = [
        f"Ablation Report — suite: {result.suite}, mode: {result.mode}",
        f"Baseline average: {result.baseline_average:.4f}",
        "",
        f"{'Section':<40} {'Delta':>8} {'Chars':>8} {'Verdict':>12}",
        "-" * 72,
    ]
    for r in result.sorted_by_impact:
        verdict = "KEEP" if r.delta < -0.01 else "TRIM?" if r.delta >= 0 else "neutral"
        arrow = "+" if r.delta >= 0 else ""
        lines.append(f"{r.section:<40} {arrow}{r.delta:>7.4f} {r.chars_saved:>8} {verdict:>12}")

    if result.trim_candidates:
        lines.append("")
        lines.append(f"Trim candidates (removal doesn't hurt): {', '.join(r.section for r in result.trim_candidates)}")

    return "\n".join(lines)
