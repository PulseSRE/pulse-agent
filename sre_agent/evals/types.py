"""Types for deterministic agent evaluation scenarios and results."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class EvalExpected:
    min_overall: float | None = None
    max_overall: float | None = None
    should_block_release: bool | None = None
    required_blockers: list[str] = field(default_factory=list)


@dataclass
class EvalScenario:
    scenario_id: str
    category: str
    description: str
    tool_calls: list[str]
    rejected_tools: int
    duration_seconds: float
    user_confirmed_resolution: bool | None
    final_response: str
    had_policy_violation: bool = False
    hallucinated_tool: bool = False
    missing_confirmation: bool = False
    verification_passed: bool | None = None
    rollback_available: bool = False
    rollback_used: bool = False
    retry_attempts: int = 0
    transient_failures: int = 0
    completed: bool = True
    expected: EvalExpected | None = None


@dataclass
class ScenarioScore:
    scenario_id: str
    category: str
    overall: float
    dimensions: dict[str, float]
    blockers: list[str]
    passed_gate: bool


@dataclass
class EvalSuiteResult:
    suite_name: str
    scenario_count: int
    passed_count: int
    gate_passed: bool
    average_overall: float
    dimension_averages: dict[str, float]
    blocker_counts: dict[str, int]
    scenarios: list[ScenarioScore]
