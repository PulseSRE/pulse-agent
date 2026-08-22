"""Tests for the plan phase contract judge."""

from __future__ import annotations

from dataclasses import dataclass, field

from sre_agent.phase_judge import PhaseVerdict, judge_phase, should_retry


@dataclass
class _Phase:
    id: str = "diagnose"
    produces: list[str] = field(default_factory=list)


@dataclass
class _Output:
    status: str = "complete"
    findings: dict = field(default_factory=dict)
    branch_signal: str | None = None
    confidence: float = 0.0
    open_questions: list[str] = field(default_factory=list)


class TestContractChecking:
    def test_a_met_contract_is_satisfied(self):
        phase = _Phase(produces=["root_cause", "severity"])
        out = _Output(findings={"root_cause": "db connection refused", "severity": "critical"})
        assert judge_phase(phase, out).satisfied is True

    def test_a_missing_field_is_named(self):
        phase = _Phase(produces=["root_cause", "severity"])
        verdict = judge_phase(phase, _Output(findings={"root_cause": "x"}))
        assert verdict.satisfied is False
        assert verdict.missing == ["severity"]
        assert "severity" in verdict.reason

    def test_no_declared_contract_passes(self):
        assert judge_phase(_Phase(produces=[]), _Output()).satisfied is True

    def test_a_failed_phase_is_not_judged_on_its_contract(self):
        # the failure is the finding; reporting "missing root_cause" would bury it
        phase = _Phase(produces=["root_cause"])
        verdict = judge_phase(phase, _Output(status="failed"))
        assert verdict.satisfied is False
        assert verdict.missing == []
        assert "failed" in verdict.reason

    def test_confidence_and_branch_signal_count_as_produced(self):
        phase = _Phase(produces=["confidence", "branch_signal"])
        out = _Output(confidence=0.8, branch_signal="database")
        assert judge_phase(phase, out).satisfied is True


class TestEmptyValues:
    """Filling a blank is not satisfying a contract."""

    def test_unknown_does_not_satisfy(self):
        for filler in ("unknown", "N/A", "TBD", "unclear", "-", "  ", "none"):
            verdict = judge_phase(_Phase(produces=["root_cause"]), _Output(findings={"root_cause": filler}))
            assert verdict.satisfied is False, f"{filler!r} should not satisfy the contract"

    def test_empty_collections_do_not_satisfy(self):
        for empty in ([], {}, ()):
            verdict = judge_phase(_Phase(produces=["evidence"]), _Output(findings={"evidence": empty}))
            assert verdict.satisfied is False

    def test_zero_and_false_are_real_values(self):
        # a measured zero is an answer; treating it as missing would be wrong
        assert judge_phase(_Phase(produces=["restarts"]), _Output(findings={"restarts": 0})).satisfied
        assert judge_phase(_Phase(produces=["healthy"]), _Output(findings={"healthy": False})).satisfied

    def test_a_populated_list_satisfies(self):
        assert judge_phase(_Phase(produces=["evidence"]), _Output(findings={"evidence": ["a"]})).satisfied


class TestRetryPolicy:
    def test_satisfied_phases_are_not_retried(self):
        assert should_retry(PhaseVerdict("p", satisfied=True), attempts_used=1) is False

    def test_an_incomplete_contract_is_retried(self):
        assert should_retry(PhaseVerdict("p", False, missing=["root_cause"]), attempts_used=1) is True

    def test_retries_are_bounded(self):
        v = PhaseVerdict("p", False, missing=["root_cause"])
        assert should_retry(v, attempts_used=2) is False

    def test_execution_failure_is_not_retried_here(self):
        # nothing missing, just failed — that is the circuit breaker's concern
        v = PhaseVerdict("p", False, missing=[], reason="phase execution failed")
        assert should_retry(v, attempts_used=1) is False


class TestRetryHint:
    def test_names_the_missing_fields(self):
        hint = PhaseVerdict("p", False, missing=["root_cause", "severity"]).as_retry_hint()
        assert "root_cause" in hint
        assert "severity" in hint

    def test_offers_partial_as_an_honest_way_out(self):
        # otherwise the retry incentive is to invent a value
        hint = PhaseVerdict("p", False, missing=["root_cause"]).as_retry_hint()
        assert "partial" in hint

    def test_satisfied_verdicts_have_no_hint(self):
        assert PhaseVerdict("p", satisfied=True).as_retry_hint() == ""
