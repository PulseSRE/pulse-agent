"""Webhook escalation for critical findings."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging

from ..config import get_settings

logger = logging.getLogger("pulse_agent.monitor")


def _get_webhook_url() -> str:
    return get_settings().server.webhook_url


def _get_webhook_secret() -> str:
    return get_settings().server.webhook_secret


async def _send_webhook(finding: dict) -> None:
    """Send critical findings to a configured webhook URL for escalation."""
    webhook_url = _get_webhook_url()
    if not webhook_url:
        return
    try:
        import urllib.request

        payload = json.dumps(
            {
                "severity": finding.get("severity"),
                "title": finding.get("title"),
                "summary": finding.get("summary"),
                "resources": finding.get("resources", []),
                "timestamp": finding.get("timestamp"),
            }
        ).encode()
        headers: dict[str, str] = {"Content-Type": "application/json"}
        webhook_secret = _get_webhook_secret()
        if webhook_secret:
            sig = hmac.new(webhook_secret.encode(), payload, hashlib.sha256).hexdigest()
            headers["X-Pulse-Signature"] = f"sha256={sig}"
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers=headers,
        )
        await asyncio.to_thread(urllib.request.urlopen, req, timeout=5)
    except Exception as e:
        logger.error("Webhook delivery failed: %s", e)
