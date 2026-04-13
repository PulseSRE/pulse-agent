"""View CRUD and sharing REST endpoints."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import time
import uuid

from fastapi import APIRouter, Depends, Query, Request

from ..config import get_settings
from .auth import get_owner, verify_token

logger = logging.getLogger("pulse_agent.api")

router = APIRouter()


@router.get("/views")
async def rest_list_views(owner: str = Depends(get_owner)):
    """List all views for the current user."""
    from .. import db

    views = db.list_views(owner)
    return {"views": views or [], "owner": owner}


@router.get("/views/{view_id}")
async def rest_get_view(
    view_id: str,
    owner: str = Depends(get_owner),
):
    """Get a single view by ID."""
    from fastapi.responses import JSONResponse

    from .. import db

    view = db.get_view(view_id, owner)
    if view is None:
        return JSONResponse(status_code=404, content={"error": "View not found"})
    return view


@router.post("/views")
async def rest_create_view(
    request: Request,
    owner: str = Depends(get_owner),
):
    """Save a new view for the current user."""
    from fastapi.responses import JSONResponse

    from .. import db

    body = await request.json()

    view_id = body.get("id", f"cv-{uuid.uuid4().hex[:12]}")
    if not re.match(r"^[a-zA-Z0-9_-]{1,64}$", view_id):
        return JSONResponse(status_code=400, content={"error": "view id must be alphanumeric/hyphens, max 64 chars"})
    title = str(body.get("title", "Untitled View"))[:200]
    description = str(body.get("description", ""))[:1000]
    layout = body.get("layout", [])
    positions = body.get("positions", {})
    icon = str(body.get("icon", ""))[:50]

    if not layout:
        return JSONResponse(status_code=400, content={"error": "layout is required"})
    if not isinstance(layout, list) or len(layout) > 50:
        return JSONResponse(status_code=400, content={"error": "layout must be a list with at most 50 widgets"})
    # Reject payloads over 1MB
    import json as _json

    if len(_json.dumps(layout)) > 1_000_000:
        return JSONResponse(status_code=400, content={"error": "layout payload too large (max 1MB)"})

    result = db.save_view(owner, view_id, title, description, layout, positions, icon)
    if result is None:
        return JSONResponse(status_code=500, content={"error": "Failed to save view"})
    return {"id": result, "owner": owner}


@router.put("/views/{view_id}")
async def rest_update_view(
    view_id: str,
    request: Request,
    owner: str = Depends(get_owner),
):
    """Update a view (title, description, layout, positions). Owner only."""
    from fastapi.responses import JSONResponse

    from .. import db

    body = await request.json()

    # Extract only allowed fields -- never pass raw body as **kwargs
    updates = {}
    for key in ("title", "description", "icon", "layout", "positions"):
        if key in body:
            updates[key] = body[key]

    # Create version snapshot only when explicitly requested (save=true in body)
    if body.get("save"):
        updates["_snapshot"] = True
        updates["_action"] = body.get("action", "save")

    result = db.update_view(view_id, owner, **updates)
    if not result:
        return JSONResponse(status_code=404, content={"error": "View not found or not owned by you"})
    return {"updated": True}


@router.delete("/views/{view_id}")
async def rest_delete_view(
    view_id: str,
    owner: str = Depends(get_owner),
):
    """Delete a view. Owner only."""
    from fastapi.responses import JSONResponse

    from .. import db

    deleted = db.delete_view(view_id, owner)
    if not deleted:
        return JSONResponse(status_code=404, content={"error": "View not found or not owned by you"})
    return {"deleted": True}


@router.post("/views/{view_id}/clone")
async def rest_clone_view(
    view_id: str,
    request: Request,
    owner: str = Depends(get_owner),
):
    """Clone a view to the current user's account. Only the owner can clone their own views."""
    from fastapi.responses import JSONResponse

    from .. import db

    # Verify the caller owns the source view
    source = db.get_view(view_id, owner)
    if source is None:
        return JSONResponse(status_code=404, content={"error": "View not found or not owned by you"})
    new_id = db.clone_view(view_id, owner)
    if new_id is None:
        return JSONResponse(status_code=500, content={"error": "Clone failed"})
    return {"id": new_id, "owner": owner}


