# Database

PostgreSQL backs memory, monitoring, dashboards, analytics, SLOs, inbox, and runtime artifacts. There is no SQLite fallback. The connection URL is `PULSE_AGENT_DATABASE_URL`; `db.py`, `db_schema.py`, and `db_migrations.py` define the connection and schema contract.

## Development and tests

Use separate databases for development and tests. See [TESTING](TESTING.md) for a disposable test database: pytest **drops the public schema**. A development example is:

```bash
podman run -d --name pulse-dev-pg -p 127.0.0.1:5434:5432 \
  -e POSTGRES_USER=pulse -e POSTGRES_PASSWORD=pulse \
  -e POSTGRES_DB=pulse_dev postgres:16-alpine
export PULSE_AGENT_DATABASE_URL=postgresql://pulse:pulse@localhost:5434/pulse_dev
```

This password is for local disposable use. Migrations run when the database is initialized; no manual schema creation is required.

## Schema by Feature

### Memory (3 tables)

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `incidents` | Past interactions with scores | query, tool_sequence, resolution, outcome, score |
| `runbooks` | Learned diagnostic procedures | trigger_keywords, tool_sequence, success_count, failure_count |
| `patterns` | Recurring issue detection | pattern_type, keywords, frequency, last_seen |

### Monitoring (5 tables)

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `findings` | Scanner-detected issues | severity, message, resolved, namespace, resource |
| `actions` | Auto-fix history with rollback | tool, status, before_state, after_state, rollback_action |
| `investigations` | Root cause analysis reports | suspected_cause, confidence, evidence, alternatives_considered |
| `scan_runs` | Per-cycle scanner timing | duration_ms, total_findings, scanner_results (JSONB) |
| `context_entries` | Cross-agent shared context | source, category, summary, namespace |

### Analytics (7 tables)

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `tool_usage` | Every tool invocation | tool_name, agent_mode, status, duration_ms, session_id |
| `tool_turns` | Per-turn metadata | tools_offered, tools_called, input_tokens, output_tokens, feedback |
| `tool_predictions` | TF-IDF prediction scores | token, tool_name, score, hit_count, miss_count |
| `tool_cooccurrence` | Tool pair frequency | tool_a, tool_b, frequency |
| `skill_usage` | Skill routing analytics | skill_name, query_summary, tools_called, handoff_from |
| `skill_selection_log` | ORCA routing decisions | channel_scores (JSONB), fused_scores (JSONB), selected_skill |
| `prompt_log` | System prompt audit trail | prompt_hash, total_tokens, sections (JSONB) |

### Views (2 tables)

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `views` | User-scoped dashboards | owner, title, layout (JSON), positions (JSON) |
| `view_versions` | Dashboard version history | view_id, version, action, layout (JSON) |

### Chat (2 tables)

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `chat_sessions` | Session metadata | owner, title, agent_mode, message_count |
| `chat_messages` | Message content | session_id (FK), role, content, components_json |

### Evals (1 table)

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `eval_runs` | Eval suite results | suite_name, score, gate_passed, dimensions (JSONB) |

### SLOs (1 table)

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `slo_definitions` | SLO/SLI configuration | service_name, slo_type, target, window_days |

### Postmortems (1 table)

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `postmortems` | Auto-generated incident reports | incident_type, plan_id, root_cause, timeline, prevention (JSONB) |

### PromQL (1 table)

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `promql_queries` | Query reliability tracking | query_hash, success_count, failure_count |

### Metrics (1 table)

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `metrics` | Agent performance metrics | metric_name, value, time_window |

### Plans (1 table)

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `plan_executions` | Phased investigation plan runs | template_id, phase, status, progress_events (JSONB) |

### Ops Inbox (1 table)

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `inbox_items` | Unified worklist item | finding_id, status, priority, assignee, dedup_key |

### User Analytics (2 tables)

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `user_events` | Raw UI/user event stream | event_type, user_id, payload (JSONB) |
| `user_interactions` | Aggregated interaction outcomes | interaction_type, outcome, user_id |

Additional tables support operational flags, inbox mutes, episodes and symptoms, cluster memory (`environment_facts`, `workload_baselines`), learning candidates, user skills, and runtime artifact/version storage. The feature grouping above is a navigation aid; `db_schema.py` is the complete DDL.

