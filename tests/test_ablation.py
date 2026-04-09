"""Tests for prompt section ablation framework."""

from __future__ import annotations

import os
from unittest.mock import patch

from sre_agent.evals.ablation import (
    ALL_SECTIONS,
    AblationResult,
    SectionResult,
    format_ablation,
    run_ablation,
)
from sre_agent.intelligence import _get_excluded_sections


class TestExcludedSections:
    def test_empty_env_returns_empty_set(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("PULSE_PROMPT_EXCLUDE_SECTIONS", None)
            assert _get_excluded_sections() == set()

    def test_single_section(self):
        with patch.dict(os.environ, {"PULSE_PROMPT_EXCLUDE_SECTIONS": "chain_hints"}):
            assert _get_excluded_sections() == {"chain_hints"}

    def test_multiple_sections(self):
        with patch.dict(os.environ, {"PULSE_PROMPT_EXCLUDE_SECTIONS": "chain_hints,component_schemas"}):
            result = _get_excluded_sections()
            assert "chain_hints" in result
            assert "component_schemas" in result

    def test_whitespace_trimmed(self):
        with patch.dict(os.environ, {"PULSE_PROMPT_EXCLUDE_SECTIONS": " chain_hints , component_schemas "}):
            result = _get_excluded_sections()
            assert "chain_hints" in result
            assert "component_schemas" in result

    def test_empty_string_returns_empty_set(self):
        with patch.dict(os.environ, {"PULSE_PROMPT_EXCLUDE_SECTIONS": ""}):
            assert _get_excluded_sections() == set()


class TestAllSections:
    def test_all_sections_is_list(self):
        assert isinstance(ALL_SECTIONS, list)
        assert len(ALL_SECTIONS) >= 10

    def test_all_sections_are_strings(self):
        for s in ALL_SECTIONS:
            assert isinstance(s, str)

    def test_includes_chain_hints(self):
        assert "chain_hints" in ALL_SECTIONS

    def test_includes_intelligence_sections(self):
        intel_sections = [s for s in ALL_SECTIONS if s.startswith("intelligence_")]
        assert len(intel_sections) == 8

    def test_includes_component_sections(self):
        comp_sections = [s for s in ALL_SECTIONS if s.startswith("component_")]
        assert len(comp_sections) == 3


class TestAblationResult:
    def test_sorted_by_impact(self):
        result = AblationResult(
            suite="test",
            mode="sre",
            baseline_average=0.85,
            results=[
                SectionResult("a", 0.85, 0.85, 0.0, 100),
                SectionResult("b", 0.85, 0.80, -0.05, 200),
                SectionResult("c", 0.85, 0.87, 0.02, 50),
            ],
        )
        sorted_r = result.sorted_by_impact
        assert sorted_r[0].section == "b"  # most negative delta first
        assert sorted_r[-1].section == "c"

    def test_trim_candidates(self):
        result = AblationResult(
            suite="test",
            mode="sre",
            baseline_average=0.85,
            results=[
                SectionResult("safe_to_remove", 0.85, 0.86, 0.01, 500),
                SectionResult("neutral", 0.85, 0.85, 0.0, 200),
                SectionResult("needed", 0.85, 0.80, -0.05, 100),
            ],
        )
        trims = result.trim_candidates
        assert len(trims) == 2
        names = [t.section for t in trims]
        assert "safe_to_remove" in names
        assert "neutral" in names
        assert "needed" not in names


class TestRunAblation:
    def test_runs_with_deterministic_suite(self):
        """Ablation on deterministic suite (scores won't change since responses are pre-baked,
        but it verifies the machinery works)."""
        result = run_ablation(suite="core", sections=["chain_hints", "component_schemas"], mode="sre")
        assert result.suite == "core"
        assert result.mode == "sre"
        assert result.baseline_average > 0
        assert len(result.results) == 2
        for r in result.results:
            assert r.section in ("chain_hints", "component_schemas")
            # Deterministic suites won't show delta (pre-baked responses)
            assert r.delta == 0.0

    def test_env_var_cleaned_up(self):
        """Verify PULSE_PROMPT_EXCLUDE_SECTIONS is cleaned up after ablation."""
        run_ablation(suite="core", sections=["chain_hints"], mode="sre")
        assert "PULSE_PROMPT_EXCLUDE_SECTIONS" not in os.environ


class TestFormatAblation:
    def test_text_format(self):
        result = AblationResult(
            suite="core",
            mode="sre",
            baseline_average=0.85,
            results=[
                SectionResult("chain_hints", 0.85, 0.85, 0.0, 180),
            ],
        )
        text = format_ablation(result, "text")
        assert "Ablation Report" in text
        assert "chain_hints" in text
        assert "Baseline average" in text

    def test_json_format(self):
        result = AblationResult(
            suite="core",
            mode="sre",
            baseline_average=0.85,
            results=[
                SectionResult("chain_hints", 0.85, 0.85, 0.0, 180),
            ],
        )
        import json

        j = format_ablation(result, "json")
        data = json.loads(j)
        assert data["suite"] == "core"
        assert len(data["results"]) == 1
