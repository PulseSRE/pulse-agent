"""Structured logging factory for Pulse Agent.

Usage::

    from sre_agent.log import get_logger
    logger = get_logger()
    logger.info("scan_complete", findings=5, duration_ms=120)

During migration, both stdlib ``logging.getLogger()`` and structlog
``get_logger()`` coexist. structlog wraps stdlib so existing handlers
still work — all log output gets the same processors.
"""

from __future__ import annotations

import structlog


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger bound to *name*.

    If *name* is ``None``, structlog infers the caller's module.
    """
    return structlog.get_logger(name)
