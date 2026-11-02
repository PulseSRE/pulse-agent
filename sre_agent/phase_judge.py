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

# Names of resource types and well-known label/taint keys. They appear in tool
# output ("low on resource: ephemeral-storage") but name what ran out, not
# what used it up.
_RESOURCE_TYPE = re.compile(r"^(ephemeral-storage|hugepages-[a-z0-9]+|([a-z0-9-]+\.)*kubernetes\.io(/.*)?)$")


def _is_specific(token: str) -> bool:
    return (
        len(token) >= 5
        and any(c in token for c in "-_/.")
        and any(c.isalpha() for c in token)
        and not _RESOURCE_TYPE.match(token)
    )


def _specific_names(text: str) -> set[str]:
    """Specific names in ``text``; a path also yields its specific segments."""
    names = set()
    for token in _NAME_TOKEN.findall(text.lower()):
        token = token.strip("._-*")
        if _is_specific(token):
            names.add(token)
        if "/" in token and not _RESOURCE_TYPE.match(token):
            names |= {part.strip("._-*") for part in token.split("/") if _is_specific(part.strip("._-*"))}
    return names


# A value that opens by saying it was not found names candidates, not a
# finding. Live: "Not conclusively identified at pod level — node-level image
# storage implicated; report-gen-55f only used 120Mi, ruling it out;
# log-shipper-7d9f-q2x plausible but unconfirmed", reported at 0.71.
_UNDETERMINED = re.compile(
    r"^\W*(not\s+(conclusively\s+)?(identified|isolated|determined|confirmed|verified|measured|known)"
    r"|not\s+conclusively|undetermined|unknown|unclear|inconclusive|unconfirmed|unverified"
    r"|could\s+not|cannot\s+be|unable\s+to)\b"
)


# Where a value's claim ends and its commentary starts: "X (detail) — why;
# Y was ruled out". Only the claim is the culprit; what the commentary names
# (a victim it ruled out, a suspect it could not confirm) is not.
_CLAIM_END = re.compile(r";|\.\s|\s[\u2014\u2013-]\s|\s--\s")  # em dash, en dash, hyphen


def _claim(value: str) -> str:
    return _CLAIM_END.split(value, maxsplit=1)[0].strip()


def _incident_names(incident: dict | None) -> set[str]:
    """The finding's own resources: naming them as the culprit restates the finding."""
    names: set[str] = set()
    for resource in (incident or {}).get("resources") or []:
        if isinstance(resource, dict):
            for key in ("name", "namespace"):
                value = str(resource.get(key) or "").strip().lower()
                if value:
                    names.add(value)
    return names


def _named_in(text: str, name: str) -> bool:
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", text))


def _argument_names(value: str, arg: str, exclude: set[str]) -> set[str]:
    """The names by which ``value`` names the object a call's argument targeted.

    Equal to the argument, whatever its shape ("es-0", "fluentd"), or
    containing it as a whole word; a pod name also counts by its owner
    ("fluentd" for fluentd-x7k2p). Only this direction: an argument that merely
    contains the value (a namespace, a selector) did not examine it.
    """
    from .monitor.confidence import _strip_pod_hash

    arg = arg.strip().lower()
    if not arg or arg in exclude:
        return set()
    if value == arg:
        return {arg}
    return {
        candidate
        for candidate in {arg, _strip_pod_hash(arg)}
        if len(candidate) >= 3 and candidate not in exclude and _named_in(value, candidate)
    }


def unmeasured_fields(phase, output, incident: dict | None = None, ledger=None) -> list[str]:
    """Declared ``measured`` fields whose value names nothing the phase examined.

    A phase declares a field ``measured`` when its value is the diagnosis itself
    (node-pressure's ``top_consumer``: what filled the disk). Presence is not
    enough there: "container images" satisfied the contract with a guess, and
    the node's own conditions, metrics and events, which every disk-pressure
    cause shares, were enough evidence to report the guess at 0.7+.

    A value is examined when a call in this phase that succeeded either
    targeted it (an object argument: pod_name, name, resource_name, path,
    container) or returned it (a specific name in the value appears in the
    tool's own output), and the phase's own ``root_cause`` names it too. Only
    what ran and what it returned count, from the
    phase's ``CallLedger``; the model's observations, however they cite a call,
    are its account of the data, not the data. The finding's own resources
    never count. ``ledger`` is the phase's ``evidence.CallLedger``; without
    one nothing was examined.

    Only the value's leading claim is matched, not its commentary. Live, a
    wrong answer read "Unreclaimed/dangling container images (image GC
    failing); evicted pod report-gen-55f's own usage (120Mi) is too small to be
    the real driver", and the victim it ruled out was in the events and in the
    root cause, so it counted as the examined culprit.

    The root-cause condition closes a hedge seen live: "Not conclusively
    isolated — likely unreclaimed container images; log-shipper-7d9f-q2x is
    the remaining suspect but unverified", after a describe_pod of that pod,
    with a root cause blaming image GC. The pod was examined, but it is not the
    culprit the diagnosis asserts.
    """
    findings = getattr(output, "findings", {}) or {}
    exclude = _incident_names(incident)
    calls = [c for c in (ledger.calls.values() if ledger is not None else []) if c.read]
    cause = str(findings.get("root_cause") or "").lower() if "root_cause" in (phase.produces or []) else None

    gaps = []
    for name in getattr(phase, "measured", None) or []:
        value = findings.get(name)
        if not _is_present(value):
            continue  # reported as missing by judge_phase
        text = _claim(str(value).strip().lower())
        if _UNDETERMINED.match(text):
            gaps.append(name)
            continue
        named = {n for n in _specific_names(text) if n not in exclude}
        examined: set[str] = set()
        for call in calls:
            for arg in call.object_args.values():
                examined |= _argument_names(text, arg, exclude)
            result = call.result_text.lower()
            examined |= {n for n in named if n in result}
        if cause is not None:
            examined = {n for n in examined if _named_in(cause, n)}
        if not examined:
            gaps.append(name)
    return gaps


def judge_phase(phase, output) -> PhaseVerdict:
    """Decide whether *output* satisfies *phase*'s declared contract.

    A phase that failed outright is not judged against its contract — the failure
    is the finding, and reporting it as "missing root_cause" would bury it.
    ``output.unexamined`` lists the ``measured`` fields the run that produced
    it found unexamined (``unmeasured_fields``, which needs that run's calls).
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

    unmeasured = [f for f in getattr(output, "unexamined", None) or [] if f not in missing]

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
