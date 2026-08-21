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
        except Exception as e:
            logger.debug("migration statement skipped (likely already applied): %s", e)


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
    """Add agent view columns: type, status, visibility, trigger, finding, cluster, claim.

    Uses ``ADD COLUMN IF NOT EXISTS`` (idempotent at the SQL level) rather than
    per-statement try/except: the previous per-column try/except swallowed
    DuplicateColumn errors, but since none of these statements committed until
    after the whole loop, a failure on any later column rolled back the
    earlier, still-uncommitted successful columns in the same transaction --
    silently dropping them on a partial re-run (e.g. after an interrupted
    migration).
    """
    db.executescript("""
        ALTER TABLE views ADD COLUMN IF NOT EXISTS view_type TEXT NOT NULL DEFAULT 'custom';
        ALTER TABLE views ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';
        ALTER TABLE views ADD COLUMN IF NOT EXISTS trigger_source TEXT NOT NULL DEFAULT 'user';
        ALTER TABLE views ADD COLUMN IF NOT EXISTS finding_id TEXT;
        ALTER TABLE views ADD COLUMN IF NOT EXISTS cluster_id TEXT NOT NULL DEFAULT '';
        ALTER TABLE views ADD COLUMN IF NOT EXISTS claimed_by TEXT;
        ALTER TABLE views ADD COLUMN IF NOT EXISTS claimed_at TEXT;
        ALTER TABLE views ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'private';
    """)


def _migrate_020_action_outcomes(db: Database) -> None:
    """Add outcome tracking to actions table for fix success rate metrics."""
    db.executescript("""
        ALTER TABLE actions ADD COLUMN IF NOT EXISTS outcome TEXT NOT NULL DEFAULT 'unknown';
        CREATE INDEX IF NOT EXISTS idx_actions_finding_id ON actions (finding_id);
    """)


def _migrate_021_inbox_items(db: Database) -> None:
    """Create inbox_items table for unified SRE worklist."""
    from .db_schema import INBOX_ITEMS_SCHEMA

    db.executescript(INBOX_ITEMS_SCHEMA)


def _migrate_022_user_interactions(db: Database) -> None:
    """Create user_interactions table for HITL audit trail."""
    from .db_schema import USER_INTERACTIONS_SCHEMA

    db.executescript(USER_INTERACTIONS_SCHEMA)


def _migrate_023_operational_flags(db: Database) -> None:
    """Create operational_flags table so the auto-fix kill switch survives restarts."""
    from .db_schema import OPERATIONAL_FLAGS_SCHEMA

    db.executescript(OPERATIONAL_FLAGS_SCHEMA)


def _migrate_024_inbox_mutes(db: Database) -> None:
    """Create inbox_mutes so operators can silence a known-noisy condition."""
    from .db_schema import INBOX_MUTES_SCHEMA

    db.executescript(INBOX_MUTES_SCHEMA)


# Open statuses, spelled the same way the inbox module does.
_OPEN_STATUSES = "('new', 'triaged', 'claimed', 'in_progress', 'agent_reviewing')"

# 'crashloop::Pod/x' -> 'crashloop:kuadrant-system:Pod/x'. Anchored on the first
# ':' rather than split_part(), because the fallback key form is
# f"{category}:{namespace}:{title}" and a title may contain colons of its own.
_REKEY_EXPR = "regexp_replace(i.correlation_key, '^([^:]*)::', '\\1:' || n.ns || ':')"

# Old-format keys are matched with strpos(), not LIKE '%::%'. Database.execute()
# always passes a params tuple, so psycopg2 treats every % in the statement as a
# placeholder and the LIKE form dies with "tuple index out of range" before it
# reaches the server.
_NS_FROM_RESOURCES = """
    LEFT JOIN LATERAL (
        SELECT NULLIF(i.resources::jsonb -> 0 ->> 'namespace', '') AS ns
    ) n ON TRUE
"""