@router.post("/views/{view_id}/share")
async def rest_share_view(
    view_id: str,
    owner: str = Depends(get_owner),
):
    """Generate a share link for a view. The link allows others to clone it."""
    from fastapi.responses import JSONResponse

    from .. import db

    view = db.get_view(view_id, owner)
    if view is None:
        return JSONResponse(status_code=404, content={"error": "View not found or not owned by you"})

    secret = os.environ.get("PULSE_SHARE_TOKEN_KEY", "") or get_settings().ws_token
    if not secret:
        return JSONResponse(status_code=503, content={"error": "Server not configured for sharing"})
    expires = int(time.time()) + 86400  # 24 hours
    payload = f"{view_id}:{expires}"
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    share_token = f"{payload}:{signature}"

    return {"share_token": share_token, "view_id": view_id, "expires_in": 86400}


@router.post("/views/claim/{share_token:path}")
async def rest_claim_shared_view(
    share_token: str,
    owner: str = Depends(get_owner),
):
    """Claim a shared view using a share token. Clones the view to your account."""
    from fastapi.responses import JSONResponse

    from .. import db

    # Verify share token -- format is view_id:expires:full_hmac_sha256
    # The signature covers view_id:expires using the server's WS token as secret
    parts = share_token.split(":")
    if len(parts) != 3:
        return JSONResponse(status_code=400, content={"error": "Invalid share token"})

    view_id, expires_str, signature = parts
    try:
        expires = int(expires_str)
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "Invalid share token"})

    if int(time.time()) > expires:
        return JSONResponse(status_code=410, content={"error": "Share link has expired"})

    secret = get_settings().ws_token
    if not secret:
        return JSONResponse(status_code=503, content={"error": "Server not configured"})
    expected_sig = hmac.new(secret.encode(), f"{view_id}:{expires_str}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected_sig):
        return JSONResponse(status_code=400, content={"error": "Invalid share token"})
    new_id = db.clone_view(view_id, owner)
    if new_id is None:
        return JSONResponse(status_code=404, content={"error": "Source view not found"})
    return {"id": new_id, "owner": owner}


# ---------------------------------------------------------------------------
# View Version History
# ---------------------------------------------------------------------------


@router.get("/views/{view_id}/versions")
async def rest_view_versions(
    view_id: str,
    owner: str = Depends(get_owner),
):
    """List version history for a view."""
    from .. import db

    # Verify ownership
    view = db.get_view(view_id, owner)
    if not view:
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=404, content={"error": "View not found"})
    versions = db.list_view_versions(view_id) or []
    return {"versions": versions, "view_id": view_id}


@router.post("/views/{view_id}/undo")
async def rest_undo_view(
    view_id: str,
    request: Request,
    owner: str = Depends(get_owner),
):
    """Undo the last change to a view (restore previous version)."""
    from fastapi.responses import JSONResponse

    from .. import db

    body = await request.json()
    version = body.get("version")

    if version is not None:
        # Restore specific version
        result = db.restore_view_version(view_id, owner, int(version))
    else:
        # Undo last change -- find the latest version and restore it
        versions = db.list_view_versions(view_id, limit=1)
        if not versions:
            return JSONResponse(status_code=404, content={"error": "No version history available"})
        result = db.restore_view_version(view_id, owner, versions[0]["version"])

    if not result:
        return JSONResponse(status_code=404, content={"error": "Version not found or access denied"})
    return {"undone": True, "view_id": view_id}


# ---------------------------------------------------------------------------
# Live Query Refresh -- lightweight Prometheus proxy for view widgets
# ---------------------------------------------------------------------------


@router.get("/query")
async def rest_query(
    q: str = Query(..., description="PromQL query string"),
    time_range: str = Query("", alias="range", description="Time range, e.g. '1h', '24h'"),
    _auth=Depends(verify_token),
):
    """Execute a PromQL query and return a ComponentSpec for live widget refresh.

    No Claude/LLM involved -- direct Prometheus proxy.
    """

    from ..k8s_tools import get_prometheus_query

    result = get_prometheus_query(query=q, time_range=time_range)

    if isinstance(result, tuple) and len(result) == 2:
        _text_result, component = result
        if component:
            return {"component": component}
        return {"component": None, "text": _text_result}
    return {"component": None, "text": str(result)}
