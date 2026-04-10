"""Chat history persistence — fire-and-forget recording of agent conversations."""

from __future__ import annotations

import json
import logging

from .db import get_database

logger = logging.getLogger("pulse_agent.chat_history")


def create_session(session_id: str, owner: str, mode: str = "auto", title: str = "New Chat") -> None:
    """Create a new chat session."""
    try:
        db = get_database()
        db.execute(
            "INSERT INTO chat_sessions (id, owner, agent_mode, title) VALUES (?, ?, ?, ?) ON CONFLICT (id) DO NOTHING",
            (session_id, owner, mode, title),
        )
        db.commit()
    except Exception:
        logger.debug("Failed to create chat session", exc_info=True)


def save_message(session_id: str, role: str, content: str, components: list | None = None) -> None:
    """Save a message to a chat session (fire-and-forget)."""
    try:
        db = get_database()
        components_json = json.dumps(components) if components else None
        db.execute(
            "INSERT INTO chat_messages (session_id, role, content, components_json) VALUES (?, ?, ?, ?)",
            (session_id, role, content[:50000], components_json),
        )
        db.execute(
            "UPDATE chat_sessions SET message_count = message_count + 1, updated_at = NOW() WHERE id = ?",
            (session_id,),
        )
        db.commit()
    except Exception:
        logger.debug("Failed to save chat message", exc_info=True)


def auto_title(session_id: str, first_query: str) -> None:
    """Auto-generate a title from the first user message."""
    try:
        title = first_query.strip()[:80]
        if not title:
            return
        db = get_database()
        db.execute(
            "UPDATE chat_sessions SET title = ? WHERE id = ? AND title = 'New Chat'",
            (title, session_id),
        )
        db.commit()
    except Exception:
        logger.debug("Failed to auto-title chat session", exc_info=True)


def list_sessions(owner: str, limit: int = 50) -> list[dict]:
    """List chat sessions for a user, newest first."""
    try:
        db = get_database()
        rows = db.fetchall(
            "SELECT id, title, agent_mode, message_count, created_at, updated_at "
            "FROM chat_sessions WHERE owner = ? ORDER BY updated_at DESC LIMIT ?",
            (owner, limit),
        )
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
        db = get_database()
        # Verify ownership
        row = db.fetchone("SELECT id FROM chat_sessions WHERE id = ? AND owner = ?", (session_id, owner))
        if not row:
            return {"messages": [], "total": 0}
        total_row = db.fetchone("SELECT COUNT(*) AS cnt FROM chat_messages WHERE session_id = ?", (session_id,))
        total = total_row["cnt"] if total_row else 0
        rows = db.fetchall(
            "SELECT role, content, components_json, created_at FROM chat_messages "
            "WHERE session_id = ? ORDER BY created_at ASC LIMIT ? OFFSET ?",
            (session_id, limit, offset),
        )
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
                    pass
            messages.append(msg)
        return {"messages": messages, "total": total}
    except Exception:
        logger.debug("Failed to get chat messages", exc_info=True)
        return {"messages": [], "total": 0}


def delete_session(session_id: str, owner: str) -> bool:
    """Delete a chat session (cascades to messages)."""
    try:
        db = get_database()
        db.execute("DELETE FROM chat_sessions WHERE id = ? AND owner = ?", (session_id, owner))
        db.commit()
        return True
    except Exception:
        logger.debug("Failed to delete chat session", exc_info=True)
        return False


def rename_session(session_id: str, owner: str, title: str) -> bool:
    """Rename a chat session."""
    try:
        db = get_database()
        db.execute(
            "UPDATE chat_sessions SET title = ?, updated_at = NOW() WHERE id = ? AND owner = ?",
            (title[:200], session_id, owner),
        )
        db.commit()
        return True
    except Exception:
        logger.debug("Failed to rename chat session", exc_info=True)
        return False
