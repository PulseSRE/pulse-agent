"""Scenario loading for deterministic eval suites."""

from __future__ import annotations

import json
from importlib import resources

from .types import EvalExpected, EvalScenario


def _expected_from_raw(raw: dict) -> EvalExpected | None:
    if not raw:
        return None
    return EvalExpected(
        min_overall=raw.get("min_overall"),
        max_overall=raw.get("max_overall"),
        should_block_release=raw.get("should_block_release"),
        required_blockers=list(raw.get("required_blockers", [])),
    )


def load_suite(suite_name: str) -> list[EvalScenario]:
    """Load eval scenarios from packaged JSON fixtures."""
    package = "sre_agent.evals.scenarios_data"
    file_name = f"{suite_name}.json"
    with resources.files(package).joinpath(file_name).open("r", encoding="utf-8") as fh:
        payload = json.load(fh)

    scenarios: list[EvalScenario] = []
    for raw in payload.get("scenarios", []):
        scenarios.append(
            EvalScenario(
                scenario_id=raw["scenario_id"],
                category=raw["category"],
                description=raw["description"],
                tool_calls=list(raw.get("tool_calls", [])),
                rejected_tools=int(raw.get("rejected_tools", 0)),
                duration_seconds=float(raw.get("duration_seconds", 0.0)),
                user_confirmed_resolution=raw.get("user_confirmed_resolution"),
                final_response=raw.get("final_response", ""),
                had_policy_violation=bool(raw.get("had_policy_violation", False)),
                hallucinated_tool=bool(raw.get("hallucinated_tool", False)),
                missing_confirmation=bool(raw.get("missing_confirmation", False)),
                verification_passed=raw.get("verification_passed"),
                rollback_available=bool(raw.get("rollback_available", False)),
                rollback_used=bool(raw.get("rollback_used", False)),
                retry_attempts=int(raw.get("retry_attempts", 0)),
                transient_failures=int(raw.get("transient_failures", 0)),
                completed=bool(raw.get("completed", True)),
                expected=_expected_from_raw(raw.get("expected", {})),
            )
        )
    return scenarios
