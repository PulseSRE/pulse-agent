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

import json
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


def _now_ms() -> int:
    return int(time.time() * 1000)


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
    """Holds learning candidates between investigation and verification.

    Backed by Postgres. This used to be a dict on a module global, which meant
    every pod restart wiped the pending set — and since a candidate is recorded
    at investigation time and promoted only when verification confirms the fix on
    a LATER scan cycle, an in-memory store could only learn from an investigation
    whose verification happened to land in the same pod lifetime. In practice it
    learned nothing.

    Falls back to memory when no database is reachable, so tests and offline runs
    still work, but a real deployment persists.
    """

    def __init__(self, ttl_seconds: int = CANDIDATE_TTL_SECONDS, db=None, use_db: bool = True) -> None:
        self._candidates: dict[str, LearningCandidate] = {}
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._db = db
        self._use_db = use_db or db is not None
        self.promoted = 0
        self.discarded = 0
        self.expired = 0

    @property
    def db(self):
        """The database, or None when unavailable — callers fall back to memory."""
        if not self._use_db:
            return None
        if self._db is None:
            try:
                from .db import get_database

                self._db = get_database()
            except Exception:
                logger.debug("No database for trajectory learning; using memory", exc_info=True)
                self._use_db = False
                return None
        return self._db

    def record(self, candidate: LearningCandidate) -> None:
        """Hold a trajectory pending its outcome. Learns nothing yet."""
        db = self.db
        if db is not None:
            try:
                # One pending row per key: a re-investigation supersedes the
                # earlier attempt rather than queueing a second candidate.
                db.execute(
                    "DELETE FROM learning_candidates WHERE candidate_key = ? AND status = 'pending'",
                    (candidate.key,),
                )
                db.execute(
                    """
                    INSERT INTO learning_candidates
                        (candidate_key, category, title, root_cause, summary, confidence,
                         evidence_json, tools_json, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        candidate.key,
                        candidate.category,
                        candidate.title,
                        candidate.root_cause,
                        candidate.summary,
                        float(candidate.confidence),
                        json.dumps(candidate.evidence),
                        json.dumps(candidate.tools_called),
                        int(candidate.created_at * 1000),
                    ),
                )
                db.commit()
            except Exception:
                logger.warning("Failed to persist learning candidate %s", candidate.key, exc_info=True)
        with self._lock:
            self._candidates[candidate.key] = candidate
        logger.info(
            "Learning candidate recorded: %s (confidence=%.2f, evidence=%d) — awaiting verification",
            candidate.key,
            candidate.confidence,
            len(candidate.evidence),
        )

    def _load_pending(self, key: str) -> LearningCandidate | None:
        """Rehydrate a candidate recorded before this process started."""
        db = self.db
        if db is None:
            return None
        try:
            row = db.fetchone(
                "SELECT candidate_key, category, title, root_cause, summary, confidence, "
                "evidence_json, tools_json, created_at FROM learning_candidates "
                "WHERE candidate_key = ? AND status = 'pending' ORDER BY id DESC LIMIT 1",
                (key,),
            )
        except Exception:
            logger.debug("Failed to load pending candidate %s", key, exc_info=True)
            return None
        if not row:
            return None
        try:
            evidence = json.loads(row["evidence_json"] or "[]")
            tools = json.loads(row["tools_json"] or "[]")
        except (TypeError, ValueError):
            evidence, tools = [], []
        return LearningCandidate(
            key=row["candidate_key"],
            category=row["category"] or "",
            title=row["title"] or "",
            root_cause=row["root_cause"] or "",
            summary=row["summary"] or "",
            confidence=float(row["confidence"] or 0.0),
            evidence=evidence,
            tools_called=tools,
            created_at=float(row["created_at"] or 0) / 1000.0,
        )

    def _resolve(self, key: str, status: str, reason: str) -> bool:
        """Mark the pending row for *key* resolved. Rows are kept as history."""
        db = self.db
        if db is None:
            return False
        try:
            db.execute(
                "UPDATE learning_candidates SET status = ?, reason = ?, resolved_at = ? "
                "WHERE candidate_key = ? AND status = 'pending'",
                (status, reason[:500], _now_ms(), key),
            )
            db.commit()
            return True
        except Exception:
            logger.warning("Failed to mark candidate %s as %s", key, status, exc_info=True)
            return False

    def promote(self, key: str) -> LearningCandidate | None:
        """The fix was verified. Return the candidate if it is worth learning from."""
        with self._lock:
            candidate = self._candidates.pop(key, None)
        if candidate is None:
            candidate = self._load_pending(key)
        if candidate is None:
            return None

        learnable, reason = candidate.is_learnable()
        if not learnable:
            self._resolve(key, "discarded", reason)
            self.discarded += 1
            logger.info("Verified trajectory %s not learnable: %s", key, reason)
            return None

        self._resolve(key, "promoted", "")
        self.promoted += 1
        logger.info("Promoting verified trajectory %s: %s", key, candidate.root_cause)
        return candidate

    def discard(self, key: str, reason: str) -> None:
        """The fix did not resolve the finding. Drop the candidate unlearned."""
        with self._lock:
            candidate = self._candidates.pop(key, None)
        resolved = self._resolve(key, "discarded", reason)
        if candidate is not None or resolved:
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

        db_expired = 0
        db = self.db
        if db is not None:
            try:
                db.execute(
                    "UPDATE learning_candidates SET status = 'expired', "
                    "reason = 'verification never arrived', resolved_at = ? "
                    "WHERE status = 'pending' AND created_at < ?",
                    (_now_ms(), int(cutoff * 1000)),
                )
                db.commit()
                row = db.fetchone("SELECT COUNT(*) AS c FROM learning_candidates WHERE status = 'expired'")
                db_expired = int(row["c"]) if row else 0
            except Exception:
                logger.warning("Failed to expire stale candidates", exc_info=True)

        count = max(len(stale), 0)
        if count or db_expired:
            self.expired += count
            logger.info("Expired %d in-memory candidate(s); %d expired total on record", count, db_expired)
        return count

    def pending_count(self) -> int:
        db = self.db
        if db is not None:
            try:
                row = db.fetchone("SELECT COUNT(*) AS c FROM learning_candidates WHERE status = 'pending'")
                if row is not None:
                    return int(row["c"])
            except Exception:
                logger.debug("Failed to count pending candidates", exc_info=True)
        with self._lock:
            return len(self._candidates)

    def stats(self) -> dict[str, int]:
        """Counters for the learning loop — how much was learned versus dropped.

        Read from the database when there is one, so the numbers describe the
        whole history rather than what this process happens to remember.
        """
        db = self.db
        if db is not None:
            try:
                rows = db.fetchall("SELECT status, COUNT(*) AS c FROM learning_candidates GROUP BY status")
                counts = {r["status"]: int(r["c"]) for r in rows}
                return {
                    "pending": counts.get("pending", 0),
                    "promoted": counts.get("promoted", 0),
                    "discarded": counts.get("discarded", 0),
                    "expired": counts.get("expired", 0),
                }
            except Exception:
                logger.debug("Failed to read learning stats", exc_info=True)
        return {
            "pending": len(self._candidates),
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