def _migrate_025_rekey_inbox_correlation_keys(db: Database) -> None:
    """Re-key inbox items left orphaned when correlation keys gained a namespace.

    v2.9.0 changed the key from ``category::Kind/name`` to
    ``category:namespace:Kind/name`` so that same-named workloads in different
    namespaces stopped colliding. Existing rows kept the old key. Every
    subsequent scan computed the new form, failed to match, and opened a second
    item — leaving the original frozen at the values it held the moment the
    format changed, unable to be updated, resolved or muted, because no finding
    will ever produce its key again.

    On the cluster this was found on: 38 open items stuck at whatever they said
    two hours before, alongside 24 live ones, with 16 workloads showing both.

    Two passes. Where an open namespaced item already covers the condition, the
    orphan is a duplicate and is resolved. Otherwise the key is rewritten in
    place and the item becomes live again — the next scan refreshes its title
    and resources like any other.

    Resolved rows are deliberately left alone. They are history, nothing reads
    them by key except the 24h reopen lookup, and re-keying them there would
    resurrect old items rather than clean anything up.

    Items with no recoverable namespace are also left alone: ``category::X`` is
    exactly what the current code produces for a cluster-scoped finding, so
    those keys were never wrong.
    """
    db.execute(
        f"""
        UPDATE inbox_items o
        SET status = 'resolved',
            resolved_at = EXTRACT(EPOCH FROM NOW())::bigint,
            updated_at = EXTRACT(EPOCH FROM NOW())::bigint
        FROM (
            SELECT i.id, {_REKEY_EXPR} AS new_key
            FROM inbox_items i {_NS_FROM_RESOURCES}
            WHERE strpos(i.correlation_key, '::') > 0
              AND i.status IN {_OPEN_STATUSES}
              AND n.ns IS NOT NULL
        ) c
        WHERE o.id = c.id
          AND EXISTS (
              SELECT 1 FROM inbox_items live
              WHERE live.id <> o.id
                AND live.correlation_key = c.new_key
                AND live.status IN {_OPEN_STATUSES}
          )
        """
    )
    db.execute(
        f"""
        UPDATE inbox_items o
        SET correlation_key = c.new_key,
            namespace = COALESCE(NULLIF(o.namespace, ''), c.ns)
        FROM (
            SELECT i.id, n.ns, {_REKEY_EXPR} AS new_key
            FROM inbox_items i {_NS_FROM_RESOURCES}
            WHERE strpos(i.correlation_key, '::') > 0
              AND i.status IN {_OPEN_STATUSES}
              AND n.ns IS NOT NULL
        ) c
        WHERE o.id = c.id
          AND o.status IN {_OPEN_STATUSES}
        """
    )


def _migrate_026_episodes(db: Database) -> None:
    """Give an incident an identity.

    Until now the product had findings and inbox items — both of which mean
    "this is wrong". Neither means "this happened". So when one cause produced
    fourteen wrong things, there were fourteen equal rows and no way to say
    they were one event with a cause and a blast radius.

    Note the name: `incidents` was already taken by the agent's memory store
    (past queries, tool sequences, scores), and the UI's "Incident Center" is a
    findings list. The word was spoken for twice and meant neither thing.
    """
    from .db_schema import EPISODES_SCHEMA

    db.executescript(EPISODES_SCHEMA)


def _migrate_027_episode_dismissal(db: Database) -> None:
    """Let an operator close an episode the scanner will not close itself.

    Without this the only way an episode ended was its cause finding
    resolving, so a card whose cause had genuinely stopped — but whose metric
    window had not yet rolled over — could not be cleared by anyone. The
    window bug is fixed separately; this is the escape hatch for the next time
    something similar is true.
    """
    db.execute("ALTER TABLE episodes ADD COLUMN IF NOT EXISTS dismissed_by TEXT")


