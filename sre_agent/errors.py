"""Structured error types for the Pulse Agent.

Provides ToolError — a typed error that classifies K8s API errors into
categories matching the UI's PulseError, with actionable suggestions.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import UTC, datetime

from kubernetes.client.rest import ApiException

logger = logging.getLogger("pulse_agent")


@dataclasses.dataclass
class ToolError:
    """Structured error returned by tools instead of raw strings."""

    message: str
    category: str  # unauthorized, forbidden, not_found, conflict, validation, server, network, quota
    status_code: int | None = None
    operation: str = ""
    suggestions: list[str] = dataclasses.field(default_factory=list)
    timestamp: str = dataclasses.field(default_factory=lambda: datetime.now(UTC).isoformat())

    def __str__(self) -> str:
        """Backward compat — tools that return str(result) get the message."""
        return self.message

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def classify_api_error(e: ApiException, operation: str = "") -> ToolError:
    """Classify a Kubernetes ApiException into a structured ToolError."""
    status = e.status or 0
    reason = e.reason or ""
    body_msg = ""
    try:
        import json

        body = json.loads(e.body) if e.body else {}
        body_msg = body.get("message", "")
    except Exception:
        body_msg = str(e.body)[:200] if e.body else ""

    msg = body_msg or f"Error ({status}): {reason}"

    # Quota errors are 403 with specific message content
    if status == 403 and any(kw in msg.lower() for kw in ("quota", "exceeded", "limit")):
        return ToolError(
            message=msg,
            category="quota",
            status_code=status,
            operation=operation,
            suggestions=[
                "Check resource quotas in the namespace",
                "Clean up unused resources or request a quota increase",
            ],
        )

    if status == 401:
        return ToolError(
            message=msg,
            category="unauthorized",
            status_code=status,
            operation=operation,
            suggestions=[
                "Session expired — reconnect to refresh credentials",
            ],
        )

    if status == 403:
        return ToolError(
            message=msg,
            category="forbidden",
            status_code=status,
            operation=operation,
            suggestions=[
                "You don't have permission for this resource",
                "Check your RBAC role bindings in this namespace",
            ],
        )

    if status == 404:
        return ToolError(
            message=msg,
            category="not_found",
            status_code=status,
            operation=operation,
            suggestions=[
                "The resource may have been deleted",
                "Check the namespace exists",
            ],
        )

    if status == 409:
        return ToolError(
            message=msg,
            category="conflict",
            status_code=status,
            operation=operation,
            suggestions=[
                "Another process modified this resource",
                "Retry the operation",
            ],
        )

    if status == 422:
        return ToolError(
            message=msg,
            category="validation",
            status_code=status,
            operation=operation,
            suggestions=[
                "Check the resource spec for invalid fields",
                "Review the error message for specific field issues",
            ],
        )

    if status >= 500:
        return ToolError(
            message=msg,
            category="server",
            status_code=status,
            operation=operation,
            suggestions=[
                "Check cluster health and API server status",
                "The API server may be overloaded — retry in a moment",
            ],
        )

    return ToolError(
        message=msg,
        category="unknown",
        status_code=status,
        operation=operation,
    )


def classify_exception(e: Exception, operation: str = "") -> ToolError:
    """Wrap an arbitrary exception as a ToolError."""
    if isinstance(e, ApiException):
        return classify_api_error(e, operation)

    msg = f"{type(e).__name__}: {e}"

    # Network-level errors
    if any(kw in type(e).__name__.lower() for kw in ("connection", "timeout", "url", "socket")):
        return ToolError(
            message=msg,
            category="network",
            status_code=None,
            operation=operation,
            suggestions=[
                "Check cluster connectivity",
                "The API server may be restarting",
            ],
        )

    return ToolError(
        message=msg,
        category="server",
        status_code=None,
        operation=operation,
        suggestions=[],
    )
