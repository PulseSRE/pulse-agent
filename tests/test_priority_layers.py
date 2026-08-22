"""Ranking that ignores its own causal model.

Pulse has had a causal layer model since episodes were built — infrastructure
explains platform explains workload explains signal. The inbox ranking never
used it.

Measured on the reference cluster: four workload-layer pod crashloops outranked
an infrastructure-layer node that had gone NotReady, and derived `audit_events`
rows outranked both. The product knew which of those was the cause and sorted
it fourth. Meanwhile the whole top ten spanned 5.69 to 5.22 — an ordering that
was correct and told an operator nothing, because age contributes up to 3.5
points (age_bonus 2.0 + novelty 1.5) while the entire severity range
contributes at most 3, and every item was about the same age.
"""

from __future__ import annotations

import time

import pytest

from sre_agent.inbox import compute_priority_score

NOW = int(time.time())


def score(category: str, severity: str = "critical", age_hours: float = 0.0) -> float:
    return compute_priority_score(
        severity=severity,
        confidence=0.9,
        noise_score=0.0,
        created_at=NOW - int(age_hours * 3600),
        due_date=None,
        category=category,
    )


def test_the_node_outranks_the_pods_it_explains():
    """The exact inversion seen on the cluster, at equal severity and age."""
    assert score("nodes") > score("crashloop")


def test_a_derived_signal_ranks_below_the_workload_it_describes():
    """An audit_events row is an observation *about* something else.

    Seven of them outranked a NotReady node. A signal is the weakest claim on
    an operator's attention, not the strongest.
    """
    assert score("audit_events") < score("crashloop")
    assert score("alerts") < score("workloads")


def test_the_ladder_holds_end_to_end():
    infra, platform, workload, signal = (score("nodes"), score("operators"), score("crashloop"), score("alerts"))
    assert infra > platform > workload > signal


def test_severity_still_wins_within_a_layer():
    """Layer ranks *between* equals; it must not flatten severity inside one."""
    assert score("crashloop", "critical") > score("crashloop", "warning")
    assert score("nodes", "critical") > score("nodes", "warning")


def test_a_critical_signal_does_not_outrank_a_critical_infrastructure_finding():
    """The case that motivated this: both critical, one causal."""
    assert score("nodes", "critical") > score("audit_events", "critical")


def test_an_unclassified_category_is_not_demoted():
    """A category nobody has mapped yet is not evidence of unimportance.

    Silently ranking it below everything is how a new scanner's output
    disappears from the queue it was written to fill.
    """
    # approx, not ==: novelty_bonus reads the clock at call time, so two
    # calls a microsecond apart differ in the seventh decimal.
    assert score("a_scanner_added_next_week") == pytest.approx(score("crashloop"))


def test_omitting_the_category_keeps_the_old_behaviour():
    """Callers that do not pass a category must not shift under them."""
    without = compute_priority_score("critical", 0.9, 0.0, NOW, None)
    assert without == pytest.approx(score("crashloop"))


def test_the_spread_is_wide_enough_to_read():
    """The original complaint was not the order — it was that nothing separated.

    Across one severity the layers must differ by more than the noise an
    operator sees between two items of the same age.
    """
    spread = score("nodes") - score("alerts")
    assert spread > 3.0, f"layers only separate by {spread:.2f}"