Full DDL: `sre_agent/db_schema.py`

## Migrations

Migrations are forward-only and recorded in `schema_migrations`. The current registry ends at **036**. The migration registry is authoritative, including changes to existing tables:

| Version | Name |
|---|---|
| 001 | `baseline` |
| 002 | `tool_usage` |
| 003 | `promql_queries` |
| 004 | `token_tracking` |
| 005 | `scan_runs` |
| 006 | `eval_runs` |
| 007 | `chat_history` |
| 008 | `skill_usage` |
| 009 | `tool_source` |
| 010 | `prompt_log` |
| 011 | `routing_decisions` |
| 012 | `bigint_timestamps` |
| 013 | `tool_predictions` |
| 014 | `skill_selection_log` |
| 015 | `postmortems` |
| 016 | `slo_definitions` |
| 017 | `plan_executions` |
| 018 | `user_events` |
| 019 | `agent_views` |
| 020 | `action_outcomes` |
| 021 | `inbox_items` |
| 022 | `user_interactions` |
| 023 | `operational_flags` |
| 024 | `inbox_mutes` |
| 025 | `rekey_inbox_correlation_keys` |
| 026 | `episodes` |
| 027 | `episode_dismissal` |
| 028 | `inbox_reset_baseline` |
| 029 | `action_approval` |
| 030 | `episode_cause_onset` |
| 031 | `action_correlation_key` |
| 032 | `cluster_memory` |
| 033 | `learning_candidates` |
| 034 | `action_snapshots` |
| 035 | `user_skills` |
| 036 | `runtime_artifacts` |

Add the next unused integer and migration callable to `ALL_MIGRATIONS`; maintain the fresh-install DDL in `db_schema.py` too. Test fresh creation and upgrade behavior. Do not manually insert a migration row to silence a failing migration: that can leave the application using columns which do not exist.

## Pooling and failures

`PULSE_AGENT_DB_POOL_MIN` defaults to 2; `PULSE_AGENT_DB_POOL_MAX` defaults to **20**. The synchronous database wrapper uses a threaded psycopg2 pool. Follow existing commit/rollback conventions so thread-local connections are returned. Pool exhaustion can reflect long transactions, leaked checkouts, locks, or capacity; inspect logs and PostgreSQL activity before changing the pool size.

Some analytics writes use `@db_safe` to return a fallback on database errors. This does not make unavailable persistence healthy, and errors still need investigation. Monitor pause-state reads fail closed when the pause flag cannot be read.

## Production operations

The operator provisions PostgreSQL and credentials when its database configuration enables the bundled service. Use its current CRD/README for storage, image, service, and external-database settings. Secrets are reconciled by the operator, not Helm `lookup()`. NetworkPolicy admits the agent and, when enabled, the Temporal workload on TCP 5432.

Resource names and database credentials depend on the CR. Discover them with `oc get statefulsets,services,secrets,pvc -n <namespace>` and the CR rather than copying old Helm release names. Take a PostgreSQL backup and test restoring it to an isolated database before upgrades. A PVC snapshot is storage-level evidence and should be validated for database consistency.

Password rotation must update the actual PostgreSQL role and the Secret/connection string together, then restart consumers as needed. Editing only the Secret does not change the database role password. Avoid printing credentials in logs or committing connection strings.

The default single-instance database is not an HA service. Increasing StatefulSet replicas alone does not configure PostgreSQL replication or failover. If HA is required, provision a supported managed PostgreSQL service or database operator and configure Pulse's external connection through the operator; validate backups, failover, network policy, and migration compatibility.

## Troubleshooting

- Connection refused: check the service/container, actual host/port, pod readiness, and NetworkPolicy.
- Migration failure: preserve logs and inspect `SELECT * FROM schema_migrations ORDER BY version DESC`; repair through a reviewed migration and restore plan, not a fabricated applied-version row.
- Pool exhaustion: inspect outstanding transactions and locks, and verify application commit/rollback paths.
- Full volume: check supported PVC expansion and retention requirements. Tables use different timestamp representations; do not copy a generic SQL deletion across all tables.
