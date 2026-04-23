"""Parallel skill output synthesis — merge, conflict detection, fallback.

When two skills run in parallel, this module merges their outputs into a
single coherent response. Contradictions are surfaced as structured Conflict
objects for human arbitration rather than silently resolved.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("pulse_agent.synthesis")

SYNTHESIS_MODEL = "claude-sonnet-4-6"


@dataclass
class Conflict:
    topic: str
    skill_a: str
    position_a: str
    skill_b: str
    position_b: str


@dataclass
class SynthesisResult:
    unified_response: str
    conflicts: list[Conflict] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)


@dataclass
class ParallelSkillResult:
    primary_output: str
    secondary_output: str
    primary_skill: str
    secondary_skill: str
    primary_confidence: float
    secondary_confidence: float
    duration_ms: int
    primary_tokens: dict[str, int] = field(default_factory=dict)
    secondary_tokens: dict[str, int] = field(default_factory=dict)
    primary_components: list[dict] = field(default_factory=list)
    secondary_components: list[dict] = field(default_factory=list)


_SYNTHESIS_SYSTEM = (
    "You merge two AI skill outputs into one coherent response.\n\n"
    "Rules:\n"
    "1. Combine non-conflicting findings into a single narrative. Do not repeat information.\n"
    "2. If the skills contradict each other, emit a JSON conflict block — do NOT resolve it.\n"
    "3. Attribute findings to their source skill where relevant.\n"
    "4. Keep the merged response concise and actionable.\n\n"
    "Output format:\n"
    "- Write the merged response as plain text (markdown OK).\n"
    "- If conflicts exist, end with a JSON block:\n"
    '```json\n{"conflicts": [{"topic": "...", "skill_a": "...", "position_a": "...", '
    '"skill_b": "...", "position_b": "..."}]}\n```\n'
    "- If no conflicts, do not include the JSON block."
)


def _build_fallback_response(result: ParallelSkillResult) -> str:
    return (
        f"## {result.primary_skill.upper()} Analysis\n\n"
        f"{result.primary_output}\n\n"
        f"## {result.secondary_skill.upper()} Analysis\n\n"
        f"{result.secondary_output}"
    )


def _parse_conflicts(text: str) -> tuple[str, list[Conflict]]:
    conflicts: list[Conflict] = []
    json_start = text.rfind("```json")
    if json_start == -1:
        return text, conflicts

    json_end = text.find("```", json_start + 7)
    if json_end == -1:
        return text, conflicts

    json_str = text[json_start + 7 : json_end].strip()
    clean_text = text[:json_start].strip()

    try:
        data = json.loads(json_str)
        for c in data.get("conflicts", []):
            conflicts.append(
                Conflict(
                    topic=c.get("topic", ""),
                    skill_a=c.get("skill_a", ""),
                    position_a=c.get("position_a", ""),
                    skill_b=c.get("skill_b", ""),
                    position_b=c.get("position_b", ""),
                )
            )
    except (json.JSONDecodeError, KeyError):
        logger.debug("Failed to parse conflict JSON from synthesis", exc_info=True)

    return clean_text, conflicts


async def synthesize_parallel_outputs(
    result: ParallelSkillResult,
    query: str,
    client,
    on_text_delta=None,
) -> SynthesisResult:
    """Merge two parallel skill outputs into a unified response.

    Uses Claude Sonnet for intelligent merging and conflict detection.
    Falls back to concatenation if the synthesis call fails.

    Args:
        on_text_delta: optional async callback(str) for streaming synthesis text.
    """
    try:
        user_content = (
            f"Original user query: {query}\n\n"
            f"--- {result.primary_skill.upper()} SKILL OUTPUT ---\n"
            f"{result.primary_output}\n\n"
            f"--- {result.secondary_skill.upper()} SKILL OUTPUT ---\n"
            f"{result.secondary_output}"
        )

        if on_text_delta:
            collected: list[str] = []
            async with client.messages.stream(
                model=SYNTHESIS_MODEL,
                max_tokens=4096,
                system=_SYNTHESIS_SYSTEM,
                messages=[{"role": "user", "content": user_content}],
            ) as stream:
                async for text in stream.text_stream:
                    collected.append(text)
                    await on_text_delta(text)
            raw_text = "".join(collected)
        else:
            response = await client.messages.create(
                model=SYNTHESIS_MODEL,
                max_tokens=4096,
                system=_SYNTHESIS_SYSTEM,
                messages=[{"role": "user", "content": user_content}],
            )
            raw_text = response.content[0].text

        clean_text, conflicts = _parse_conflicts(raw_text)

        return SynthesisResult(
            unified_response=clean_text,
            conflicts=conflicts,
            sources={
                result.primary_skill: result.primary_output[:200],
                result.secondary_skill: result.secondary_output[:200],
            },
        )

    except Exception:
        logger.warning("Synthesis failed, falling back to concatenation", exc_info=True)
        fallback = _build_fallback_response(result)
        if on_text_delta:
            try:
                await on_text_delta(fallback)
            except Exception:
                pass
        return SynthesisResult(
            unified_response=fallback,
            conflicts=[],
            sources={
                result.primary_skill: result.primary_output[:200],
                result.secondary_skill: result.secondary_output[:200],
            },
        )
