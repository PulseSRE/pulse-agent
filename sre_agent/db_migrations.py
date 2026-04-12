"""Lightweight schema migration system for Pulse Agent."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .db import Database

logger = logging.getLogger("pulse_agent.db")


def run_migrations(db: Database) -> None:
    """Apply pending migrations in order."""
    # Create migrations tracking table
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    db.commit()

    # Get current version
    row = db.fetchone("SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations")
    current = row["v"] if row else 0

    for version, name, fn in MIGRATIONS:
        if version <= current:
            continue
        logger.info("Applying migration %d: %s", version, name)
        try:
            fn(db)
            db.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (%s, %s, NOW())",
                (version, name),
            )
            db.commit()
        except Exception:
            logger.exception("Migration %d failed: %s", version, name)
            raise


def _migrate_001_baseline(db: Database) -> None:
    """Initial schema -- create all tables if they don't exist."""
    from .db_schema import ALL_SCHEMAS

    db.executescript(ALL_SCHEMAS)


def _migrate_002_tool_usage(db: Database) -> None:
    """Add tool_usage and tool_turns tables for tool call tracking."""
    from .db_schema import TOOL_TURNS_SCHEMA, TOOL_USAGE_INDEX_SCHEMA, TOOL_USAGE_SCHEMA

    db.executescript(TOOL_USAGE_SCHEMA + TOOL_TURNS_SCHEMA + TOOL_USAGE_INDEX_SCHEMA)


def _migrate_003_promql_queries(db: Database) -> None:
    """Add promql_queries table for tracking query success/failure rates."""
    from .db_schema import PROMQL_QUERIES_SCHEMA

    db.executescript(PROMQL_QUERIES_SCHEMA)


def _migrate_004_token_tracking(db: Database) -> None:
    """Add token usage columns to tool_turns."""
    for col in ["input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"]:
        try:
            db.execute(f"ALTER TABLE tool_turns ADD COLUMN {col} INTEGER")
            db.commit()
        except Exception:
            pass  # Column may already exist (execute rolls back on error)


def _migrate_005_scan_runs(db: Database) -> None:
    """Add scan_runs table for scan history tracking."""
    from .db_schema import SCAN_RUNS_SCHEMA

    db.executescript(SCAN_RUNS_SCHEMA)


def _migrate_006_eval_runs(db: Database) -> None:
    """Add eval_runs table for tracking eval scores over time."""
    from .db_schema import EVAL_RUNS_SCHEMA

    db.executescript(EVAL_RUNS_SCHEMA)


def _migrate_007_chat_history(db: Database) -> None:
    """Add chat_sessions and chat_messages tables for chat history persistence."""
    from .db_schema import CHAT_MESSAGES_SCHEMA, CHAT_SESSIONS_SCHEMA

    db.executescript(CHAT_SESSIONS_SCHEMA + CHAT_MESSAGES_SCHEMA)


def _migrate_008_skill_usage(db: Database) -> None:
    """Add skill_usage table for skill analytics and transparency."""
    from .db_schema import SKILL_USAGE_SCHEMA

    db.executescript(SKILL_USAGE_SCHEMA)


def _migrate_009_tool_source(db: Database) -> None:
    """Add tool_source column to track native vs MCP tool calls."""
    db.executescript("""
        ALTER TABLE tool_usage ADD COLUMN IF NOT EXISTS tool_source TEXT DEFAULT 'native';
        CREATE INDEX IF NOT EXISTS idx_tool_usage_source ON tool_usage(tool_source);
    """)


MIGRATIONS = [
    (1, "baseline", _migrate_001_baseline),
    (2, "tool_usage", _migrate_002_tool_usage),
    (3, "promql_queries", _migrate_003_promql_queries),
    (4, "token_tracking", _migrate_004_token_tracking),
    (5, "scan_runs", _migrate_005_scan_runs),
    (6, "eval_runs", _migrate_006_eval_runs),
    (7, "chat_history", _migrate_007_chat_history),
    (8, "skill_usage", _migrate_008_skill_usage),
    (9, "tool_source", _migrate_009_tool_source),
]
