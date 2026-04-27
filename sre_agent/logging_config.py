"""Structured JSON logging configuration.

Uses structlog to wrap stdlib logging so that *both* ``logging.getLogger()``
and ``structlog.get_logger()`` emit structured output through the same
processor chain.  Existing modules keep working without changes.
"""

from __future__ import annotations

import logging
import sys

import structlog


def _rename_and_enrich(
    logger: logging.Logger, method_name: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Rename structlog fields to match the existing JSON schema and add static fields."""
    if "event" in event_dict:
        event_dict["message"] = event_dict.pop("event")
    if "logger_name" in event_dict:
        event_dict["logger"] = event_dict.pop("logger_name")
    if "level" in event_dict:
        event_dict["level"] = event_dict["level"].upper()
    event_dict.setdefault("service", "pulse-agent")
    return event_dict


def configure_logging() -> None:
    """Configure structured JSON logging for production, human-readable for dev."""
    from .config import get_settings

    _s = get_settings()
    log_format = _s.server.log_format
    log_level = _s.server.log_level.upper()

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    if log_format == "json":
        final_processors: list[structlog.types.Processor] = [
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            _rename_and_enrich,
            structlog.processors.JSONRenderer(),
        ]
    else:
        final_processors = [
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(colors=False),
        ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    root = logging.getLogger()
    root.setLevel(log_level)
    root.handlers.clear()

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=final_processors,
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    root.addHandler(handler)

    # Suppress noisy libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("kubernetes").setLevel(logging.WARNING)
