"""``GET /changes`` — what changed around a resource in a time window.

Answers the resource page's "what changed?" from the same cluster readers the
episode view uses (``monitor/change_correlation.py``), scoped by
``monitor/resource_changes.py`` to the resource, its owner chain, the config
it references, and cluster upgrades.

Reads run with the caller's forwarded token, so a user only sees what their
RBAC allows; a refused read is a 403 with the reason, never an empty list.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import urllib3
from fastapi import APIRouter, Depends, HTTPException, Query
from kubernetes.config import ConfigException

from ..k8s_client import user_token_context
from ..k8s_tools.validators import _validate_k8s_name, _validate_k8s_namespace
from ..monitor.resource_changes import CLUSTER_SCOPED_KINDS, SUPPORTED_KINDS, ChangeReadError, collect_changes
from .auth import get_user_token, verify_token

logger = logging.getLogger("pulse_agent.api")

router = APIRouter(tags=["changes"])

DEFAULT_WINDOW = timedelta(hours=2)
MAX_WINDOW = timedelta(days=7)
_KINDS = {k.lower(): k for k in SUPPORTED_KINDS}


def _bad(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=detail)


def _parse_time(value: str, field: str) -> datetime:
    try:
        ts = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise _bad(f"{field} must be an ISO 8601 timestamp, got {value!r}") from None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _window(since: str | None, until: str | None) -> tuple[int, int]:
    end = _parse_time(until, "until") if until else datetime.now(UTC)
    start = _parse_time(since, "since") if since else end - DEFAULT_WINDOW
    if start >= end:
        raise _bad("since must be before until")
    if end - start > MAX_WINDOW:
        raise _bad("The window may be at most 7 days; narrow since/until")
    return int(start.timestamp()), int(end.timestamp())


def _validate(namespace: str, kind: str, name: str) -> str:
    """Return the canonical kind ("" for none); raise 400 on bad input."""
    if err := _validate_k8s_namespace(namespace):
        raise _bad(err.removeprefix("Error: "))
    canonical = ""
    if kind:
        canonical = _KINDS.get(kind.lower(), "")
        if not canonical:
            raise _bad(f"Change history is not tracked for kind {kind!r}; supported: {', '.join(SUPPORTED_KINDS)}")
    if name:
        if not canonical:
            raise _bad("name requires kind")
        if err := _validate_k8s_name(name, "name"):
            raise _bad(err.removeprefix("Error: "))
    if canonical and canonical not in CLUSTER_SCOPED_KINDS:
        if not name:
            raise _bad(f"kind {canonical} requires name")
        if not namespace:
            raise _bad(f"kind {canonical} requires namespace")
    if canonical == "ClusterVersion" and name and name != "version":
        raise _bad("The only ClusterVersion is 'version'")
    return canonical


@router.get("/changes")
async def get_changes(
    namespace: str = Query(""),
    kind: str = Query(""),
    name: str = Query(""),
    since: str | None = Query(None, description="ISO 8601; default until minus 2 hours"),
    until: str | None = Query(None, description="ISO 8601; default now"),
    user_token: str | None = Depends(get_user_token),
    _auth=Depends(verify_token),
) -> dict[str, Any]:
    """Changes to a resource, its owners, the config it references, and the cluster.

    ``{"changes": [{"at", "kind", "namespace", "name", "summary", "actor",
    "source"}], "available": bool}``, newest first. ``unreadable`` is added when
    an optional source (Secret metadata, Argo CD, ClusterVersion) was refused.
    """
    canonical = _validate(namespace, kind, name)
    start, end = _window(since, until)

    def _run() -> dict[str, Any]:
        with user_token_context(user_token):
            return collect_changes(namespace, canonical, name, start, end)

    try:
        return await asyncio.to_thread(_run)
    except ChangeReadError as e:
        raise HTTPException(status_code=e.status, detail=e.detail) from None
    except (ConfigException, urllib3.exceptions.HTTPError, OSError):
        # No cluster config, or the API server cannot be reached: the change
        # source is not running. Say so instead of an empty "nothing changed".
        logger.warning("Change history unavailable", exc_info=True)
        return {"changes": [], "available": False}
