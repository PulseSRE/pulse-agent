"""Learn from trajectories that actually worked, not from confident guesses.

Scaffolding used to run at investigation time, the moment a diagnosis cleared a
confidence threshold — before any fix had been applied and long before anything
confirmed the finding went away. A confidently wrong root cause became a skill,
and that skill then routed future incidents toward the same wrong answer.

The success signal already existed: ``monitor/verification_pipeline.py`` checks on
a later scan whether the finding is still active. This module holds the trajectory
in between, so learning is gated on that outcome rather than on self-assessment.

    investigation  ->  record()      candidate, learns nothing yet
    verification   ->  promote()     finding resolved, now it is a lesson
                       discard()     finding still active, drop it

A candidate that is never verified expires rather than being learned by default.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger("pulse_agent.trajectory")

# A candidate whose verification never arrives is dropped rather than promoted.
# Two hours is comfortably longer than the verification delay at any sane scan
# interval, and short enough that a stuck candidate does not linger for a day.
CANDIDATE_TTL_SECONDS = 7200

# Below this the diagnosis is not worth learning from even if the finding cleared,
# because the finding may have resolved for reasons the investigation never named.
MIN_CONFIDENCE_TO_LEARN = 0.6

# A trajectory that named no evidence cannot be a lesson: there is nothing to
# generalise from. Derived confidence already caps these low, this is belt and
# braces for trajectories recorded before that landed.
MIN_EVIDENCE_TO_LEARN = 1


def candidate_key(category: str, resources: list[dict] | None) -> str:
    """Key a trajectory by what both sides of the gate can see.

    The investigation has a finding; the verification pipeline has an action
    payload carrying the same category and resources. Keying on those lets the two
    meet without threading an id through the fix path.
    """
    resource_part = "_"
    if resources:
        r0 = resources[0]
        name = str(r0.get("name", ""))
        kind = str(r0.get("kind", ""))
        if kind == "Pod":
            from .monitor.confidence import _strip_pod_hash

            name = _strip_pod_hash(name)
        resource_part = f"{kind}:{r0.get('namespace', '')}:{name}"
    return f"{category}:{resource_part}"


@dataclass
class LearningCandidate:
    """An investigation held pending the verdict on whether its fix worked."""

    key: str
    category: str
    title: str
    root_cause: str
    summary: str
    confidence: float
    evidence: list[dict] = field(default_factory=list)
    tools_called: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def is_learnable(self) -> tuple[bool, str]:
        """Whether this trajectory is worth generalising, and why not if it isn't."""
        if self.confidence < MIN_CONFIDENCE_TO_LEARN:
            return False, f"confidence {self.confidence:.2f} below {MIN_CONFIDENCE_TO_LEARN}"
        if len(self.evidence) < MIN_EVIDENCE_TO_LEARN:
            return False, "no evidence recorded"
        if not self.root_cause or self.root_cause.lower() in {"unknown", "unclear", ""}:
            return False, "no root cause identified"
        return True, ""


class TrajectoryLearner:
    """Holds learning candidates between investigation and verification."""

    def __init__(self, ttl_seconds: int = CANDIDATE_TTL_SECONDS) -> None:
        self._candidates: dict[str, LearningCandidate] = {}
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self.promoted = 0
        self.discarded = 0
        self.expired = 0

    def record(self, candidate: LearningCandidate) -> None:
        """Hold a trajectory pending its outcome. Learns nothing yet."""
        with self._lock:
            self._candidates[candidate.key] = candidate
        logger.info(
            "Learning candidate recorded: %s (confidence=%.2f, evidence=%d) — awaiting verification",
            candidate.key,
            candidate.confidence,
            len(candidate.evidence),
        )

    def promote(self, key: str) -> LearningCandidate | None:
        """The fix was verified. Return the candidate if it is worth learning from."""
        with self._lock:
            candidate = self._candidates.pop(key, None)
        if candidate is None:
            return None

        learnable, reason = candidate.is_learnable()
        if not learnable:
            self.discarded += 1
            logger.info("Verified trajectory %s not learnable: %s", key, reason)
            return None

        self.promoted += 1
        logger.info("Promoting verified trajectory %s: %s", key, candidate.root_cause)
        return candidate

    def discard(self, key: str, reason: str) -> None:
        """The fix did not resolve the finding. Drop the candidate unlearned."""
        with self._lock:
            candidate = self._candidates.pop(key, None)
        if candidate is not None:
            self.discarded += 1
            logger.info("Discarding unverified trajectory %s: %s", key, reason)

    def expire_stale(self, now: float | None = None) -> int:
        """Drop candidates whose verification never arrived.

        Silence is not success. A candidate nobody confirmed is dropped rather
        than promoted, so an investigation whose fix was never applied cannot be
        learned from by default.
        """
        cutoff = (now if now is not None else time.time()) - self._ttl
        with self._lock:
            stale = [k for k, c in self._candidates.items() if c.created_at < cutoff]
            for key in stale:
                self._candidates.pop(key, None)
        if stale:
            self.expired += len(stale)
            logger.info("Expired %d learning candidate(s) with no verification", len(stale))
        return len(stale)

    def pending_count(self) -> int:
        with self._lock:
            return len(self._candidates)

    def stats(self) -> dict[str, int]:
        """Counters for the learning loop — how much was learned versus dropped."""
        return {
            "pending": self.pending_count(),
            "promoted": self.promoted,
            "discarded": self.discarded,
            "expired": self.expired,
        }


_learner: TrajectoryLearner | None = None


def get_learner() -> TrajectoryLearner:
    """Process-wide learner, mirroring how the monitor holds pending verifications."""
    global _learner
    if _learner is None:
        _learner = TrajectoryLearner()
    return _learner


def reset_learner() -> None:
    """Drop all held candidates — used by tests."""
    global _learner
    _learner = None
