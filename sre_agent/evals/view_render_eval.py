"""Rendered-page eval for the view designer: does the dashboard it builds look right?

Every other view-designer eval scores tool calls or the saved spec. None of
them sees the page, and the failures that reached users were visible ones:
blank charts, a crashed node map, half-empty rows, widgets with no data.
This eval looks at the page:

1. A real dashboard request goes to the agent's ``/ws/agent`` API. The agent's
   real tools read a mock cluster (``mock_cluster.server``) with a coherent
   incident, so widgets carry real data and the numbers agree across widgets.
2. The saved view is opened in pulse-ui (pointed at the same mock) and
   screenshotted, with deterministic checks per grid cell: crashed widgets,
   empty or failed queries, page errors, cells whose content overflows.
3. A vision judge scores the screenshot against the request.

Nightly and non-gating: the output is a report and screenshots to read, not a
pass/fail. Run with ``--scripted`` (agent pointed at ``mock_cluster.scripted_model``)
to exercise the pipeline without model credentials; scores then say nothing
about the model and the report says so.

Example::

    python -m sre_agent.evals.view_render_eval \\
        --agent-url ws://127.0.0.1:8080 --ui-url http://127.0.0.1:9000 \\
        --out artifacts/view-render
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

PROMPTS: list[dict[str, Any]] = [
    {
        "id": "namespace_overview",
        "prompt": "Build me a dashboard for the production namespace: health, resource usage and anything that needs attention.",
    },
    {
        "id": "node_capacity",
        "prompt": "Create a node capacity dashboard showing CPU and memory per node and which nodes are under pressure.",
    },
    {
        "id": "top_consumers",
        "prompt": "Make a dashboard of the pods using the most memory and CPU in production, with trends over the last hour.",
    },
    {
        "id": "incident_board",
        "prompt": "Build an incident dashboard for production: firing alerts, recent warning events, and the crashlooping pod's logs.",
    },
    {
        "id": "workload_status",
        "prompt": "Create a dashboard of deployment status in production: ready vs desired replicas, restarts, and pod health.",
    },
]

# Text a widget shows when it has nothing to show or broke. Matched per grid cell.
EMPTY_PATTERNS = re.compile(r"No data|No resources found|No results", re.I)
BROKEN_PATTERNS = re.compile(r"failed to render|Query failed|Something went wrong", re.I)

JUDGE_RUBRIC = """You are reviewing a dashboard an AI agent built for an SRE in response to a request.
Judge only what the screenshot shows. Score each dimension 1-5 (5 = excellent):

- answers_request: does the page show what was asked for, and nothing irrelevant dominating it?
- widget_fit: is each widget the right kind for its data (trends as line charts, rankings as bars or tables, a few headline numbers as stat cards, lists as tables)?
- readability: can a person read it at a glance — clear titles, sensible units, labels not truncated or overlapping?
- layout: is space used well — no large empty areas, no half-empty rows, related widgets near each other, the most important information first?
- integrity: are all widgets rendered with data (no blank charts, error boxes, empty tables, cut-off content)?

