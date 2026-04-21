"""View CRUD and sharing REST endpoints."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import re as _re
import time
import uuid

from fastapi import APIRouter, Depends, Query, Request

from ..config import get_settings
from .auth import get_owner, verify_token

logger = logging.getLogger("pulse_agent.api")

_NESTED_QUANTIFIER_RE = _re.compile(r"[+*]\)*[+*]")


def _validate_regex_pattern(pattern: str) -> str | None:
    """Return error message if pattern is unsafe, None if safe."""
    if len(pattern) > 100:
        return "Pattern too long (max 100 characters)"
    if _NESTED_QUANTIFIER_RE.search(pattern):
        return "Pattern contains nested quantifiers (ReDoS risk)"
    try:
        _re.compile(pattern)
    except _re.error as e:
        return f"Invalid regex: {e}"
    return None


router = APIRouter()


@router.get("/views")
async def rest_list_views(
    owner: str = Depends(get_owner),
    view_type: str | None = Query(None),
    visibility: str | None = Query(None),
    exclude_status: str | None = Query(None),
):
    """List views with optional filtering by type, visibility, and status."""
    from .. import db

    views = db.list_views(owner, view_type=view_type, visibility=visibility, exclude_status=exclude_status)
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

    # Snapshot the view at share time
    snapshot_version = db.snapshot_view(view_id, action="shared")
    if snapshot_version is None:
        return JSONResponse(status_code=500, content={"error": "Failed to create share snapshot"})

    secret = os.environ.get("PULSE_SHARE_TOKEN_KEY", "") or get_settings().ws_token
    if not secret:
        return JSONResponse(status_code=503, content={"error": "Server not configured for sharing"})
    expires = int(time.time()) + 86400  # 24 hours
    payload = f"{view_id}:{expires}:{snapshot_version}"
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

    # Verify share token -- supports both old (3-part) and new (4-part) tokens
    # Old format: view_id:expires:signature
    # New format: view_id:expires:snapshot_version:signature
    parts = share_token.split(":")
    if len(parts) == 4:
        view_id, expires_str, snapshot_version_str, signature = parts
        snapshot_version = int(snapshot_version_str)
        sig_payload = f"{view_id}:{expires_str}:{snapshot_version_str}"
    elif len(parts) == 3:
        view_id, expires_str, signature = parts
        snapshot_version = None
        sig_payload = f"{view_id}:{expires_str}"
    else:
        return JSONResponse(status_code=400, content={"error": "Invalid share token"})

    try:
        expires = int(expires_str)
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "Invalid share token"})

    if int(time.time()) > expires:
        return JSONResponse(status_code=410, content={"error": "Share link has expired"})

    secret = os.environ.get("PULSE_SHARE_TOKEN_KEY", "") or get_settings().ws_token
    if not secret:
        return JSONResponse(status_code=503, content={"error": "Server not configured"})
    expected_sig = hmac.new(secret.encode(), sig_payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected_sig):
        return JSONResponse(status_code=400, content={"error": "Invalid share token"})

    # Clone from the snapshot version if available
    if snapshot_version is not None:
        new_id = db.clone_view_at_version(view_id, owner, snapshot_version)
    else:
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


# ---------------------------------------------------------------------------
# Log Counts -- enrichment endpoint for live table log datasources
# ---------------------------------------------------------------------------


@router.get("/log-counts")
async def rest_log_counts(
    namespace: str = Query(..., description="K8s namespace"),
    pattern: str = Query("error|Error|ERROR", description="Grep pattern to count"),
    label_selector: str = Query("", alias="labelSelector", description="Pod label selector"),
    tail_lines: int = Query(100, alias="tailLines", ge=10, le=1000, description="Log lines to scan per pod"),
    _auth=Depends(verify_token),
):
    """Count log lines matching a pattern per pod.  Used by live table log enrichment.

    Returns ``{"counts": {"pod-name": N, ...}}`` with one entry per pod.
    Capped at 20 pods to avoid API overload.
    """
    import asyncio
    import re

    from fastapi.responses import JSONResponse

    from ..k8s_client import get_core_client, safe
    from ..k8s_tools.validators import _validate_k8s_namespace

    ns_err = _validate_k8s_namespace(namespace)
    if ns_err:
        return JSONResponse(status_code=400, content={"error": ns_err})

    pattern_err = _validate_regex_pattern(pattern)
    if pattern_err:
        return JSONResponse(status_code=400, content={"error": pattern_err})

    core = get_core_client()

    pods_result = safe(
        lambda: core.list_namespaced_pod(
            namespace,
            label_selector=label_selector or "",
            limit=20,
        )
    )
    if isinstance(pods_result, str):
        return {"counts": {}, "error": pods_result}

    compiled = re.compile(pattern) if pattern else None

    async def _count_pod(pod_name: str) -> tuple[str, int]:
        try:
            logs = await asyncio.to_thread(core.read_namespaced_pod_log, pod_name, namespace, tail_lines=tail_lines)
            if isinstance(logs, str) and compiled:
                return (pod_name, len(compiled.findall(logs)))
            return (pod_name, 0)
        except Exception:
            return (pod_name, 0)

    pod_names = [p.metadata.name for p in pods_result.items if p.metadata and p.metadata.name]
    results = await asyncio.gather(*[_count_pod(n) for n in pod_names])
    return {"counts": dict(results)}


# ---------------------------------------------------------------------------
# Action Execution — execute a tool from a view's action_button component
# ---------------------------------------------------------------------------


@router.post("/views/{view_id}/actions")
async def rest_execute_action(
    view_id: str,
    request: Request,
    owner: str = Depends(get_owner),
):
    """Execute a tool action from a view's action_button component."""
    import asyncio

    from fastapi.responses import JSONResponse

    from .. import db
    from ..tool_registry import TOOL_REGISTRY, WRITE_TOOL_NAMES
    from .sanitize import ACTION_BLOCKED_TOOLS, validate_action_input

    body = await request.json()
    action = body.get("action", "")
    action_input = body.get("action_input", {})

    if not action:
        return JSONResponse(status_code=400, content={"error": "Missing 'action' field"})
    if not isinstance(action_input, dict):
        return JSONResponse(status_code=400, content={"error": "'action_input' must be a dict"})

    if action in ACTION_BLOCKED_TOOLS:
        return JSONResponse(status_code=403, content={"error": f"Tool '{action}' is not allowed via action buttons"})
    if action not in TOOL_REGISTRY:
        return JSONResponse(status_code=400, content={"error": f"Tool '{action}' not found"})

    settings = get_settings()
    if action in WRITE_TOOL_NAMES and settings.max_trust_level < 1:
        return JSONResponse(status_code=403, content={"error": "Write operations disabled (trust level 0)"})

    view = db.get_view(view_id, owner)
    if view is None:
        return JSONResponse(status_code=404, content={"error": "View not found or not owned by you"})

    from ..agent import _circuit_breaker

    if _circuit_breaker.is_open:
        return JSONResponse(
            status_code=503, content={"error": "Service temporarily unavailable (circuit breaker open)"}
        )

    input_err = validate_action_input(action_input)
    if input_err:
        return JSONResponse(status_code=400, content={"error": input_err})

    from ..agent import _execute_tool

    tool_map = {action: TOOL_REGISTRY[action]}
    text, component, meta = await asyncio.to_thread(_execute_tool, action, action_input, tool_map)

    try:
        from ..tool_usage import record_tool_call

        record_tool_call(
            session_id=f"view-action-{view_id}",
            turn_number=0,
            agent_mode="view_action",
            tool_name=action,
            tool_category=None,
            input_data=action_input,
            status=meta.get("status", "error"),
            error_message=meta.get("error_message"),
            error_category=meta.get("error_category"),
            duration_ms=0,
            result_bytes=meta.get("result_bytes", 0),
            requires_confirmation=action in WRITE_TOOL_NAMES,
            was_confirmed=True,
        )
    except Exception:
        pass

    status_code = 200 if meta.get("status") == "success" else 500
    return JSONResponse(
        status_code=status_code,
        content={
            "result": text,
            "component": component,
            "status": meta.get("status", "error"),
            "error_message": meta.get("error_message"),
        },
    )


