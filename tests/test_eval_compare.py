"""Tests for eval A/B comparison."""

from __future__ import annotations

import json
from pathlib import Path

from sre_agent.evals.compare import compare_results, format_comparison


def _write_result(path: Path, suite_name: str, scenarios: list[dict], average_overall: float) -> None:
    data = {
        "suite_name": suite_name,
        "scenario_count": len(scenarios),
        "passed_count": sum(1 for s in scenarios if s.get("passed_gate", True)),
        "gate_passed": all(s.get("passed_gate", True) for s in scenarios),
        "average_overall": average_overall,
        "dimension_averages": {},
        "blocker_counts": {},
        "scenarios": scenarios,
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


class TestCompareResults:
    def test_identical_results_no_regressions(self, tmp_path):
        scenarios = [
            {
                "scenario_id": "s1",
                "category": "sre",
                "overall": 0.85,
                "dimensions": {},
                "blockers": [],
                "passed_gate": True,
            },
            {
                "scenario_id": "s2",
                "category": "sre",
                "overall": 0.90,
                "dimensions": {},
                "blockers": [],
                "passed_gate": True,
            },
        ]
        base = tmp_path / "baseline.json"
        curr = tmp_path / "current.json"
        _write_result(base, "test", scenarios, 0.875)
        _write_result(curr, "test", scenarios, 0.875)

        result = compare_results(base, curr)
        assert result.regression_count == 0
        assert result.improvement_count == 0
        assert result.unchanged_count == 2
        assert result.gate_passed is True
        assert result.overall_delta == 0.0

    def test_detects_regression(self, tmp_path):
        base_scenarios = [
            {
                "scenario_id": "s1",
                "category": "sre",
                "overall": 0.90,
                "dimensions": {},
                "blockers": [],
                "passed_gate": True,
            },
        ]
        curr_scenarios = [
            {
                "scenario_id": "s1",
                "category": "sre",
                "overall": 0.80,
                "dimensions": {},
                "blockers": [],
                "passed_gate": True,
            },
        ]
        base = tmp_path / "baseline.json"
        curr = tmp_path / "current.json"
        _write_result(base, "test", base_scenarios, 0.90)
        _write_result(curr, "test", curr_scenarios, 0.80)

        result = compare_results(base, curr)
        assert result.regression_count == 1
        assert result.has_regressions is True
        assert result.gate_passed is False
        assert result.overall_delta == -0.10

    def test_detects_improvement(self, tmp_path):
        base_scenarios = [
            {
                "scenario_id": "s1",
                "category": "sre",
                "overall": 0.70,
                "dimensions": {},
                "blockers": [],
                "passed_gate": True,
            },
        ]
        curr_scenarios = [
            {
                "scenario_id": "s1",
                "category": "sre",
                "overall": 0.85,
                "dimensions": {},
                "blockers": [],
                "passed_gate": True,
            },
        ]
        base = tmp_path / "baseline.json"
        curr = tmp_path / "current.json"
        _write_result(base, "test", base_scenarios, 0.70)
        _write_result(curr, "test", curr_scenarios, 0.85)

        result = compare_results(base, curr)
        assert result.improvement_count == 1
        assert result.regression_count == 0
        assert result.gate_passed is True
        assert result.overall_delta == 0.15

    def test_small_delta_counts_as_unchanged(self, tmp_path):
        base_scenarios = [
            {
                "scenario_id": "s1",
                "category": "sre",
                "overall": 0.85,
                "dimensions": {},
                "blockers": [],
                "passed_gate": True,
            },
        ]
        curr_scenarios = [
            {
                "scenario_id": "s1",
                "category": "sre",
                "overall": 0.83,
                "dimensions": {},
                "blockers": [],
                "passed_gate": True,
            },
        ]
        base = tmp_path / "baseline.json"
        curr = tmp_path / "current.json"
        _write_result(base, "test", base_scenarios, 0.85)
        _write_result(curr, "test", curr_scenarios, 0.83)

        result = compare_results(base, curr)
        assert result.unchanged_count == 1
        assert result.regression_count == 0

    def test_new_scenarios_tracked(self, tmp_path):
        base_scenarios = [
            {
                "scenario_id": "s1",
                "category": "sre",
                "overall": 0.85,
                "dimensions": {},
                "blockers": [],
                "passed_gate": True,
            },
        ]
        curr_scenarios = [
            {
                "scenario_id": "s1",
                "category": "sre",
                "overall": 0.85,
                "dimensions": {},
                "blockers": [],
                "passed_gate": True,
            },
            {
                "scenario_id": "s2",
                "category": "sre",
                "overall": 0.90,
                "dimensions": {},
                "blockers": [],
                "passed_gate": True,
            },
        ]
        base = tmp_path / "baseline.json"
        curr = tmp_path / "current.json"
        _write_result(base, "test", base_scenarios, 0.85)
        _write_result(curr, "test", curr_scenarios, 0.875)

        result = compare_results(base, curr)
        assert result.new_scenarios == ["s2"]
        assert result.removed_scenarios == []

    def test_removed_scenarios_tracked(self, tmp_path):
        base_scenarios = [
            {
                "scenario_id": "s1",
                "category": "sre",
                "overall": 0.85,
                "dimensions": {},
                "blockers": [],
                "passed_gate": True,
            },
            {
                "scenario_id": "s2",
                "category": "sre",
                "overall": 0.90,
                "dimensions": {},
                "blockers": [],
                "passed_gate": True,
            },
        ]
        curr_scenarios = [
            {
                "scenario_id": "s1",
                "category": "sre",
                "overall": 0.85,
                "dimensions": {},
                "blockers": [],
                "passed_gate": True,
            },
        ]
        base = tmp_path / "baseline.json"
        curr = tmp_path / "current.json"
        _write_result(base, "test", base_scenarios, 0.875)
        _write_result(curr, "test", curr_scenarios, 0.85)

        result = compare_results(base, curr)
        assert result.removed_scenarios == ["s2"]
        assert result.new_scenarios == []

    def test_dimension_deltas_computed(self, tmp_path):
        base_scenarios = [
            {
                "scenario_id": "s1",
                "category": "sre",
                "overall": 0.85,
                "dimensions": {"task_success": 0.8, "safety": 1.0},
                "blockers": [],
                "passed_gate": True,
            },
        ]
        curr_scenarios = [
            {
                "scenario_id": "s1",
                "category": "sre",
                "overall": 0.90,
                "dimensions": {"task_success": 0.9, "safety": 1.0},
                "blockers": [],
                "passed_gate": True,
            },
        ]
        base = tmp_path / "baseline.json"
        curr = tmp_path / "current.json"
        _write_result(base, "test", base_scenarios, 0.85)
        _write_result(curr, "test", curr_scenarios, 0.90)

        result = compare_results(base, curr)
        assert result.deltas[0].dimension_deltas["task_success"] == 0.1
        assert result.deltas[0].dimension_deltas["safety"] == 0.0

    def test_overall_average_regression_triggers_flag(self, tmp_path):
        base_scenarios = [
            {
                "scenario_id": "s1",
                "category": "sre",
                "overall": 0.85,
                "dimensions": {},
                "blockers": [],
                "passed_gate": True,
            },
            {
                "scenario_id": "s2",
                "category": "sre",
                "overall": 0.85,
                "dimensions": {},
                "blockers": [],
                "passed_gate": True,
            },
        ]
        curr_scenarios = [
            {
                "scenario_id": "s1",
                "category": "sre",
                "overall": 0.83,
                "dimensions": {},
                "blockers": [],
                "passed_gate": True,
            },
            {
                "scenario_id": "s2",
                "category": "sre",
                "overall": 0.80,
                "dimensions": {},
                "blockers": [],
                "passed_gate": True,
            },
        ]
        base = tmp_path / "baseline.json"
        curr = tmp_path / "current.json"
        _write_result(base, "test", base_scenarios, 0.85)
        _write_result(curr, "test", curr_scenarios, 0.815)

        result = compare_results(base, curr)
        # Overall dropped by 0.035, exceeds 0.02 threshold
        assert result.has_regressions is True

    def test_missing_baseline_raises(self, tmp_path):
        curr = tmp_path / "current.json"
        _write_result(curr, "test", [], 0.0)
        try:
            compare_results(tmp_path / "nonexistent.json", curr)
            raise AssertionError("Should have raised FileNotFoundError")
        except FileNotFoundError:
            pass

    def test_empty_scenarios(self, tmp_path):
        base = tmp_path / "baseline.json"
        curr = tmp_path / "current.json"
        _write_result(base, "test", [], 0.0)
        _write_result(curr, "test", [], 0.0)

        result = compare_results(base, curr)
        assert result.regression_count == 0
        assert result.gate_passed is True


class TestFormatComparison:
    def test_text_format(self, tmp_path):
        scenarios = [
            {
                "scenario_id": "s1",
                "category": "sre",
                "overall": 0.85,
                "dimensions": {},
                "blockers": [],
                "passed_gate": True,
            },
        ]
        base = tmp_path / "baseline.json"
        curr = tmp_path / "current.json"
        _write_result(base, "test", scenarios, 0.85)
        _write_result(curr, "test", scenarios, 0.85)

        result = compare_results(base, curr)
        text = format_comparison(result, "text")
        assert "Comparison:" in text
        assert "Gate: PASS" in text
        assert "s1" in text

    def test_json_format(self, tmp_path):
        scenarios = [
            {
                "scenario_id": "s1",
                "category": "sre",
                "overall": 0.85,
                "dimensions": {},
                "blockers": [],
                "passed_gate": True,
            },
        ]
        base = tmp_path / "baseline.json"
        curr = tmp_path / "current.json"
        _write_result(base, "test", scenarios, 0.85)
        _write_result(curr, "test", scenarios, 0.85)

        result = compare_results(base, curr)
        j = format_comparison(result, "json")
        data = json.loads(j)
        assert data["gate_passed"] is True
        assert len(data["deltas"]) == 1

    def test_regression_text_shows_markers(self, tmp_path):
        base_scenarios = [
            {
                "scenario_id": "s1",
                "category": "sre",
                "overall": 0.90,
                "dimensions": {},
                "blockers": [],
                "passed_gate": True,
            },
        ]
        curr_scenarios = [
            {
                "scenario_id": "s1",
                "category": "sre",
                "overall": 0.70,
                "dimensions": {},
                "blockers": [],
                "passed_gate": True,
            },
        ]
        base = tmp_path / "baseline.json"
        curr = tmp_path / "current.json"
        _write_result(base, "test", base_scenarios, 0.90)
        _write_result(curr, "test", curr_scenarios, 0.70)

        result = compare_results(base, curr)
        text = format_comparison(result, "text")
        assert "!!!" in text
        assert "FAIL" in text
