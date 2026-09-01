"""Tests for selector eval framework."""

from __future__ import annotations

from sre_agent.evals.selector_eval import format_selector_eval, run_selector_eval


class TestSelectorEval:
    def test_runs_without_error(self):
        result = run_selector_eval()
        assert result.total_scenarios >= 20
        assert result.passed > 0

    def test_recall_above_threshold(self):
        result = run_selector_eval()
        assert result.recall_at_5 >= 0.80, f"Recall@5 too low: {result.recall_at_5}"

    def test_latency_under_limit(self):
        import os

        result = run_selector_eval()
        limit = 500 if os.environ.get("CI") else 100
        assert result.latency_p99_ms < limit, f"Latency p99 too high: {result.latency_p99_ms}ms (limit {limit}ms)"

    def test_cold_start_coverage(self):
        result = run_selector_eval()
        assert result.cold_start_coverage >= 0.90

    def test_format_output(self):
        result = run_selector_eval()
        text = format_selector_eval(result)
        assert "Selector Eval" in text
        assert "Recall" in text


class TestRoutingMakesNoNetworkCalls:
    """The eval asserts a latency bound, so routing must not call anything.

    Asserting the *number* only tells you something is slow, and it took two
    attempts to find out what: the first fix closed the LLM fallback, and the
    remaining five seconds were the selector's SLO context lookup querying a
    Prometheus that a CI runner does not have. These tests name the property
    instead — under `offline_routing`, nothing reaches out — so a third network
    path added to routing fails here with an explanation rather than as an
    unexplained regression in a p99.
    """

    def test_slo_context_is_not_queried_while_offline(self):
        from unittest.mock import patch

        from sre_agent.skill_loader import classify_query, load_skills
        from sre_agent.skill_router import offline_routing

        load_skills()
        with patch("sre_agent.slo_registry.get_slo_registry") as slo:
            with offline_routing():
                # A context dict is what makes the lookup reachable at all.
                classify_query("check what alerts are firing", context={})
            assert not slo.called, "routing reached Prometheus while offline"

    def test_llm_fallback_is_not_called_while_offline(self):
        from unittest.mock import patch

        from sre_agent.skill_loader import classify_query, load_skills
        from sre_agent.skill_router import offline_routing

        load_skills()
        with patch("sre_agent.skill_router._llm_classify") as llm:
            with offline_routing():
                classify_query("zzzz qqqq nothing matches this at all")
            assert not llm.called, "routing reached the LLM while offline"

    def test_slo_context_is_skipped_when_there_is_nowhere_to_put_it(self):
        """Even online: the result was fetched and discarded when context is None."""
        from unittest.mock import patch

        from sre_agent.skill_loader import classify_query, load_skills

        load_skills()
        with patch("sre_agent.slo_registry.get_slo_registry") as slo:
            classify_query("check what alerts are firing")
            assert not slo.called, "a Prometheus round-trip whose result is thrown away"

    def test_slo_context_still_reaches_a_caller_that_supplies_one(self):
        """The guard must not silently disable a real feature."""
        from unittest.mock import MagicMock, patch

        from sre_agent.skill_loader import classify_query, load_skills

        load_skills()
        registry = MagicMock()
        registry.get_context_for_selector.return_value = "### SLO Alerts\n- checkout burn rate"
        context: dict = {}
        with patch("sre_agent.slo_registry.get_slo_registry", return_value=registry):
            classify_query("check what alerts are firing", context=context)
        assert context.get("slo_alerts", "").startswith("### SLO Alerts")
