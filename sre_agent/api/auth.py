"""Authentication and user identity helpers."""

from __future__ import annotations

import collections
import hashlib
import hmac
import logging
import time

from fastapi import Header, HTTPException, Query

from ..config import get_settings

logger = logging.getLogger("pulse_agent.api")

# User identity cache (LRU with TTL)
_user_cache: collections.OrderedDict[str, tuple[str, float]] = collections.OrderedDict()
_USER_CACHE_TTL = 60  # seconds
_USER_CACHE_MAX = 500  # evict oldest entries beyond this


def _verify_ws_token(websocket) -> str:
    """Verify WebSocket token and return the client token. Closes with 4001 if invalid."""
    client_token = websocket.query_params.get("token", "")
    expected = get_settings().server.ws_token
    if not expected or not hmac.compare_digest(client_token, expected):
        return ""
    return client_token


def _verify_rest_token(authorization: str | None = Header(None), token: str | None = Query(None)):
    """Verify token for REST endpoints. Prefers Authorization header; query param is deprecated."""
    expected = get_settings().server.ws_token
    if not expected:
        raise HTTPException(status_code=503, detail="Server not configured")
    client_token = ""
    if authorization and authorization.startswith("Bearer "):
        client_token = authorization[7:]
    elif token:
        client_token = token
        logger.warning("REST token via query param (deprecated) — use Authorization: Bearer header instead")
    if not client_token or not hmac.compare_digest(client_token, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _get_current_user(
    x_forwarded_access_token: str | None = None,
    x_forwarded_user: str | None = None,
) -> str:
    """Extract username from OAuth proxy headers.

    Priority: X-Forwarded-User > TokenReview > PULSE_AGENT_DEV_USER > token hash.
    The OAuth proxy sets X-Forwarded-User with the authenticated username -- this is
    the most reliable source since OpenShift tokens are opaque (sha256~...), not JWTs.

    PULSE_AGENT_DEV_USER is deliberately NOT first. It exists for local development,
    where no OAuth proxy is in front of the agent and no identity headers arrive at
    all -- so a fallback is all it ever needs to be. Checking it first meant that
    setting it in a deployment that *does* sit behind the proxy (a stray env var, a
    copied manifest) silently collapsed every caller's identity to that one name,
    overriding real authenticated users with no error and no log line. As a fallback
    it still works for local dev, but can no longer mask a real identity.
    """
    # Best source: OAuth proxy sets X-Forwarded-User directly
    if x_forwarded_user and isinstance(x_forwarded_user, str) and x_forwarded_user.strip():
        username = x_forwarded_user.strip()
        # One-time migration: move hash-based views to real username
        if not _user_cache.get(f"_migrated_{username}"):
            try:
                from .. import db

                migrated = db.migrate_view_ownership(username)
                if migrated:
                    logger.info("Migrated %d views to user '%s'", migrated, username)
            except Exception:
                logger.debug("View ownership migration failed for user '%s'", username, exc_info=True)
            _user_cache[f"_migrated_{username}"] = (username, time.time())
        return username

    token = x_forwarded_access_token or ""

    if not token:
        # No proxy identity of any kind: this is the local-development shape that
        # PULSE_AGENT_DEV_USER exists for. In a real deployment the proxy always
        # supplies one of the two headers, so this branch is not reachable there.
        dev_user = get_settings().agent.dev_user
        if dev_user:
            return dev_user
        raise HTTPException(
            status_code=401,
            detail="User identity required. X-Forwarded-Access-Token or X-Forwarded-User header is missing.",
        )

    # Use full hash to prevent collision attacks (was [:16])
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    # Check cache (evict if expired)
    cached = _user_cache.get(token_hash)
    if cached:
        if (time.time() - cached[1]) < _USER_CACHE_TTL:
            return cached[0]
        # Don't evict yet -- keep stale entry in case TokenReview fails

    # Resolve via Kubernetes TokenReview
    try:
        from kubernetes import client as k8s_client

        from ..k8s_client import _load_k8s

        _load_k8s()
        auth_api = k8s_client.AuthenticationV1Api()
        # V1TokenReview, not TokenReview: the un-versioned aliases were removed
        # from the kubernetes client (gone by 36.x), and the AttributeError was
        # swallowed by this except as "TokenReview API unavailable" — silently
        # demoting every caller to a token-hash pseudonym that can never match
        # PULSE_AGENT_ADMIN_USERS.
        review = k8s_client.V1TokenReview(spec=k8s_client.V1TokenReviewSpec(token=token))
        result = auth_api.create_token_review(review)
        if result.status.authenticated:
            username = result.status.user.username
            _cache_user(token_hash, username)
            return username
    except Exception:
        # If we have a cached identity (even stale), keep using it during API outage
        if cached:
            logger.warning("TokenReview API unavailable, extending cached identity '%s'", cached[0])
            _cache_user(token_hash, cached[0])  # refresh timestamp
            return cached[0]
        # exc_info, not a bare message: this except once hid an AttributeError
        # (a removed client alias) for multiple releases because the log line
        # could not distinguish "API unreachable" from "our call is broken".
        logger.warning("TokenReview failed, using token-derived identity", exc_info=True)

    # Final fallback: stable identity derived from token hash.
    # OpenShift tokens are sha256~ format (not JWTs), so we can't decode them.
    # NOT cached: caching the pseudonym poisoned the cache — once stored, the
    # exception path above kept "extending" it, so the caller stayed a ghost
    # even after TokenReview recovered. Recomputing a hash per request is free.
    return f"user-{token_hash[:16]}"


def verify_token(authorization: str | None = Header(None), token: str | None = Query(None)):
    """FastAPI dependency — verifies auth token. Use as Depends(verify_token)."""
    _verify_rest_token(authorization, token)


def get_owner(
    authorization: str | None = Header(None),
    token: str | None = Query(None),
    x_forwarded_access_token: str | None = Header(None, alias="X-Forwarded-Access-Token"),
    x_forwarded_user: str | None = Header(None, alias="X-Forwarded-User"),
) -> str:
    """FastAPI dependency — verifies token and returns the authenticated user. Use as Depends(get_owner)."""
    _verify_rest_token(authorization, token)
    return _get_current_user(x_forwarded_access_token, x_forwarded_user)


def require_admin(
    authorization: str | None = Header(None),
    token: str | None = Query(None),
    x_forwarded_access_token: str | None = Header(None, alias="X-Forwarded-Access-Token"),
    x_forwarded_user: str | None = Header(None, alias="X-Forwarded-User"),
) -> str:
    """FastAPI dependency for endpoints that mutate the agent's own behaviour.

    Stricter than verify_token in two ways.

    First, it requires a real authenticated user rather than only the shared
    WS token. The shared token authorises the UI as a whole; it says nothing
    about who is driving it, so on its own it cannot attribute a skill edit to
    anyone. Skill mutation rewrites the system prompt, which is the most
    powerful thing this API exposes, so it should never be reachable by
    possession of a service credential alone.

    Second, when server.admin_users is set it restricts the change to that
    list. Left empty it permits any authenticated user, which is the
    pre-existing behaviour — defaulting to deny would lock existing
    deployments out of their own skill editor on upgrade.
    """
    _verify_rest_token(authorization, token)
    user = _get_current_user(x_forwarded_access_token, x_forwarded_user)

    configured = get_settings().server.admin_users
    allowed = {u.strip() for u in configured.split(",") if u.strip()}
    if allowed and user not in allowed:
        logger.warning("Rejected skill mutation by non-admin user '%s'", user)
        raise HTTPException(status_code=403, detail="Administrator access required")
    if not allowed:
        logger.warning(
            "Skill mutation by '%s' — PULSE_AGENT_ADMIN_USERS is unset, so any "
            "authenticated user may rewrite the system prompt. Set it in production.",
            user,
        )
    return user


def extract_user_token(headers) -> str | None:
    """Extract user OAuth token from request/websocket headers. Returns None if absent or disabled."""
    from ..config import get_settings

    if not get_settings().agent.token_forwarding:
        return None
    token = headers.get("x-forwarded-access-token") if hasattr(headers, "get") else None
    return token or None


def get_user_token(
    x_forwarded_access_token: str | None = Header(None, alias="X-Forwarded-Access-Token"),
) -> str | None:
    """FastAPI dependency — extracts user OAuth token from proxy header."""
    from ..config import get_settings

    if not get_settings().agent.token_forwarding:
        return None
    return x_forwarded_access_token or None


def _cache_user(token_hash: str, username: str) -> None:
    """Cache a user identity with O(1) LRU eviction."""
    _user_cache[token_hash] = (username, time.time())
    _user_cache.move_to_end(token_hash)
    while len(_user_cache) > _USER_CACHE_MAX:
        _user_cache.popitem(last=False)
