"""Chat history REST endpoints."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from ..chat_history import create_session as create_chat_session
from ..chat_history import delete_session, get_messages, list_sessions, rename_session
from .auth import get_owner

logger = logging.getLogger("pulse_agent.api")

router = APIRouter()


@router.get("/chat/sessions")
async def rest_list_sessions(owner: str = Depends(get_owner), limit: int = Query(50, ge=1, le=200)):
    """List chat sessions for the current user."""
    return {"sessions": list_sessions(owner, limit=limit), "owner": owner}


@router.get("/chat/sessions/{session_id}/messages")
async def rest_get_messages(
    session_id: str,
    owner: str = Depends(get_owner),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Get messages for a chat session with pagination."""
    return get_messages(session_id, owner, limit=limit, offset=offset)


@router.post("/chat/sessions")
async def rest_create_session(request: Request, owner: str = Depends(get_owner)):
    """Create a new chat session."""
    body = await request.json()
    session_id = str(uuid.uuid4())
    title = body.get("title", "New Chat")[:200]
    agent_mode = body.get("agent_mode", "auto")[:20]
    create_chat_session(session_id, owner, mode=agent_mode, title=title)
    return {"id": session_id, "owner": owner}


@router.delete("/chat/sessions/{session_id}")
async def rest_delete_session(session_id: str, owner: str = Depends(get_owner)):
    """Delete a chat session."""
    deleted = delete_session(session_id, owner)
    if not deleted:
        return JSONResponse(status_code=404, content={"error": "Session not found or not owned by you"})
    return {"deleted": True}


@router.put("/chat/sessions/{session_id}")
async def rest_rename_session(session_id: str, request: Request, owner: str = Depends(get_owner)):
    """Rename a chat session."""
    body = await request.json()
    title = body.get("title", "")[:200]
    if not title:
        return JSONResponse(status_code=400, content={"error": "title is required"})
    renamed = rename_session(session_id, owner, title)
    if not renamed:
        return JSONResponse(status_code=404, content={"error": "Session not found or not owned by you"})
    return {"renamed": True}
