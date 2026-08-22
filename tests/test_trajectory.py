"""Tests for the verified-trajectory learning gate."""

from __future__ import annotations

import pytest

from sre_agent.trajectory import (
    MIN_CONFIDENCE_TO_LEARN,
    LearningCandidate,
    TrajectoryLearner,
    candidate_key,
    get_learner,
    reset_learner,
)


def _candidate(**overrides) -> LearningCandidate:
    base = {
        "key": "crashloop:Pod:production:api",
        "category": "crashloop",
        "title": "api pod crash-looping",
        "root_cause": "database connection refused",
        "summary": "api cannot reach db-service",
        "confidence": 0.9,
        "evidence": [{"observation": "connection refused to db-service:5432", "kind": "log"}],
        "tools_called": ["get_pod_logs"],
    }
    base.update(overrides)
    return LearningCandidate(**base)


@pytest.fixture(autouse=True)
def _clean_learner():
    reset_learner()
    yield
    reset_learner()


class TestCandidateKey:
    """Both sides of the gate must derive the same key from what each can see."""

    def test_investigation_and_verification_agree(self):
        resources = [{"kind": "Pod", "namespace": "production", "name": "api-5f58f69bd6-w4x22"}]
        assert candidate_key("crashloop", resources) == candidate_key("crashloop", list(resources))

    def test_replicaset_hash_is_stripped_so_recreated_pods_match(self):
        before = candidate_key("crashloop", [{"kind": "Pod", "namespace": "p", "name": "api-5f58f69bd6-w4x22"}])
        after = candidate_key("crashloop", [{"kind": "Pod", "namespace": "p", "name": "api-7d9f8c1a2b-zz999"}])
        assert before == after

    def test_no_resources_still_produces_a_key(self):
        assert candidate_key("nodepressure", []) == "nodepressure:_"

    def test_different_categories_do_not_collide(self):
        r = [{"kind": "Pod", "namespace": "p", "name": "api"}]
        assert candidate_key("crashloop", r) != candidate_key("oomkill", r)


class TestLearnability:
    def test_a_well_evidenced_diagnosis_is_learnable(self):
        assert _candidate().is_learnable()[0] is True

    def test_low_confidence_is_not_learnable(self):
        ok, reason = _candidate(confidence=MIN_CONFIDENCE_TO_LEARN - 0.1).is_learnable()
        assert ok is False
        assert "confidence" in reason

    def test_no_evidence_is_not_learnable(self):
        ok, reason = _candidate(evidence=[]).is_learnable()
        assert ok is False
        assert "evidence" in reason

    def test_unknown_root_cause_is_not_learnable(self):
        for cause in ("unknown", "", "Unclear"):
            ok, reason = _candidate(root_cause=cause).is_learnable()
            assert ok is False, cause
            assert "root cause" in reason


class TestGate:
    """Nothing is learned until an outcome says the fix worked."""

    def test_recording_alone_learns_nothing(self):
        learner = TrajectoryLearner(use_db=False)
        learner.record(_candidate())
        assert learner.pending_count() == 1
        assert learner.promoted == 0

    def test_verification_promotes(self):
        learner = TrajectoryLearner(use_db=False)
        c = _candidate()
        learner.record(c)
        promoted = learner.promote(c.key)
        assert promoted is not None
        assert promoted.root_cause == "database connection refused"
        assert learner.promoted == 1
        assert learner.pending_count() == 0

    def test_a_fix_that_did_not_hold_teaches_nothing(self):
        learner = TrajectoryLearner(use_db=False)
        c = _candidate()
        learner.record(c)
        learner.discard(c.key, "verification still_failing")
        assert learner.promote(c.key) is None
        assert learner.promoted == 0
        assert learner.discarded == 1

    def test_confidently_wrong_diagnosis_is_never_learned(self):
        # the case this gate exists for: high confidence, fix did not resolve it
        learner = TrajectoryLearner(use_db=False)
        c = _candidate(confidence=0.99)
        learner.record(c)
        learner.discard(c.key, "verification still_failing")
        assert learner.promoted == 0

    def test_verified_but_unlearnable_is_not_promoted(self):
        learner = TrajectoryLearner(use_db=False)
        c = _candidate(evidence=[])
        learner.record(c)
        assert learner.promote(c.key) is None
        assert learner.discarded == 1

    def test_promoting_an_unknown_key_is_harmless(self):
        assert TrajectoryLearner(use_db=False).promote("never-recorded") is None

    def test_promotion_is_single_use(self):
        learner = TrajectoryLearner(use_db=False)
        c = _candidate()
        learner.record(c)
        assert learner.promote(c.key) is not None
        assert learner.promote(c.key) is None


class TestExpiry:
    """Silence is not success."""

    def test_stale_candidates_expire_unlearned(self):
        learner = TrajectoryLearner(ttl_seconds=100, use_db=False)
        c = _candidate(created_at=0.0)
        learner.record(c)
        assert learner.expire_stale(now=1000.0) == 1
        assert learner.pending_count() == 0
        assert learner.promoted == 0
        assert learner.expired == 1

    def test_fresh_candidates_survive(self):
        learner = TrajectoryLearner(ttl_seconds=100, use_db=False)
        learner.record(_candidate(created_at=990.0))
        assert learner.expire_stale(now=1000.0) == 0
        assert learner.pending_count() == 1

    def test_expired_candidate_cannot_later_be_promoted(self):
        learner = TrajectoryLearner(ttl_seconds=100, use_db=False)
        c = _candidate(created_at=0.0)
        learner.record(c)
        learner.expire_stale(now=1000.0)
        assert learner.promote(c.key) is None


