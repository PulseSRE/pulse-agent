"""RBAC, SCC and webhook change history (``GET /rbac/changes``) and the Pulse
write ledger (``POST /rbac/ledger``).

The history is what the agent observed (``sre_agent/rbac_history.py``): each
change with the version before it, so the UI can show a diff and restore the
previous version with the user's own token. It is served read-only and only
for objects the caller may read: every (kind, namespace) in the answer is
checked with a SelfSubjectAccessReview as the caller, and what they may not
`get` is left out and counted in ``hidden``. A refused or failed check hides
the rows; it never shows them.

The ledger records writes made through Pulse. The actor is the signed-in user
from the login proxy; a name in the body is ignored.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ..k8s_client import user_token_context
from ..rbac_history import WATCHED_KINDS, status
from .auth import get_owner, get_user_token, require_user_token, verify_token

logger = logging.getLogger("pulse_agent.api")

router = APIRouter(tags=["rbac-history"])

DEFAULT_WINDOW = timedelta(days=7)
MAX_WINDOW = timedelta(days=30)
MAX_LIMIT = 500
MAX_LEDGER_BODY = 256 * 1024
_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.:]*[a-z0-9])?$")
_NS_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_VERBS = {"create", "update", "patch", "delete"}
_OUTCOMES = {"ok", "failed"}


def _iso(ms: int | None) -> str | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, UTC).isoformat()


def _since_ms(since: str | None) -> int:
    now = datetime.now(UTC)
    if not since:
        return int((now - DEFAULT_WINDOW).timestamp() * 1000)
    try:
        ts = datetime.fromisoformat(since.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"since must be an ISO 8601 timestamp, got {since!r}") from None
    ts = ts if ts.tzinfo else ts.replace(tzinfo=UTC)
    if now - ts > MAX_WINDOW:
        raise HTTPException(
            status_code=400, detail="since may be at most 30 days ago (the history is kept for 30 days)"
        )
    return int(ts.timestamp() * 1000)


def is_system_object(kind: str, namespace: str, name: str) -> bool:
    """Objects the platform creates and reconciles; hidden unless asked for."""
    if name.startswith(("system:", "openshift-", "openshift:")):
        return True
    return bool(namespace) and (
        namespace.startswith(("openshift-", "kube-")) or namespace in {"openshift", "kube-system"}
    )


def _can_get(group: str, plural: str, namespace: str) -> bool:
    from kubernetes import client

    from ..k8s_client import get_authorization_client

    review = client.V1SelfSubjectAccessReview(
        spec=client.V1SelfSubjectAccessReviewSpec(
            resource_attributes=client.V1ResourceAttributes(
                group=group, resource=plural, verb="get", namespace=namespace or None
            )
        )
    )
    resp = get_authorization_client().create_self_subject_access_review(review)
    return bool(resp.status and resp.status.allowed)


def _visible(rows: list[dict], user_token: str | None) -> tuple[list[dict], int]:
    """Keep the rows the caller may read; a failed check hides (fail closed)."""
    decided: dict[tuple[str, str], bool] = {}
    kept: list[dict] = []
    hidden = 0
    with user_token_context(user_token):
        for r in rows:
            kind = r["kind"]
            spec = WATCHED_KINDS.get(kind)
            if spec is None:
                hidden += 1
                continue
            key = (kind, r.get("namespace") or "")
            if key not in decided:
                try:
                    decided[key] = _can_get(spec[0], spec[2], key[1])
                except Exception:
                    logger.warning("Access check for %s in %r failed; hiding its history", kind, key[1], exc_info=True)
                    decided[key] = False
            if decided[key]:
                kept.append(r)
            else:
                hidden += 1
    return kept, hidden


def _actor_for(change: dict, ledger: list[dict], window_ms: int) -> dict | None:
    """The Pulse write that most plausibly made this change: same object, before the
    observation and no earlier than one observation window before it."""
    best = None
    for e in ledger:
        if (e["kind"], e.get("namespace") or "", e["name"]) != (
            change["kind"],
            change.get("namespace") or "",
            change["name"],
        ):
            continue
        if e.get("outcome") != "ok" or not (change["observed_at"] - window_ms <= e["at"] <= change["observed_at"]):
            continue
        if best is None or e["at"] > best["at"]:
            best = e
    if best is None:
        return None
    return {
        "name": best["actor"],
        "source": best["source"],
        "at": _iso(best["at"]),
        "ledger_id": best["id"],
        "reason": best.get("reason") or "",
    }


def _change_json(r: dict, actor: dict | None) -> dict:
    return {
        "id": r["id"],
        "kind": r["kind"],
        "namespace": r.get("namespace") or "",
        "name": r["name"],
        "change": r["change"],
        "observed_at": _iso(r["observed_at"]),
        "resource_version": r.get("resource_version") or "",
        "uid": r.get("uid") or "",
        "manager": r.get("manager") or "",
        "operation": r.get("operation") or "",
        "manager_time": r.get("manager_time"),
        "actor": actor,
        "previous_id": r.get("previous_id"),
        "before": r.get("previous_content"),
        "after": r.get("content"),
    }


def _ledger_json(e: dict) -> dict:
    return {
        "id": e["id"],
        "at": _iso(e["at"]),
        "actor": e["actor"],
        "source": e["source"],
        "verb": e["verb"],
        "kind": e["kind"],
        "namespace": e.get("namespace") or "",
        "name": e["name"],
        "reason": e.get("reason") or "",
        "outcome": e.get("outcome") or "ok",
        "has_before": e.get("before") is not None,
        "has_after": e.get("after") is not None,
    }


def _unavailable() -> JSONResponse:
    logger.warning("RBAC history read failed", exc_info=True)
    return JSONResponse(
        status_code=503, content={"detail": "RBAC history storage is unavailable; the database could not be reached."}
    )


@router.get("/rbac/changes")
async def rbac_changes(
    since: str | None = Query(None, description="ISO 8601; default 7 days ago, at most 30"),
    kind: str = Query(""),
    namespace: str | None = Query(None, description="exact namespace; '' for cluster-scoped objects"),
    name: str = Query(""),
    include_system: bool = Query(False),
    limit: int = Query(200, ge=1, le=MAX_LIMIT),
    user_token: str | None = Depends(get_user_token),
    _auth=Depends(verify_token),
) -> Any:
    """Observed changes to RBAC, SCC and admission webhook objects, newest first.

    See API_CONTRACT.md (``GET /rbac/changes``) for the response.
    """
    require_user_token(user_token)
    if kind and kind not in WATCHED_KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of: {', '.join(WATCHED_KINDS)}")
    if namespace and not _NS_RE.match(namespace):
        raise HTTPException(status_code=400, detail="namespace is not a valid name")
    if name and not _NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="name is not a valid object name")
    since_ms = _since_ms(since)

    from ..config import get_settings
    from ..repositories import get_rbac_history_repo

    repo = get_rbac_history_repo()
    try:
        # Read more than asked when system objects are filtered out afterwards.
        raw = await asyncio.to_thread(
            repo.fetch_changes, since_ms, kind, namespace, name, limit if include_system else MAX_LIMIT * 4
        )
        ledger = await asyncio.to_thread(repo.fetch_ledger, since_ms - 3600_000, list(WATCHED_KINDS), 2000)
        first = await asyncio.to_thread(repo.first_observed_at)
    except Exception:
        return _unavailable()

    system_hidden = 0
    if not include_system:
        kept = [r for r in raw if not is_system_object(r["kind"], r.get("namespace") or "", r["name"])]
        system_hidden = len(raw) - len(kept)
        raw = kept[:limit]
    rows, hidden = await asyncio.to_thread(_visible, raw, user_token)
    ledger_rows, _ = await asyncio.to_thread(_visible, ledger, user_token)
    window_ms = max(get_settings().monitor.scan_interval, 60) * 2 * 1000 + 60_000
    st = status()
    return {
        "history": {
            "since": _iso(first),
            "last_observed_at": _iso(st["last_observed_at"]),
            "last_error": st["last_error"],
            "kinds": list(WATCHED_KINDS),
            "unwatched": [{"kind": k, "reason": v} for k, v in sorted(st["unwatched"].items())],
            "retention_days": 30,
            "source": "observed",
        },
        "changes": [_change_json(r, _actor_for(r, ledger, window_ms)) for r in rows],
        "ledger": [_ledger_json(e) for e in ledger_rows if e["at"] >= since_ms][:limit],
        "hidden": hidden,
        "system_hidden": system_hidden,
    }


@router.post("/rbac/ledger")
async def record_ledger(request: Request, actor: str = Depends(get_owner)) -> Any:
    """Record a write made through the Pulse UI. The actor is the signed-in user."""
    raw = await request.body()
    if len(raw) > MAX_LEDGER_BODY:
        return JSONResponse(status_code=413, content={"detail": "Ledger entry too large (max 256 KiB)"})
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    verb = body.get("verb")
    kind = body.get("kind")
    name = body.get("name")
    ns = body.get("namespace") or ""
    outcome = body.get("outcome") or "ok"
    if verb not in _VERBS:
        raise HTTPException(status_code=400, detail=f"verb must be one of: {', '.join(sorted(_VERBS))}")
    if kind not in WATCHED_KINDS and kind not in {"Group", "User", "Identity", "OAuthAccessToken", "OAuth", "Secret"}:
        raise HTTPException(status_code=400, detail="kind is not one Pulse records")
    if not isinstance(name, str) or not name or len(name) > 253:
        raise HTTPException(status_code=400, detail="name is required")
    if not isinstance(ns, str) or (ns and not _NS_RE.match(ns)):
        raise HTTPException(status_code=400, detail="namespace is not a valid name")
    if outcome not in _OUTCOMES:
        raise HTTPException(status_code=400, detail="outcome must be ok or failed")
    reason = body.get("reason") or ""
    if not isinstance(reason, str) or len(reason) > 2000:
        raise HTTPException(status_code=400, detail="reason must be a string of at most 2000 characters")
    if kind == "Secret" and (body.get("before") is not None or body.get("after") is not None):
        # Never store Secret content, even hashed: only that it was written.
        raise HTTPException(status_code=400, detail="Secret writes are recorded without before/after")

    from ..repositories import get_rbac_history_repo

    try:
        row = await asyncio.to_thread(
            get_rbac_history_repo().record_write,
            {
                "at": int(time.time() * 1000),
                "actor": actor,
                "source": "ui",
                "verb": verb,
                "kind": kind,
                "namespace": ns,
                "name": name,
                "reason": reason.strip(),
                "outcome": outcome,
                "before": body.get("before"),
                "after": body.get("after"),
                "detail": body.get("detail") if isinstance(body.get("detail"), dict) else None,
            },
        )
    except Exception:
        return _unavailable()
    return _ledger_json(row)
