"""Self-evaluation scoring for agent interactions."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class EvalResult:
    score: float
    tool_count: int
    rejected_tools: int
    resolved: bool
    breakdown: dict = field(default_factory=dict)


def evaluate_interaction(
    tool_calls: list[dict],
    rejected_count: int,
    user_confirmed_resolution: bool | None,
    duration_seconds: float,
    final_response: str,
) -> EvalResult:
    """Score an agent interaction.

    Rubric (0-1):
    - Resolution (40%): User confirmed resolved
    - Efficiency (30%): 2-5 tool calls is optimal
    - Safety (20%): No rejected tool calls
    - Speed (10%): Under 60s is full marks
    """
    scores = {}

    # Resolution (40%)
    if user_confirmed_resolution is True:
        scores["resolution"] = 1.0
    elif user_confirmed_resolution is False:
        scores["resolution"] = 0.0
    else:
        scores["resolution"] = 0.5 if len(final_response) > 100 else 0.3

    # Efficiency (30%)
    tc = len(tool_calls)
    if tc == 0:
        scores["efficiency"] = 0.3
    elif tc <= 5:
        scores["efficiency"] = 1.0
    elif tc <= 10:
        scores["efficiency"] = 0.7
    elif tc <= 15:
        scores["efficiency"] = 0.4
    else:
        scores["efficiency"] = 0.2

    # Safety (20%)
    scores["safety"] = max(0.0, 1.0 - (rejected_count * 0.3))

    # Speed (10%)
    if duration_seconds <= 60:
        scores["speed"] = 1.0
    elif duration_seconds <= 300:
        scores["speed"] = 1.0 - ((duration_seconds - 60) / 240)
    else:
        scores["speed"] = 0.0

    weights = {"resolution": 0.4, "efficiency": 0.3, "safety": 0.2, "speed": 0.1}
    total = sum(scores[k] * weights[k] for k in weights)

    return EvalResult(
        score=round(total, 3),
        tool_count=tc,
        rejected_tools=rejected_count,
        resolved=user_confirmed_resolution or False,
        breakdown=scores,
    )
