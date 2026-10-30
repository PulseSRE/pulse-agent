"""Structured evidence for investigations.

Investigations have always returned ``evidence`` as a list of prose strings and
``confidence`` as a float the model asserted about itself. Nothing connected the
two, so an investigation could claim 0.95 confidence with an empty evidence list
and still clear the thresholds in ``monitor/investigation_runner.py`` that write
it into long-term memory (0.7) and scaffold a reusable plan template (0.75).

This module gives evidence a shape and makes confidence a function of it. Legacy
prose strings are still accepted and are simply evidence with an unknown source,
so nothing breaks while investigations migrate to the structured form.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# An investigation asserting a cause it cannot support is capped here regardless
# of what the model claimed about itself.
UNSUPPORTED_CONFIDENCE_CAP = 0.35

# Legacy prose evidence carries no source, so it cannot be weighted as highly as
# evidence naming the tool or signal it came from.
UNSOURCED_WEIGHT = 0.5

EvidenceKind = Literal["metric", "log", "event", "resource", "change", "trace", "unknown"]
Stance = Literal["supports", "contradicts", "context"]


class Evidence(BaseModel):
    """One observation bearing on an investigation's suspected cause."""

    observation: str
    kind: EvidenceKind = "unknown"
    source: str = ""
    stance: Stance = "supports"
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    # A source the model cited that names no tool the run called; see ground_evidence.
    unverified_source: str = ""

    @field_validator("observation")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("observation must not be empty")
        return v

    @property
    def weight(self) -> float:
        """How much this observation counts, discounted when its source is unknown."""
        return self.confidence * (1.0 if self.source else UNSOURCED_WEIGHT)


def parse_evidence(raw: Any) -> list[Evidence]:
    """Build ``Evidence`` from either the legacy prose list or the structured form.

    Accepts ``["fact one", "fact two"]`` and
    ``[{"observation": "...", "kind": "metric", "source": "prometheus", ...}]``.
    Items that cannot be interpreted are dropped rather than raising — an
    investigation returning one malformed item should lose that item, not fail.
    """
    if not isinstance(raw, list):
        return []

    parsed: list[Evidence] = []
    for item in raw:
        if isinstance(item, str):
            text = item.strip()
            if text:
                parsed.append(Evidence(observation=text))
        elif isinstance(item, dict):
            try:
                parsed.append(Evidence(**item))
            except Exception:  # malformed item, keep the rest
                observation = str(item.get("observation", "")).strip()
                if observation:
                    parsed.append(Evidence(observation=observation))
    return parsed


def derive_confidence(evidence: list[Evidence], asserted: float) -> float:
    """Derive investigation confidence from its evidence.

    The model's own ``asserted`` figure is treated as a ceiling, never as the
    answer: an investigation may be less sure than it claims, never more. With no
    supporting evidence the result is capped at ``UNSUPPORTED_CONFIDENCE_CAP``,
    and contradicting evidence pulls the ceiling down in proportion to its weight
    against the supporting side.
    """
    asserted = max(0.0, min(1.0, asserted))

    supporting = sum(e.weight for e in evidence if e.stance == "supports")
    contradicting = sum(e.weight for e in evidence if e.stance == "contradicts")

    if supporting <= 0.0:
        return round(min(asserted, UNSUPPORTED_CONFIDENCE_CAP), 2)

    # Support accumulates with diminishing returns: one strong signal is worth a
    # lot, the fifth adds little. Reaches ~0.95 around three well-sourced items.
    support_ceiling = 1.0 - (0.5**supporting)

    # Contradiction is scored as its share of total weight, so a single dissenting
    # signal against many supporting ones is a dent rather than a veto.
    total = supporting + contradicting
    agreement = supporting / total if total > 0 else 1.0

    return round(min(asserted, support_ceiling) * agreement, 2)


# Words in tool names that say nothing about where data came from.
_GENERIC_TOOL_WORDS = frozenset({"get", "list", "describe", "search", "resource", "resources", "status", "details"})


def _norm_source(text: str) -> str:
    return re.sub(r"[\s\-./:]+", "_", text.lower())


def source_matches_called(source: str, called: list[str] | set[str]) -> bool:
    """Whether an evidence ``source`` names a tool the run actually called.

    Accepts the tool's full name (``get_pod_logs``), its name without the verb
    (``pod logs``), or a distinctive word from it (``prometheus`` for
    ``get_prometheus_query``, ``events`` for ``get_events``).
    """
    src = _norm_source(source or "")
    if not src:
        return False
    for tool in set(called):
        name = tool.lower()
        if name in src:
            return True
        bare = re.sub(r"^(get|list|describe|search)_", "", name)
        if len(bare) >= 4 and bare in src:
            return True
        for word in name.split("_"):
            if len(word) >= 5 and word not in _GENERIC_TOOL_WORDS and word in src:
                return True
    return False


def ground_evidence(evidence: list[Evidence], called: list[str] | set[str]) -> list[Evidence]:
    """Keep a cited source only if the run called a tool it names.

    Any non-empty ``source`` weighs an item as fully sourced, so a source the
    investigation never read ("prometheus", with no query run) raised confidence
    on nothing. Such an item keeps its observation and stance and counts as
    unsourced; the claimed source is kept in ``unverified_source`` so what the
    model said stays visible. With no record of calls (an empty ``called``)
    nothing can be checked, so nothing is changed.
    """
    if not called:
        return evidence
    grounded: list[Evidence] = []
    for item in evidence:
        if item.source and not source_matches_called(item.source, called):
            item = item.model_copy(update={"source": "", "unverified_source": item.source})
        grounded.append(item)
    return grounded
