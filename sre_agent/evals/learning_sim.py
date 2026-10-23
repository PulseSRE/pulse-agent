"""Learning simulator — does Pulse get better at incidents it has seen before?

Feeds a scripted stream of incidents through the real fix planner, action
ledger, verification pipeline, recurrence check, trajectory gate, skill
lifecycle and fix-strategy learning, and reports what each incident got.

Only the cluster and the model are scripted:

- a world says, per scenario, whether each fix strategy holds, fails outright,
  or looks healthy and then recurs;
- an investigation, where the scenario has one, returns a fixed diagnosis.

Everything that decides and learns is production code running against a
PostgreSQL database. That is what this proves: the machinery learns end to
end. It does not prove the model diagnoses real incidents better; that needs
live traffic.

The database must be disposable: each scenario starts by emptying the tables
the learning loops write to.

    PULSE_AGENT_TEST_DATABASE_URL=postgresql://... python -m sre_agent.evals.learning_sim
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
import uuid
from contextlib import ExitStack
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

HOLDS = "holds"
FAILS = "fails"
RECURS = "recurs"

# Tables the learning loops write to; emptied at the start of each scenario.
LEARNING_TABLES = (
    "fix_outcomes",
    "actions",
    "learning_candidates",
    "incidents",
    "runbooks",
    "patterns",
    "runtime_artifacts",
    "inbox_items",
)


@dataclass(frozen=True)
class Scenario:
    name: str
    summary: str
    category: str  # the finding category a scanner would raise
    # What a strategy does on this cluster. Missing means it fails.
    outcomes: dict[str, str]
    # The scripted investigation's diagnosis, or None for "the investigation
    # did not finish in time" (auto-fix then takes the category's fast path).
    diagnosis: str | None = None
    confidence: float = 0.85
    tools: tuple[str, ...] = ("get_pod_logs", "describe_pod", "get_events")
    incidents: int = 8


SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in (
        Scenario(
            name="transient-crashloop",
            summary="A restart fixes it. Pulse should keep fixing it, and learn a skill for it.",
            category="crashloop",
            diagnosis="Lost its leader-election lease during a brief API server outage; not a code fault",
            outcomes={"restart_controller": HOLDS},
        ),
        Scenario(
            name="config-crashloop",
            summary="A restart never helps (bad config) and nothing diagnoses it in time. "
            "Pulse should stop restarting and ask a person.",
            category="crashloop",
            diagnosis=None,
            outcomes={"restart_controller": FAILS},
        ),
        Scenario(
            name="memory-leak",
            summary="OOM from a leak: raising the limit and restarting both look healthy, then recur. "
            "Pulse should try the alternative, then ask a person.",
            category="oom",
            diagnosis="OOMKilled: container exceeded memory limit; usage climbs steadily until killed",
            outcomes={"patch_resources": RECURS, "restart_controller": RECURS},
        ),
    )
}


@dataclass
class IncidentResult:
    index: int
    workload: str
    strategy: str
    verdict: str  # held / failed / recurred / escalated / no_plan
    learned: str = ""  # the plan's track-record note, if any


@dataclass
class ScenarioResult:
    scenario: Scenario
    incidents: list[IncidentResult] = field(default_factory=list)
    skill: dict | None = None  # the learned skill at the end, if one exists

    @property
    def fixes_that_did_not_hold(self) -> int:
        return sum(1 for i in self.incidents if i.verdict in ("failed", "recurred"))

    @property
    def first_escalation(self) -> int | None:
        return next((i.index for i in self.incidents if i.verdict == "escalated"), None)

    def to_dict(self) -> dict:
        return {
            "scenario": self.scenario.name,
            "summary": self.scenario.summary,
            "incidents": [i.__dict__ for i in self.incidents],
            "fixes_that_did_not_hold": self.fixes_that_did_not_hold,
            "first_escalation": self.first_escalation,
            "skill": self.skill,
        }


class _World:
    """The scripted cluster: which workloads are broken, and what each fix does."""

    def __init__(self, scenario: Scenario):
        self.scenario = scenario
        self.broken: set[str] = set()
        self.recur_next_scan: set[str] = set()

    def apply(self, workload: str, strategy: str) -> None:
        effect = self.scenario.outcomes.get(strategy, FAILS)
        if effect == FAILS:
            return
        self.broken.discard(workload)
        if effect == RECURS:
            self.recur_next_scan.add(workload)

    def next_scan(self) -> set[str]:
        """Advance one scan; returns workloads whose condition came back."""
        returned = set(self.recur_next_scan)
        self.broken |= returned
        self.recur_next_scan.clear()
        return returned


def _finding(scenario: Scenario, workload: str, index: int) -> dict:
    from ..monitor.findings import _make_finding

    finding = _make_finding(
        "critical",
        scenario.category,
        f"Pod {workload}-7f9c8d6b5-x{index:03d} {scenario.category}",
        "simulated",
        [{"kind": "Pod", "name": f"{workload}-7f9c8d6b5-x{index:03d}", "namespace": "shop"}],
    )
    finding["id"] = f"f-{uuid.uuid4().hex[:10]}"
    return finding


def _reset(db) -> None:
    for table in LEARNING_TABLES:
        try:
            db.execute(f"DELETE FROM {table}")
            db.commit()
        except Exception:
            pass  # a table this schema does not have yet holds nothing to reset


def _fake_executor(world: _World, tool: str):
    def run(plan):
        workload = plan.params["resources"][0]["name"].rsplit("-", 2)[0]
        world.apply(workload, plan.strategy)
        return (tool, f"{workload} before", f"{workload} after {plan.strategy}")

    return run


async def _run_scenario(scenario: Scenario) -> ScenarioResult:
    from ..db import get_database
    from ..fix_learning import HUMAN_REVIEW
    from ..monitor import fix_planner, health_gate
    from ..monitor.actions import save_action
    from ..monitor.findings import _ts
    from ..monitor.recurrence import process_recurrences
    from ..monitor.verification_pipeline import process_verifications
    from ..skill_loader import list_skills, reload_skills
    from ..trajectory import LearningCandidate, candidate_key, get_learner, reset_learner

    db = get_database()
    _reset(db)
    reset_learner()
    reload_skills()

    world = _World(scenario)
    result = ScenarioResult(scenario)
    investigations: dict[str, dict] = {}
    # Only the attributes the verification and recurrence steps read.
    monitor: Any = SimpleNamespace(_pending_verifications={}, _scan_counter=0)

    async def _broadcast(_event):
        return None

    monitor._broadcast_raw = _broadcast

    def _health(resources):
        name = resources[0]["name"].rsplit("-", 2)[0] if resources else ""
        return (health_gate.FAIL, "still broken") if name in world.broken else (health_gate.PASS, "2/2 ready")

    with ExitStack() as stack:
        stack.enter_context(patch.object(health_gate, "check_resources", side_effect=_health))
        stack.enter_context(
            patch.object(fix_planner, "get_investigation_for_finding", side_effect=lambda fid: investigations.get(fid))
        )
        stack.enter_context(
            patch.dict(
                fix_planner._EXECUTORS,
                {
                    "restart_controller": _fake_executor(world, "delete_pod"),
                    "patch_resources": _fake_executor(world, "patch_resources"),
                },
            )
        )

        for index in range(1, scenario.incidents + 1):
            workload = f"svc{index}"  # a different workload every time: per-resource caps cannot explain a change
            finding = _finding(scenario, workload, index)
            world.broken.add(workload)

            if scenario.diagnosis is not None:
                investigations[finding["id"]] = {
                    "suspected_cause": scenario.diagnosis,
                    "recommended_fix": "simulated",
                    "confidence": scenario.confidence,
                }
                if scenario.confidence >= 0.75:
                    get_learner().record(
                        LearningCandidate(
                            key=candidate_key(scenario.category, finding["resources"]),
                            category=scenario.category,
                            title=finding["title"],
                            root_cause=scenario.diagnosis,
                            summary=scenario.diagnosis,
                            confidence=scenario.confidence,
                            evidence=[{"kind": "log", "observation": scenario.diagnosis}],
                            tools_called=list(scenario.tools),
                        )
                    )

            investigation = investigations.get(finding["id"])
            plan = fix_planner.plan_fix(investigation, finding) if investigation else None
            if plan is None:
                plan = fix_planner.default_fix_plan(scenario.category, finding)
            if plan is None:
                result.incidents.append(IncidentResult(index, workload, "", "no_plan"))
                continue
            learned = plan.params.get("track_record", "")
            if plan.strategy == HUMAN_REVIEW:
                result.incidents.append(IncidentResult(index, workload, plan.strategy, "escalated", learned))
                world.broken.discard(workload)  # a person takes it from here
                continue

            execution = fix_planner.execute_fix_with_snapshot(plan)
            tool, before, after = execution.result
            action_id = f"a-{uuid.uuid4().hex[:10]}"
            save_action(
                {
                    "id": action_id,
                    "findingId": finding["id"],
                    "timestamp": _ts(),
                    "category": scenario.category,
                    "tool": tool,
                    "status": "completed",
                    "beforeState": before,
                    "afterState": after,
                    "fixStrategy": plan.strategy,
                    "causeCategory": plan.cause_category,
                },
                category=scenario.category,
                resources=finding["resources"],
                finding=finding,
            )
            monitor._pending_verifications[action_id] = {
                "action_id": action_id,
                "finding_id": finding["id"],
                "category": scenario.category,
                "resources": finding["resources"],
                "verify_resources": finding["resources"],
                "target_scan": monitor._scan_counter + 1,
                "strategy": plan.strategy,
                "cause": plan.cause_category,
            }

            # Next scan: verification reads the world.
            monitor._scan_counter += 1
            active = [finding] if workload in world.broken else []
            await process_verifications(monitor, active)
            verdict = "held" if workload not in world.broken else "failed"

            # The scan after: a recurring fix shows its hand.
            monitor._scan_counter += 1
            if workload in world.next_scan():
                again = _finding(scenario, workload, index)
                again["title"] = finding["title"]
                again["resources"] = finding["resources"]
                await process_recurrences(monitor, [again])
                verdict = "recurred"
            world.broken.discard(workload)
            result.incidents.append(IncidentResult(index, workload, plan.strategy, verdict, learned))

    reload_skills()
    skill = next((s for s in list_skills() if s.generated_by == "auto" and s.incident_type == scenario.category), None)
    if skill is not None:
        result.skill = {"name": skill.name, "version": skill.version, "reviewed": skill.reviewed}
    # Leave nothing behind: what one run learned must not leak into whatever
    # uses this database or this process next.
    _reset(db)
    reset_learner()
    return result


def simulate(names: list[str] | None = None) -> list[ScenarioResult]:
    """Run the named scenarios (default: all) and return what each incident got."""
    from ..config import _reset_settings

    chosen = [SCENARIOS[n] for n in (names or list(SCENARIOS))]
    keys = ("PULSE_AGENT_USER_SKILLS_DIR", "PULSE_AGENT_USER_PLANS_DIR")
    old = {k: os.environ.get(k) for k in keys}
    results = []
    try:
        for scenario in chosen:
            # Learned skills and plans go to a scratch directory per scenario:
            # never the package or a real user directory, and never what an
            # earlier scenario learned.
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["PULSE_AGENT_USER_SKILLS_DIR"] = os.path.join(tmp, "skills")
                os.environ["PULSE_AGENT_USER_PLANS_DIR"] = os.path.join(tmp, "plans")
                _reset_settings()
                results.append(asyncio.run(_run_scenario(scenario)))
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _reset_settings()
        from ..skill_loader import reload_skills

        reload_skills()
    return results


def _print(results: list[ScenarioResult]) -> None:
    for r in results:
        print(f"\n{r.scenario.name}: {r.scenario.summary}")
        for i in r.incidents:
            note = f"  ({i.learned})" if i.learned else ""
            print(f"  #{i.index:<2} {i.strategy or '-':<22} {i.verdict:<10}{note}")
        print(f"  fixes that did not hold: {r.fixes_that_did_not_hold}; first escalation: {r.first_escalation}")
        if r.skill:
            print(f"  learned skill: {r.skill['name']} v{r.skill['version']} (reviewed={r.skill['reviewed']})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("scenarios", nargs="*", metavar="SCENARIO", help="default: all of " + ", ".join(SCENARIOS))
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--verbose", action="store_true", help="show the agent's own logs")
    args = parser.parse_args(argv)
    if not args.verbose:
        import logging

        # Skills report missing tools here because no tool modules are loaded;
        # that is expected and is not what the simulator is about.
        logging.getLogger("pulse_agent").setLevel(logging.ERROR)

    url = os.environ.get("PULSE_AGENT_TEST_DATABASE_URL")
    if not url:
        parser.error("set PULSE_AGENT_TEST_DATABASE_URL to a disposable database; the simulator empties tables in it")
    os.environ["PULSE_AGENT_DATABASE_URL"] = url
    from ..config import _reset_settings
    from ..db import get_database, reset_database
    from ..db_migrations import run_migrations

    _reset_settings()
    reset_database()
    run_migrations(get_database())

    results = simulate(args.scenarios or None)
    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        _print(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
