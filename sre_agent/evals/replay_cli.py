"""CLI for running recorded replay evaluations.

Usage:
    python -m sre_agent.evals.replay_cli --fixture crashloop_diagnosis
    python -m sre_agent.evals.replay_cli --all
    python -m sre_agent.evals.replay_cli --list
"""

from __future__ import annotations

import argparse
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


def _make_mock_client(
    tool_names: list[str],
    final_text: str = "Based on my investigation, the issue is likely caused by a dependency failure. I recommend checking the logs and restarting the affected deployment because the root cause appears to be a transient error.",
):
    """Build a mock client that calls the given tools then responds with text."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    tool_blocks = [
        SimpleNamespace(type="tool_use", id=f"t{i}", name=name, input={}) for i, name in enumerate(tool_names)
    ]
    text_block = SimpleNamespace(type="text", text=final_text)

    # First response: call tools
    tool_events = []
    for block in tool_blocks:
        tool_events.append(SimpleNamespace(type="content_block_start", content_block=block))
    tool_msg = SimpleNamespace(
        content=tool_blocks,
        stop_reason="tool_use",
    )
    tool_stream = MagicMock()
    tool_stream.__enter__ = MagicMock(return_value=tool_stream)
    tool_stream.__exit__ = MagicMock(return_value=False)
    tool_stream.__iter__ = MagicMock(return_value=iter(tool_events))
    tool_stream.get_final_message.return_value = tool_msg

    # Second response: final text
    text_events = [
        SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=final_text)),
    ]
    text_msg = SimpleNamespace(
        content=[text_block],
        stop_reason="end_turn",
    )
    text_stream = MagicMock()
    text_stream.__enter__ = MagicMock(return_value=text_stream)
    text_stream.__exit__ = MagicMock(return_value=False)
    text_stream.__iter__ = MagicMock(return_value=iter(text_events))
    text_stream.get_final_message.return_value = text_msg

    client = MagicMock()
    client.messages.stream.side_effect = [tool_stream, text_stream]
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
        from ..agent import create_client

        return create_client(), thinking


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

        judge_result = judge_response(
            prompt=fixture["prompt"],
            response=result["response"],
            tool_calls=[tc["name"] for tc in result["tool_calls"]],
            client=client,
        )
        output["judge"] = judge_result

    return output


def _run_multi_turn_fixture(name: str, fixture: dict, use_judge: bool, model: str, dry_run: bool) -> dict:
    """Run a multi-turn fixture."""
    from .replay import MultiTurnReplayHarness

    harness = MultiTurnReplayHarness(fixture["turns"])
    client, thinking = _setup_model(model, dry_run)

    if dry_run:
        # Build mock client from first turn's tools
        all_tools = list(fixture["turns"][0].get("recorded_responses", {}).keys())
        client = _make_mock_client(all_tools)

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
        judge_result = judge_response(
            prompt=full_prompt,
            response=last["response"],
            tool_calls=all_tools,
            client=client if not dry_run else None,
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
