"""Tests for the synthesis module — parallel skill output merging."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from sre_agent.synthesis import (
    Conflict,
    ParallelSkillResult,
    SynthesisResult,
    _build_fallback_response,
    _parse_conflicts,
    synthesize_parallel_outputs,
)


def test_conflict_dataclass():
    c = Conflict(
        topic="scaling",
        skill_a="sre",
        position_a="scale up",
        skill_b="capacity_planner",
        position_b="node at limit",
    )
    assert c.topic == "scaling"
    assert c.skill_a == "sre"


def test_synthesis_result_dataclass():
    r = SynthesisResult(
        unified_response="merged output",
        conflicts=[],
        sources={"sre": "pod analysis"},
    )
    assert r.unified_response == "merged output"
    assert r.conflicts == []


def test_parallel_skill_result_dataclass():
    r = ParallelSkillResult(
        primary_output="SRE findings",
        secondary_output="Security findings",
        primary_skill="sre",
        secondary_skill="security",
        primary_confidence=0.9,
        secondary_confidence=0.85,
        duration_ms=3000,
    )
    assert r.primary_skill == "sre"
    assert r.duration_ms == 3000


def test_fallback_response_concatenates():
    result = ParallelSkillResult(
        primary_output="SRE: pods crashing",
        secondary_output="Security: no CVEs found",
        primary_skill="sre",
        secondary_skill="security",
        primary_confidence=0.9,
        secondary_confidence=0.8,
        duration_ms=2000,
    )
    fallback = _build_fallback_response(result)
    assert "SRE: pods crashing" in fallback
    assert "Security: no CVEs found" in fallback
    assert "sre" in fallback.lower()
    assert "security" in fallback.lower()


def test_parse_conflicts_no_json():
    text = "Everything looks fine."
    clean, conflicts = _parse_conflicts(text)
    assert clean == "Everything looks fine."
    assert conflicts == []


def test_parse_conflicts_with_json():
    text = (
        "Here are findings.\n\n"
        "```json\n"
        '{"conflicts": [{"topic": "scaling", "skill_a": "sre", '
        '"position_a": "scale up", "skill_b": "cap", "position_b": "limit"}]}\n'
        "```"
    )
    clean, conflicts = _parse_conflicts(text)
    assert "Here are findings." in clean
    assert len(conflicts) == 1
    assert conflicts[0].topic == "scaling"


def test_synthesize_fallback_on_api_error():
    """When Sonnet call fails, falls back to concatenation."""
    import asyncio

    result = ParallelSkillResult(
        primary_output="SRE output",
        secondary_output="Security output",
        primary_skill="sre",
        secondary_skill="security",
        primary_confidence=0.9,
        secondary_confidence=0.8,
        duration_ms=2000,
    )
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=Exception("API down"))

    synthesis = asyncio.run(synthesize_parallel_outputs(result, "test query", mock_client))
    assert "SRE output" in synthesis.unified_response
    assert "Security output" in synthesis.unified_response
    assert synthesis.conflicts == []
