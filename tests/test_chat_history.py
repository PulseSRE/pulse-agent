"""Tests for chat history persistence — session CRUD, message saving, auto-title."""

from __future__ import annotations

from sre_agent.chat_history import (
    auto_title,
    create_session,
    delete_session,
    get_messages,
    list_sessions,
    rename_session,
    save_message,
)
from sre_agent.db import Database, reset_database, set_database
from sre_agent.db_migrations import run_migrations

from .conftest import _TEST_DB_URL


def _fresh_db() -> Database:
    """Create a clean test database with chat tables."""
    db = Database(_TEST_DB_URL)
    db.execute("DROP TABLE IF EXISTS chat_messages CASCADE")
    db.execute("DROP TABLE IF EXISTS chat_sessions CASCADE")
    db.execute("DELETE FROM schema_migrations WHERE version >= 7")
    db.commit()
    set_database(db)
    run_migrations(db)
    return db


class TestCreateSession:
    def test_create_session(self):
        _fresh_db()
        try:
            create_session("s1", "alice", "sre", "My Session")
            sessions = list_sessions("alice")
            assert len(sessions) == 1
            assert sessions[0]["id"] == "s1"
            assert sessions[0]["title"] == "My Session"
            assert sessions[0]["agent_mode"] == "sre"
            assert sessions[0]["message_count"] == 0
        finally:
            reset_database()


class TestSaveAndGetMessages:
    def test_save_and_get_messages(self):
        _fresh_db()
        try:
            create_session("s2", "bob", "auto")
            save_message("s2", "user", "Hello agent")
            save_message("s2", "assistant", "Hello! How can I help?", [{"type": "text", "content": "hi"}])
            save_message("s2", "user", "Show me pods")

            result = get_messages("s2", "bob")
            assert result["total"] == 3
            assert len(result["messages"]) == 3
            assert result["messages"][0]["role"] == "user"
            assert result["messages"][0]["content"] == "Hello agent"
            assert result["messages"][1]["role"] == "assistant"
            assert result["messages"][1]["components"] == [{"type": "text", "content": "hi"}]
            assert result["messages"][2]["role"] == "user"
            assert result["messages"][2]["content"] == "Show me pods"

            # Verify message_count updated
            sessions = list_sessions("bob")
            assert sessions[0]["message_count"] == 3

            # Verify ownership check — different user gets nothing
            result2 = get_messages("s2", "eve")
            assert result2["total"] == 0
            assert result2["messages"] == []

            # Verify pagination
            result3 = get_messages("s2", "bob", limit=2, offset=1)
            assert len(result3["messages"]) == 2
            assert result3["messages"][0]["role"] == "assistant"
        finally:
            reset_database()


class TestListSessions:
    def test_list_sessions(self):
        _fresh_db()
        try:
            create_session("s3", "carol", "sre", "Session A")
            create_session("s4", "carol", "auto", "Session B")
            create_session("s5", "dave", "security", "Dave's session")

            carol_sessions = list_sessions("carol")
            assert len(carol_sessions) == 2

            dave_sessions = list_sessions("dave")
            assert len(dave_sessions) == 1
            assert dave_sessions[0]["title"] == "Dave's session"

            # Verify limit
            limited = list_sessions("carol", limit=1)
            assert len(limited) == 1
        finally:
            reset_database()


class TestDeleteSession:
    def test_delete_session(self):
        _fresh_db()
        try:
            create_session("s6", "eve", "sre", "To Delete")
            save_message("s6", "user", "test message")

            sessions_before = list_sessions("eve")
            assert len(sessions_before) == 1

            # Delete by wrong owner — row still exists
            delete_session("s6", "mallory")
            sessions_still = list_sessions("eve")
            assert len(sessions_still) == 1

            # Delete by correct owner
            result = delete_session("s6", "eve")
            assert result is True
            sessions_after = list_sessions("eve")
            assert len(sessions_after) == 0

            # Messages should be cascade-deleted
            msgs = get_messages("s6", "eve")
            assert msgs["total"] == 0
        finally:
            reset_database()


class TestAutoTitle:
    def test_auto_title(self):
        _fresh_db()
        try:
            create_session("s7", "frank", "auto")
            sessions = list_sessions("frank")
            assert sessions[0]["title"] == "New Chat"

            auto_title("s7", "Show me crashlooping pods in production")
            sessions = list_sessions("frank")
            assert sessions[0]["title"] == "Show me crashlooping pods in production"

            # Should not overwrite if title already changed
            auto_title("s7", "Something else entirely")
            sessions = list_sessions("frank")
            assert sessions[0]["title"] == "Show me crashlooping pods in production"
        finally:
            reset_database()


class TestRenameSession:
    def test_rename_session(self):
        _fresh_db()
        try:
            create_session("s8", "grace", "sre", "Original")
            rename_session("s8", "grace", "Renamed")
            sessions = list_sessions("grace")
            assert sessions[0]["title"] == "Renamed"

            # Wrong owner — title unchanged
            rename_session("s8", "mallory", "Hacked")
            sessions2 = list_sessions("grace")
            assert sessions2[0]["title"] == "Renamed"
        finally:
            reset_database()
