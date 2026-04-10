"""Chat history REST endpoints."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse

from ..chat_history import create_session as create_chat_session
from ..chat_history import delete_session, get_messages, list_sessions, rename_session
from .auth import _get_current_user, _verify_rest_token

logger = logging.getLogger("pulse_agent.api")

router = APIRouter()


@router.get("/chat/sessions")
async def rest_list_sessions(
    authorization: str | None = Header(None),
    token: str | None = Query(None),
    x_forwarded_access_token: str | None = Header(None, alias="X-Forwarded-Access-Token"),
    x_forwarded_user: str | None = Header(None, alias="X-Forwarded-User"),
    limit: int = Query(50, ge=1, le=200),
):
    """List chat sessions for the current user."""
    _verify_rest_token(authorization, token)
    owner = _get_current_user(x_forwarded_access_token, x_forwarded_user)
    sessions = list_sessions(owner, limit=limit)
    return {"sessions": sessions, "owner": owner}


@router.get("/chat/sessions/{session_id}/messages")
async def rest_get_messages(
    session_id: str,
    authorization: str | None = Header(None),
    token: str | None = Query(None),
    x_forwarded_access_token: str | None = Header(None, alias="X-Forwarded-Access-Token"),
    x_forwarded_user: str | None = Header(None, alias="X-Forwarded-User"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Get messages for a chat session with pagination."""
    _verify_rest_token(authorization, token)
    owner = _get_current_user(x_forwarded_access_token, x_forwarded_user)
    result = get_messages(session_id, owner, limit=limit, offset=offset)
    return result


@router.post("/chat/sessions")
async def rest_create_session(
    request: Request,
    authorization: str | None = Header(None),
    token: str | None = Query(None),
    x_forwarded_access_token: str | None = Header(None, alias="X-Forwarded-Access-Token"),
    x_forwarded_user: str | None = Header(None, alias="X-Forwarded-User"),
):
    """Create a new chat session."""
    _verify_rest_token(authorization, token)
    owner = _get_current_user(x_forwarded_access_token, x_forwarded_user)
    body = await request.json()

    session_id = str(uuid.uuid4())
    title = str(body.get("title", "New Chat"))[:200]
    agent_mode = str(body.get("agent_mode", "auto"))[:20]

    create_chat_session(session_id, owner, mode=agent_mode, title=title)
    return {"id": session_id, "owner": owner}


@router.delete("/chat/sessions/{session_id}")
async def rest_delete_session(
    session_id: str,
    authorization: str | None = Header(None),
    token: str | None = Query(None),
    x_forwarded_access_token: str | None = Header(None, alias="X-Forwarded-Access-Token"),
    x_forwarded_user: str | None = Header(None, alias="X-Forwarded-User"),
):
    """Delete a chat session."""
    _verify_rest_token(authorization, token)
    owner = _get_current_user(x_forwarded_access_token, x_forwarded_user)
    deleted = delete_session(session_id, owner)
    if not deleted:
        return JSONResponse(status_code=404, content={"error": "Session not found or not owned by you"})
    return {"deleted": True}


@router.put("/chat/sessions/{session_id}")
async def rest_rename_session(
    session_id: str,
    request: Request,
    authorization: str | None = Header(None),
    token: str | None = Query(None),
    x_forwarded_access_token: str | None = Header(None, alias="X-Forwarded-Access-Token"),
    x_forwarded_user: str | None = Header(None, alias="X-Forwarded-User"),
):
    """Rename a chat session."""
    _verify_rest_token(authorization, token)
    owner = _get_current_user(x_forwarded_access_token, x_forwarded_user)
    body = await request.json()
    title = str(body.get("title", ""))[:200]
    if not title:
        return JSONResponse(status_code=400, content={"error": "title is required"})
    renamed = rename_session(session_id, owner, title)
    if not renamed:
        return JSONResponse(status_code=404, content={"error": "Session not found or not owned by you"})
    return {"renamed": True}
