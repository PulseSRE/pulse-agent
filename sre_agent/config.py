"""Startup configuration with Pydantic v2 Settings."""

from __future__ import annotations

import logging
import os

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("pulse_agent")


class PulseAgentSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PULSE_AGENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Agent
    model: str = "claude-opus-4-6"
    max_tokens: int = 16000
    harness: bool = True

    # Database (PostgreSQL required)
    database_url: str = ""

    # Memory
    memory: bool = True

    # Monitor
    scan_interval: int = 60
    crashloop_threshold: int = 3
    max_daily_investigations: int = 20
    investigation_timeout: int = 20
    autofix_enabled: bool = True
    security_followup: bool = False

    # WebSocket
    ws_token: str = ""

    # Circuit breaker
    cb_threshold: int = 3
    cb_timeout: float = 60.0

    # Tool timeout
    tool_timeout: int = 30

    # Webhook
    webhook_url: str = ""
    webhook_secret: str = ""

    # Trusted registries
    trusted_registries: list[str] = [
        "registry.redhat.io",
        "registry.access.redhat.com",
        "quay.io",
        "image-registry.openshift-image-registry.svc",
    ]

    # Tool chain intelligence
    chain_hints: bool = True
    chain_min_probability: float = 0.6
    chain_min_frequency: int = 3

    @field_validator("model")
    @classmethod
    def model_must_be_claude(cls, v: str) -> str:
        if not v.startswith("claude"):
            raise ValueError(f"Model '{v}' doesn't look like a Claude model")
        return v

    @field_validator("cb_timeout")
    @classmethod
    def cb_timeout_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("cb_timeout must be > 0")
        return v

    @field_validator("tool_timeout")
    @classmethod
    def tool_timeout_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("tool_timeout must be > 0")
        return v


# Singleton
_settings: PulseAgentSettings | None = None


def get_settings() -> PulseAgentSettings:
    global _settings
    if _settings is None:
        _settings = PulseAgentSettings()
    return _settings


def _reset_settings() -> None:
    """Reset the singleton — for testing only."""
    global _settings
    _settings = None


def validate_config() -> None:
    """Validate config on startup. Raises SystemExit on error."""
    try:
        _reset_settings()
        get_settings()

        # Check AI backend (these are Claude SDK env vars, not PULSE_AGENT_ prefixed)
        has_key = bool(os.getenv("ANTHROPIC_API_KEY"))
        has_vertex = bool(os.getenv("ANTHROPIC_VERTEX_PROJECT_ID"))
        if not has_key and not has_vertex:
            logger.critical("Config error: Must set ANTHROPIC_API_KEY or ANTHROPIC_VERTEX_PROJECT_ID")
            raise SystemExit(1)

    except SystemExit:
        raise
    except Exception as e:
        logger.critical("Config error: %s", e)
        raise SystemExit(1)
