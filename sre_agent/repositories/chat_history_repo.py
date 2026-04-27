"""Chat history repository -- all chat_sessions/chat_messages database operations.

Extracted from ``chat_history.py`` to keep domain logic cohesive.  The original
module-level functions in ``chat_history.py`` now delegate here for backward
compatibility.
"""

from __future__ import annotations

import logging

from .base import BaseRepository

logger = logging.getLogger("pulse_agent.chat_history")


class ChatHistoryRepository(BaseRepository):
    """Database operations for chat session persistence."""

    # -- Sessions --------------------------------------------------------------

    def insert_session(self, session_id: str, owner: str, mode: str, title: str) -> None:
        """Create a new chat session."""
        self.db.execute(
            "INSERT INTO chat_sessions (id, owner, agent_mode, title) VALUES (?, ?, ?, ?) ON CONFLICT (id) DO NOTHING",
            (session_id, owner, mode, title),
        )
        self.db.commit()

    def list_sessions(self, owner: str, limit: int) -> list[dict]:
        """List chat sessions for a user, newest first."""
        return self.db.fetchall(
            "SELECT id, title, agent_mode, message_count, created_at, updated_at "
            "FROM chat_sessions WHERE owner = ? ORDER BY updated_at DESC LIMIT ?",
            (owner, limit),
        )

    def fetch_session_owner(self, session_id: str, owner: str) -> dict | None:
        """Verify session ownership."""
        return self.db.fetchone(
            "SELECT id FROM chat_sessions WHERE id = ? AND owner = ?",
            (session_id, owner),
        )

    def delete_session(self, session_id: str, owner: str) -> None:
        """Delete a chat session (cascades to messages)."""
        self.db.execute(
            "DELETE FROM chat_sessions WHERE id = ? AND owner = ?",
            (session_id, owner),
        )
        self.db.commit()

    def rename_session(self, session_id: str, owner: str, title: str) -> None:
        """Rename a chat session."""
        self.db.execute(
            "UPDATE chat_sessions SET title = ?, updated_at = NOW() WHERE id = ? AND owner = ?",
            (title, session_id, owner),
        )
        self.db.commit()

    def update_title_if_new(self, session_id: str, title: str) -> None:
        """Update session title if it is still 'New Chat'."""
        self.db.execute(
            "UPDATE chat_sessions SET title = ? WHERE id = ? AND title = 'New Chat'",
            (title, session_id),
        )
        self.db.commit()

    def increment_message_count(self, session_id: str, count: int = 1) -> None:
        """Increment message_count and update timestamp."""
        self.db.execute(
            f"UPDATE chat_sessions SET message_count = message_count + {count}, updated_at = NOW() WHERE id = ?",
            (session_id,),
        )

    # -- Messages --------------------------------------------------------------

    def insert_message(self, session_id: str, role: str, content: str, components_json: str | None = None) -> None:
        """Insert a single chat message."""
        self.db.execute(
            "INSERT INTO chat_messages (session_id, role, content, components_json) VALUES (?, ?, ?, ?)",
            (session_id, role, content, components_json),
        )

    def count_messages(self, session_id: str) -> int:
        """Count messages in a session."""
        row = self.db.fetchone(
            "SELECT COUNT(*) AS cnt FROM chat_messages WHERE session_id = ?",
            (session_id,),
        )
        return row["cnt"] if row else 0

    def fetch_messages(self, session_id: str, limit: int, offset: int) -> list[dict]:
        """Fetch messages for a session."""
        return self.db.fetchall(
            "SELECT role, content, components_json, created_at FROM chat_messages "
            "WHERE session_id = ? ORDER BY created_at ASC LIMIT ? OFFSET ?",
            (session_id, limit, offset),
        )

    def commit(self) -> None:
        """Commit the current transaction."""
        self.db.commit()


# -- Singleton ---------------------------------------------------------------

_chat_history_repo: ChatHistoryRepository | None = None


def get_chat_history_repo() -> ChatHistoryRepository:
    """Return the module-level ChatHistoryRepository singleton."""
    global _chat_history_repo
    if _chat_history_repo is None:
        _chat_history_repo = ChatHistoryRepository()
    return _chat_history_repo
