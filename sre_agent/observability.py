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

DB_POOL_CHECKED_OUT = Gauge(
    "pulse_agent_db_pool_checked_out",
    "Number of DB connections currently checked out from the pool",
)

CACHE_HIT_RATE = Gauge(
    "pulse_agent_cache_hit_rate",
    "Prompt cache hit rate (cache_read_tokens / total_input_tokens)",
)

BUILD_INFO = Info(
    "pulse_agent",
    "Pulse Agent build information",
)

# Estimated list prices per 1M tokens by model family. These are estimates for
# the budget and cost metrics, not billing: set PULSE_AGENT_PRICE_INPUT_PER_MTOK
# and PULSE_AGENT_PRICE_OUTPUT_PER_MTOK to your contracted rates. They used to be
# Opus rates for every model, five times the Sonnet default.
_FAMILY_PRICES: dict[str, tuple[float, float]] = {
    "opus": (15.0, 75.0),
    "sonnet": (3.0, 15.0),
    "haiku": (1.0, 5.0),
}


def token_prices(model: str | None = None) -> dict[str, float]:
    """Per-1M-token prices for the configured (or given) model."""
    import os

    from .config import get_settings

    model = (model or get_settings().agent.model or "").lower()
    family = next((f for f in _FAMILY_PRICES if f in model), "sonnet")
    input_price, output_price = _FAMILY_PRICES[family]
    try:
        input_price = float(os.environ.get("PULSE_AGENT_PRICE_INPUT_PER_MTOK", input_price))
        output_price = float(os.environ.get("PULSE_AGENT_PRICE_OUTPUT_PER_MTOK", output_price))
    except ValueError:
        pass
    # Cache reads bill at a tenth of input, cache writes at 1.25x.
    return {
        "input": input_price,
        "output": output_price,
        "cache_read": input_price * 0.1,
        "cache_write": input_price * 1.25,
    }


def estimate_cost_usd(
    input_tokens: int = 0, output_tokens: int = 0, cache_read_tokens: int = 0, cache_write_tokens: int = 0
) -> float:
    prices = token_prices()
    return (
        input_tokens * prices["input"]
        + output_tokens * prices["output"]
        + cache_read_tokens * prices["cache_read"]
        + cache_write_tokens * prices["cache_write"]
    ) / 1_000_000


# Kept for importers; reflects the configured model at import time.
TOKEN_PRICES: dict[str, float] = {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75}


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
    prices = token_prices()
    for label, count in pairs:
        if count:
            TOKENS_TOTAL.labels(type=label).inc(count)
            COST_USD_TOTAL.labels(type=label).inc(count * prices[label] / 1_000_000)
    total_input = input_tokens + cache_read_tokens
    if total_input > 0:
        CACHE_HIT_RATE.set(cache_read_tokens / total_input)
