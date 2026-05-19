"""Tests for Prometheus observability metrics."""

from __future__ import annotations

from sre_agent.observability import (
    ACTIVE_FINDINGS,
    AUTOFIX_TOTAL,
    BUILD_INFO,
    COST_USD_TOTAL,
    INVESTIGATION_BUDGET_MAX,
    INVESTIGATION_BUDGET_REMAINING,
    INVESTIGATIONS_TOTAL,
    SCAN_DURATION_SECONDS,
    SCANNER_RUNS_TOTAL,
    TOKEN_PRICES,
    TOKENS_TOTAL,
    record_token_metrics,
)


class TestMetricRegistration:
    """Verify all metrics are importable and correctly typed."""

    def test_counters_exist(self):
        # prometheus_client strips _total suffix from _name on Counters
        assert TOKENS_TOTAL._name == "pulse_agent_tokens"
        assert COST_USD_TOTAL._name == "pulse_agent_cost_usd"
        assert INVESTIGATIONS_TOTAL._name == "pulse_agent_investigations"
        assert SCANNER_RUNS_TOTAL._name == "pulse_agent_scanner_runs"
        assert AUTOFIX_TOTAL._name == "pulse_agent_autofix"

    def test_gauges_exist(self):
        assert INVESTIGATION_BUDGET_REMAINING._name == "pulse_agent_investigation_budget_remaining"
        assert INVESTIGATION_BUDGET_MAX._name == "pulse_agent_investigation_budget_max"
        assert SCAN_DURATION_SECONDS._name == "pulse_agent_scan_duration_seconds"
        assert ACTIVE_FINDINGS._name == "pulse_agent_active_findings"

    def test_info_exists(self):
        assert BUILD_INFO._name == "pulse_agent"


class TestTokenPricing:
    def test_prices_defined(self):
        assert TOKEN_PRICES["input"] == 15.0
        assert TOKEN_PRICES["output"] == 75.0
        assert TOKEN_PRICES["cache_read"] == 1.875
        assert TOKEN_PRICES["cache_write"] == 18.75


class TestRecordTokenMetrics:
    def test_increments_counters(self):
        before_input = TOKENS_TOTAL.labels(type="input")._value.get()
        before_output = TOKENS_TOTAL.labels(type="output")._value.get()

        record_token_metrics(input_tokens=1000, output_tokens=500)

        assert TOKENS_TOTAL.labels(type="input")._value.get() == before_input + 1000
        assert TOKENS_TOTAL.labels(type="output")._value.get() == before_output + 500

    def test_increments_cost(self):
        before = COST_USD_TOTAL.labels(type="input")._value.get()

        record_token_metrics(input_tokens=1_000_000)

        after = COST_USD_TOTAL.labels(type="input")._value.get()
        assert abs((after - before) - 15.0) < 0.01

    def test_skips_zero_tokens(self):
        before_cr = TOKENS_TOTAL.labels(type="cache_read")._value.get()

        record_token_metrics(input_tokens=100, cache_read_tokens=0)

        assert TOKENS_TOTAL.labels(type="cache_read")._value.get() == before_cr

    def test_cache_tokens(self):
        before = TOKENS_TOTAL.labels(type="cache_write")._value.get()

        record_token_metrics(cache_creation_tokens=500)

        assert TOKENS_TOTAL.labels(type="cache_write")._value.get() == before + 500


class TestGaugeOperations:
    def test_investigation_budget(self):
        INVESTIGATION_BUDGET_REMAINING.set(15)
        assert INVESTIGATION_BUDGET_REMAINING._value.get() == 15

        INVESTIGATION_BUDGET_MAX.set(20)
        assert INVESTIGATION_BUDGET_MAX._value.get() == 20

    def test_scan_metrics(self):
        SCAN_DURATION_SECONDS.set(2.5)
        assert SCAN_DURATION_SECONDS._value.get() == 2.5

        ACTIVE_FINDINGS.set(7)
        assert ACTIVE_FINDINGS._value.get() == 7


class TestCostBudgetMetrics:
    def test_cost_budget_gauges_exist(self):
        from sre_agent.observability import COST_BUDGET_EXHAUSTION_TOTAL, COST_BUDGET_REMAINING_USD

        assert COST_BUDGET_REMAINING_USD._name == "pulse_agent_cost_budget_remaining_usd"
        assert COST_BUDGET_EXHAUSTION_TOTAL._name == "pulse_agent_cost_budget_exhaustion"

    def test_cost_budget_remaining_set(self):
        from sre_agent.observability import COST_BUDGET_REMAINING_USD

        COST_BUDGET_REMAINING_USD.set(42.5)
        assert COST_BUDGET_REMAINING_USD._value.get() == 42.5

    def test_cost_budget_exhaustion_inc(self):
        from sre_agent.observability import COST_BUDGET_EXHAUSTION_TOTAL

        before = COST_BUDGET_EXHAUSTION_TOTAL._value.get()
        COST_BUDGET_EXHAUSTION_TOTAL.inc()
        assert COST_BUDGET_EXHAUSTION_TOTAL._value.get() == before + 1


class TestCounterLabels:
    def test_scanner_labels(self):
        before = SCANNER_RUNS_TOTAL.labels(scanner="crashloop")._value.get()
        SCANNER_RUNS_TOTAL.labels(scanner="crashloop").inc()
        assert SCANNER_RUNS_TOTAL.labels(scanner="crashloop")._value.get() == before + 1

    def test_autofix_labels(self):
        for outcome in ("success", "failure", "skipped"):
            before = AUTOFIX_TOTAL.labels(outcome=outcome)._value.get()
            AUTOFIX_TOTAL.labels(outcome=outcome).inc()
            assert AUTOFIX_TOTAL.labels(outcome=outcome)._value.get() == before + 1
