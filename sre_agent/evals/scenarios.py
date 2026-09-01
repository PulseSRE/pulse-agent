"""Scenario loading for deterministic eval suites.

Suites are read from two places and merged: the JSON bundled inside the
package (read-only on the cluster) and the writable evals directory, where
runtime-scaffolded scenarios land and DB-persisted ones are hydrated at boot
(see ``eval_store``). Without the second source, every eval scenario the
agent scaffolded from a verified resolution was persisted and then never
read back — the suite scored only what shipped in the image.
"""

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


def _packaged_payload(suite_name: str) -> dict | None:
    package = "sre_agent.evals.scenarios_data"
    file_name = f"{suite_name}.json"
    source = resources.files(package).joinpath(file_name)
    if not source.is_file():
        return None
    with source.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _runtime_payload(suite_name: str) -> dict | None:
    from ..eval_store import scenarios_dir

    path = scenarios_dir() / f"{suite_name}.json"
    if not path.is_file():
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_raw_suite(suite_name: str) -> dict:
    """Merged raw suite payload: packaged scenarios plus runtime-written ones.

    On a scenario_id collision the runtime copy wins — it is the version that
    kept evolving after the image was built.
    """
    packaged = _packaged_payload(suite_name)
    runtime = _runtime_payload(suite_name)
    if packaged is None and runtime is None:
        raise FileNotFoundError(f"Eval suite not found in package or runtime dir: {suite_name}.json")

    merged = dict(packaged or runtime or {})
    by_id: dict[str, dict] = {}
    for payload in (packaged, runtime):
        for raw in (payload or {}).get("scenarios", []):
            by_id[raw["scenario_id"]] = raw
    merged["scenarios"] = list(by_id.values())
    return merged


def load_suite(suite_name: str) -> list[EvalScenario]:
    """Load eval scenarios from packaged JSON fixtures and the writable evals dir."""
    payload = load_raw_suite(suite_name)

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
                retry_attempts=int(raw.get("retry_attempts", 0)),
                transient_failures=int(raw.get("transient_failures", 0)),
                completed=bool(raw.get("completed", True)),
                expected=_expected_from_raw(raw.get("expected", {})),
            )
        )
    return scenarios
