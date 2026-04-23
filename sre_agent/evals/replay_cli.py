"""CLI for running recorded replay evaluations.

Usage:
    python -m sre_agent.evals.replay_cli --fixture crashloop_diagnosis
    python -m sre_agent.evals.replay_cli --all
    python -m sre_agent.evals.replay_cli --list
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .replay import ReplayHarness, list_fixtures, load_fixture, score_multi_turn, score_replay


def _make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pulse-eval replay",
        description="Run recorded replay evaluations against the agent.",
    )
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--fixture",
        help="Name of a single fixture to replay (e.g. crashloop_diagnosis).",
    )
    group.add_argument(
        "--all",
        action="store_true",
        help="Replay all available fixtures.",
    )
    group.add_argument(
        "--list",
        action="store_true",
        help="List available fixture names and exit.",
    )
    p.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    p.add_argument(
        "--judge",
        action="store_true",
        help="Also run LLM-as-judge scoring (requires API key).",
    )
    p.add_argument(
        "--model",
        default="claude-sonnet-4-6",
        help="Model for the agent (default: claude-sonnet-4-6).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Use a mock Claude client (no API key needed). Tests tool wiring and scoring only.",
    )
    return p


class _MockAsyncStream:
    """Mock async stream for eval dry-run mode."""

    def __init__(self, events, final_message):
        self._events = events
        self._final_message = final_message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def __aiter__(self):
        return self._async_iter()

    async def _async_iter(self):
        for event in self._events:
            yield event

    async def get_final_message(self):
        return self._final_message


def _make_mock_stream(tool_names: list[str], text: str, stop_reason: str = "end_turn"):
    """Build one mock stream cycle (tool calls → text response)."""
    from types import SimpleNamespace

    streams = []

    if tool_names:
        tool_blocks = [
            SimpleNamespace(type="tool_use", id=f"t{i}", name=name, input={}) for i, name in enumerate(tool_names)
        ]
        tool_events = [SimpleNamespace(type="content_block_start", content_block=b) for b in tool_blocks]
        tool_msg = SimpleNamespace(content=tool_blocks, stop_reason="tool_use")
        streams.append(_MockAsyncStream(tool_events, tool_msg))

    text_block = SimpleNamespace(type="text", text=text)
    text_events = [
        SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=text)),
    ]
    text_msg = SimpleNamespace(content=[text_block], stop_reason=stop_reason)
    streams.append(_MockAsyncStream(text_events, text_msg))

    return streams


def _make_mock_client(
    tool_names: list[str],
    final_text: str = "Based on my investigation, the issue is likely caused by a dependency failure. I recommend checking the logs and restarting the affected deployment because the root cause appears to be a transient error.",
):
    """Build a mock client that calls the given tools then responds with text (single-turn)."""
    from unittest.mock import MagicMock

    streams = _make_mock_stream(tool_names, final_text)
    client = MagicMock()
    client.messages.stream.side_effect = streams
    return client


def _make_multi_turn_mock_client(turns: list[dict], expected_keywords: list[str]):
    """Build a mock client for multi-turn conversations.

    Each turn gets its own tool call + text response cycle.
    The text response includes expected keywords and references to the turn's data.
    """
    from unittest.mock import MagicMock

    all_streams = []
    keyword_text = ", ".join(expected_keywords) if expected_keywords else "the affected resources"

    for i, turn in enumerate(turns):
        turn_tools = list(turn.get("recorded_responses", {}).keys())
        # Build a response that mentions expected keywords and the turn's context
        turn_text = (
            f"Based on turn {i + 1} investigation using {', '.join(turn_tools)}, "
            f"I found issues related to: {keyword_text}. "
            f"The {turn.get('prompt', '')[:30]} analysis shows the root cause."
        )
        streams = _make_mock_stream(turn_tools, turn_text)
        all_streams.extend(streams)

    client = MagicMock()
    client.messages.stream.side_effect = all_streams
    return client


def _setup_model(model: str, dry_run: bool):
    """Configure model settings and return (client, thinking)."""
    import os

    os.environ["PULSE_AGENT_HARNESS"] = "0"
    os.environ["PULSE_AGENT_MODEL"] = model

    import sre_agent.config as _cfg

    _cfg._settings = None

    thinking = {"type": "adaptive"}
    if "haiku" in model.lower() or "claude-3-opus" in model.lower() or "claude-3-sonnet" in model.lower():
        thinking = {"type": "disabled"}
        os.environ["PULSE_AGENT_MAX_TOKENS"] = "8192"

    if dry_run:
        return None, thinking  # caller builds mock client per fixture
    else:
        from ..agent import create_async_client

        return create_async_client(), thinking


def _run_fixture(name: str, use_judge: bool = False, model: str = "claude-sonnet-4-6", dry_run: bool = False) -> dict:
    """Run a single fixture (single-turn or multi-turn) and return the scored result."""
    fixture = load_fixture(name)

    # Multi-turn fixture
    if fixture.get("multi_turn"):
        return _run_multi_turn_fixture(name, fixture, use_judge, model, dry_run)

    harness = ReplayHarness(fixture["recorded_responses"])
    client, thinking = _setup_model(model, dry_run)

    if dry_run:
        expected_tools = fixture.get("expected", {}).get("should_use_tools", list(fixture["recorded_responses"].keys()))
        client = _make_mock_client(expected_tools)

    result = harness.run(client=client, prompt=fixture["prompt"], thinking=thinking)
    score = score_replay(result, fixture["expected"])

    output = {
        "fixture": name,
        "prompt": fixture["prompt"],
        "score": score,
        "response_preview": result["response"][:500],
        "duration_ms": result["duration_ms"],
    }

    if use_judge:
        from .judge import judge_response

        judge_result = asyncio.run(
            judge_response(
                prompt=fixture["prompt"],
                response=result["response"],
                tool_calls=[tc["name"] for tc in result["tool_calls"]],
                client=client,
            )
        )
        output["judge"] = judge_result

    return output


def _run_multi_turn_fixture(name: str, fixture: dict, use_judge: bool, model: str, dry_run: bool) -> dict:
    """Run a multi-turn fixture."""
    from .replay import MultiTurnReplayHarness

    harness = MultiTurnReplayHarness(fixture["turns"])
    client, thinking = _setup_model(model, dry_run)

    if dry_run:
        # Build a multi-turn mock client with per-turn tool call + text cycles
        expected_keywords = fixture.get("expected", {}).get("overall_should_mention", [])
        client = _make_multi_turn_mock_client(fixture["turns"], expected_keywords)

    result = harness.run(client=client, thinking=thinking)
    score = score_multi_turn(result, fixture.get("expected", {}))

    output = {
        "fixture": name,
        "multi_turn": True,
        "prompt": " → ".join(t["prompt"][:50] for t in fixture["turns"]),
        "score": score,
        "response_preview": result["turns"][-1]["response"][:500] if result["turns"] else "",
        "duration_ms": result["total_duration_ms"],
        "turn_count": len(result["turns"]),
    }

    if use_judge and result["turns"]:
        from .judge import judge_response

        # Judge the final turn (most comprehensive answer)
        last = result["turns"][-1]
        all_tools = [tc["name"] for t in result["turns"] for tc in t["tool_calls"]]
        full_prompt = " → ".join(t["prompt"] for t in fixture["turns"])
        judge_result = asyncio.run(
            judge_response(
                prompt=full_prompt,
                response=last["response"],
                tool_calls=all_tools,
                client=client if not dry_run else None,
            )
        )
        output["judge"] = judge_result

    return output


def _format_text(results: list[dict]) -> str:
    lines = []
    for r in results:
        score = r["score"]
        status = "PASS" if score["passed"] else "FAIL"
        lines.append(f"\n{'=' * 60}")
        turn_info = f"  ({r['turn_count']} turns)" if r.get("multi_turn") else ""
        lines.append(f"Fixture: {r['fixture']}{turn_info}  [{status}]  Score: {score['score']}/100")
        lines.append(f"Duration: {r['duration_ms']:.0f}ms")
        tool_calls = score.get("total_tool_calls", score.get("tool_calls", []))
        lines.append(f"Tools called: {', '.join(tool_calls) or '(none)'}")
        if r.get("error"):
            lines.append(f"Error: {r['error']}")
        lines.append("Checks:")
        for check in score["checks"]:
            mark = "  [x]" if check["passed"] else "  [ ]"
            lines.append(f"  {mark} {check['check']}")
        if r.get("judge"):
            j = r["judge"]
            lines.append(f"Judge: total={j.get('total', '?')}/100 -- {j.get('reasoning', 'N/A')}")
        lines.append(f"Response preview: {r['response_preview'][:200]}...")

    # Summary
    total = len(results)
    passed = sum(1 for r in results if r["score"]["passed"])
    lines.append(f"\n{'=' * 60}")
    lines.append(f"Summary: {passed}/{total} fixtures passed")
    return "\n".join(lines)


def main() -> None:
    args = _make_parser().parse_args()

    if args.list:
        for name in list_fixtures():
            print(name)
        return

    fixtures = list_fixtures() if args.all else [args.fixture]
    results = []
    for name in fixtures:
        try:
            result = _run_fixture(name, use_judge=args.judge, model=args.model, dry_run=args.dry_run)
            results.append(result)
        except Exception as e:
            results.append(
                {
                    "fixture": name,
                    "error": str(e),
                    "score": {"passed": False, "score": 0, "checks": [], "tool_calls": []},
                    "response_preview": "",
                    "duration_ms": 0,
                }
            )

    if args.format == "json":
        print(json.dumps(results, indent=2, default=str))
    else:
        print(_format_text(results))

    # Exit non-zero if any fixture failed
    if not all(r["score"]["passed"] for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
