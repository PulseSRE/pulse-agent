"""Skill plan data model for phased incident resolution.

A SkillPlan is a directed graph of SkillPhases. Each phase runs a skill,
produces structured output, and can branch based on findings. Plans enable
multi-phase incident resolution: triage → diagnose → remediate → verify → postmortem.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SkillPhase:
    """A single phase in a skill execution plan."""

    id: str  # "triage", "diagnose", "remediate", "verify"
    skill_name: str  # which skill to load for this phase
    required: bool = True  # must complete or plan fails
    depends_on: list[str] = field(default_factory=list)  # phase IDs that must complete first
    timeout_seconds: int = 120  # hard timeout per phase
    produces: list[str] = field(default_factory=list)  # output field names
    branch_on: str | None = None  # field from prior output to branch on
    branches: dict[str, list[str]] = field(default_factory=dict)  # branch_value -> skill_names
    parallel_with: list[str] | None = None  # phase IDs to run concurrently
    subplan: str | None = None  # incident_type of a whole plan to run as this phase
    approval_required: bool = False  # human gate before execution
    runs: str = "on_success"  # "on_success" | "always"
    success_condition: str = ""  # PromQL or check expression for verify phases
    retry_limit: int = 1  # max attempts before marking failed


@dataclass
class SkillPlan:
    """A directed graph of skill phases for structured incident resolution."""

    id: str
    name: str  # "latency-degradation-v2"
    phases: list[SkillPhase] = field(default_factory=list)
    incident_type: str = ""  # for template matching
    max_total_duration: int = 1800  # 30 min hard cap
    generated_by: str = "human"  # "human" | "auto"
    reviewed: bool = True


@dataclass
class SkillOutput:
    """Structured output from a skill phase — passed to subsequent phases."""

    skill_id: str
    phase_id: str
    status: str = "complete"  # "complete" | "partial" | "failed" | "needs_escalation"
    findings: dict = field(default_factory=dict)  # structured diagnosis
    branch_signal: str | None = None  # activates conditional branches
    evidence_summary: str = ""  # max 300 tokens — compressed key facts
    actions_taken: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    confidence: float = 0.0
    # Fields the phase declared in `produces` but did not return (phase_judge).
    # Kept structurally rather than only as a note inside evidence_summary,
    # because the reason a phase went partial is the part an operator needs and
    # was the part being dropped at the persistence boundary.
    contract_missing: list[str] = field(default_factory=list)


@dataclass
class PlanResult:
    """Result of executing a complete skill plan."""

    plan_id: str
    plan_name: str
    status: str = "complete"  # "complete" | "partial" | "failed" | "cancelled"
    phase_outputs: dict[str, SkillOutput] = field(default_factory=dict)  # phase_id -> output
    total_duration_ms: int = 0
    phases_completed: int = 0
    phases_total: int = 0


def validate_plan(plan: SkillPlan) -> list[str]:
    """Validate a skill plan for structural correctness.

    Returns list of error messages. Empty list = valid.
    """
    errors: list[str] = []

    if not plan.phases:
        errors.append("Plan has no phases")
        return errors

    phase_ids = {p.id for p in plan.phases}

    # Check for duplicate IDs
    if len(phase_ids) != len(plan.phases):
        seen: set[str] = set()
        for p in plan.phases:
            if p.id in seen:
                errors.append(f"Duplicate phase ID: {p.id}")
            seen.add(p.id)

    # Check dependencies reference valid phases
    for phase in plan.phases:
        for dep in phase.depends_on:
            if dep not in phase_ids:
                errors.append(f"Phase '{phase.id}' depends on unknown phase '{dep}'")

        # Check parallel_with references valid phases
        if phase.parallel_with:
            for par in phase.parallel_with:
                if par not in phase_ids:
                    errors.append(f"Phase '{phase.id}' parallel_with unknown phase '{par}'")

        # Check branch targets reference valid skill names (can't validate at data model level)
        # but branch_on must reference a field in produces of a dependency
        if phase.branch_on and not phase.depends_on:
            errors.append(f"Phase '{phase.id}' has branch_on but no depends_on")

    # Cycle detection (topological sort)
    visited: set[str] = set()
    in_stack: set[str] = set()
    dep_map = {p.id: set(p.depends_on) for p in plan.phases}

    def has_cycle(node: str) -> bool:
        if node in in_stack:
            return True
        if node in visited:
            return False
        visited.add(node)
        in_stack.add(node)
        for dep in dep_map.get(node, set()):
            if has_cycle(dep):
                return True
        in_stack.discard(node)
        return False

    for pid in phase_ids:
        if has_cycle(pid):
            errors.append(f"Cycle detected involving phase '{pid}'")
            break

    return errors


def topological_order(plan: SkillPlan) -> list[SkillPhase]:
    """Return phases in dependency-respecting execution order."""
    dep_map = {p.id: set(p.depends_on) for p in plan.phases}
    phase_map = {p.id: p for p in plan.phases}

    ordered: list[SkillPhase] = []
    remaining = set(dep_map.keys())

    while remaining:
        # Find phases with all deps satisfied
        ready = {pid for pid in remaining if dep_map[pid].issubset({p.id for p in ordered})}
        if not ready:
            break  # Cycle — should have been caught by validate_plan
        for pid in sorted(ready):  # Deterministic order
            ordered.append(phase_map[pid])
            remaining.discard(pid)

    return ordered
