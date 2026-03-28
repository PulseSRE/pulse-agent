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
        default="claude-sonnet-4-20250514",
        help="Model for the agent (default: claude-sonnet-4-20250514).",
    )
    return p


def _run_fixture(name: str, use_judge: bool = False, model: str = "claude-sonnet-4-20250514") -> dict:
    """Run a single fixture and return the scored result."""
    fixture = load_fixture(name)
    harness = ReplayHarness(fixture["recorded_responses"])

    # Create a real client for the agent
    from ..agent import create_client
    import os
    os.environ.setdefault("PULSE_AGENT_MODEL", model)
    os.environ["PULSE_AGENT_HARNESS"] = "0"  # Disable harness for replay

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
        lines.append(f"\n{'='*60}")
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
    lines.append(f"\n{'='*60}")
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
            result = _run_fixture(name, use_judge=args.judge, model=args.model)
            results.append(result)
        except Exception as e:
            results.append({
                "fixture": name,
                "error": str(e),
                "score": {"passed": False, "score": 0, "checks": [], "tool_calls": []},
                "response_preview": "",
                "duration_ms": 0,
            })

    if args.format == "json":
        print(json.dumps(results, indent=2, default=str))
    else:
        print(_format_text(results))

    # Exit non-zero if any fixture failed
    if not all(r["score"]["passed"] for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
