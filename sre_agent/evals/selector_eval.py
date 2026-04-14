"""Selector-specific eval framework — measures routing accuracy."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from importlib import resources

logger = logging.getLogger("pulse_agent.evals.selector")


@dataclass
class SelectorEvalResult:
    recall_at_5: float = 0.0
    precision_at_3: float = 0.0
    latency_p99_ms: float = 0.0
    cold_start_coverage: float = 0.0
    total_scenarios: int = 0
    passed: int = 0
    failed_scenarios: list[dict] = field(default_factory=list)


def run_selector_eval(suite_path: str = "selector") -> SelectorEvalResult:
    """Run the selector eval suite. Deterministic — no LLM calls."""
    from ..skill_loader import _keyword_index, _skills, load_skills
    from ..skill_selector import SkillSelector

    # Ensure skills loaded
    load_skills()
    selector = SkillSelector(_skills, keyword_index=_keyword_index)

    # Load scenarios
    package = "sre_agent.evals.scenarios_data"
    file_name = f"{suite_path}.json"
    with resources.files(package).joinpath(file_name).open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    scenarios = data.get("scenarios", [])
    result = SelectorEvalResult(total_scenarios=len(scenarios))

    latencies: list[float] = []
    correct_in_top_5 = 0
    correct_in_top_3 = 0
    got_at_least_one = 0

    for scenario in scenarios:
        query = scenario["query"]
        expected = scenario["expected_skill"]
        acceptable = set(scenario.get("acceptable", [expected]))

        start = time.monotonic()
        selection = selector.select(query)
        elapsed_ms = (time.monotonic() - start) * 1000
        latencies.append(elapsed_ms)

        selected = selection.skill_name

        # Recall@5: is expected skill in fused_scores top 5?
        top_5 = sorted(selection.fused_scores.keys(), key=lambda k: -selection.fused_scores[k])[:5]
        if expected in top_5 or selected in acceptable:
            correct_in_top_5 += 1

        # Precision@3: of top 3, how many are acceptable?
        top_3 = sorted(selection.fused_scores.keys(), key=lambda k: -selection.fused_scores[k])[:3]
        if top_3:
            precision = sum(1 for s in top_3 if s in acceptable) / len(top_3)
            correct_in_top_3 += precision
        else:
            # No scores = fallback. Count as correct if selected is acceptable.
            if selected in acceptable:
                correct_in_top_3 += 1

        # Cold start: did we get at least one skill?
        if selection.fused_scores or selected:
            got_at_least_one += 1

        # Pass/fail
        if selected in acceptable:
            result.passed += 1
        else:
            result.failed_scenarios.append({
                "id": scenario["id"],
                "query": query[:60],
                "expected": expected,
                "got": selected,
                "scores": {k: round(v, 3) for k, v in selection.fused_scores.items()},
            })

    n = max(len(scenarios), 1)
    result.recall_at_5 = round(correct_in_top_5 / n, 4)
    result.precision_at_3 = round(correct_in_top_3 / n, 4)
    result.cold_start_coverage = round(got_at_least_one / n, 4)

    if latencies:
        latencies.sort()
        p99_idx = min(int(len(latencies) * 0.99), len(latencies) - 1)
        result.latency_p99_ms = round(latencies[p99_idx], 2)

    return result


def format_selector_eval(result: SelectorEvalResult) -> str:
    """Format selector eval as text."""
    lines = [
        f"Selector Eval: {result.passed}/{result.total_scenarios} passed",
        f"  Recall@5:           {result.recall_at_5:.2%}",
        f"  Precision@3:        {result.precision_at_3:.2%}",
        f"  Latency p99:        {result.latency_p99_ms:.1f}ms",
        f"  Cold start coverage: {result.cold_start_coverage:.2%}",
    ]
    if result.failed_scenarios:
        lines.append("  Failures:")
        for f in result.failed_scenarios:
            lines.append(f"    {f['id']}: expected={f['expected']} got={f['got']} ({f['query']})")
    return "\n".join(lines)
