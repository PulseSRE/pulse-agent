"""Auto-generate eval scenarios when skills are scaffolded from resolutions.

Called as a side-effect of skill scaffolding (fire-and-forget).
Writes to sre_agent/evals/scenarios_data/scaffolded.json and
sre_agent/evals/fixtures/scaffolded_<id>.json.

All auto-generated scenarios have expected.should_block_release=false
so they never gate releases.
"""

from __future__ import annotations

import json
import logging
import re
import types
from pathlib import Path

_fcntl: types.ModuleType | None
try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None

logger = logging.getLogger("pulse_agent.eval_scaffolder")

_SCENARIOS_DIR = Path(__file__).parent / "evals" / "scenarios_data"
_FIXTURES_DIR = Path(__file__).parent / "evals" / "fixtures"
_SUITE_FILE = _SCENARIOS_DIR / "scaffolded.json"
_MAX_EVIDENCE_CHARS = 500


def _sanitize_id(skill_name: str, category: str) -> str | None:
    safe = re.sub(r"[^a-z0-9_-]", "", skill_name.lower().replace(" ", "-"))
    if not safe:
        return None
    cat = re.sub(r"[^a-z0-9_]", "", category.lower())
    return f"scaffolded_{safe}_{cat}"[:80]


def _bootstrap_suite() -> dict:
    return {
        "suite_name": "scaffolded",
        "description": "Auto-generated eval scenarios from skill scaffolding — informational, never gates releases",
        "scenarios": [],
    }


def _append_scenario(scenario: dict) -> bool:
    try:
        _SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)

        with open(_SUITE_FILE, "a+", encoding="utf-8") as fh:
            if _fcntl is not None:
                _fcntl.flock(fh, _fcntl.LOCK_EX)
            try:
                fh.seek(0)
                content = fh.read()
                if content.strip():
                    suite = json.loads(content)
                else:
                    suite = _bootstrap_suite()

                existing_ids = {s["scenario_id"] for s in suite.get("scenarios", [])}
                if scenario["scenario_id"] in existing_ids:
                    logger.debug("Scenario %s already exists, skipping", scenario["scenario_id"])
                    return False

                suite.setdefault("scenarios", []).append(scenario)

                fh.seek(0)
                fh.truncate()
                json.dump(suite, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            finally:
                if _fcntl is not None:
                    _fcntl.flock(fh, _fcntl.LOCK_UN)

        logger.info("Appended eval scenario: %s", scenario["scenario_id"])
        return True

    except Exception:
        logger.debug("Failed to append eval scenario", exc_info=True)
        return False


def _write_fixture(scenario_id: str, fixture: dict) -> bool:
    try:
        _FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
        fixture_path = _FIXTURES_DIR / f"{scenario_id}.json"

        resolved = fixture_path.resolve()
        if not str(resolved).startswith(str(_FIXTURES_DIR.resolve())):
            logger.warning("Path traversal blocked for fixture: %s", scenario_id)
            return False

        with open(fixture_path, "w", encoding="utf-8") as fh:
            json.dump(fixture, fh, indent=4, ensure_ascii=False)
            fh.write("\n")

        logger.info("Wrote eval fixture: %s", fixture_path.name)
        return True

    except Exception:
        logger.debug("Failed to write eval fixture", exc_info=True)
        return False


def _extract_keywords(text: str, max_count: int = 3) -> list[str]:
    tokens = re.findall(r"[A-Za-z]{3,}", text)
    seen: set[str] = set()
    result: list[str] = []
    for t in tokens:
        low = t.lower()
        if low not in seen and low not in {"the", "and", "was", "for", "that", "this", "with", "from"}:
            seen.add(low)
            result.append(t)
            if len(result) >= max_count:
                break
    return result


def scaffold_eval_from_plan(
    *,
    skill_name: str,
    finding: dict,
    plan_result: object,
    tools_called: list[str],
    confidence: float,
    duration_seconds: float,
) -> bool:
    category = finding.get("category", "unknown")
    scenario_id = _sanitize_id(skill_name, category)
    if not scenario_id:
        return False

    phase_outputs = getattr(plan_result, "phase_outputs", {})

    diagnose = phase_outputs.get("diagnose")
    root_cause = "unknown"
    evidence = ""
    if diagnose:
        root_cause = diagnose.findings.get("root_cause", "unknown") if hasattr(diagnose, "findings") else "unknown"
        evidence = getattr(diagnose, "evidence_summary", "")

    verify = phase_outputs.get("verify")
    verification_passed = None
    if verify:
        verification_passed = getattr(verify, "status", None) == "completed"

    has_write = any(
        t in tools_called
        for t in [
            "restart_deployment",
            "scale_deployment",
            "rollback_deployment",
            "delete_pod",
            "drain_node",
            "cordon_node",
            "apply_yaml",
            "patch_resource",
        ]
    )

    final_response = f"{finding.get('title', 'Incident')} resolved. Root cause: {root_cause}"
    if evidence:
        final_response = f"{evidence[:200]}. Root cause: {root_cause}"

    scenario = {
        "scenario_id": scenario_id,
        "category": "sre" if category not in ("security", "rbac", "compliance") else "security",
        "description": f"Auto-generated: {finding.get('title', skill_name)[:100]}",
        "tool_calls": tools_called[:10],
        "rejected_tools": 0,
        "duration_seconds": round(duration_seconds, 1),
        "user_confirmed_resolution": None,
        "final_response": final_response[:500],
        "verification_passed": verification_passed,
        "rollback_available": has_write,
        "retry_attempts": 0,
        "transient_failures": 0,
        "expected": {"should_block_release": False},
    }

    if not _append_scenario(scenario):
        return False

    recorded_responses: dict[str, str] = {}
    for _phase_id, output in phase_outputs.items():
        summary = getattr(output, "evidence_summary", "")
        if summary:
            for tool in getattr(output, "actions_taken", []):
                if tool not in recorded_responses:
                    recorded_responses[tool] = summary[:_MAX_EVIDENCE_CHARS]

    keywords = _extract_keywords(root_cause)

    fixture = {
        "name": scenario_id,
        "prompt": finding.get("title", skill_name),
        "recorded_responses": recorded_responses,
        "expected": {
            "should_mention": keywords,
            "should_use_tools": tools_called[:3],
            "max_tool_calls": min(len(tools_called) * 2, 15),
        },
    }

    _write_fixture(scenario_id, fixture)
    return True


def scaffold_eval_from_investigation(
    *,
    skill_name: str,
    finding: dict,
    investigation_result: dict,
) -> bool:
    category = finding.get("category", "unknown")
    scenario_id = _sanitize_id(skill_name, category)
    if not scenario_id:
        return False

    summary = investigation_result.get("summary", "")
    suspected_cause = investigation_result.get("suspectedCause", "unknown")

    final_response = f"{summary[:200]}. Suspected cause: {suspected_cause}"

    scenario = {
        "scenario_id": scenario_id,
        "category": "sre" if category not in ("security", "rbac", "compliance") else "security",
        "description": f"Auto-generated: {finding.get('title', skill_name)[:100]}",
        "tool_calls": ["proactive_investigation"],
        "rejected_tools": 0,
        "duration_seconds": 30.0,
        "user_confirmed_resolution": None,
        "final_response": final_response[:500],
        "verification_passed": None,
        "rollback_available": False,
        "retry_attempts": 0,
        "transient_failures": 0,
        "expected": {"should_block_release": False},
    }

    return _append_scenario(scenario)
