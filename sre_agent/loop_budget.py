"""Bounds for the inner agent loop, and honesty about hitting them.

The loop stopped at ``MAX_ITERATIONS = 25`` with a log line:

    logger.warning("Agent hit max iteration limit (%d)", MAX_ITERATIONS)

and then returned whatever text had accumulated. Nobody outside the log ever
learned the investigation was cut short. An operator reading a partial diagnosis
had no way to tell it apart from a complete one — which is the worst failure mode
available to a diagnostic tool, because a truncated answer still reads as an
answer.

This module adds three things:

    a budget       iterations, tokens and wall-clock, not just a turn count
    a warning      the model is told to conclude before it is cut off
    a notice       the reader is told when a limit was reached, and which

Long tool results are compacted rather than the run being abandoned, and
compaction says what it dropped instead of quietly shrinking the context.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger("pulse_agent.loop_budget")

# Leave room to actually write a conclusion after the warning fires. Warning at
# the very last iteration would be pointless — there would be no turn left to use.
WRAP_UP_FRACTION = 0.8

# Tool results older than this many turns are the ones worth compacting: the
# agent has already read them and extracted what it needed.
KEEP_RAW_RECENT_TURNS = 6

# Below this a tool result is not worth compacting — the summary would cost as
# much as the payload.
MIN_COMPACTABLE_CHARS = 2000


@dataclass
class LoopBudget:
    """What one agent turn is allowed to spend, and what it has spent."""

    max_iterations: int = 25
    max_input_tokens: int = 0  # 0 = no token ceiling
    max_seconds: float = 0.0  # 0 = no wall-clock ceiling

    iterations: int = 0
    input_tokens: int = 0
    started_at: float = field(default_factory=time.monotonic)
    _warned: bool = False

    def record_iteration(self, input_tokens: int = 0) -> None:
        self.iterations += 1
        self.input_tokens += max(0, int(input_tokens or 0))

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def exhausted(self) -> str:
        """Which limit was reached, or empty string if none was."""
        if self.iterations >= self.max_iterations:
            return f"iteration limit ({self.max_iterations})"
        if self.max_input_tokens and self.input_tokens >= self.max_input_tokens:
            return f"token budget ({self.max_input_tokens:,} input tokens)"
        if self.max_seconds and self.elapsed >= self.max_seconds:
            return f"time budget ({self.max_seconds:.0f}s)"
        return ""

    def should_warn(self) -> bool:
        """Whether to tell the model to start concluding. Fires once."""
        if self._warned:
            return False
        near_iterations = self.iterations >= max(1, int(self.max_iterations * WRAP_UP_FRACTION))
        near_tokens = bool(self.max_input_tokens) and self.input_tokens >= self.max_input_tokens * WRAP_UP_FRACTION
        near_time = bool(self.max_seconds) and self.elapsed >= self.max_seconds * WRAP_UP_FRACTION
        if near_iterations or near_tokens or near_time:
            self._warned = True
            return True
        return False

    def wrap_up_notice(self) -> str:
        """What to tell the model when it is close to a limit."""
        return (
            "You are close to this turn's budget. Stop gathering new data and give "
            "your conclusion now, based on what you already have. State plainly what "
            "you could not check rather than guessing at it."
        )

    def cutoff_notice(self, limit: str) -> str:
        """What to tell the reader when a limit ended the turn."""
        return (
            f"\n\n---\n**This investigation stopped early — it reached its {limit}.** "
            f"{self.iterations} steps were taken. The findings above are what was established "
            "before that point and may be incomplete; anything not mentioned was not ruled out."
        )

    def summary(self) -> dict[str, float | int]:
        return {
            "iterations": self.iterations,
            "input_tokens": self.input_tokens,
            "elapsed_seconds": round(self.elapsed, 1),
        }


def compact_tool_results(
    messages: list[dict],
    *,
    keep_recent: int = KEEP_RAW_RECENT_TURNS,
    min_chars: int = MIN_COMPACTABLE_CHARS,
) -> tuple[list[dict], int]:
    """Shrink old tool results, keeping recent ones intact.

    The agent has already read older results and taken what it needed from them;
    carrying the raw payloads forward crowds out the conversation. Recent results
    are left alone because they may still be actively reasoned over.

    Compaction is visible in the content it leaves behind, not silent — a reader
    of the transcript can see that a payload was shortened and by how much. Returns
    the messages and the number of characters reclaimed.
    """
    if len(messages) <= keep_recent:
        return messages, 0

    cutoff = len(messages) - keep_recent
    reclaimed = 0
    out: list[dict] = []

    for index, message in enumerate(messages):
        if index >= cutoff or message.get("role") != "user":
            out.append(message)
            continue

        content = message.get("content")
        if not isinstance(content, list):
            out.append(message)
            continue

        new_content = []
        changed = False
        for block in content:
            if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                new_content.append(block)
                continue
            text = block.get("content")
            if not isinstance(text, str) or len(text) < min_chars:
                new_content.append(block)
                continue

            head = text[:600].rstrip()
            reclaimed += len(text) - len(head)
            new_content.append(
                {
                    **block,
                    "content": (
                        f"{head}\n\n[earlier tool result compacted — {len(text):,} characters, "
                        f"first 600 kept. Re-run the tool if you need the rest.]"
                    ),
                }
            )
            changed = True

        out.append({**message, "content": new_content} if changed else message)

    if reclaimed:
        logger.info("Compacted older tool results, reclaimed %d characters", reclaimed)
    return out, reclaimed
