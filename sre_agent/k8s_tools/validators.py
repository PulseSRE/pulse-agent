"""Input validation helpers and shared constants for Kubernetes tools."""

from __future__ import annotations

import re

# Shared constants
MAX_RESULTS = 200
MAX_REPLICAS = 100

# RFC 1123 name validation for K8s resources
_K8S_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9\-\.]{0,251}[a-z0-9])?$")
_K8S_NAMESPACE_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?$")


def _validate_k8s_name(value: str, field: str = "name") -> str | None:
    """Validate a K8s resource name. Returns error message or None if valid."""
    if not value:
        return f"Error: {field} is required."
    if len(value) > 253:
        return f"Error: {field} too long (max 253 chars)."
    if not _K8S_NAME_RE.match(value):
        return f"Error: {field} '{value}' is not a valid Kubernetes name (RFC 1123)."
    return None


def _validate_k8s_namespace(value: str) -> str | None:
    """Validate a K8s namespace name. Returns error message or None if valid."""
    if not value:
        return None  # namespace is often optional
    if len(value) > 63:
        return "Error: namespace too long (max 63 chars)."
    if not _K8S_NAMESPACE_RE.match(value):
        return f"Error: namespace '{value}' is not a valid Kubernetes namespace name."
    return None
