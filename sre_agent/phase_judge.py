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

    def as_retry_hint(self) -> str:
        """A note to hand back to the model so a retry knows what was wrong."""
        if self.satisfied or not self.missing:
            return ""
        fields = ", ".join(self.missing)
        return (
            f"Your previous attempt at this phase did not produce: {fields}. "
            "Provide those fields explicitly. If a value genuinely cannot be "
            "determined, say so and set status to 'partial' rather than leaving "
            "it out or answering 'unknown'."
        )


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


def judge_phase(phase, output) -> PhaseVerdict:
    """Decide whether *output* satisfies *phase*'s declared contract.

    A phase that failed outright is not judged against its contract — the failure
    is the finding, and reporting it as "missing root_cause" would bury it.
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

    if missing:
        return PhaseVerdict(
            phase_id,
            satisfied=False,
            missing=missing,
            reason=f"did not produce: {', '.join(missing)}",
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
    if not verdict.missing:
        return False
    return attempts_used < max_attempts
