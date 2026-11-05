"""RBAC, SCC and webhook change history (``GET /rbac/changes``) and the Pulse
write ledger (``POST /rbac/ledger``).

The history is what the agent observed (``sre_agent/rbac_history.py``): each
change with the version before it, so the UI can show a diff and restore the
previous version with the user's own token. It is served read-only and only
for objects the caller may read: every (kind, namespace) in the answer is
checked with a SelfSubjectAccessReview as the caller, and what they may not
`get` is left out and counted in ``hidden``. A refused or failed check hides
the rows; it never shows them.

The ledger records writes made through Pulse. An entry is accepted only from a
caller who may make that write (SelfSubjectAccessReview on the exact object,
as the caller); the actor is who the caller's own token says they are
(SelfSubjectReview), never a name in the body. A change is credited to a
ledger entry only when the entry's resourceVersion is the observed version's.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ..k8s_client import user_token_context
from ..rbac_history import WATCHED_KINDS, status
from .auth import _get_current_user, get_user_token, require_user_token, verify_token

logger = logging.getLogger("pulse_agent.api")

router = APIRouter(tags=["rbac-history"])

DEFAULT_WINDOW = timedelta(days=7)
MAX_WINDOW = timedelta(days=30)
MAX_LIMIT = 500
MAX_LEDGER_BODY = 256 * 1024
MAX_SNAPSHOT = 64 * 1024
LEDGER_RATE_PER_MINUTE = 60
_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.:]*[a-z0-9])?$")
_NS_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_VERBS = {"create", "update", "patch", "delete"}
_OUTCOMES = {"ok", "failed"}

# Ledger kinds besides the watched ones: (group, plural, namespaced).
_LEDGER_KINDS: dict[str, tuple[str, str, bool]] = {
    **{k: (v[0], v[2], v[3]) for k, v in WATCHED_KINDS.items()},
    "Group": ("user.openshift.io", "groups", False),
    "User": ("user.openshift.io", "users", False),
    "Identity": ("user.openshift.io", "identities", False),
    "OAuthAccessToken": ("oauth.openshift.io", "oauthaccesstokens", False),
    "OAuth": ("config.openshift.io", "oauths", False),
    "Secret": ("", "secrets", True),
}

_rate_lock = threading.Lock()
_rate: dict[str, collections.deque] = {}


def _rate_limited(actor: str, now: float | None = None) -> bool:
    """True when ``actor`` already recorded LEDGER_RATE_PER_MINUTE entries in the last minute."""
    now = time.monotonic() if now is None else now
    with _rate_lock:
        q = _rate.setdefault(actor, collections.deque())
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= LEDGER_RATE_PER_MINUTE:
            return True
        q.append(now)
        return False


def reset_rate_limit_for_tests() -> None:
    with _rate_lock:
        _rate.clear()


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
    # Nothing older than the retention window is kept; asking for more is the window.
    ts = max(ts, now - MAX_WINDOW)
    return int(ts.timestamp() * 1000)


def is_system_object(kind: str, namespace: str, name: str) -> bool:
    """Objects the platform creates and reconciles; hidden unless asked for."""
    if name.startswith(("system:", "openshift-", "openshift:")):
        return True
    return bool(namespace) and (
        namespace.startswith(("openshift-", "kube-")) or namespace in {"openshift", "kube-system"}
    )


def _can(verb: str, group: str, plural: str, namespace: str, name: str = "") -> bool:
    """SelfSubjectAccessReview as whoever is active (the caller, inside user_token_context)."""
    from kubernetes import client

    from ..k8s_client import get_authorization_client

    review = client.V1SelfSubjectAccessReview(
        spec=client.V1SelfSubjectAccessReviewSpec(
            resource_attributes=client.V1ResourceAttributes(
                group=group, resource=plural, verb=verb, namespace=namespace or None, name=name or None
            )
        )
    )
    resp = get_authorization_client().create_self_subject_access_review(review)
    return bool(resp.status and resp.status.allowed)


def _can_get(group: str, plural: str, namespace: str) -> bool:
    return _can("get", group, plural, namespace)


class _ActorUnknownError(Exception):
    pass


def _whoami(user_token: str | None) -> str | None:
    """The username the caller's own token authenticates as (SelfSubjectReview).

    None when the API is not served (404) or there is no token (forwarding
    off); raises _ActorUnknownError when the token is refused.
    """
    if not user_token:
        return None
    from kubernetes import client
    from kubernetes.client.rest import ApiException

    from ..k8s_client import _get_user_api_client, user_token_context

    with user_token_context(user_token):
        api = client.AuthenticationV1Api(api_client=_get_user_api_client(user_token))
        try:
            resp = api.create_self_subject_review(client.V1SelfSubjectReview())
        except ApiException as e:
            if e.status == 404:
                return None
            raise _ActorUnknownError(f"{e.status} {e.reason}") from None
    user = getattr(getattr(resp, "status", None), "user_info", None)
    name = getattr(user, "username", None)
    if not name:
        raise _ActorUnknownError("SelfSubjectReview returned no username")
    return str(name)


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


def _actor_for(change: dict, ledger: list[dict]) -> dict | None:
    """The Pulse write that produced exactly this observed version: same object and
    the same resourceVersion (for a deletion, the version that was deleted)."""
    rv = change.get("resource_version") or ""
    if not rv:
        return None
    key = (change["kind"], change.get("namespace") or "", change["name"])
    for e in ledger:
        if (e["kind"], e.get("namespace") or "", e["name"]) != key or e.get("outcome") != "ok":
            continue
        if (e.get("resource_version") or "") != rv:
            continue
        if (change["change"] == "deleted") != (e.get("verb") == "delete"):
            continue
        return {
            "name": e["actor"],
            "verified": bool(e.get("actor_verified")),
            "source": e["source"],
            "at": _iso(e["at"]),
            "ledger_id": e["id"],
            "reason": e.get("reason") or "",
        }
    return None


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
        "resource_version": e.get("resource_version") or "",
        "actor_verified": bool(e.get("actor_verified")),
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

    from ..repositories import get_rbac_history_repo

    repo = get_rbac_history_repo()
    fetch_limit = limit if include_system else MAX_LIMIT * 4
    try:
        raw = await asyncio.to_thread(repo.fetch_changes, since_ms, kind, namespace, name, fetch_limit)
        # A day earlier: the write lands before the scan that observes it.
        ledger = await asyncio.to_thread(repo.fetch_ledger, since_ms - 86_400_000, list(WATCHED_KINDS), 5000)
        first = await asyncio.to_thread(repo.first_observed_at)
    except Exception:
        return _unavailable()

    # Permission first, so system_hidden counts only what the caller could have seen.
    rows, hidden = await asyncio.to_thread(_visible, raw, user_token)
    system_hidden = 0
    if not include_system:
        kept = [r for r in rows if not is_system_object(r["kind"], r.get("namespace") or "", r["name"])]
        system_hidden = len(rows) - len(kept)
        rows = kept
    truncated = len(rows) > limit or len(raw) >= fetch_limit
    rows = rows[:limit]
    ledger_rows, _ = await asyncio.to_thread(_visible, ledger, user_token)
    recent_ledger = [e for e in ledger_rows if e["at"] >= since_ms]
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
        "changes": [_change_json(r, _actor_for(r, ledger_rows)) for r in rows],
        "ledger": [_ledger_json(e) for e in recent_ledger][:limit],
        "hidden": hidden,
        "system_hidden": system_hidden,
        "truncated": truncated or len(recent_ledger) > limit,
    }


async def _read_capped(request: Request, cap: int) -> bytes | None:
    """The body, or None when it is larger than ``cap`` (checked before reading it all)."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cap:
        return None
    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > cap:
            return None
    return bytes(buf)


