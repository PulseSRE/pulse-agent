"""Reaching a person who is not looking at Pulse.

Everything the product knows is worth nothing at 03:00 if the only way to
learn it is to open a tab. This is the one path out.

What gets sent matters as much as that it is sent. Notifying per critical
finding is how a monitoring system teaches people to filter it: on the
reference cluster that would have been 33 messages for one control-plane
problem. So the events here are the two that mean something to a human —
an episode opening, which is one event with a cause, and a fix proposed,
which is something waiting on *them*. Findings that are symptoms of an open
episode are deliberately silent: their episode already spoke for them.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from typing import Any

from ..config import get_settings

logger = logging.getLogger("pulse_agent.monitor")


def _get_webhook_url() -> str:
    return get_settings().server.webhook_url


def _get_webhook_secret() -> str:
    return get_settings().server.webhook_secret


async def _post(event: str, body: dict[str, Any]) -> None:
    """Deliver one event. Never raises — a missed notification is not an outage."""
    webhook_url = _get_webhook_url()
    if not webhook_url:
        return
    try:
        import urllib.request

        payload = json.dumps({"event": event, **body}).encode()
        headers: dict[str, str] = {"Content-Type": "application/json"}
        webhook_secret = _get_webhook_secret()
        if webhook_secret:
            sig = hmac.new(webhook_secret.encode(), payload, hashlib.sha256).hexdigest()
            headers["X-Pulse-Signature"] = f"sha256={sig}"
        req = urllib.request.Request(webhook_url, data=payload, headers=headers)
        await asyncio.to_thread(urllib.request.urlopen, req, timeout=5)
    except Exception as e:
        logger.error("Webhook delivery failed (%s): %s", event, e)


async def _send_webhook(finding: dict) -> None:
    """Send a critical finding for escalation.

    Silent for anything an open episode already explains. One control-plane
    problem produced nine findings on the reference cluster; nine messages
    describing one event is how people learn to mute the channel.
    """
    try:
        from ..inbox import _finding_corr_key
        from .episodes import symptom_keys_by_episode

        key = _finding_corr_key(finding)
        if key and key in symptom_keys_by_episode():
            return
    except Exception:
        # Never let the suppression check stop the notification. Sending twice
        # is a nuisance; sending nothing is the failure this module exists to
        # prevent.
        logger.debug("Could not check episode membership for webhook", exc_info=True)

    await _post(
        "finding",
        {
            "severity": finding.get("severity"),
            "title": finding.get("title"),
            "summary": finding.get("summary"),
            "resources": finding.get("resources", []),
            "timestamp": finding.get("timestamp"),
        },
    )


async def notify_episode_opened(episode_id: str, finding: dict) -> None:
    """Announce a new episode — one event with a cause, rather than N findings."""
    await _post(
        "episode_opened",
        {
            "episodeId": episode_id,
            "severity": finding.get("severity"),
            "cause": finding.get("title"),
            "summary": finding.get("summary"),
            "resources": finding.get("resources", []),
            "startedAt": finding.get("startedAt"),
        },
    )


async def notify_fix_proposed(action: dict, finding: dict) -> None:
    """Announce a fix nobody was connected to approve.

    The one notification that asks for something back. It carries the action id
    because the answer is a single call to
    ``POST /fix-history/{id}/approve`` — a message that reports a problem
    without saying what to do about it is only half a notification.
    """
    await _post(
        "fix_proposed",
        {
            "actionId": action.get("id"),
            "findingId": action.get("findingId"),
            "title": finding.get("title"),
            "proposal": action.get("reasoning"),
            "resources": finding.get("resources", []),
            "approveWith": f"POST /fix-history/{action.get('id')}/approve",
        },
    )
