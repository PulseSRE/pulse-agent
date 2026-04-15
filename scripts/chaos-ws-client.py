#!/usr/bin/env python3
"""Lightweight WebSocket client for chaos testing.

Connects to /ws/monitor, subscribes, and writes received findings/events
to a JSONL file that chaos-test.sh reads for detection scoring.

Usage:
    python3 scripts/chaos-ws-client.py \
        --url ws://agent-svc:8080/ws/monitor \
        --token <ws-token> \
        --output /tmp/chaos-findings.jsonl \
        --trust-level 3 \
        --auto-fix-categories crashloop,image_pull

The process runs until killed (SIGTERM/SIGINT). chaos-test.sh starts it
in the background and kills it on exit.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [chaos-ws] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("chaos-ws")


async def run(
    url: str,
    token: str,
    output: str,
    trust_level: int,
    auto_fix_categories: list[str],
) -> None:
    try:
        import websockets
    except ImportError:
        log.error("websockets package not installed — pip install websockets")
        sys.exit(1)

    ws_url = f"{url}?token={token}"
    log.info("Connecting to %s", url)

    try:
        async with websockets.connect(ws_url, open_timeout=15) as ws:
            # Send subscribe_monitor
            subscribe = {
                "type": "subscribe_monitor",
                "trustLevel": trust_level,
                "autoFixCategories": auto_fix_categories,
            }
            await ws.send(json.dumps(subscribe))
            log.info(
                "Subscribed: trust=%d categories=%s",
                trust_level,
                auto_fix_categories,
            )

            # Open output file in append mode — each message is one JSON line
            with open(output, "a") as f:
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    msg_type = msg.get("type", "")
                    f.write(json.dumps(msg) + "\n")
                    f.flush()

                    # Log interesting events
                    if msg_type == "finding":
                        log.info(
                            "FINDING: %s [%s] severity=%s",
                            msg.get("title", "?"),
                            msg.get("category", "?"),
                            msg.get("severity", "?"),
                        )
                    elif msg_type == "action_report":
                        log.info(
                            "ACTION: %s status=%s",
                            msg.get("tool", "?"),
                            msg.get("status", "?"),
                        )
                    elif msg_type == "resolution":
                        log.info(
                            "RESOLVED: %s method=%s",
                            msg.get("title", "?"),
                            msg.get("method", "?"),
                        )
                    elif msg_type == "monitor_status":
                        log.info(
                            "SCAN: findings=%d cycle=%s",
                            msg.get("activeFindings", 0),
                            msg.get("scanCycle", "?"),
                        )

    except Exception as e:
        log.error("WebSocket error: %s", e)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Chaos test WebSocket client")
    parser.add_argument("--url", required=True, help="ws://host:port/ws/monitor")
    parser.add_argument("--token", required=True, help="PULSE_AGENT_WS_TOKEN")
    parser.add_argument(
        "--output",
        default="/tmp/chaos-findings.jsonl",
        help="Output JSONL file (default: /tmp/chaos-findings.jsonl)",
    )
    parser.add_argument(
        "--trust-level",
        type=int,
        default=3,
        help="Trust level for auto-fix (default: 3)",
    )
    parser.add_argument(
        "--auto-fix-categories",
        default="crashloop,image_pull",
        help="Comma-separated auto-fix categories",
    )
    args = parser.parse_args()

    categories = [c.strip() for c in args.auto_fix_categories.split(",") if c.strip()]

    # Handle signals for clean shutdown
    loop = asyncio.new_event_loop()

    def _shutdown(sig: int, _frame: object) -> None:
        log.info("Received signal %d, shutting down", sig)
        loop.stop()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        loop.run_until_complete(
            run(args.url, args.token, args.output, args.trust_level, categories)
        )
    except RuntimeError:
        pass  # loop.stop() from signal handler


if __name__ == "__main__":
    main()
