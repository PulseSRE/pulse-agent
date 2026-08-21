"""Tests for structured evidence and derived confidence."""

from __future__ import annotations

import pytest

from sre_agent.evidence import (
    UNSUPPORTED_CONFIDENCE_CAP,
    Evidence,
    derive_confidence,
    parse_evidence,
)


class TestParseEvidence:
    """Legacy prose and the structured form both have to work during migration."""

    def test_legacy_string_list(self):
        ev = parse_evidence(["pod restarted 12 times", "OOMKilled in last hour"])
        assert len(ev) == 2
        assert ev[0].observation == "pod restarted 12 times"
        assert ev[0].kind == "unknown"
        assert ev[0].source == ""

    def test_structured_form(self):
        ev = parse_evidence(
            [{"observation": "p99 rose to 730ms", "kind": "metric", "source": "prometheus", "confidence": 0.9}]
        )
        assert len(ev) == 1
        assert ev[0].kind == "metric"
        assert ev[0].source == "prometheus"
        assert ev[0].confidence == 0.9

    def test_mixed_forms_coexist(self):
        ev = parse_evidence(["legacy fact", {"observation": "structured fact", "kind": "log"}])
        assert [e.observation for e in ev] == ["legacy fact", "structured fact"]

    def test_non_list_returns_empty(self):
        assert parse_evidence(None) == []
        assert parse_evidence("not a list") == []
        assert parse_evidence({"observation": "a dict is not a list"}) == []

    def test_blank_observations_dropped(self):
        assert parse_evidence(["", "   ", "real"]) == parse_evidence(["real"])

    def test_malformed_item_keeps_its_observation(self):
        ev = parse_evidence([{"observation": "valid text", "kind": "not-a-real-kind"}])
        assert len(ev) == 1
        assert ev[0].observation == "valid text"
        assert ev[0].kind == "unknown"

    def test_one_bad_item_does_not_drop_the_rest(self):
        ev = parse_evidence([{"no_observation_key": 1}, "good fact"])
        assert [e.observation for e in ev] == ["good fact"]

    def test_empty_observation_rejected(self):
        with pytest.raises(ValueError):
            Evidence(observation="   ")


class TestDerivedConfidence:
    """The model's assertion is a ceiling, never the answer."""

    def test_no_evidence_is_capped_however_confident_the_model_claims_to_be(self):
        assert derive_confidence([], 0.95) == UNSUPPORTED_CONFIDENCE_CAP

    def test_no_evidence_does_not_inflate_a_low_assertion(self):
        assert derive_confidence([], 0.1) == 0.1

    def test_assertion_is_a_ceiling_not_a_floor(self):
        strong = [Evidence(observation=f"signal {i}", source="prometheus", confidence=0.9) for i in range(4)]
        assert derive_confidence(strong, 0.5) == 0.5

    @staticmethod
    def _n_supporting(n: int) -> float:
        ev = [Evidence(observation=f"signal {i}", source="k8s", confidence=0.9) for i in range(n)]
        return derive_confidence(ev, 1.0)

    def test_more_supporting_evidence_raises_confidence(self):
        assert self._n_supporting(1) < self._n_supporting(3) < self._n_supporting(6)

    def test_three_well_sourced_signals_reach_high_confidence(self):
        assert self._n_supporting(3) >= 0.8

    def test_support_has_diminishing_returns(self):
        first_two = self._n_supporting(3) - self._n_supporting(1)
        next_three = self._n_supporting(6) - self._n_supporting(3)
        assert first_two > next_three

    def test_contradiction_reduces_confidence(self):
        supporting = [Evidence(observation="a", source="k8s", confidence=0.9)]
        with_contra = supporting + [Evidence(observation="b", source="k8s", confidence=0.9, stance="contradicts")]
        assert derive_confidence(with_contra, 1.0) < derive_confidence(supporting, 1.0)

    def test_lone_dissent_dents_rather_than_vetoes(self):
        many = [Evidence(observation=f"s{i}", source="k8s", confidence=0.9) for i in range(5)]
        one_against = many + [Evidence(observation="d", source="k8s", confidence=0.9, stance="contradicts")]
        assert derive_confidence(one_against, 1.0) > 0.7

    def test_context_evidence_neither_supports_nor_contradicts(self):
        ctx = [Evidence(observation="cluster is OpenShift", source="k8s", stance="context")]
        assert derive_confidence(ctx, 0.95) == UNSUPPORTED_CONFIDENCE_CAP

    def test_unsourced_evidence_counts_for_less(self):
        sourced = [Evidence(observation="a", source="prometheus", confidence=0.9)]
        unsourced = [Evidence(observation="a", confidence=0.9)]
        assert derive_confidence(unsourced, 1.0) < derive_confidence(sourced, 1.0)

    def test_result_stays_in_range(self):
        for asserted in (0.0, 0.5, 1.0):
            for n in (0, 1, 10):
                ev = [Evidence(observation=f"s{i}", source="k8s") for i in range(n)]
                assert 0.0 <= derive_confidence(ev, asserted) <= 1.0
