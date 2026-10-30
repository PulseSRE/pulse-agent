"""Run the monitor-investigation replay lane.

    python -m sre_agent.evals.investigation_replay_cli --all --model claude-sonnet-5 --format json
    python -m sre_agent.evals.investigation_replay_cli --all --dry-run

Exits non-zero when any fixture fails, any attempt is unsafe, or confidence is
miscalibrated (see ``investigation_replay.verdict``). The JSON output is the
artifact CI keeps: one row per fixture with every attempt, plus the verdict.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from . import investigation_replay as ir


def _make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--all", action="store_true", help="Run every investigation fixture")
    target.add_argument("--fixture", help="Run one fixture by name")
    target.add_argument("--list", action="store_true", help="List fixtures")
    p.add_argument("--model", default="claude-sonnet-5")
    p.add_argument("--dry-run", action="store_true", help="Scripted model: checks the harness, not the agent")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument(
        "--retries",
        type=int,
        default=1,
        help="Re-run a failing fixture this many times; a pass on retry counts, an unsafe attempt never does",
    )
    p.add_argument("--format", choices=["json", "text"], default="text")
    return p


def _client_for(fixture: dict, dry_run: bool):
    if dry_run:
        return ir.ScriptedClient(fixture["recorded_responses"])
    from ..agent import create_async_client

    return create_async_client()


def _attempt(name: str, dry_run: bool) -> dict:
    fixture = ir.load_fixture(name)
    expected = ir.dry_run_expected(fixture["expected"]) if dry_run else fixture["expected"]

    async def _go() -> dict:
        client = _client_for(fixture, dry_run)
        try:
            return await ir.run_investigation(fixture, client)
        finally:
            try:
                await client.close()
            except Exception:
                pass

    try:
        outcome = asyncio.run(_go())
    except Exception as exc:  # an infrastructure failure, reported as one
        return {
            "fixture": name,
            "dry_run": dry_run,
            "error": f"{type(exc).__name__}: {exc}",
            "score": {"passed": False, "safe": True, "checks": []},
        }
    report = outcome["report"]
    return {
        "fixture": name,
        "dry_run": dry_run,
        "path": outcome["path"],
        "plan": outcome["plan"],
        "score": ir.score_investigation(outcome, expected),
        "write_attempts": outcome["write_attempts"],
        "agent_runs": outcome["agent_runs"],
        "tool_attempts": outcome["tool_attempts"],
        "report": {
            "suspected_cause": str(report.get("suspected_cause") or "")[:500],
            "summary": str(report.get("summary") or "")[:500],
            "confidence": report.get("confidence"),
            "evidence": (report.get("evidenceDetail") or [])[:10],
        },
    }


def _run_fixture(name: str, dry_run: bool, retries: int) -> dict:
    attempts = [_attempt(name, dry_run)]
    while not attempts[-1]["score"]["passed"] and len(attempts) <= retries:
        attempts.append(_attempt(name, dry_run))
    final = next((a for a in attempts if a["score"]["passed"]), attempts[-1])
    return {**final, "attempts": attempts}


def _format_text(rows: list[dict], gate: dict) -> str:
    lines = []
    for row in rows:
        mark = "PASS" if row["score"]["passed"] else "FAIL"
        tries = f" ({len(row['attempts'])} attempts)" if len(row["attempts"]) > 1 else ""
        conf = row["score"].get("confidence")
        lines.append(f"{mark}  {row['fixture']}  path={row.get('path', '?')} confidence={conf}{tries}")
        if "error" in row:
            lines.append(f"      error: {row['error']}")
        for check in row["score"]["checks"]:
            if not check["passed"]:
                lines.append(f"      x [{check['kind']}] {check['check']}")
    calib = gate.get("calibration")
    if calib:
        lines.append("")
        lines.append(
            f"Calibration: brier={calib['brier']} (max {calib['max_brier']}) over {calib['samples']} attempts; "
            f"mean confidence right={calib['mean_confidence_when_right']} wrong={calib['mean_confidence_when_wrong']}"
        )
        if calib["overconfident"]:
            lines.append(
                f"Overconfident wrong (>= {calib['overconfident_threshold']}): {', '.join(calib['overconfident'])}"
            )
    cites = gate.get("citations") or {}
    if cites.get("total"):
        lines.append(f"Evidence citing a call that returned data: {cites['cited']}/{cites['total']} ({cites['rate']})")
    lines.append("")
    passed = sum(1 for r in rows if r["score"]["passed"])
    lines.append(f"{passed}/{len(rows)} fixtures passed; unsafe: {', '.join(gate['unsafe']) or 'none'}")
    lines.append("GATE: " + ("PASS" if gate["passed"] else "FAIL"))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = _make_parser().parse_args(argv)
    if args.list:
        print("\n".join(ir.list_fixtures()))
        return 0
    if args.concurrency < 1 or args.retries < 0:
        print("--concurrency must be >= 1 and --retries >= 0", file=sys.stderr)
        return 2

    import os

    import sre_agent.config as _cfg

    # As in replay_cli: the loop's own prompt assembly would rebuild the prompt
    # from live cluster state, so it is off; the model is the one asked for.
    os.environ["PULSE_AGENT_HARNESS"] = "0"
    os.environ["PULSE_AGENT_MODEL"] = args.model
    _cfg._settings = None

    names = ir.list_fixtures() if args.all else [args.fixture]
    with ir.investigation_isolation():
        if args.concurrency > 1 and len(names) > 1:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                rows = list(pool.map(lambda n: _run_fixture(n, args.dry_run, args.retries), names))
        else:
            rows = [_run_fixture(n, args.dry_run, args.retries) for n in names]

    gate = ir.verdict(rows, dry_run=args.dry_run)
    if args.format == "json":
        print(json.dumps({"verdict": gate, "fixtures": rows}, indent=2, default=str))
    else:
        print(_format_text(rows, gate))
    return 0 if gate["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
