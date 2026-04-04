"""PostgreSQL table schemas for Pulse Agent.

Schemas are PostgreSQL-native and are passed directly to ``Database.executescript()``.
"""

INCIDENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id SERIAL PRIMARY KEY,
    timestamp TEXT NOT NULL,
    query TEXT NOT NULL,
    query_keywords TEXT NOT NULL,
    tool_sequence TEXT NOT NULL,
    resolution TEXT NOT NULL,
    outcome TEXT DEFAULT 'unknown',
    namespace TEXT DEFAULT '',
    resource_type TEXT DEFAULT '',
    error_type TEXT DEFAULT '',
    tool_count INTEGER DEFAULT 0,
    rejected_tools INTEGER DEFAULT 0,
    duration_seconds REAL DEFAULT 0,
    score REAL DEFAULT 0
);
"""

RUNBOOKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS runbooks (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    trigger_keywords TEXT NOT NULL,
    tool_sequence TEXT NOT NULL,
    success_count INTEGER DEFAULT 1,
    failure_count INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source_incident_id INTEGER
);
"""

PATTERNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS patterns (
    id SERIAL PRIMARY KEY,
    pattern_type TEXT NOT NULL,
    description TEXT NOT NULL,
    keywords TEXT NOT NULL,
    incident_ids TEXT NOT NULL,
    frequency INTEGER DEFAULT 1,
    last_seen TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    metadata TEXT DEFAULT '{}'
);
"""

METRICS_SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (
    id SERIAL PRIMARY KEY,
    timestamp TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    value REAL NOT NULL,
    time_window TEXT DEFAULT 'session'
);
"""

ACTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    finding_id TEXT,
    timestamp BIGINT,
    category TEXT,
    tool TEXT,
    input TEXT,
    status TEXT DEFAULT 'pending',
    before_state TEXT,
    after_state TEXT,
    error TEXT,
    reasoning TEXT,
    duration_ms INTEGER,
    rollback_available INTEGER DEFAULT 0,
    rollback_action TEXT,
    resources TEXT,
    verification_status TEXT,
    verification_evidence TEXT,
    verification_timestamp BIGINT
);
"""

INVESTIGATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS investigations (
    id TEXT PRIMARY KEY,
    finding_id TEXT,
    timestamp INTEGER,
    category TEXT,
    severity TEXT,
    status TEXT,
    summary TEXT,
    suspected_cause TEXT,
    recommended_fix TEXT,
    confidence REAL,
    error TEXT,
    resources TEXT,
    evidence TEXT,
    alternatives_considered TEXT
);
"""

CONTEXT_ENTRIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS context_entries (
    id SERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    category TEXT NOT NULL,
    summary TEXT NOT NULL,
    details TEXT NOT NULL,
    timestamp BIGINT NOT NULL,
    namespace TEXT DEFAULT '',
    resources TEXT DEFAULT '[]'
);
"""

FINDINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS findings (
    id TEXT PRIMARY KEY,
    cluster TEXT NOT NULL,
    namespace TEXT DEFAULT '',
    resource TEXT DEFAULT '',
    severity TEXT DEFAULT 'info',
    message TEXT NOT NULL,
    timestamp BIGINT,
    resolved INTEGER DEFAULT 0
);
"""

VIEWS_SCHEMA = """
CREATE TABLE IF NOT EXISTS views (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    icon TEXT DEFAULT '',
    layout TEXT NOT NULL,
    positions TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

VIEW_VERSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS view_versions (
    id SERIAL PRIMARY KEY,
    view_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    action TEXT NOT NULL,
    layout TEXT NOT NULL,
    positions TEXT DEFAULT '{}',
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
"""

TOOL_USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_usage (
    id              SERIAL PRIMARY KEY,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    session_id      TEXT NOT NULL,
    turn_number     INTEGER NOT NULL,
    agent_mode      TEXT NOT NULL,
    tool_name       TEXT NOT NULL,
    tool_category   TEXT,
    input_summary   JSONB,
    status          TEXT NOT NULL,
    error_message   TEXT,
    error_category  TEXT,
    duration_ms     INTEGER,
    result_bytes    INTEGER,
    requires_confirmation BOOLEAN DEFAULT FALSE,
    was_confirmed   BOOLEAN
);
"""

TOOL_TURNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_turns (
    id              SERIAL PRIMARY KEY,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    session_id      TEXT NOT NULL,
    turn_number     INTEGER NOT NULL,
    agent_mode      TEXT NOT NULL,
    query_summary   TEXT,
    tools_offered   TEXT[],
    tools_called    TEXT[],
    feedback        TEXT,
    UNIQUE(session_id, turn_number)
);
"""

TOOL_USAGE_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_tool_usage_timestamp ON tool_usage(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_tool_usage_tool_name ON tool_usage(tool_name);
CREATE INDEX IF NOT EXISTS idx_tool_usage_session ON tool_usage(session_id);
CREATE INDEX IF NOT EXISTS idx_tool_usage_mode ON tool_usage(agent_mode);
CREATE INDEX IF NOT EXISTS idx_tool_usage_status ON tool_usage(status);
CREATE INDEX IF NOT EXISTS idx_tool_turns_session ON tool_turns(session_id);
CREATE INDEX IF NOT EXISTS idx_tool_turns_feedback ON tool_turns(feedback) WHERE feedback IS NOT NULL;
"""

PROMQL_QUERIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS promql_queries (
    id SERIAL PRIMARY KEY,
    query_hash TEXT NOT NULL,
    query_template TEXT NOT NULL,
    category TEXT DEFAULT '',
    success_count INT DEFAULT 0,
    failure_count INT DEFAULT 0,
    last_success TIMESTAMPTZ,
    last_failure TIMESTAMPTZ,
    avg_series_count FLOAT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(query_hash)
);
"""

INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_incidents_keywords ON incidents(query_keywords);
CREATE INDEX IF NOT EXISTS idx_incidents_error_type ON incidents(error_type);
CREATE INDEX IF NOT EXISTS idx_runbooks_keywords ON runbooks(trigger_keywords);
CREATE INDEX IF NOT EXISTS idx_patterns_keywords ON patterns(keywords);
CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status);
CREATE INDEX IF NOT EXISTS idx_actions_ts ON actions(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_actions_category ON actions(category);
CREATE INDEX IF NOT EXISTS idx_findings_cluster ON findings(cluster);
CREATE INDEX IF NOT EXISTS idx_investigations_ts ON investigations(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_investigations_finding ON investigations(finding_id);
CREATE INDEX IF NOT EXISTS idx_context_entries_ts ON context_entries(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_context_entries_ns ON context_entries(namespace);
CREATE INDEX IF NOT EXISTS idx_views_owner ON views(owner);
CREATE UNIQUE INDEX IF NOT EXISTS idx_views_owner_title ON views(owner, title);
CREATE INDEX IF NOT EXISTS idx_view_versions_view ON view_versions(view_id, version DESC);
"""

ALL_SCHEMAS = (
    INCIDENTS_SCHEMA
    + RUNBOOKS_SCHEMA
    + PATTERNS_SCHEMA
    + METRICS_SCHEMA
    + ACTIONS_SCHEMA
    + INVESTIGATIONS_SCHEMA
    + CONTEXT_ENTRIES_SCHEMA
    + FINDINGS_SCHEMA
    + VIEWS_SCHEMA
    + VIEW_VERSIONS_SCHEMA
    + TOOL_USAGE_SCHEMA
    + TOOL_TURNS_SCHEMA
    + INDEX_SCHEMA
    + TOOL_USAGE_INDEX_SCHEMA
    + PROMQL_QUERIES_SCHEMA
)
