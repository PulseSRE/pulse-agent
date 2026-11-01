"""Check that a plan phase did what it said it would, before the plan moves on.

Every ``SkillPhase`` declares ``produces`` — the output fields that phase is
responsible for. Until now that list was only ever shown to the model as
"Expected outputs: ..." in the prompt. Nothing checked it. A diagnose phase
declaring ``produces: [root_cause, confidence]`` could return neither and the plan
would advance to remediation anyway, acting on a diagnosis that was never made.

This is the piece Hermes calls persistent goal behaviour: a judge decides whether
the objective was actually met, and execution continues if it was not. The check
here is deliberately deterministic — the contract is a list of field names, so
verifying it needs no second model call, no latency and no cost.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger("pulse_agent.phase_judge")

# Values that are technically present but carry no information. A phase that
# answers "unknown" has not satisfied its contract, it has filled in a blank.
_EMPTY_VALUES = {"", "unknown", "none", "n/a", "na", "null", "tbd", "unclear", "-"}


@dataclass
class PhaseVerdict:
    """Whether a phase met its declared contract, and what is missing if not."""

    phase_id: str
    satisfied: bool
    missing: list[str] = field(default_factory=list)
    reason: str = ""
    # Declared ``measured`` fields that were filled in but name nothing a
    # successful call in the phase examined (see ``unmeasured_fields``).
    unmeasured: list[str] = field(default_factory=list)

    def as_retry_hint(self) -> str:
        """A note to hand back to the model so a retry knows what was wrong."""
        if self.satisfied or not (self.missing or self.unmeasured):
            return ""
        parts = []
        if self.missing:
            parts.append(
                f"Your previous attempt at this phase did not produce: {', '.join(self.missing)}. "
                "Provide those fields explicitly. If a value genuinely cannot be "
                "determined, say so and set status to 'partial' rather than leaving "
                "it out or answering 'unknown'."
            )
        if self.unmeasured:
            parts.append(
                f"Your previous attempt named {', '.join(self.unmeasured)} without examining it: "
                "the value names no specific workload, path or process that one of your tool calls in this "
                "phase read. Name the specific one and read it (its logs, metrics or description), citing "
                "that call in your evidence. If you cannot measure it, say so; a diagnosis whose culprit "
                "was never examined is reported at reduced confidence."
            )
        return " ".join(parts)


def _is_present(value: object) -> bool:
    """Whether a produced value actually carries information."""
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in _EMPTY_VALUES
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) > 0
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return True
    return bool(value)


# A specific thing a value can name: a Kubernetes object name, a path, a
# dotted or underscored identifier. "container images" or "the kubelet" name a
# category, not something a call could have read.
_NAME_TOKEN = re.compile(r"[a-z0-9/][a-z0-9._/*-]*")


def _specific_names(text: str) -> set[str]:
    names = set()
    for token in _NAME_TOKEN.findall(text.lower()):
        token = token.strip("._-*")
        if len(token) >= 5 and any(c in token for c in "-_/.") and any(c.isalpha() for c in token):
            names.add(token)
    return names


def _input_names(summary: str) -> set[str]:
    """Specific names among a call's argument values ("pod_name=api-7f" -> api-7f)."""
    names: set[str] = set()
    for part in str(summary or "").split(", "):
        _, sep, value = part.partition("=")
        names |= _specific_names(value if sep else part)
    return names


def _incident_names(incident: dict | None) -> set[str]:
    """The finding's own resources: naming them as the culprit restates the finding."""
    names: set[str] = set()
    for resource in (incident or {}).get("resources") or []:
        if isinstance(resource, dict):
            for key in ("name", "namespace"):
                names |= _specific_names(str(resource.get(key) or ""))
    return names


def unmeasured_fields(phase, output, incident: dict | None = None) -> list[str]:
    """Declared ``measured`` fields whose value names nothing the phase examined.

    A phase declares a field ``measured`` when its value is the diagnosis itself
    (node-pressure's ``top_consumer``: what filled the disk). Presence is not
    enough there: "container images" satisfied the contract with a guess, and
    the node's own conditions, metrics and events, which every disk-pressure
    cause shares, were enough evidence to report the guess at 0.7+.

    The value counts as measured when it names a specific thing (an object name
    or path, not a category) other than the finding's own resources, and that
    thing is either the target of a call that succeeded in this phase or, for a
    path, appears in a supporting observation sourced to such a call (a
    per-path usage breakdown names the path in its output, not its query).
    """
    findings = getattr(output, "findings", {}) or {}
    exclude = _incident_names(incident)
    calls = [c for c in getattr(output, "tool_calls", None) or [] if c.get("status", "success") == "success"]
    targets = set().union(*(_input_names(str(c.get("input") or "")) for c in calls)) - exclude if calls else set()
    observed = " ".join(
        str(e.get("observation") or "").lower()
        for e in getattr(output, "evidence", None) or []
        if isinstance(e, dict) and e.get("source") and e.get("stance") == "supports"
    )

    gaps = []
    for name in getattr(phase, "measured", None) or []:
        value = findings.get(name)
        if not _is_present(value):
            continue  # reported as missing by judge_phase
        named = _specific_names(str(value)) - exclude
        examined = any(n in t or t in n for n in named for t in targets) or any(
            "/" in n and n in observed for n in named
        )
        if not examined:
            gaps.append(name)
    return gaps


def judge_phase(phase, output, incident: dict | None = None) -> PhaseVerdict:
    """Decide whether *output* satisfies *phase*'s declared contract.

    A phase that failed outright is not judged against its contract — the failure
    is the finding, and reporting it as "missing root_cause" would bury it.
    ``incident`` is the finding the plan is investigating; its resources are not
    accepted as the value of a ``measured`` field.
    """
    phase_id = getattr(phase, "id", "?")
    produces = list(getattr(phase, "produces", []) or [])

    status = getattr(output, "status", "complete")
    if status == "failed":
        return PhaseVerdict(phase_id, satisfied=False, reason="phase execution failed")

    if not produces:
        # No declared contract, nothing to hold it to.
        return PhaseVerdict(phase_id, satisfied=True)

    findings = getattr(output, "findings", {}) or {}
    # branch_signal and confidence are first-class fields rather than findings
    # keys, so a phase declaring them as outputs should get credit for them.
    extras = {}
    if getattr(output, "branch_signal", None):
        extras["branch_signal"] = output.branch_signal
    if getattr(output, "confidence", 0):
        extras["confidence"] = output.confidence

    missing = [name for name in produces if not _is_present(findings.get(name, extras.get(name)))]

    unmeasured = unmeasured_fields(phase, output, incident)

    if missing or unmeasured:
        reasons = []
        if missing:
            reasons.append(f"did not produce: {', '.join(missing)}")
        if unmeasured:
            reasons.append(f"named without examining: {', '.join(unmeasured)}")
        return PhaseVerdict(
            phase_id,
            satisfied=False,
            missing=missing,
            reason="; ".join(reasons),
            unmeasured=unmeasured,
        )
    return PhaseVerdict(phase_id, satisfied=True)


def should_retry(verdict: PhaseVerdict, attempts_used: int, max_attempts: int = 2) -> bool:
    """Whether an unsatisfied phase is worth another attempt.

    A phase that failed to execute is not retried here — that is the circuit
    breaker's and the caller's concern. Only an incomplete contract is retried,
    because that is the case where asking again plausibly helps.
    """
    if verdict.satisfied:
        return False
    if not (verdict.missing or verdict.unmeasured):
        return False
    return attempts_used < max_attempts
