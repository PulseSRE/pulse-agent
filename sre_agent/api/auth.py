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
    """Verify token for REST endpoints -- accepts Bearer header or query param."""
    expected = get_settings().server.ws_token
    if not expected:
        raise HTTPException(status_code=503, detail="Server not configured")
    client_token = ""
    if authorization and authorization.startswith("Bearer "):
        client_token = authorization[7:]
    elif token:
        client_token = token
    if not client_token or not hmac.compare_digest(client_token, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _get_current_user(
    x_forwarded_access_token: str | None = None,
    x_forwarded_user: str | None = None,
) -> str:
    """Extract username from OAuth proxy headers.

    Priority: PULSE_AGENT_DEV_USER > X-Forwarded-User > TokenReview > JWT decode > token hash.
    The OAuth proxy sets X-Forwarded-User with the authenticated username -- this is
    the most reliable source since OpenShift tokens are opaque (sha256~...), not JWTs.
    """
    dev_user = get_settings().agent.dev_user
    if dev_user:
        return dev_user

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
        review = k8s_client.TokenReview(spec=k8s_client.TokenReviewSpec(token=token))
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
        logger.warning("TokenReview API unavailable, using token-derived identity")

    # Final fallback: stable identity derived from token hash.
    # OpenShift tokens are sha256~ format (not JWTs), so we can't decode them.
    fallback_user = f"user-{token_hash[:16]}"
    _cache_user(token_hash, fallback_user)
    return fallback_user


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
