"""Startup configuration validation."""

import logging
import os

logger = logging.getLogger("pulse_agent")


def validate_config() -> None:
    """Validate required configuration on startup. Raises SystemExit on error."""
    errors = []

    # API key
    if not os.getenv("ANTHROPIC_API_KEY") and not os.getenv("ANTHROPIC_VERTEX_PROJECT_ID"):
        errors.append("Must set ANTHROPIC_API_KEY or ANTHROPIC_VERTEX_PROJECT_ID")

    # Circuit breaker
    try:
        cb_timeout = float(os.getenv("PULSE_AGENT_CB_TIMEOUT", "60"))
        if cb_timeout <= 0:
            errors.append("PULSE_AGENT_CB_TIMEOUT must be > 0")
    except ValueError:
        errors.append("PULSE_AGENT_CB_TIMEOUT must be a number")

    try:
        cb_threshold = int(os.getenv("PULSE_AGENT_CB_THRESHOLD", "3"))
        if cb_threshold <= 0:
            errors.append("PULSE_AGENT_CB_THRESHOLD must be > 0")
    except ValueError:
        errors.append("PULSE_AGENT_CB_THRESHOLD must be an integer")

    # Model
    model = os.getenv("PULSE_AGENT_MODEL", "claude-opus-4-6")
    if not model.startswith("claude"):
        errors.append(f"PULSE_AGENT_MODEL '{model}' doesn't look like a Claude model")

    # Tool timeout
    try:
        tool_timeout = int(os.getenv("PULSE_AGENT_TOOL_TIMEOUT", "30"))
        if tool_timeout <= 0:
            errors.append("PULSE_AGENT_TOOL_TIMEOUT must be > 0")
    except ValueError:
        errors.append("PULSE_AGENT_TOOL_TIMEOUT must be an integer")

    if errors:
        for e in errors:
            logger.critical("Config error: %s", e)
        raise SystemExit(1)
