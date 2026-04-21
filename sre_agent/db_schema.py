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
    verification_timestamp BIGINT,
    outcome TEXT NOT NULL DEFAULT 'unknown'
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
    view_type TEXT NOT NULL DEFAULT 'custom',
    status TEXT NOT NULL DEFAULT 'active',
    trigger_source TEXT NOT NULL DEFAULT 'user',
    finding_id TEXT,
    cluster_id TEXT NOT NULL DEFAULT '',
    claimed_by TEXT,
    claimed_at TEXT,
    visibility TEXT NOT NULL DEFAULT 'private',
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
    was_confirmed   BOOLEAN,
    tool_source     TEXT DEFAULT 'native'
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
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    cache_read_tokens INTEGER,
    cache_creation_tokens INTEGER,
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

SCAN_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_runs (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    duration_ms INTEGER,
    total_findings INTEGER,
    scanner_results JSONB,
    session_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_scan_runs_timestamp ON scan_runs (timestamp DESC);
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

EVAL_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS eval_runs (
    id              SERIAL PRIMARY KEY,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    suite_name      TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'cli',
    model           TEXT DEFAULT '',
    scenario_count  INTEGER NOT NULL,
    passed_count    INTEGER NOT NULL,
    gate_passed     BOOLEAN NOT NULL,
    average_overall REAL NOT NULL,
    dimensions      JSONB,
    blocker_counts  JSONB,
    scenarios       JSONB,
    prompt_audit    JSONB,
    judge_avg       REAL
);
CREATE INDEX IF NOT EXISTS idx_eval_runs_suite ON eval_runs(suite_name, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_eval_runs_ts ON eval_runs(timestamp DESC);
"""

CHAT_SESSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT 'New Chat',
    agent_mode TEXT NOT NULL DEFAULT 'auto',
    message_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_owner ON chat_sessions(owner);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_updated ON chat_sessions(updated_at DESC);
"""

CHAT_MESSAGES_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_messages (
    id SERIAL PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    components_json TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id);
"""

SKILL_USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS skill_usage (
    id              SERIAL PRIMARY KEY,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    session_id      TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    skill_name      TEXT NOT NULL,
    skill_version   INTEGER NOT NULL DEFAULT 1,
    query_summary   TEXT,
    tools_called    TEXT[],
    tool_count      INTEGER DEFAULT 0,
    handoff_from    TEXT,
    handoff_to      TEXT,
    duration_ms     INTEGER,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    feedback        TEXT,
    eval_score      REAL
);
CREATE INDEX IF NOT EXISTS idx_skill_usage_skill ON skill_usage(skill_name, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_skill_usage_user ON skill_usage(user_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_skill_usage_session ON skill_usage(session_id);
"""

PROMPT_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS prompt_log (
    id              SERIAL PRIMARY KEY,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    session_id      TEXT NOT NULL,
    turn_number     INTEGER NOT NULL,
    skill_name      TEXT NOT NULL,
    skill_version   INTEGER NOT NULL,
    prompt_hash     TEXT NOT NULL,
    static_chars    INTEGER NOT NULL,
    dynamic_chars   INTEGER NOT NULL,
    total_tokens    INTEGER NOT NULL,
    sections        JSONB,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    cache_read_tokens INTEGER,
    cache_creation_tokens INTEGER
);
CREATE INDEX IF NOT EXISTS idx_prompt_log_session ON prompt_log(session_id);
CREATE INDEX IF NOT EXISTS idx_prompt_log_hash ON prompt_log(prompt_hash);
CREATE INDEX IF NOT EXISTS idx_prompt_log_skill ON prompt_log(skill_name, timestamp DESC);
"""

TOOL_PREDICTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_predictions (
    token       TEXT NOT NULL,
    tool_name   TEXT NOT NULL,
    score       FLOAT NOT NULL DEFAULT 1.0,
    hit_count   INT NOT NULL DEFAULT 1,
    miss_count  INT NOT NULL DEFAULT 0,
    last_seen   TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (token, tool_name)
);
CREATE INDEX IF NOT EXISTS idx_tool_predictions_token ON tool_predictions(token);
"""

TOOL_COOCCURRENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_cooccurrence (
    tool_a      TEXT NOT NULL,
    tool_b      TEXT NOT NULL,
    frequency   INT NOT NULL DEFAULT 1,
    PRIMARY KEY (tool_a, tool_b)
);
"""

SKILL_SELECTION_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS skill_selection_log (
    id              SERIAL PRIMARY KEY,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    session_id      TEXT NOT NULL,
    query_summary   TEXT NOT NULL,
    channel_scores  JSONB NOT NULL,
    fused_scores    JSONB NOT NULL,
    selected_skill  TEXT NOT NULL,
    threshold_used  FLOAT NOT NULL,
    conflicts_detected JSONB,
    skill_overridden TEXT,
    tools_requested_missing TEXT[],
    selection_ms    INTEGER,
    incident_priority TEXT,
    channel_weights JSONB
);
CREATE INDEX IF NOT EXISTS idx_skill_selection_log_ts ON skill_selection_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_skill_selection_log_skill ON skill_selection_log(selected_skill);
CREATE INDEX IF NOT EXISTS idx_skill_selection_log_session ON skill_selection_log(session_id);
"""

POSTMORTEMS_SCHEMA = """
CREATE TABLE IF NOT EXISTS postmortems (
    id TEXT PRIMARY KEY,
    incident_type TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    timeline TEXT,
    root_cause TEXT,
    contributing_factors JSONB,
    blast_radius JSONB,
    actions_taken JSONB,
    prevention JSONB,
    metrics_impact TEXT,
    confidence FLOAT DEFAULT 0.0,
    generated_at BIGINT DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_postmortems_type ON postmortems(incident_type);
"""

SLO_DEFINITIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS slo_definitions (
    id SERIAL PRIMARY KEY,
    service_name TEXT NOT NULL,
    slo_type TEXT NOT NULL,
    target FLOAT NOT NULL,
    window_days INTEGER DEFAULT 30,
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(service_name, slo_type)
);
"""

PLAN_EXECUTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS plan_executions (
    id              TEXT PRIMARY KEY,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    template_id     TEXT NOT NULL,
    template_name   TEXT NOT NULL,
    incident_type   TEXT NOT NULL,
    finding_id      TEXT,
    status          TEXT NOT NULL,
    phases_total    INTEGER NOT NULL,
    phases_completed INTEGER NOT NULL,
    total_duration_ms INTEGER,
    phase_details   JSONB,
    confidence      FLOAT DEFAULT 0.0,
    postmortem_id   TEXT
);
CREATE INDEX IF NOT EXISTS idx_plan_executions_template ON plan_executions(template_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_plan_executions_ts ON plan_executions(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_plan_executions_status ON plan_executions(status);
"""

USER_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_events (
    id          SERIAL PRIMARY KEY,
    timestamp   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    session_id  TEXT NOT NULL,
    user_id     TEXT NOT NULL DEFAULT '',
    event_type  TEXT NOT NULL,
    page        TEXT NOT NULL DEFAULT '',
    data        JSONB DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_user_events_ts ON user_events(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_user_events_page ON user_events(page, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_user_events_type ON user_events(event_type);
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
    + SCAN_RUNS_SCHEMA
    + EVAL_RUNS_SCHEMA
    + CHAT_SESSIONS_SCHEMA
    + CHAT_MESSAGES_SCHEMA
    + SKILL_USAGE_SCHEMA
    + PROMPT_LOG_SCHEMA
    + TOOL_PREDICTIONS_SCHEMA
    + TOOL_COOCCURRENCE_SCHEMA
    + SKILL_SELECTION_LOG_SCHEMA
    + POSTMORTEMS_SCHEMA
    + SLO_DEFINITIONS_SCHEMA
    + PLAN_EXECUTIONS_SCHEMA
    + USER_EVENTS_SCHEMA
)
