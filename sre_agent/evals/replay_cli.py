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

from .replay import ReplayHarness, list_fixtures, load_fixture, score_replay


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
        default="claude-3-5-haiku@20241022",
        help="Model for the agent (default: claude-3-5-haiku@20241022).",
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


def _run_fixture(
    name: str, use_judge: bool = False, model: str = "claude-sonnet-4-20250514", dry_run: bool = False
) -> dict:
    """Run a single fixture and return the scored result."""
    fixture = load_fixture(name)
    harness = ReplayHarness(fixture["recorded_responses"])

    import os

    os.environ["PULSE_AGENT_HARNESS"] = "0"  # Disable harness for replay

    if dry_run:
        # Use mock client — no API key needed
        expected_tools = fixture.get("expected", {}).get("should_use_tools", list(fixture["recorded_responses"].keys()))
        client = _make_mock_client(expected_tools)
    else:
        from ..agent import create_client

        os.environ.setdefault("PULSE_AGENT_MODEL", model)
        client = create_client()
    result = harness.run(client=client, prompt=fixture["prompt"])
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


def _format_text(results: list[dict]) -> str:
    lines = []
    for r in results:
        score = r["score"]
        status = "PASS" if score["passed"] else "FAIL"
        lines.append(f"\n{'=' * 60}")
        lines.append(f"Fixture: {r['fixture']}  [{status}]  Score: {score['score']}/100")
        lines.append(f"Duration: {r['duration_ms']:.0f}ms")
        lines.append(f"Tools called: {', '.join(score['tool_calls']) or '(none)'}")
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