class TestStats:
    def test_counters_track_the_learning_loop(self):
        learner = TrajectoryLearner(use_db=False)
        good, bad = _candidate(), _candidate(key="other")
        learner.record(good)
        learner.record(bad)
        learner.promote(good.key)
        learner.discard(bad.key, "still failing")
        assert learner.stats() == {"pending": 0, "promoted": 1, "discarded": 1, "expired": 0}

    def test_learner_is_process_wide(self):
        get_learner().record(_candidate())
        assert get_learner().pending_count() == 1


class _FakeDB:
    """Mirrors Database: execute stages, commit applies, fetch reads."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self._pending: list = []
        self.commits = 0

    def commit(self):
        for fn in self._pending:
            fn()
        self._pending.clear()
        self.commits += 1

    def execute(self, sql: str, params: tuple = ()):
        q = " ".join(sql.split())
        if q.startswith("DELETE FROM learning_candidates"):
            self._pending.append(
                lambda: self.rows.__setitem__(
                    slice(None),
                    [r for r in self.rows if not (r["candidate_key"] == params[0] and r["status"] == "pending")],
                )
            )
        elif q.startswith("INSERT INTO learning_candidates"):
            row = {
                "candidate_key": params[0],
                "category": params[1],
                "title": params[2],
                "root_cause": params[3],
                "summary": params[4],
                "confidence": params[5],
                "evidence_json": params[6],
                "tools_json": params[7],
                "created_at": params[8],
                "status": "pending",
                "reason": "",
            }
            self._pending.append(lambda: self.rows.append(row))
        elif q.startswith("UPDATE learning_candidates SET status = ? , reason") or q.startswith(
            "UPDATE learning_candidates SET status = ?, reason"
        ):
            status, reason, _resolved, key = params

            def _apply():
                for r in self.rows:
                    if r["candidate_key"] == key and r["status"] == "pending":
                        r["status"], r["reason"] = status, reason

            self._pending.append(_apply)
        elif "status = 'expired'" in q:
            cutoff = params[1]

            def _expire():
                for r in self.rows:
                    if r["status"] == "pending" and r["created_at"] < cutoff:
                        r["status"] = "expired"

            self._pending.append(_expire)

    def fetchone(self, sql: str, params: tuple = ()):
        q = " ".join(sql.split())
        if "COUNT(*)" in q and "'pending'" in q:
            return {"c": sum(1 for r in self.rows if r["status"] == "pending")}
        if "COUNT(*)" in q and "'expired'" in q:
            return {"c": sum(1 for r in self.rows if r["status"] == "expired")}
        for r in self.rows:
            if r["candidate_key"] == params[0] and r["status"] == "pending":
                return r
        return None

    def fetchall(self, sql: str, params: tuple = ()):
        counts: dict[str, int] = {}
        for r in self.rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        return [{"status": k, "c": v} for k, v in counts.items()]


class TestPersistence:
    """A candidate must survive the restart that separates it from its verdict."""

    def test_a_candidate_survives_a_new_process(self):
        db = _FakeDB()
        first = TrajectoryLearner(db=db)
        c = _candidate()
        first.record(c)

        # the pod restarts: new learner, same database, nothing in memory
        second = TrajectoryLearner(db=db)
        assert second._candidates == {}
        promoted = second.promote(c.key)
        assert promoted is not None, "a restart must not lose a pending trajectory"
        assert promoted.root_cause == c.root_cause
        assert promoted.evidence == c.evidence

    def test_stats_describe_the_whole_history_not_this_process(self):
        db = _FakeDB()
        a, b = _candidate(), _candidate(key="second")
        first = TrajectoryLearner(db=db)
        first.record(a)
        first.record(b)
        first.promote(a.key)

        second = TrajectoryLearner(db=db)
        assert second.stats()["promoted"] == 1
        assert second.stats()["pending"] == 1

    def test_reinvestigation_supersedes_rather_than_queues(self):
        db = _FakeDB()
        learner = TrajectoryLearner(db=db)
        learner.record(_candidate(root_cause="first guess"))
        learner.record(_candidate(root_cause="better guess"))
        assert learner.pending_count() == 1
        assert learner.promote(_candidate().key).root_cause == "better guess"

    def test_resolved_rows_are_kept_as_history(self):
        db = _FakeDB()
        learner = TrajectoryLearner(db=db)
        c = _candidate()
        learner.record(c)
        learner.promote(c.key)
        assert any(r["status"] == "promoted" for r in db.rows), "history must not be deleted"

    def test_expiry_persists(self):
        db = _FakeDB()
        learner = TrajectoryLearner(ttl_seconds=100, db=db)
        learner.record(_candidate(created_at=0.0))
        learner.expire_stale(now=10_000.0)
        assert TrajectoryLearner(db=db).stats()["expired"] == 1

    def test_no_database_still_works(self):
        learner = TrajectoryLearner(use_db=False)
        c = _candidate()
        learner.record(c)
        assert learner.promote(c.key) is not None
