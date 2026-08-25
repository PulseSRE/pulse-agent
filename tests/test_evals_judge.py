"""Tests for evals/judge.py — LLM-as-judge scoring."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sre_agent.evals.judge import JUDGE_PROMPT_TEMPLATE, judge_response


class TestJudgePromptTemplate:
    def test_has_rubric_dimensions(self):
        assert "Correctness" in JUDGE_PROMPT_TEMPLATE
        assert "Completeness" in JUDGE_PROMPT_TEMPLATE
        assert "Actionability" in JUDGE_PROMPT_TEMPLATE
        assert "Safety" in JUDGE_PROMPT_TEMPLATE

    def test_has_placeholders(self):
        assert "{prompt}" in JUDGE_PROMPT_TEMPLATE
        assert "{response}" in JUDGE_PROMPT_TEMPLATE
        assert "{tool_calls}" in JUDGE_PROMPT_TEMPLATE

    def test_format_succeeds(self):
        result = JUDGE_PROMPT_TEMPLATE.format(
            prompt="Why is my pod crashing?",
            response="The pod is OOMKilled.",
            tool_calls='["list_pods", "get_pod_logs"]',
        )
        assert "Why is my pod crashing?" in result
        assert "OOMKilled" in result


class TestJudgeResponse:
    @pytest.mark.asyncio
    async def test_successful_judge(self):
        mock_msg = SimpleNamespace(
            content=[
                SimpleNamespace(
                    text=json.dumps(
                        {
                            "correctness": 25,
                            "completeness": 20,
                            "actionability": 15,
                            "safety": 18,
                            "total": 78,
                            "reasoning": "Good diagnosis.",
                        }
                    )
                )
            ]
        )
        client = MagicMock()
        client.messages.create = AsyncMock(return_value=mock_msg)

        result = await judge_response(
            prompt="Why is my pod crashing?",
            response="OOMKilled — increase memory limits.",
            tool_calls=["list_pods", "describe_pod"],
            client=client,
        )
        assert result is not None
        assert result["total"] == 78
        assert result["correctness"] == 25

    @pytest.mark.asyncio
    async def test_strips_markdown_fences(self):
        text_with_fences = (
            "```json\n"
            + json.dumps(
                {
                    "total": 80,
                    "correctness": 25,
                    "completeness": 25,
                    "actionability": 15,
                    "safety": 15,
                    "reasoning": "ok",
                }
            )
            + "\n```"
        )
        mock_msg = SimpleNamespace(content=[SimpleNamespace(text=text_with_fences)])
        client = MagicMock()
        client.messages.create = AsyncMock(return_value=mock_msg)

        result = await judge_response("q", "a", ["t"], client=client)
        assert result is not None
        assert result["total"] == 80

    @pytest.mark.asyncio
    async def test_no_client_no_api_key(self):
        with patch("sre_agent.agent.create_async_client", side_effect=RuntimeError("no key")):
            result = await judge_response("q", "a", ["t"], client=None)
        assert result is None

    @pytest.mark.asyncio
    async def test_api_call_failure(self):
        client = MagicMock()
        client.messages.create = AsyncMock(side_effect=RuntimeError("API error"))
        result = await judge_response("q", "a", ["t"], client=client)
        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_json_response(self):
        mock_msg = SimpleNamespace(content=[SimpleNamespace(text="not json at all")])
        client = MagicMock()
        client.messages.create = AsyncMock(return_value=mock_msg)
        result = await judge_response("q", "a", ["t"], client=client)
        assert result is None

    @pytest.mark.asyncio
    async def test_default_model(self):
        mock_msg = SimpleNamespace(
            content=[
                SimpleNamespace(
                    text=json.dumps(
                        {
                            "total": 50,
                            "correctness": 10,
                            "completeness": 10,
                            "actionability": 10,
                            "safety": 10,
                            "reasoning": "ok",
                        }
                    )
                )
            ]
        )
        client = MagicMock()
        client.messages.create = AsyncMock(return_value=mock_msg)

        await judge_response("q", "a", ["t"], client=client)
        call_kwargs = client.messages.create.call_args[1]
        assert call_kwargs["model"] == "claude-sonnet-5"

    @pytest.mark.asyncio
    async def test_custom_model(self):
        mock_msg = SimpleNamespace(
            content=[
                SimpleNamespace(
                    text=json.dumps(
                        {
                            "total": 50,
                            "correctness": 10,
                            "completeness": 10,
                            "actionability": 10,
                            "safety": 10,
                            "reasoning": "ok",
                        }
                    )
                )
            ]
        )
        client = MagicMock()
        client.messages.create = AsyncMock(return_value=mock_msg)

        await judge_response("q", "a", ["t"], client=client, model="claude-haiku-4-20250514")
        call_kwargs = client.messages.create.call_args[1]
        assert call_kwargs["model"] == "claude-haiku-4-20250514"


class TestJudgeMedianSampling:
    def test_median_discards_outlier_grade(self, monkeypatch):
        import asyncio

        from sre_agent.evals import judge as judge_mod

        grades = iter(
            [
                {
                    "correctness": 28,
                    "completeness": 27,
                    "actionability": 18,
                    "safety": 19,
                    "total": 92,
                    "reasoning": "solid",
                },
                {
                    "correctness": 27,
                    "completeness": 26,
                    "actionability": 17,
                    "safety": 19,
                    "total": 89,
                    "reasoning": "fine",
                },
                {
                    "correctness": 12,
                    "completeness": 10,
                    "actionability": 8,
                    "safety": 15,
                    "total": 45,
                    "reasoning": "outlier",
                },
            ]
        )

        async def fake_judge(*args, **kwargs):
            return next(grades)

        monkeypatch.setattr(judge_mod, "judge_response", fake_judge)
        result = asyncio.run(judge_mod.judge_response_median("p", "r", [], samples=3))
        assert result["total"] == 89  # median, not mean — the 45 outlier cannot flip a gate
        assert result["samples"] == 3
        assert result["total_spread"] == [45, 92]
        assert result["reasoning"] == "fine"  # from the sample closest to the median

    def test_single_sample_is_plain_judge(self, monkeypatch):
        import asyncio

        from sre_agent.evals import judge as judge_mod

        async def fake_judge(*args, **kwargs):
            return {"total": 77, "reasoning": "x"}

        monkeypatch.setattr(judge_mod, "judge_response", fake_judge)
        result = asyncio.run(judge_mod.judge_response_median("p", "r", [], samples=1))
        assert result == {"total": 77, "reasoning": "x"}

    def test_all_failed_samples_returns_none(self, monkeypatch):
        import asyncio

        from sre_agent.evals import judge as judge_mod

        async def fake_judge(*args, **kwargs):
            return None

        monkeypatch.setattr(judge_mod, "judge_response", fake_judge)
        assert asyncio.run(judge_mod.judge_response_median("p", "r", [], samples=3)) is None


class TestJudgeUnavailableIsLoud:
    """A missing judge score must not quietly re-gate on keywords.

    integration_incident_correlation scores 96/100 with a [96, 96] spread, yet
    failed the release gate: every judge sample returned malformed JSON, the
    median came back None, and the fixture was then graded on a `mentions`
    substring that the judge makes advisory. The result looked like a
    regression and was not one.
    """

    def test_missing_judge_fails_explicitly(self):
        from sre_agent.evals.replay_cli import _apply_judge_gate

        score = {
            "passed": True,
            "checks": [{"check": "mentions 'configuration'", "passed": False, "kind": "content"}],
        }
        out = _apply_judge_gate(score, None, judge_min=60)
        assert out["passed"] is False
        judge_checks = [c for c in out["checks"] if c.get("kind") == "judge"]
        assert judge_checks and "unavailable" in judge_checks[0]["check"]

    def test_judge_without_threshold_is_untouched(self):
        from sre_agent.evals.replay_cli import _apply_judge_gate

        score = {"passed": True, "checks": []}
        assert _apply_judge_gate(score, None, judge_min=None) == score

    def test_median_retries_once_when_every_sample_fails(self, monkeypatch):
        import asyncio

        from sre_agent.evals import judge as judge_mod

        calls = {"n": 0}

        async def flaky(*args, **kwargs):
            calls["n"] += 1
            # First round (3 samples) all fail; the retry round succeeds.
            if calls["n"] <= 3:
                return None
            return {"total": 91, "correctness": 28, "reasoning": "ok"}

        monkeypatch.setattr(judge_mod, "judge_response", flaky)
        result = asyncio.run(judge_mod.judge_response_median("p", "r", [], samples=3))
        assert result is not None
        assert result["total"] == 91
        assert calls["n"] == 6  # one failed round, one retry round
