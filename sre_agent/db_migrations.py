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
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (%s, %s, NOW()) "
                "ON CONFLICT (version) DO NOTHING",
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


def _migrate_010_prompt_log(db: Database) -> None:
    """Add prompt_log table for tracking system prompts sent to Claude."""
    from .db_schema import PROMPT_LOG_SCHEMA

    db.executescript(PROMPT_LOG_SCHEMA)


def _migrate_011_routing_decisions(db: Database) -> None:
    """Add routing decision columns to tool_turns for misroute tracking."""
    db.executescript("""
        ALTER TABLE tool_turns ADD COLUMN IF NOT EXISTS routing_skill TEXT;
        ALTER TABLE tool_turns ADD COLUMN IF NOT EXISTS routing_score INTEGER;
        ALTER TABLE tool_turns ADD COLUMN IF NOT EXISTS routing_competing JSONB;
        ALTER TABLE tool_turns ADD COLUMN IF NOT EXISTS routing_used_llm BOOLEAN DEFAULT FALSE;
        CREATE INDEX IF NOT EXISTS idx_tool_turns_routing ON tool_turns(routing_skill) WHERE routing_skill IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_tool_turns_timestamp ON tool_turns(timestamp DESC);
    """)


def _migrate_012_bigint_timestamps(db: Database) -> None:
    """Fix INTEGER timestamp columns that overflow with millisecond epoch values."""
    db.executescript("""
        ALTER TABLE investigations ALTER COLUMN timestamp TYPE BIGINT;
    """)


def _migrate_013_tool_predictions(db: Database) -> None:
    """Add tool_predictions and tool_cooccurrence tables for adaptive tool selection."""
    from .db_schema import TOOL_COOCCURRENCE_SCHEMA, TOOL_PREDICTIONS_SCHEMA

    db.executescript(TOOL_PREDICTIONS_SCHEMA + TOOL_COOCCURRENCE_SCHEMA)


def _migrate_014_skill_selection_log(db: Database) -> None:
    """Add skill_selection_log table for ORCA selector observability."""
    from .db_schema import SKILL_SELECTION_LOG_SCHEMA

    db.executescript(SKILL_SELECTION_LOG_SCHEMA)


def _migrate_015_postmortems(db: Database) -> None:
    """Add postmortems table for auto-generated incident reports."""
    from .db_schema import POSTMORTEMS_SCHEMA

    db.executescript(POSTMORTEMS_SCHEMA)


def _migrate_016_slo_definitions(db: Database) -> None:
    """Add slo_definitions table for SLO/SLI tracking."""
    from .db_schema import SLO_DEFINITIONS_SCHEMA

    db.executescript(SLO_DEFINITIONS_SCHEMA)


def _migrate_017_plan_executions(db: Database) -> None:
    """Add plan_executions table for plan analytics."""
    from .db_schema import PLAN_EXECUTIONS_SCHEMA

    db.executescript(PLAN_EXECUTIONS_SCHEMA)


def _migrate_018_user_events(db: Database) -> None:
    """Add user_events table for session analytics."""
    from .db_schema import USER_EVENTS_SCHEMA

    db.executescript(USER_EVENTS_SCHEMA)


def _migrate_019_agent_views(db: Database) -> None:
    """Add agent view columns: type, status, visibility, trigger, finding, cluster, claim."""
    for col, typ, default in [
        ("view_type", "TEXT", "'custom'"),
        ("status", "TEXT", "'active'"),
        ("trigger_source", "TEXT", "'user'"),
        ("finding_id", "TEXT", None),
        ("cluster_id", "TEXT", "''"),
        ("claimed_by", "TEXT", None),
        ("claimed_at", "TEXT", None),
        ("visibility", "TEXT", "'private'"),
    ]:
        try:
            default_clause = f" DEFAULT {default}" if default else ""
            not_null = " NOT NULL" if default else ""
            db.execute(f"ALTER TABLE views ADD COLUMN {col} {typ}{not_null}{default_clause}")
        except Exception:
            pass
    db.commit()


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
    (10, "prompt_log", _migrate_010_prompt_log),
    (11, "routing_decisions", _migrate_011_routing_decisions),
    (12, "bigint_timestamps", _migrate_012_bigint_timestamps),
    (13, "tool_predictions", _migrate_013_tool_predictions),
    (14, "skill_selection_log", _migrate_014_skill_selection_log),
    (15, "postmortems", _migrate_015_postmortems),
    (16, "slo_definitions", _migrate_016_slo_definitions),
    (17, "plan_executions", _migrate_017_plan_executions),
    (18, "user_events", _migrate_018_user_events),
    (19, "agent_views", _migrate_019_agent_views),
]