def _migrate_028_inbox_reset_baseline(db: Database) -> None:
    """Let an operator re-baseline the inbox: count from now, keep the history.

    Two tables rather than one. `inbox_resets` is the watermark every
    count-based scanner reads — after a reset, an occurrence only counts if it
    happened after this moment. `restart_baselines` is the part that cannot be
    derived: a container's restart_count is cumulative for the life of the pod
    and the Kubernetes API will not tell you how many of those happened in the
    last hour. Without a snapshot taken at reset time, the next scan reports
    "restarting (122x)" again and the reset looks broken.

    Event counts get no such snapshot on purpose. Events expire (one hour by
    default), so a baseline taken at reset is meaningless within a scan or two;
    filtering on last-seen is both simpler and more honest.
    """
    db.execute("""
        CREATE TABLE IF NOT EXISTS inbox_resets (
            id SERIAL PRIMARY KEY,
            reset_at BIGINT NOT NULL,
            reset_by TEXT NOT NULL,
            items_archived INTEGER NOT NULL DEFAULT 0,
            episodes_closed INTEGER NOT NULL DEFAULT 0
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS restart_baselines (
            reset_id INTEGER NOT NULL,
            namespace TEXT NOT NULL,
            pod TEXT NOT NULL,
            container TEXT NOT NULL,
            restart_count INTEGER NOT NULL,
            PRIMARY KEY (reset_id, namespace, pod, container)
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_inbox_resets_at ON inbox_resets(reset_at DESC)")


def _migrate_029_action_approval(db: Database) -> None:
    """Record who approved a proposed fix.

    Proposals can now outlive the moment they were raised — trust level 2 used
    to require an operator holding a WebSocket open with 120 seconds to answer,
    which is why nobody ever did. An approval that arrives hours later is a
    person taking responsibility for a change to a live cluster, and that is
    worth a name against it rather than an anonymous state transition.
    """
    db.execute("ALTER TABLE actions ADD COLUMN IF NOT EXISTS approved_by TEXT")
    db.execute("ALTER TABLE actions ADD COLUMN IF NOT EXISTS approved_at BIGINT")


def _migrate_030_episode_cause_onset(db: Database) -> None:
    """When the *cause* began, as distinct from when Pulse opened the episode.

    "What changed just before this started" is the first question in any
    incident, and the answer was always empty on a real cluster. The window was
    anchored on the episode's own creation time — but an episode opens when
    Pulse first manages to build one, which can be a day after the condition
    began. Observed live: a cause firing for 30 hours, an episode 12 minutes
    old, and a change window covering the 30 minutes before the episode, which
    is a day after anything interesting happened.

    Nullable on purpose. Only conditions that report their own onset (firing
    alerts, via Prometheus) can fill it; everything else keeps falling back to
    the episode's start, which is the best that is known for it.
    """
    db.execute("ALTER TABLE episodes ADD COLUMN IF NOT EXISTS cause_started_at BIGINT")


def _migrate_031_action_correlation_key(db: Database) -> None:
    """Give an action the identity of the *condition*, not of one sighting of it.

    ``finding["id"]`` is ``f-{uuid4}``, regenerated by ``_make_finding`` on
    every scan, so the same problem carries a different finding id every 65
    seconds. Any dedupe keyed on it therefore never matches — which is how
    unattended proposing produced 718 proposals for a handful of conditions on
    the reference cluster, each one with a distinct finding id and each one
    looking brand new.

    The correlation key is the identity the rest of the system already uses for
    "this same condition, seen again". Actions need it for the same reason the
    inbox does.
    """
    db.execute("ALTER TABLE actions ADD COLUMN IF NOT EXISTS correlation_key TEXT")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_actions_pending_by_key "
        "ON actions(correlation_key, status) WHERE status IN ('proposed', 'approved')"
    )


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
    (20, "action_outcomes", _migrate_020_action_outcomes),
    (21, "inbox_items", _migrate_021_inbox_items),
    (22, "user_interactions", _migrate_022_user_interactions),
    (23, "operational_flags", _migrate_023_operational_flags),
    (24, "inbox_mutes", _migrate_024_inbox_mutes),
    (25, "rekey_inbox_correlation_keys", _migrate_025_rekey_inbox_correlation_keys),
    (26, "episodes", _migrate_026_episodes),
    (27, "episode_dismissal", _migrate_027_episode_dismissal),
    (28, "inbox_reset_baseline", _migrate_028_inbox_reset_baseline),
    (29, "action_approval", _migrate_029_action_approval),
    (30, "episode_cause_onset", _migrate_030_episode_cause_onset),
    (31, "action_correlation_key", _migrate_031_action_correlation_key),
]
