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
from pathlib import Path

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
        default="claude-sonnet-5",
        help="Model for the agent (default: claude-sonnet-5).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Use a mock Claude client (no API key needed). Tests tool wiring and scoring only.",
    )
    p.add_argument(
        "--stub-config",
        action="store_true",
        help=(
            "Replay with the old stub configuration (one-line system prompt, parameterless tool "
            "stubs) instead of Pulse's real skill prompt and tool schemas. For comparison only."
        ),
    )
    p.add_argument(
        "--mode",
        default=None,
        help="Force a skill (sre, security, view_designer, ...) instead of routing each prompt.",
    )
    p.add_argument(
        "--concurrency",
        type=int,
        default=1,
        metavar="N",
        help=(
            "Run N fixtures at once (default 1). The suite is API-bound, so this is "
            "bounded by provider rate limits rather than CPU — 4 to 6 is usually the "
            "useful range. Fixture order in the output is preserved either way."
        ),
    )
    p.add_argument(
        "--confirm-retries",
        type=int,
        default=2,
        metavar="N",
        help=(
            "Re-run each suspected regression up to N times before failing the gate. "
            "The judge is non-deterministic, so a fixture that fails once and passes "
            "on retry is noise rather than a regression. 0 disables confirmation."
        ),
    )
    p.add_argument(
        "--baseline",
        metavar="PATH",
        help=(
            "Compare against a stored baseline and fail if a fixture that passed there "
            "does not pass now. Gates on regression rather than on an absolute pass rate, "
            "so a suite that is not yet fully green can still block work that makes it worse."
        ),
    )
    p.add_argument(
        "--save-baseline",
        metavar="PATH",
        help="Write this run's per-fixture pass/fail to PATH and exit successfully.",
    )
    p.add_argument(
        "--judge-min",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Gate on the judge's total score (0-100) instead of keyword matching. "
            "Requires --judge. Content checks become advisory; structure checks still gate."
        ),
    )
    p.add_argument(
        "--judge-samples",
        type=int,
        default=1,
        metavar="N",
        help=(
            "Sample the judge N times per fixture and gate on the per-dimension median. "
            "Judge-score variance flips borderline fixtures run-to-run; the median over "
            "the same transcript is stable and cheap relative to the agent run."
        ),
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
    """Configure model settings and return (client, thinking).

    ``PULSE_AGENT_HARNESS=0`` disables the agent loop's *own* prompt assembly
    and tool re-selection. The replay harness has already done both, offline,
    via ``build_replay_config`` — leaving the loop's copy enabled would rebuild
    the prompt from live cluster state mid-run.
    """
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


def _expected_for(expected: dict, dry_run: bool) -> dict:
    """Drop the checks a mock client decides, when replaying in dry-run.

    In dry-run the response is a fixed string from ``_make_mock_client`` and the
    call sequence is synthesised from the fixture, so ``should_mention`` measures
    that string and ``should_use_tools_in_order`` measures the mock's ordering —
    neither says anything about the agent. What remains (tools dispatched, forbidden
    tools avoided, call budget respected) verifies that the replay harness still
    drives the loop, which is all this mode can honestly check. Live runs are scored
    against the full expectation.
    """
    if not dry_run:
        return expected
    mock_determined = {"should_mention", "overall_should_mention", "should_use_tools_in_order"}
    trimmed = {k: v for k, v in expected.items() if k not in mock_determined}
    if "per_turn" in trimmed:
        trimmed["per_turn"] = [{k: v for k, v in t.items() if k not in mock_determined} for t in trimmed["per_turn"]]
    return trimmed


def _apply_judge_gate(score: dict, judge: dict | None, judge_min: int | None) -> dict:
    """Let the judge decide correctness instead of keyword matching.

    ``score_replay`` fails a fixture unless every ``should_mention`` substring
    appears verbatim, so a correct diagnosis phrased differently fails while a
    wrong one that name-drops the right nouns passes. When a judge score is
    available and a threshold is set, content checks drop to advisory and the
    judge's total gates instead. Structure checks — tools dispatched, forbidden
    tools avoided, call budget — always keep gating, since those are objective.

    With no threshold or no judge result the score is returned untouched, so
    existing behaviour is unchanged unless ``--judge-min`` is passed.
    """
    if judge_min is None:
        return score

    total = judge.get("total") if judge else None
    if not isinstance(total, (int, float)):
        # The caller asked for judge gating and there is no judge score. Falling
        # through to content checks would quietly grade this fixture by a
        # different, weaker standard than every other fixture in the run, and
        # report the result as if it were comparable. Say so instead; the
        # runner's existing regression retry absorbs transient judge failures.
        checks = list(score.get("checks", []))
        checks.append(
            {
                "check": f"judge score unavailable (judge_min={judge_min} requested)",
                "passed": False,
                "kind": "judge",
            }
        )
        return {**score, "checks": checks, "passed": False}

    checks = []
    for check in score.get("checks", []):
        if check.get("kind") == "content":
            check = {**check, "advisory": True}
        checks.append(check)

    checks.append(
        {
            "check": f"judge total >= {judge_min} (actual: {total})",
            "passed": total >= judge_min,
            "weight": 1,
            "kind": "judge",
        }
    )

    gating = [c for c in checks if not c.get("advisory")]
    return {**score, "checks": checks, "passed": all(c["passed"] for c in gating)}


def _run_fixture(
    name: str,
    use_judge: bool = False,
    model: str = "claude-sonnet-5",
    dry_run: bool = False,
    judge_min: int | None = None,
    stub_config: bool = False,
    mode: str | None = None,
    judge_samples: int = 1,
) -> dict:
    """Run a single fixture (single-turn or multi-turn) and return the scored result."""
    fixture = load_fixture(name)

    # Multi-turn fixture
    if fixture.get("multi_turn"):
        return _run_multi_turn_fixture(
            name, fixture, use_judge, model, dry_run, judge_min, stub_config, mode, judge_samples
        )

    harness = ReplayHarness(
        fixture["recorded_responses"],
        mode=mode or fixture.get("mode"),
        stub_config=stub_config,
    )
    client, thinking = _setup_model(model, dry_run)

    if dry_run:
        expected_tools = fixture.get("expected", {}).get("should_use_tools", list(fixture["recorded_responses"].keys()))
        client = _make_mock_client(expected_tools)

    result = harness.run(client=client, prompt=fixture["prompt"], thinking=thinking)
    score = score_replay(result, _expected_for(fixture["expected"], dry_run))

    output = {
        "fixture": name,
        "prompt": fixture["prompt"],
        "score": score,
        "response_preview": result["response"][:500],
        "duration_ms": result["duration_ms"],
        "mode": result.get("mode"),
        "offered_tool_count": result.get("offered_tool_count"),
        "unrecorded_tool_calls": result.get("unrecorded_tool_calls", []),
        "unoffered_recorded_tools": result.get("unoffered_recorded_tools", []),
    }

    if use_judge:
        from .judge import judge_response_median

        judge_result = asyncio.run(
            judge_response_median(
                prompt=fixture["prompt"],
                response=result["response"],
                tool_calls=[tc["name"] for tc in result["tool_calls"]],
                client=client,
                samples=judge_samples,
            )
        )
        output["judge"] = judge_result
        output["score"] = _apply_judge_gate(output["score"], judge_result, judge_min)

    return output


def _run_multi_turn_fixture(
    name: str,
    fixture: dict,
    use_judge: bool,
    model: str,
    dry_run: bool,
    judge_min: int | None = None,
    stub_config: bool = False,
    mode: str | None = None,
    judge_samples: int = 1,
) -> dict:
    """Run a multi-turn fixture."""
    from .replay import MultiTurnReplayHarness

    harness = MultiTurnReplayHarness(
        fixture["turns"],
        mode=mode or fixture.get("mode"),
        stub_config=stub_config,
    )
    client, thinking = _setup_model(model, dry_run)

    if dry_run:
        # Build a multi-turn mock client with per-turn tool call + text cycles
        expected_keywords = fixture.get("expected", {}).get("overall_should_mention", [])
        client = _make_multi_turn_mock_client(fixture["turns"], expected_keywords)

    result = harness.run(client=client, thinking=thinking)
    score = score_multi_turn(result, _expected_for(fixture.get("expected", {}), dry_run))

    output = {
        "fixture": name,
        "multi_turn": True,
        "prompt": " → ".join(t["prompt"][:50] for t in fixture["turns"]),
        "score": score,
        "response_preview": result["turns"][-1]["response"][:500] if result["turns"] else "",
        "duration_ms": result["total_duration_ms"],
        "turn_count": len(result["turns"]),
        "mode": " → ".join(result.get("modes", [])),
        "unrecorded_tool_calls": result.get("unrecorded_tool_calls", []),
        "unoffered_recorded_tools": result.get("unoffered_recorded_tools", []),
    }

    if use_judge and result["turns"]:
        from .judge import judge_response_median

        # Judge the final turn (most comprehensive answer)
        last = result["turns"][-1]
        all_tools = [tc["name"] for t in result["turns"] for tc in t["tool_calls"]]
        full_prompt = " → ".join(t["prompt"] for t in fixture["turns"])
        judge_result = asyncio.run(
            judge_response_median(
                prompt=full_prompt,
                response=last["response"],
                tool_calls=all_tools,
                client=client if not dry_run else None,
                samples=judge_samples,
            )
        )
        output["judge"] = judge_result
        output["score"] = _apply_judge_gate(output["score"], judge_result, judge_min)

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
        if r.get("mode"):
            offered = r.get("offered_tool_count")
            offered_txt = f", {offered} tools offered" if offered else ""
            lines.append(f"Skill: {r['mode']}{offered_txt}")
        tool_calls = score.get("total_tool_calls", score.get("tool_calls", []))
        lines.append(f"Tools called: {', '.join(tool_calls) or '(none)'}")
        if r.get("unrecorded_tool_calls"):
            lines.append(f"Called with no recording: {', '.join(r['unrecorded_tool_calls'])}")
        if r.get("unoffered_recorded_tools"):
            lines.append(f"Recorded but not offered: {', '.join(r['unoffered_recorded_tools'])}")
        if r.get("error"):
            lines.append(f"Error: {r['error']}")
        lines.append("Checks:")
        for check in score["checks"]:
            mark = "  [x]" if check["passed"] else "  [ ]"
            if check.get("advisory"):
                mark = "  [~]" if check["passed"] else "  [!]"
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

    if args.concurrency < 1:
        print("--concurrency must be at least 1.", file=sys.stderr)
        sys.exit(2)

    if args.judge_min is not None and not args.judge:
        print("--judge-min requires --judge (there is no judge score to gate on).", file=sys.stderr)
        sys.exit(2)

    fixtures = list_fixtures() if args.all else [args.fixture]

    def _run_one(name: str) -> dict:
        try:
            return _run_fixture(
                name,
                use_judge=args.judge,
                model=args.model,
                dry_run=args.dry_run,
                judge_min=args.judge_min,
                stub_config=args.stub_config,
                mode=args.mode,
                judge_samples=args.judge_samples,
            )
        except Exception as e:
            return {
                "fixture": name,
                "error": str(e),
                "score": {"passed": False, "score": 0, "checks": [], "tool_calls": []},
                "response_preview": "",
                "duration_ms": 0,
            }

    # Hold isolation open for the whole run. Each fixture still enters it, but
    # refcounting means the patches are applied once here and removed once at the
    # end — so no fixture finishing can restore a live cluster read underneath a
    # fixture still in flight, and there is no unpatched window between fixtures.
    # False mirrors the harness default; the CLI exposes no way to change it, and
    # a direct-harness caller that disagrees will fail loudly rather than run
    # under isolation it did not ask for.
    from .replay_config import offline_context

    with offline_context(allow_llm_tool_picker=False):
        results = _execute(fixtures, _run_one, args.concurrency)

    if args.format == "json":
        print(json.dumps(results, indent=2, default=str))
    else:
        print(_format_text(results))

    if args.save_baseline:
        _write_baseline(results, args.save_baseline)
        return

    if args.baseline:
        baseline = _load_baseline(args.baseline)
        if not baseline:
            print("Baseline could not be read — refusing to pass silently.", file=sys.stderr)
            sys.exit(2)
        regressed = _regressions(results, baseline)
        passed_now = sum(1 for r in results if r["score"]["passed"])
        print(
            f"\nAgainst baseline: {passed_now}/{len(results)} passing "
            f"(baseline had {sum(1 for v in baseline.values() if v)}); {len(regressed)} suspected regression(s)",
            file=sys.stderr,
        )

        # These fixtures are scored by an LLM judge, which is not deterministic.
        # Four consecutive main commits produced pass counts of 32, 26, 28 and 27
        # with almost no overlap in *which* fixtures failed — one run even sat
        # above the baseline and still failed the gate because a single fixture
        # flipped. Blocking on a first failure therefore blocks on noise, which
        # made main permanently red and the signal worthless.
        #
        # So a suspected regression is re-run before it counts. A fixture that
        # passes on retry was noise; one that fails every attempt is a real
        # regression. Retries are per-fixture, so a clean run costs nothing.
        if regressed and args.confirm_retries > 0:
            regressed, flaky = _confirm_regressions(regressed, _run_one, args.concurrency, args.confirm_retries)
            if flaky:
                print(
                    f"FLAKY (failed once, passed on retry — not counted): {', '.join(flaky)}",
                    file=sys.stderr,
                )

        if regressed:
            print("REGRESSED: " + ", ".join(regressed), file=sys.stderr)
            sys.exit(1)
        return

    # No baseline given — every fixture must pass.
    if not all(r["score"]["passed"] for r in results):
        sys.exit(1)


def _load_baseline(path: str) -> dict[str, bool]:
    """Fixture name -> whether it passed, from a stored baseline run."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read baseline {path}: {exc}", file=sys.stderr)
        return {}
    if isinstance(data, dict) and "fixtures" in data:
        return {k: bool(v) for k, v in data["fixtures"].items()}
    if isinstance(data, list):
        return {r["fixture"]: bool(r.get("score", {}).get("passed")) for r in data if r.get("fixture")}
    return {}


def _regressions(results: list[dict], baseline: dict[str, bool]) -> list[str]:
    """Fixtures that passed in the baseline and do not now.

    Deliberately per-fixture rather than an aggregate count: a run that fixes one
    fixture and breaks another has the same total and is not the same thing.
    """
    out: list[str] = []
    for r in results:
        name = str(r.get("fixture") or "")
        if not name:
            continue
        if baseline.get(name) and not r.get("score", {}).get("passed"):
            out.append(name)
    return sorted(out)


def _write_baseline(results: list[dict], path: str) -> None:
    payload = {
        "generated_from": "replay_cli",
        "total": len(results),
        "passed": sum(1 for r in results if r.get("score", {}).get("passed")),
        "fixtures": {r["fixture"]: bool(r.get("score", {}).get("passed")) for r in results if r.get("fixture")},
    }
    Path(path).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Baseline written to {path}: {payload['passed']}/{payload['total']} passing", file=sys.stderr)


def _confirm_regressions(suspected: list[str], run_one, concurrency: int, retries: int) -> tuple[list[str], list[str]]:
    """Re-run suspected regressions; keep only those that fail every attempt.

    Returns (confirmed, flaky). A fixture is confirmed only if it fails the
    original run and all ``retries`` re-runs — with a non-deterministic judge,
    a single failure is as likely to be noise as signal.
    """
    from .replay_config import offline_context

    still_failing = list(suspected)
    for attempt in range(1, retries + 1):
        if not still_failing:
            break
        print(
            f"Re-running {len(still_failing)} suspected regression(s), attempt {attempt}/{retries}...",
            file=sys.stderr,
        )
        with offline_context(allow_llm_tool_picker=False):
            rerun = _execute(still_failing, run_one, concurrency)
        still_failing = [r["fixture"] for r in rerun if not r["score"]["passed"]]

    confirmed = [name for name in suspected if name in set(still_failing)]
    flaky = [name for name in suspected if name not in set(still_failing)]
    return confirmed, flaky


def _execute(fixtures: list[str], run_one, concurrency: int) -> list[dict]:
    """Run fixtures, in parallel when asked, preserving their declared order."""
    if concurrency > 1 and len(fixtures) > 1:
        # Each fixture runs its own asyncio.run, so threads rather than a shared
        # loop. The work is API-bound, so the GIL is not the constraint —
        # provider rate limits are, which is what bounds the worker count.
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            # map preserves input order regardless of completion order, so a run
            # stays comparable to the one before it.
            return list(pool.map(run_one, fixtures))
    return [run_one(name) for name in fixtures]


if __name__ == "__main__":
    main()