Reply with JSON only:
{"answers_request": n, "widget_fit": n, "readability": n, "layout": n, "integrity": n,
 "issues": ["short, specific problems you can see"], "summary": "one sentence"}"""

JUDGE_DIMENSIONS = ("answers_request", "widget_fit", "readability", "layout", "integrity")


@dataclass
class CaseResult:
    id: str
    prompt: str
    view_id: str = ""
    title: str = ""
    kinds: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    agent_seconds: float = 0.0
    agent_error: str = ""
    screenshot: str = ""
    cells: int = 0
    broken_cells: list[str] = field(default_factory=list)
    empty_cells: list[str] = field(default_factory=list)
    overflowing_cells: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    render_error: str = ""
    judge: dict[str, Any] = field(default_factory=dict)

    @property
    def judge_score(self) -> float | None:
        values = [self.judge[d] for d in JUDGE_DIMENSIONS if isinstance(self.judge.get(d), (int, float))]
        return round(sum(values) / len(values), 2) if values else None


# ---------------------------------------------------------------------------
# 1. Ask the agent
# ---------------------------------------------------------------------------


def _kinds(layout: list[dict]) -> list[str]:
    out: list[str] = []
    for comp in layout or []:
        kind = comp.get("kind", "?")
        if kind in ("tabs",):
            for tab in comp.get("tabs", []):
                out.extend(_kinds(tab.get("components", [])))
        elif kind in ("grid", "section") and comp.get("items"):
            out.append(kind)
        else:
            out.append(kind)
    return out


async def ask_agent(agent_url: str, token: str, user: str, prompt: str, timeout: float) -> dict[str, Any]:
    """Send one request; return the last saved view spec, tools used and any error."""
    import websockets

    url = f"{agent_url.rstrip('/')}/ws/agent?token={token}"
    headers = {"X-Forwarded-Access-Token": user}
    result: dict[str, Any] = {"spec": None, "tools": [], "error": "", "text": ""}
    started = time.monotonic()
    try:
        async with websockets.connect(url, additional_headers=headers, max_size=2**24, open_timeout=30) as ws:
            await ws.send(json.dumps({"type": "message", "content": prompt}))
            while True:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    result["error"] = f"timed out after {timeout:.0f}s"
                    break
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                event = json.loads(raw)
                kind = event.get("type")
                if kind == "view_spec":
                    result["spec"] = event.get("spec")
                elif kind == "tool_use":
                    result["tools"].append(event.get("tool") or event.get("name", ""))
                elif kind == "confirm_request":
                    # A dashboard request should never need a write. Refuse it and record that it asked.
                    result["tools"].append(f"CONFIRM:{event.get('tool', '')}")
                    await ws.send(
                        json.dumps({"type": "confirm_response", "approved": False, "nonce": event.get("nonce", "")})
                    )
                elif kind == "error":
                    # The turn is over: the agent does not send "done" after an error.
                    result["error"] = str(event.get("message", "error"))
                    break
                elif kind == "done":
                    result["text"] = event.get("full_response", "")
                    break
    except TimeoutError:
        result["error"] = f"timed out after {timeout:.0f}s"
    except Exception as e:  # connection refused, auth rejected, protocol error
        result["error"] = f"{type(e).__name__}: {e}"
    result["seconds"] = round(time.monotonic() - started, 1)
    return result


# ---------------------------------------------------------------------------
# 2. Render it
# ---------------------------------------------------------------------------

_CELL_PROBE = """
() => [...document.querySelectorAll('.react-grid-item')].map((cell) => {
  const title = (cell.querySelector('h1,h2,h3,h4,[class*="title"]')?.textContent || '').trim().slice(0, 80);
  const scrollers = [cell, ...cell.querySelectorAll('*')].filter((el) => {
    const cs = getComputedStyle(el);
    return /(auto|scroll)/.test(cs.overflowY) && el.scrollHeight > el.clientHeight + 4;
  });
  const clipped = [...cell.querySelectorAll('*')].some((el) => {
    const r = el.getBoundingClientRect(), c = cell.getBoundingClientRect();
    return r.height > 0 && r.bottom > c.bottom + 4 && getComputedStyle(el).visibility !== 'hidden';
  });
  return { title, text: cell.innerText.slice(0, 2000), scrolls: scrollers.length > 0, clipped };
})
"""


# A first-time visitor's chrome: the guided tour, an open AI sidebar and toasts
# for the mock cluster's findings would cover the page being judged.
_FIRST_VISIT_SETUP = """
try {
  localStorage.setItem('openshiftpulse-tour-completed', 'true');
  localStorage.setItem('openshiftpulse-ui-storage', JSON.stringify({state: {aiSidebarExpanded: false}, version: 0}));
} catch (e) {}
"""
_HIDE_OVERLAYS = '[aria-label="Notifications"] { display: none !important; }'

# The view scrolls inside its own container, so a full-page screenshot stops at
# the viewport. Mark that container and report how tall its content is.
_MEASURE_VIEW = """
() => {
  const grid = document.querySelector('.react-grid-layout');
  if (!grid) return null;
  let el = grid.parentElement;
  while (el && !/(auto|scroll)/.test(getComputedStyle(el).overflowY)) el = el.parentElement;
  el = el || grid;
  el.setAttribute('data-eval-view', '');
  return { top: el.getBoundingClientRect().top, height: el.scrollHeight };
}
"""


async def render_view(
    ui_url: str, view_id: str, out_path: Path, executable: str = "", width: int = 1440, height: int = 1000
) -> dict[str, Any]:
    """Screenshot ``/custom/{id}`` and probe each grid cell."""
    from playwright.async_api import async_playwright

    info: dict[str, Any] = {"cells": [], "page_errors": [], "error": ""}
    async with async_playwright() as p:
        launch: dict[str, Any] = {"args": ["--no-sandbox"]}
        if executable:
            launch["executable_path"] = executable
        browser = await p.chromium.launch(**launch)
        try:
            page = await browser.new_page(viewport={"width": width, "height": height})
            await page.add_init_script(_FIRST_VISIT_SETUP)
            page.on("pageerror", lambda e: info["page_errors"].append(str(e)[:300]))
            await page.goto(f"{ui_url.rstrip('/')}/custom/{view_id}", wait_until="domcontentloaded", timeout=60_000)
            try:
                await page.wait_for_selector(".react-grid-item", timeout=30_000)
            except Exception:
                info["error"] = "no grid cells rendered"
            try:
                await page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:
                pass  # live widgets poll; settle on the fixed wait below
            # Widgets mount, fetch, then draw; an error boundary swaps in a beat later.
            await page.wait_for_timeout(3000)
            if await page.get_by_text("View not found").count():
                info["error"] = "View not found"
            await page.add_style_tag(content=_HIDE_OVERLAYS)
            box = await page.evaluate(_MEASURE_VIEW)
            if box:
                # Grow the window to the view's full height (width unchanged, so the
                # grid keeps its layout), let charts redraw, then shoot the view alone.
                full = min(int(box["top"] + box["height"]) + 24, 8000)
                if full > height:
                    await page.set_viewport_size({"width": width, "height": full})
                    await page.wait_for_timeout(1500)
            info["cells"] = await page.evaluate(_CELL_PROBE)
            target = page.locator("[data-eval-view]")
            if box and await target.count():
                await target.screenshot(path=str(out_path))
            else:
                await page.screenshot(path=str(out_path))
        finally:
            await browser.close()
    info["page_errors"] = [m for m in info["page_errors"] if not re.search(r"WebSocket|Failed to fetch|discover", m)]
    return info


# ---------------------------------------------------------------------------
# 3. Judge it
# ---------------------------------------------------------------------------


def _parse_json(text: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return {"error": f"judge returned no JSON: {text[:200]}"}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as e:
        return {"error": f"judge JSON invalid: {e}"}


def judge_screenshot(image: Path, prompt: str, kinds: list[str], model: str, samples: int = 1) -> dict[str, Any]:
    """Vision-judge the page; with several samples, take each dimension's median."""
    from ..agent import create_client

    client = create_client()
    data = base64.standard_b64encode(image.read_bytes()).decode()
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}},
        {
            "type": "text",
            "text": f"Request: {prompt}\nWidget kinds on the page: {', '.join(kinds) or 'none'}\n\n{JUDGE_RUBRIC}",
        },
    ]
    runs = []
    for _ in range(max(1, samples)):
        resp = client.messages.create(model=model, max_tokens=800, messages=[{"role": "user", "content": content}])
        runs.append(_parse_json("".join(b.text for b in resp.content if getattr(b, "type", "") == "text")))
    good = [r for r in runs if "error" not in r]
    if not good:
        return runs[0]
    merged: dict[str, Any] = {}
    for dim in JUDGE_DIMENSIONS:
        values = [r[dim] for r in good if isinstance(r.get(dim), (int, float))]
        if values:
            merged[dim] = statistics.median(values)
    merged["issues"] = good[0].get("issues", [])
    merged["summary"] = good[0].get("summary", "")
    return merged


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _check_cells(result: CaseResult, cells: list[dict]) -> None:
    result.cells = len(cells)
    for i, cell in enumerate(cells):
        label = cell.get("title") or f"cell {i + 1}"
        text = cell.get("text", "")
        if BROKEN_PATTERNS.search(text):
            result.broken_cells.append(label)
        elif EMPTY_PATTERNS.search(text) or not text.strip():
            result.empty_cells.append(label)
        if cell.get("clipped") and not cell.get("scrolls"):
            result.overflowing_cells.append(label)


