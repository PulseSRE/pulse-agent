"""Tool results that carry an image for the model to look at.

Tool results are text. ``preview_dashboard`` needs to hand the model a
screenshot of the page it is about to save, so a tool may attach one image to
its text with ``attach_image``. The agent loop splits it off before the text
is truncated (a cut base64 string is a broken image) and sends it as an image
block next to the text.

Screenshots are large in tokens, and once the model has acted on one it is
only history: ``drop_stale_images`` replaces every image but the newest with a
short note before each model call.
"""

from __future__ import annotations

from typing import Any

# Not valid in tool text, so a result cannot be mistaken for an image by accident.
_MARK = "\x00pulse-image\x00"
_MEDIA_TYPES = {"image/png", "image/jpeg", "image/webp"}
STALE_NOTE = "[An earlier screenshot was removed to save context.]"


def attach_image(text: str, data_b64: str, media_type: str = "image/png") -> str:
    """Return ``text`` with an image attached for the model."""
    if media_type not in _MEDIA_TYPES:
        raise ValueError(f"unsupported image type {media_type}")
    return f"{_MARK}{media_type};{data_b64}{_MARK}{text}"


def split_image(text: Any) -> tuple[dict | None, Any]:
    """``(image source, text)`` if ``text`` carries an attached image, else ``(None, text)``."""
    if not isinstance(text, str) or not text.startswith(_MARK):
        return None, text
    end = text.find(_MARK, len(_MARK))
    if end < 0:
        return None, text
    header = text[len(_MARK) : end]
    media_type, _, data = header.partition(";")
    if media_type not in _MEDIA_TYPES or not data:
        return None, text[end + len(_MARK) :]
    return {"type": "base64", "media_type": media_type, "data": data}, text[end + len(_MARK) :]


def result_content(text: Any) -> Any:
    """Tool-result ``content`` for the API: an image block plus text, or the text unchanged."""
    image, rest = split_image(text)
    if image is None:
        return rest
    return [{"type": "image", "source": image}, {"type": "text", "text": rest or "(screenshot attached)"}]


def drop_stale_images(messages: list[dict]) -> int:
    """Replace every image in tool results except the newest with a note. Returns how many were dropped.

    Mutates ``messages`` in place, as the agent loop expects.
    """
    found: list[tuple[list, int]] = []
    for message in messages:
        if message.get("role") != "user" or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                continue
            inner = block.get("content")
            if isinstance(inner, list):
                found.extend(
                    (inner, i) for i, b in enumerate(inner) if isinstance(b, dict) and b.get("type") == "image"
                )
    stale = found[:-1]
    for inner, i in stale:
        inner[i] = {"type": "text", "text": STALE_NOTE}
    return len(stale)