# ---------------------------------------------------------------------------
# View Lifecycle — status transitions and claims
# ---------------------------------------------------------------------------


@router.post("/views/{view_id}/status")
async def rest_transition_status(
    view_id: str,
    request: Request,
    owner: str = Depends(get_owner),
):
    """Transition a view's status."""
    from fastapi.responses import JSONResponse

    from .. import db

    body = await request.json()
    new_status = body.get("status")
    if not new_status:
        return JSONResponse(status_code=400, content={"error": "Missing 'status' field"})

    ok = db.transition_view_status(view_id, owner, new_status)
    if not ok:
        return JSONResponse(status_code=409, content={"error": "Invalid status transition"})
    return {"transitioned": True, "view_id": view_id, "status": new_status}


@router.post("/views/{view_id}/claim")
async def rest_claim_view(
    view_id: str,
    owner: str = Depends(get_owner),
):
    """Claim a team view."""
    from fastapi.responses import JSONResponse

    from .. import db

    ok = db.claim_view(view_id, owner)
    if not ok:
        return JSONResponse(status_code=409, content={"error": "Cannot claim this view"})
    return {"claimed": True, "view_id": view_id, "claimed_by": owner}


@router.delete("/views/{view_id}/claim")
async def rest_unclaim_view(
    view_id: str,
    owner: str = Depends(get_owner),
):
    """Release a claim on a view."""
    from fastapi.responses import JSONResponse

    from .. import db

    ok = db.unclaim_view(view_id, owner)
    if not ok:
        return JSONResponse(status_code=409, content={"error": "Cannot unclaim — you don't hold the claim"})
    return {"unclaimed": True, "view_id": view_id}
