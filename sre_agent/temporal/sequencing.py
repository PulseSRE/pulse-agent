"""Pure sequencing decisions for the plan interpreter workflow.

Everything a workflow decides must be deterministic, so the decisions live
here as pure functions over plain dicts — testable without Temporal, and
incapable of sneaking in IO.
"""

from __future__ import annotations

#: Plan features the interpreter does not execute yet. Plans using them keep
#: running on the in-process engine; the run endpoint refuses them with the
#: list of offending features rather than executing a plan half-faithfully.
UNSUPPORTED_PHASE_FEATURES = ("branch_on", "parallel_with")


def unsupported_features(plan: dict) -> list[str]:
    """Features in ``plan`` the interpreter can't honour, empty when runnable."""
    found: set[str] = set()
    for phase in plan.get("phases", []):
        for feature in UNSUPPORTED_PHASE_FEATURES:
            if phase.get(feature):
                found.add(f"{phase.get('id', '?')}.{feature}")
        if phase.get("branches"):
            found.add(f"{phase.get('id', '?')}.branches")
    return sorted(found)


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
