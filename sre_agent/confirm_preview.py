"""Server-built previews attached to a ``confirm_request``.

The confirmation a person approves used to carry only the model's tool input.
For some writes that is not enough to decide on: a silence's input does not
say who it will be created as, when it ends, or which firing alerts it hides.
A preview is computed here, as the caller (their forwarded token), from the
same code the tool runs, and sent alongside the input. It is advisory: the
tool re-validates everything when it runs, and a preview that cannot be built
is simply left out.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("pulse_agent.confirm_preview")

PREVIEW_TIMEOUT_SECONDS = 8.0


def _builders() -> dict[str, Callable[[dict], dict[str, Any]]]:
    from .rbac_writes import PREVIEW_BUILDERS as ACCESS_PREVIEWS
    from .silences import preview_create, preview_expire

    return {"create_silence": preview_create, "expire_silence": preview_expire, **ACCESS_PREVIEWS}


def has_preview(tool_name: str) -> bool:
    return tool_name in _builders()


def build_preview(tool_name: str, tool_input: dict, user_token: str | None = None) -> dict[str, Any] | None:
    builder = _builders().get(tool_name)
    if builder is None:
        return None
    from .k8s_client import user_token_context

    try:
        with user_token_context(user_token):
            return builder(dict(tool_input or {}))
    except Exception:
        logger.warning("Confirm preview for %s failed", tool_name, exc_info=True)
        return None


async def build_preview_async(tool_name: str, tool_input: dict, user_token: str | None = None) -> dict[str, Any] | None:
    if not has_preview(tool_name):
        return None
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(build_preview, tool_name, tool_input, user_token), timeout=PREVIEW_TIMEOUT_SECONDS
        )
    except TimeoutError:
        logger.warning("Confirm preview for %s timed out", tool_name)
        return None
