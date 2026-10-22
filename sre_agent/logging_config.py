"""Structured JSON logging configuration.

Uses structlog to wrap stdlib logging so that *both* ``logging.getLogger()``
and ``structlog.get_logger()`` emit structured output through the same
processor chain.  Existing modules keep working without changes.
"""

from __future__ import annotations

import logging
import re
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


class RedactQueryTokens(logging.Filter):
    """Mask ``token=`` query parameters in log lines.

    nginx forwards the shared WebSocket token as ``?token=`` to the agent, and
    uvicorn logs every WebSocket accept with the full path, so the token sat in
    the agent's logs for anyone who can read them.
    """

    _PATTERN = re.compile(r"(?i)([?&]token=)[^&\s\"']+")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and "token=" in record.msg:
            record.msg = self._PATTERN.sub(r"\1<redacted>", record.msg)
        # uvicorn passes the path as a positional argument.
        if isinstance(record.args, tuple) and any(isinstance(a, str) and "token=" in a for a in record.args):
            record.args = tuple(self._PATTERN.sub(r"\1<redacted>", a) if isinstance(a, str) else a for a in record.args)
        return True


def install_token_redaction() -> None:
    """Attach the redaction filter to the loggers that print request paths."""
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", ""):
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactQueryTokens) for f in logger.filters):
            logger.addFilter(RedactQueryTokens())
    for handler in (
        logging.getLogger().handlers
        + logging.getLogger("uvicorn.access").handlers
        + logging.getLogger("uvicorn.error").handlers
    ):
        if not any(isinstance(f, RedactQueryTokens) for f in handler.filters):
            handler.addFilter(RedactQueryTokens())


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
