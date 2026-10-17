"""A stand-in Messages API that plays a fixed dashboard-building script.

The rendered-page eval is meant to run the live model. This exists so the
rest of the pipeline (agent tools against the mock cluster, view saving,
rendering, deterministic checks) can be exercised without model credentials:
locally, and in CI when the Vertex secrets are absent. Its scores say nothing
about the model; the report marks them as scripted.

Point the agent at it with ``ANTHROPIC_BASE_URL=http://127.0.0.1:<port>``.
Each agent turn gets the next step of the script, skipping tools the request
did not offer, and finishes with a short text reply. Requests without tools
(routing helpers, summaries) get a short text answer.

Run: ``python -m sre_agent.evals.mock_cluster.scripted_model --port 18003``
"""

from __future__ import annotations

import argparse
import json
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

SCRIPT: list[list[tuple[str, dict[str, Any]]]] = [
    [("namespace_summary", {"namespace": "production"}), ("cluster_metrics", {})],
    [
        (
            "get_prometheus_query",
            {
                "query": 'topk(5, sum by (pod) (container_memory_working_set_bytes{namespace="production"}))',
                "title": "Memory by pod (production)",
            },
        ),
        ("get_firing_alerts", {}),
        ("get_events", {"namespace": "production"}),
    ],
    [("create_dashboard", {"title": "Production overview", "description": "Health, memory and alerts for production"})],
]

FINAL_TEXT = "Built the Production overview dashboard."


def _assistant_turns(messages: list[dict]) -> int:
    return sum(1 for m in messages if m.get("role") == "assistant")


def _plan(body: dict) -> tuple[list[dict], str]:
    """Content blocks for this request and the stop reason."""
    offered = {t.get("name") for t in body.get("tools") or []}
    if "create_dashboard" not in offered:
        return [{"type": "text", "text": FINAL_TEXT if offered else "ok"}], "end_turn"
    step = _assistant_turns(body.get("messages") or [])
    while step < len(SCRIPT):
        calls = [(n, a) for n, a in SCRIPT[step] if n in offered]
        if calls:
            blocks = [
                {"type": "tool_use", "id": f"toolu_{uuid.uuid4().hex[:20]}", "name": n, "input": a} for n, a in calls
            ]
            return blocks, "tool_use"
        step += 1
    return [{"type": "text", "text": FINAL_TEXT}], "end_turn"


def _message(body: dict, blocks: list[dict], stop: str) -> dict:
    return {
        "id": f"msg_{uuid.uuid4().hex[:20]}",
        "type": "message",
        "role": "assistant",
        "model": body.get("model", "scripted"),
        "content": blocks,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 10,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _stream(body: dict, blocks: list[dict], stop: str):
    start = _message(body, [], "")
    start["stop_reason"] = None
    yield _sse("message_start", {"type": "message_start", "message": start})
    for i, block in enumerate(blocks):
        if block["type"] == "text":
            yield _sse(
                "content_block_start",
                {"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}},
            )
            yield _sse(
                "content_block_delta",
                {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": block["text"]}},
            )
        else:
            yield _sse(
                "content_block_start",
                {"type": "content_block_start", "index": i, "content_block": {**block, "input": {}}},
            )
            yield _sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": i,
                    "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])},
                },
            )
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": i})
    yield _sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": {
                "output_tokens": 10,
                "input_tokens": 10,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
    )
    yield _sse("message_stop", {"type": "message_stop"})


def build_app() -> FastAPI:
    app = FastAPI()

    @app.post("/v1/messages")
    async def messages(request: Request):
        body = await request.json()
        blocks, stop = _plan(body)
        if body.get("stream"):
            return StreamingResponse(_stream(body, blocks, stop), media_type="text/event-stream")
        return JSONResponse(_message(body, blocks, stop))

    @app.post("/v1/messages/count_tokens")
    async def count_tokens(request: Request):
        return {"input_tokens": 100}

    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=18003)
    args = parser.parse_args()
    uvicorn.run(build_app(), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
