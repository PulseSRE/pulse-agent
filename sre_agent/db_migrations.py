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


MIGRATIONS = [
    (1, "baseline", _migrate_001_baseline),
    (2, "tool_usage", _migrate_002_tool_usage),
    (3, "promql_queries", _migrate_003_promql_queries),
]
