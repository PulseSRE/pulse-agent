"""Pure sequencing decisions for the plan interpreter workflow.

Everything a workflow decides must be deterministic, so the decisions live
here as pure functions over plain dicts — testable without Temporal, and
incapable of sneaking in IO.
"""

from __future__ import annotations

#: Plan features the interpreter does not execute yet. Empty since branching
#: and wave-parallelism landed — kept (with its checker) because the refusal
#: machinery is the right place for the *next* feature the interpreter cannot
#: honour, and because the workflow's pre-patch replay path still calls it.
UNSUPPORTED_PHASE_FEATURES: tuple[str, ...] = ()


def unsupported_features(plan: dict) -> list[str]:
    """Features in ``plan`` the interpreter can't honour, empty when runnable."""
    found: set[str] = set()
    for phase in plan.get("phases", []):
        for feature in UNSUPPORTED_PHASE_FEATURES:
            if phase.get(feature):
                found.add(f"{phase.get('id', '?')}.{feature}")
    return sorted(found)


def resolve_branch(phase: dict, outputs: dict[str, dict]) -> str | None:
    """The skill a ``branch_on`` phase should run, or None to keep its own.

    Mirrors the in-process engine exactly (plan_runtime's branch block):
    walk the phase's dependencies in declared order; the first one whose
    findings carry the ``branch_on`` key — or which emitted a
    ``branch_signal`` — supplies the branch value; ``branches[str(value)]``
    names the skills and the first is taken. Any miss (no value, no matching
    branch, empty skill list) leaves the phase's declared skill in place,
    which is also what the engine does.
    """
    branch_key = phase.get("branch_on")
    branches = phase.get("branches") or {}
    if not branch_key or not branches:
        return None
    branch_value = None
    for dep_id in phase.get("depends_on", []):
        dep = outputs.get(dep_id)
        if not dep:
            continue
        if dep.get("findings", {}).get(branch_key):
            branch_value = dep["findings"][branch_key]
            break
        if dep.get("branch_signal"):
            branch_value = dep["branch_signal"]
            break
    if branch_value is None:
        return None
    matched = branches.get(str(branch_value), [])
    return matched[0] if matched else None


def ready_phases(phases: list[dict], done: set[str]) -> list[dict]:
    """Phases whose dependencies are all settled and which have not run.

    "Settled" is membership in ``done`` regardless of how the phase ended —
    the in-process engine lets optional phases fail without blocking their
    dependents, and the interpreter keeps that behaviour.
    """
    out = []
    for phase in phases:
        if phase["id"] in done:
            continue
        if all(dep in done for dep in phase.get("depends_on", [])):
            out.append(phase)
    return out


def derive_status(phases: list[dict], outputs: dict[str, dict]) -> str:
    """Overall plan status from per-phase outcomes, mirroring the in-process engine.

    A required phase that failed (or never ran) makes the plan ``partial``;
    a phase awaiting a human leaves it ``partial`` too — the plan finished
    everything it was allowed to finish. All complete means ``complete``.
    """
    saw_incomplete = False
    for phase in phases:
        out = outputs.get(phase["id"])
        status = out.get("status") if out else None
        if status in ("complete",):
            continue
        if phase.get("required", True) and status in (None, "failed"):
            return "partial"
        saw_incomplete = True
    return "partial" if saw_incomplete else "complete"