def render_markdown(results: list[CaseResult], meta: dict[str, Any]) -> str:
    lines = ["### View designer — rendered pages", ""]
    if meta.get("scripted"):
        lines += ["> Scripted model: this run checks the pipeline, not the model. Scores are not meaningful.", ""]
    lines += [
        f"Model `{meta.get('model', '?')}` · {len(results)} requests · judge `{meta.get('judge_model') or 'off'}`",
        "",
        "| Request | Widgets | Broken | Empty | Overflow | Page errors | Judge (1-5) | Notes |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        note = r.agent_error or r.render_error or r.judge.get("summary", "") or r.judge.get("error", "")
        score = r.judge_score if r.judge_score is not None else "—"
        lines.append(
            f"| {r.id} | {r.cells} ({', '.join(sorted(set(r.kinds)))}) | {len(r.broken_cells)} | "
            f"{len(r.empty_cells)} | {len(r.overflowing_cells)} | {len(r.page_errors)} | {score} | "
            f"{note.replace('|', '/')[:140]} |"
        )
    scores = [r.judge_score for r in results if r.judge_score is not None]
    if scores:
        lines += ["", f"Mean judge score: **{statistics.mean(scores):.2f}** / 5"]
    issues = [(r.id, i) for r in results for i in r.judge.get("issues", [])]
    if issues:
        lines += ["", "<details><summary>Judge issues</summary>", ""]
        lines += [f"- **{cid}**: {issue}" for cid, issue in issues]
        lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


async def run(args: argparse.Namespace) -> list[CaseResult]:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cases = [c for c in PROMPTS if not args.only or c["id"] in args.only]
    results: list[CaseResult] = []
    for case in cases:
        result = CaseResult(id=case["id"], prompt=case["prompt"])
        print(f"[{case['id']}] asking the agent…", file=sys.stderr)
        reply = await ask_agent(args.agent_url, args.token, args.user, case["prompt"], args.timeout)
        result.agent_seconds = reply.get("seconds", 0.0)
        result.tools = [t for t in reply["tools"] if t]
        spec = reply.get("spec")
        if not spec:
            result.agent_error = reply["error"] or "no dashboard was saved"
            results.append(result)
            continue
        result.agent_error = reply["error"]
        result.view_id = spec.get("id", "")
        result.title = spec.get("title", "")
        result.kinds = _kinds(spec.get("layout") or [])

        shot = out / f"{case['id']}.png"
        print(f"[{case['id']}] rendering {result.view_id}…", file=sys.stderr)
        try:
            info = await render_view(args.ui_url, result.view_id, shot, args.chromium)
        except Exception as e:
            result.render_error = f"{type(e).__name__}: {e}"
            results.append(result)
            continue
        result.screenshot = shot.name
        result.render_error = info["error"]
        result.page_errors = info["page_errors"]
        _check_cells(result, info["cells"])

        if args.judge_model and not result.render_error:
            print(f"[{case['id']}] judging…", file=sys.stderr)
            try:
                result.judge = judge_screenshot(
                    shot, case["prompt"], result.kinds, args.judge_model, args.judge_samples
                )
            except Exception as e:
                result.judge = {"error": f"{type(e).__name__}: {e}"}
        results.append(result)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agent-url", default="ws://127.0.0.1:8080")
    parser.add_argument("--ui-url", default="http://127.0.0.1:9000")
    parser.add_argument("--token", default=os.environ.get("PULSE_AGENT_WS_TOKEN", ""))
    parser.add_argument(
        "--user",
        default="e2e-test-user",
        help="X-Forwarded-Access-Token sent with requests; must match the UI's so it can open the views",
    )
    parser.add_argument("--out", default="artifacts/view-render")
    parser.add_argument("--only", nargs="*", default=[], help="request ids to run (default: all)")
    parser.add_argument("--timeout", type=float, default=300.0, help="seconds per request")
    parser.add_argument(
        "--chromium",
        default=os.environ.get("PULSE_EVAL_CHROMIUM", ""),
        help="Chromium executable (default: Playwright's own)",
    )
    parser.add_argument("--judge-model", default="", help="vision judge model; empty disables judging")
    parser.add_argument("--judge-samples", type=int, default=1)
    parser.add_argument("--model", default=os.environ.get("PULSE_AGENT_MODEL", ""), help="recorded in the report")
    parser.add_argument("--scripted", action="store_true", help="the agent is on the scripted model; mark the report")
    args = parser.parse_args(argv)

    results = asyncio.run(run(args))
    meta = {
        "model": "scripted" if args.scripted else args.model,
        "judge_model": args.judge_model,
        "scripted": args.scripted,
        "ran_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    out = Path(args.out)
    report = {"meta": meta, "results": [{**asdict(r), "judge_score": r.judge_score} for r in results]}
    (out / "report.json").write_text(json.dumps(report, indent=2))
    markdown = render_markdown(results, meta)
    (out / "report.md").write_text(markdown)
    print(markdown)
    # Non-gating by design. Exit non-zero only when nothing could be evaluated,
    # which means the harness itself is broken rather than the pages being poor.
    return 0 if any(r.screenshot for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
