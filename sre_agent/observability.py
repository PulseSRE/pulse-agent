"""Prometheus metrics for Pulse Agent cost and usage observability."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Info

TOKENS_TOTAL = Counter(
    "pulse_agent_tokens_total",
    "Total tokens consumed by Claude API calls",
    ["type"],
)

COST_USD_TOTAL = Counter(
    "pulse_agent_cost_usd_total",
    "Estimated cost in USD",
    ["type"],
)

INVESTIGATIONS_TOTAL = Counter(
    "pulse_agent_investigations_total",
    "Total investigations triggered",
)

INVESTIGATION_BUDGET_REMAINING = Gauge(
    "pulse_agent_investigation_budget_remaining",
    "Remaining investigations in the daily budget",
)

INVESTIGATION_BUDGET_MAX = Gauge(
    "pulse_agent_investigation_budget_max",
    "Maximum daily investigation budget",
)

SCANNER_RUNS_TOTAL = Counter(
    "pulse_agent_scanner_runs_total",
    "Total scanner runs",
    ["scanner"],
)

AUTOFIX_TOTAL = Counter(
    "pulse_agent_autofix_total",
    "Total auto-fix attempts",
    ["outcome"],
)

SCAN_DURATION_SECONDS = Gauge(
    "pulse_agent_scan_duration_seconds",
    "Duration of the last cluster scan",
)

ACTIVE_FINDINGS = Gauge(
    "pulse_agent_active_findings",
    "Number of currently active findings",
)

COST_BUDGET_REMAINING_USD = Gauge(
    "pulse_agent_cost_budget_remaining_usd",
    "Remaining daily cost budget in USD",
)

COST_BUDGET_EXHAUSTION_TOTAL = Counter(
    "pulse_agent_cost_budget_exhaustion_total",
    "Number of times the daily cost budget was exhausted",
)

BUILD_INFO = Info(
    "pulse_agent",
    "Pulse Agent build information",
)

# Pricing per 1M tokens (Vertex AI Claude Opus)
TOKEN_PRICES: dict[str, float] = {
    "input": 15.0,
    "output": 75.0,
    "cache_read": 1.875,
    "cache_write": 18.75,
}


def record_token_metrics(
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> None:
    """Increment Prometheus token and cost counters."""
    pairs = [
        ("input", input_tokens),
        ("output", output_tokens),
        ("cache_read", cache_read_tokens),
        ("cache_write", cache_creation_tokens),
    ]
    for label, count in pairs:
        if count:
            TOKENS_TOTAL.labels(type=label).inc(count)
            COST_USD_TOTAL.labels(type=label).inc(count * TOKEN_PRICES[label] / 1_000_000)
