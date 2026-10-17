"""Client for the render service (pulse-ui render/), which screenshots a draft dashboard.

The service loads the real UI with the requesting user's token, so the
screenshot shows what that user will see, with their permissions. It is
deployed by pulse-operator only when enabled; with no ``render_url``
configured, previews are unavailable and the tool says so.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .config import get_settings

RENDER_TIMEOUT_S = 60


class RenderUnavailableError(RuntimeError):
    """The render service is not configured, not reachable, or refused the request."""


def configured() -> bool:
    agent = get_settings().agent
    return bool(agent.render_url and agent.render_token)


def render_draft(spec: dict[str, Any], user_token: str, width: int = 1440) -> dict[str, Any]:
    """Render ``spec`` ({title, description, layout, positions}) as ``user_token`` would see it."""
    agent = get_settings().agent
    if not (agent.render_url and agent.render_token):
        raise RenderUnavailableError("no render service configured")
    body = json.dumps({"spec": spec, "userToken": user_token, "width": width}).encode()
    req = urllib.request.Request(
        f"{agent.render_url.rstrip('/')}/render",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {agent.render_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=RENDER_TIMEOUT_S) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b"{}").get("error", "")
        except Exception:
            detail = ""
        raise RenderUnavailableError(f"render service answered {e.code}{': ' + detail if detail else ''}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RenderUnavailableError(f"render service unreachable ({type(e).__name__})") from e