@router.post("/rbac/ledger")
async def record_ledger(
    request: Request,
    user_token: str | None = Depends(get_user_token),
    _auth=Depends(verify_token),
    x_forwarded_access_token: str | None = Header(None, alias="X-Forwarded-Access-Token"),
    x_forwarded_user: str | None = Header(None, alias="X-Forwarded-User"),
) -> Any:
    """Record a write made through the Pulse UI, after checking the caller may make it."""
    require_user_token(user_token)
    raw = await _read_capped(request, MAX_LEDGER_BODY)
    if raw is None:
        return JSONResponse(status_code=413, content={"detail": "Ledger entry too large (max 256 KiB)"})
    try:
        body = json.loads(raw or b"null")
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    verb = body.get("verb")
    kind = body.get("kind")
    name = body.get("name")
    ns = body.get("namespace") or ""
    outcome = body.get("outcome") or "ok"
    rv = body.get("resource_version") or ""
    if verb not in _VERBS:
        raise HTTPException(status_code=400, detail=f"verb must be one of: {', '.join(sorted(_VERBS))}")
    if kind not in _LEDGER_KINDS:
        raise HTTPException(status_code=400, detail="kind is not one Pulse records")
    if not isinstance(name, str) or not name or len(name) > 253:
        raise HTTPException(status_code=400, detail="name is required")
    if not isinstance(ns, str) or (ns and not _NS_RE.match(ns)):
        raise HTTPException(status_code=400, detail="namespace is not a valid name")
    if outcome not in _OUTCOMES:
        raise HTTPException(status_code=400, detail="outcome must be ok or failed")
    if not isinstance(rv, str) or len(rv) > 64:
        raise HTTPException(status_code=400, detail="resource_version must be a string")
    reason = body.get("reason") or ""
    if not isinstance(reason, str) or len(reason) > 2000:
        raise HTTPException(status_code=400, detail="reason must be a string of at most 2000 characters")
    if kind == "Secret" and (body.get("before") is not None or body.get("after") is not None):
        # Never store Secret content, even hashed: only that it was written.
        raise HTTPException(status_code=400, detail="Secret writes are recorded without before/after")
    if body.get("detail") is not None and len(json.dumps(body["detail"], default=str)) > 4096:
        return JSONResponse(status_code=413, content={"detail": "'detail' is larger than 4 KiB"})
    for field in ("before", "after"):
        if body.get(field) is not None and len(json.dumps(body[field], default=str)) > MAX_SNAPSHOT:
            return JSONResponse(status_code=413, content={"detail": f"'{field}' is larger than 64 KiB"})

    # Who: the caller's own token, not a header anyone behind the proxy could set.
    try:
        verified = await asyncio.to_thread(_whoami, user_token)
    except _ActorUnknownError as e:
        raise HTTPException(status_code=401, detail=f"Could not tell who you are from your token ({e})") from None
    except Exception:
        logger.warning("SelfSubjectReview failed", exc_info=True)
        raise HTTPException(status_code=503, detail="Could not verify who you are; nothing was recorded") from None
    actor = verified or _get_current_user(x_forwarded_access_token, x_forwarded_user)

    if _rate_limited(actor):
        return JSONResponse(
            status_code=429, content={"detail": f"At most {LEDGER_RATE_PER_MINUTE} ledger entries per minute"}
        )

    # Only a write the caller is allowed to make can be recorded as theirs.
    group, plural, namespaced = _LEDGER_KINDS[kind]

    def _allowed() -> bool:
        with user_token_context(user_token):
            return _can(verb, group, plural, ns if namespaced else "", name)

    try:
        allowed = await asyncio.to_thread(_allowed)
    except Exception:
        logger.warning("Ledger access check failed", exc_info=True)
        raise HTTPException(
            status_code=503, detail="Could not check that you may make this write; nothing was recorded"
        ) from None
    if not allowed:
        raise HTTPException(
            status_code=403, detail=f"You may not {verb} {kind} {name}, so it is not recorded as your write"
        )

    from ..repositories import get_rbac_history_repo

    try:
        row = await asyncio.to_thread(
            get_rbac_history_repo().record_write,
            {
                "at": int(time.time() * 1000),
                "actor": actor,
                "actor_verified": verified is not None,
                "source": "ui",
                "verb": verb,
                "kind": kind,
                "namespace": ns if namespaced else "",
                "name": name,
                "resource_version": rv,
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
