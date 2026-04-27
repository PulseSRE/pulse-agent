"""Chat history persistence — fire-and-forget recording of agent conversations."""

from __future__ import annotations

import json
import logging

logger = logging.getLogger("pulse_agent.chat_history")


def _repo():
    from .repositories.chat_history_repo import get_chat_history_repo

    return get_chat_history_repo()


def create_session(session_id: str, owner: str, mode: str = "auto", title: str = "New Chat") -> None:
    """Create a new chat session."""
    try:
        _repo().insert_session(session_id, owner, mode, title)
    except Exception:
        logger.debug("Failed to create chat session", exc_info=True)


def save_message(session_id: str, role: str, content: str, components: list | None = None) -> None:
    """Save a message to a chat session (fire-and-forget)."""
    try:
        repo = _repo()
        components_json = json.dumps(components) if components else None
        repo.insert_message(session_id, role, content[:50000], components_json)
        repo.increment_message_count(session_id, 1)
        repo.commit()
    except Exception:
        logger.debug("Failed to save chat message", exc_info=True)


def save_turn(
    session_id: str,
    user_content: str,
    assistant_content: str,
    components: list | None = None,
    is_first_turn: bool = False,
) -> None:
    """Save both user and assistant messages in a single commit."""
    try:
        repo = _repo()
        repo.insert_message(session_id, "user", user_content[:50000])
        components_json = json.dumps(components) if components else None
        repo.insert_message(session_id, "assistant", assistant_content[:50000], components_json)
        repo.increment_message_count(session_id, 2)
        if is_first_turn:
            title = user_content.strip()[:80]
            if title:
                repo.update_title_if_new(session_id, title)
        repo.commit()
    except Exception:
        logger.debug("Failed to save chat turn", exc_info=True)


def auto_title(session_id: str, first_query: str) -> None:
    """Auto-generate a title from the first user message."""
    try:
        title = first_query.strip()[:80]
        if not title:
            return
        _repo().update_title_if_new(session_id, title)
    except Exception:
        logger.debug("Failed to auto-title chat session", exc_info=True)


def list_sessions(owner: str, limit: int = 50) -> list[dict]:
    """List chat sessions for a user, newest first."""
    try:
        rows = _repo().list_sessions(owner, limit)
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "agent_mode": r["agent_mode"],
                "message_count": r["message_count"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
            for r in rows
        ]
    except Exception:
        logger.debug("Failed to list chat sessions", exc_info=True)
        return []


def get_messages(session_id: str, owner: str, limit: int = 100, offset: int = 0) -> dict:
    """Get messages for a session. Returns {messages: [...], total: int}."""
    try:
        repo = _repo()
        # Verify ownership
        row = repo.fetch_session_owner(session_id, owner)
        if not row:
            return {"messages": [], "total": 0}
        total = repo.count_messages(session_id)
        rows = repo.fetch_messages(session_id, limit, offset)
        messages = []
        for r in rows:
            msg: dict = {
                "role": r["role"],
                "content": r["content"],
                "timestamp": int(r["created_at"].timestamp() * 1000) if r["created_at"] else 0,
            }
            if r["components_json"]:
                try:
                    msg["components"] = json.loads(r["components_json"])
                except Exception:
                    logger.debug("Failed to parse components_json for chat message", exc_info=True)
            messages.append(msg)
        return {"messages": messages, "total": total}
    except Exception:
        logger.debug("Failed to get chat messages", exc_info=True)
        return {"messages": [], "total": 0}


def delete_session(session_id: str, owner: str) -> bool:
    """Delete a chat session (cascades to messages)."""
    try:
        _repo().delete_session(session_id, owner)
        return True
    except Exception:
        logger.debug("Failed to delete chat session", exc_info=True)
        return False


def rename_session(session_id: str, owner: str, title: str) -> bool:
    """Rename a chat session."""
    try:
        _repo().rename_session(session_id, owner, title[:200])
        return True
    except Exception:
        logger.debug("Failed to rename chat session", exc_info=True)
        return False
