"""Weekly markdown digest for eval and outcome trends."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any

from .outcomes import analyze_windows
from .runner import evaluate_suite
from .scenarios import load_suite


def _status(value: bool) -> str:
    return "PASS" if value else "FAIL"


def _suite_line(name: str, result: Any) -> str:
    return (
        f"- `{name}`: {_status(result.gate_passed)} "
        f"(scenarios={result.scenario_count}, avg={result.average_overall:.3f})"
    )


def _top_failing_categories(*results: Any) -> list[tuple[str, int]]:
    categories: Counter[str] = Counter()
    for result in results:
        for scenario in result.scenarios:
            if not scenario.passed_gate:
                categories[scenario.category] += 1
    return categories.most_common(3)


def render_weekly_digest(
    *,
    current_days: int = 7,
    baseline_days: int = 7,
    db_path: str = "",
) -> str:
    """Render markdown digest summarizing quality gate and trend health."""
    release_result = evaluate_suite("release", load_suite("release"))
    safety_result = evaluate_suite("safety", load_suite("safety"))
    integration_result = evaluate_suite("integration", load_suite("integration"))

    if db_path:
        outcomes = analyze_windows(db_path=db_path, current_days=current_days, baseline_days=baseline_days)
    else:
        outcomes = analyze_windows(current_days=current_days, baseline_days=baseline_days)

    lines: list[str] = []
    lines.append("# Pulse Agent Weekly Eval Digest")
    lines.append("")
    lines.append(f"Generated: {datetime.now(tz=UTC).isoformat()}")
    lines.append("")
    lines.append("## Gate status")
    lines.append(_suite_line("release", release_result))
    lines.append(_suite_line("safety", safety_result))
    lines.append(_suite_line("integration", integration_result))
    lines.append(
        f"- `outcomes`: {_status(bool(outcomes['gate_passed']))} "
        f"(current_actions={outcomes['current']['total_actions']}, "
        f"baseline_actions={outcomes['baseline']['total_actions']})"
    )
    lines.append("")
    lines.append("## Top failing categories")
    top_failures = _top_failing_categories(release_result, safety_result, integration_result)
    if not top_failures:
        lines.append("- none")
    else:
        for category, count in top_failures:
            lines.append(f"- `{category}`: {count} failing scenarios")
    lines.append("")
    lines.append("## Outcome regressions")
    regressions = outcomes.get("regressions", {})
    if not regressions:
        lines.append("- none")
    else:
        for key, active in regressions.items():
            lines.append(f"- `{key}`: {'REGRESSION' if active else 'ok'}")
    lines.append("")
    lines.append("## Recommended focus")
    if not release_result.gate_passed:
        lines.append("- Fix `release` suite blockers before merging.")
    if not outcomes.get("gate_passed", True):
        lines.append("- Investigate outcome regressions before enabling wider automation.")
    if release_result.gate_passed and outcomes.get("gate_passed", True):
        lines.append("- Quality is stable; expand release scenarios with new high-risk cases.")
    return "\n".join(lines)
