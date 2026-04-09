"""Tests for evals/judge.py — LLM-as-judge scoring."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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
    def test_successful_judge(self):
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
        client.messages.create.return_value = mock_msg

        result = judge_response(
            prompt="Why is my pod crashing?",
            response="OOMKilled — increase memory limits.",
            tool_calls=["list_pods", "describe_pod"],
            client=client,
        )
        assert result is not None
        assert result["total"] == 78
        assert result["correctness"] == 25

    def test_strips_markdown_fences(self):
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
        client.messages.create.return_value = mock_msg

        result = judge_response("q", "a", ["t"], client=client)
        assert result is not None
        assert result["total"] == 80

    def test_no_client_no_api_key(self):
        with patch("sre_agent.agent.create_client", side_effect=RuntimeError("no key")):
            result = judge_response("q", "a", ["t"], client=None)
        assert result is None

    def test_api_call_failure(self):
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("API error")
        result = judge_response("q", "a", ["t"], client=client)
        assert result is None

    def test_invalid_json_response(self):
        mock_msg = SimpleNamespace(content=[SimpleNamespace(text="not json at all")])
        client = MagicMock()
        client.messages.create.return_value = mock_msg
        result = judge_response("q", "a", ["t"], client=client)
        assert result is None

    def test_default_model(self):
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
        client.messages.create.return_value = mock_msg

        judge_response("q", "a", ["t"], client=client)
        call_kwargs = client.messages.create.call_args[1]
        assert call_kwargs["model"] == "claude-3-5-haiku@20241022"

    def test_custom_model(self):
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
        client.messages.create.return_value = mock_msg

        judge_response("q", "a", ["t"], client=client, model="claude-haiku-4-20250514")
        call_kwargs = client.messages.create.call_args[1]
        assert call_kwargs["model"] == "claude-haiku-4-20250514"
