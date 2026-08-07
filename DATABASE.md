# Database

PostgreSQL is required for all production features — memory, monitoring, views, tool analytics, SLOs, evals. Schema defined in `sre_agent/db_schema.py`, migrations in `sre_agent/db_migrations.py`.

## Local Development

```bash
# Start PostgreSQL
podman run -d --name pulse-test-pg \
  -p 5433:5432 \
  -e POSTGRES_USER=pulse \
  -e POSTGRES_PASSWORD=pulse \
  -e POSTGRES_DB=pulse_test \
  postgres:16-alpine

# Set connection URL
export PULSE_AGENT_DATABASE_URL=postgresql://pulse:pulse@localhost:5433/pulse_test
```

Migrations apply automatically on first connection. No manual schema setup needed.

**Restart between sessions:** `podman start pulse-test-pg`

**Reset data:** `podman rm -f pulse-test-pg` and re-run the create command.

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

**Total: 28 tables, 30+ indexes.**

Full DDL: `sre_agent/db_schema.py`

## Migrations

### How They Work

1. On first `get_database()` call, `run_migrations(db)` executes
2. Migrations tracked in `schema_migrations` table (version, name, applied_at)
3. Each migration runs only if its version > current max version
4. Migrations are forward-only — no rollback support
5. Failures roll back the individual migration and raise an exception

### Current Migrations (v001 – v022)

| Version | Name | What it does |
|---------|------|-------------|
| 1 | baseline | Creates all tables from `ALL_SCHEMAS` |
| 2 | tool_usage | Adds `tool_usage`, `tool_turns` tables |
| 3 | promql_queries | Adds `promql_queries` table |
| 4 | token_tracking | Adds token columns to `tool_turns` |
| 5 | scan_runs | Adds `scan_runs` table |
| 6 | eval_runs | Adds `eval_runs` table |
| 7 | chat_history | Adds `chat_sessions`, `chat_messages` tables |
| 8 | skill_usage | Adds `skill_usage` table |
| 9 | tool_source | Adds `tool_source` column to `tool_usage` |
| 10 | prompt_log | Adds `prompt_log` table |
| 11 | routing_decisions | Adds routing columns to `tool_turns` |
| 12 | bigint_timestamps | Fixes timestamp overflow in `investigations` |
| 13 | tool_predictions | Adds `tool_predictions`, `tool_cooccurrence` tables |
| 14 | skill_selection_log | Adds ORCA selector logging |
| 15 | postmortems | Adds `postmortems` table |
| 16 | slo_definitions | Adds `slo_definitions` table |
| 17 | plan_executions | Adds `plan_executions` table (phased plan runtime) |
| 18 | user_events | Adds `user_events` table (raw UI event stream) |
| 19 | agent_views | Adds agent-lifecycle columns to `views` (view_type, status, trigger_source) |
| 20 | action_outcomes | Adds outcome-tracking columns to `actions` |
| 21 | inbox_items | Adds `inbox_items` table (unified Ops Inbox) |
| 22 | user_interactions | Adds `user_interactions` table (aggregated interaction outcomes) |

### Adding a New Migration

1. Add a new entry to `ALL_MIGRATIONS` in `db_migrations.py`:
   ```python
   (17, "my_feature", """
       CREATE TABLE IF NOT EXISTS my_table (
           id SERIAL PRIMARY KEY,
           ...
       );
   """),
   ```
2. Also add the table to `ALL_SCHEMAS` in `db_schema.py` (for fresh installs)
3. Update the version reference in `CLAUDE.md` if needed
4. Test: `python3 -m pytest tests/ -k "migration" -v`

## Connection Pooling

### Configuration

| Setting | Env Var | Default | Description |
|---------|---------|---------|-------------|
| `db_pool_min` | `PULSE_AGENT_DB_POOL_MIN` | 2 | Minimum pool connections |
| `db_pool_max` | `PULSE_AGENT_DB_POOL_MAX` | 10 | Maximum pool connections |

Uses `psycopg2.pool.ThreadedConnectionPool` with thread-local connection tracking.

### How It Works

- `execute()` checks out a connection from the pool into thread-local storage
- Connection stays checked out until `commit()` or error rollback
- `fetchone()` / `fetchall()` use short-lived ad-hoc connections (auto-returned)
- `@db_safe` decorator catches all `psycopg2.Error` and `OSError`, returns `None` on failure
- Fire-and-forget writes (tool_usage, analytics) use `@db_safe` to prevent DB errors from blocking the agent

### Common Pitfall

**Missing `commit()`**: After `execute()`, the connection stays checked out. If you forget `commit()`, the connection leaks and the pool exhausts. Every `execute()` must be followed by `commit()`.

## Production Deployment

### Helm Chart (PostgreSQL StatefulSet)

Deployed automatically via the umbrella chart. Key values:

| Value | Default | Description |
|-------|---------|-------------|
| `database.postgresql.enabled` | `true` | Deploy PostgreSQL StatefulSet |
| `database.postgresql.storage` | `5Gi` | PVC size |
| `database.postgresql.storageClass` | (default) | Storage class for PVC |
| `database.postgresql.auth.username` | `pulse` | Database user |
| `database.postgresql.auth.database` | `pulse` | Database name |

### Security

- Non-root container with `runAsNonRoot: true`
- `readOnlyRootFilesystem` not set (PG needs write access to data dir)
- Capabilities dropped: `ALL`
- Seccomp: `RuntimeDefault`
- NetworkPolicy: only agent pods can connect (port 5432)
- Password auto-generated as K8s Secret, preserved across upgrades via `lookup()`

### Backup and Restore

**Manual backup:**
```bash
oc exec pulse-openshift-sre-agent-postgresql-0 -n openshiftpulse -- \
  pg_dump -U pulse pulse > backup.sql
```

**Restore:**
```bash
oc exec -i pulse-openshift-sre-agent-postgresql-0 -n openshiftpulse -- \
  psql -U pulse pulse < backup.sql
```

**PVC snapshot** (if storage class supports it):
```bash
oc get pvc -n openshiftpulse  # find the PG PVC name
# Create VolumeSnapshot per your storage provider
```

### Password Rotation

```bash
# 1. Generate new password
NEW_PW=$(openssl rand -hex 16)

# 2. Update K8s secret
oc patch secret pulse-openshift-sre-agent-pg-auth -n openshiftpulse \
  -p "{\"data\":{\"password\":\"$(echo -n $NEW_PW | base64)\"}}"

# 3. Update PostgreSQL
oc exec pulse-openshift-sre-agent-postgresql-0 -n openshiftpulse -- \
  psql -U postgres -c "ALTER USER pulse PASSWORD '$NEW_PW';"

# 4. Restart agent to pick up new password
oc rollout restart deployment/pulse-openshift-sre-agent -n openshiftpulse
```

## High Availability

The default deployment is single-instance (1 replica StatefulSet with RWO PVC). For HA:

### Option 1: Managed PostgreSQL (Recommended)

Use a managed PostgreSQL service (AWS RDS, GCP Cloud SQL, Azure Database) instead of the in-cluster StatefulSet:

1. Set `database.postgresql.enabled=false` in Helm values
2. Create the database and user on the managed service
3. Set `PULSE_AGENT_DATABASE_URL` to the managed instance URL
4. The agent handles connection pooling — no pgBouncer needed for typical workloads

**Benefits:** Automated backups, failover, monitoring, scaling. No PVC management.

### Option 2: Streaming Replication

For in-cluster HA with the existing StatefulSet:

1. Increase `replicas` to 2+ in the StatefulSet
2. Configure primary/standby replication via `postgresql.conf`:
   - Primary: `wal_level=replica`, `max_wal_senders=3`
   - Standby: `primary_conninfo`, `hot_standby=on`
3. Use a connection proxy (pgBouncer or PgPool-II) for failover
4. The agent writes to primary only — read replicas can serve `fetchone`/`fetchall` if you add a read URL

**Not recommended** for most deployments — the operational complexity exceeds the benefit for a single-agent workload.

### Option 3: Crunchy PGO / CloudNativePG

Use a Kubernetes-native PostgreSQL operator:

1. Install [CloudNativePG](https://cloudnative-pg.io/) or [Crunchy PGO](https://access.crunchydata.com/documentation/postgres-operator/)
2. Create a `Cluster` CR with 2+ instances
3. Set `database.postgresql.enabled=false`
4. Point `PULSE_AGENT_DATABASE_URL` to the operator-managed service
5. Operator handles failover, backups, and WAL archiving automatically

**Best of both worlds** — HA without leaving the cluster.

## Troubleshooting

### Connection pool exhaustion

**Symptom:** Agent hangs, "could not obtain connection" errors.

**Cause:** Missing `commit()` after `execute()` — connections leak.

**Fix:** Check recent code changes for `db.execute()` without matching `db.commit()`. Restart the agent to clear the pool.

### Migration failure on startup

**Symptom:** Agent crashes with `psycopg2.Error` on boot.

**Cause:** Partially applied migration left the schema in an inconsistent state.

**Fix:**
```bash
# Check current migration version
oc exec pulse-openshift-sre-agent-postgresql-0 -n openshiftpulse -- \
  psql -U pulse pulse -c "SELECT * FROM schema_migrations ORDER BY version DESC LIMIT 5;"

# If a migration is partially applied, manually fix the schema and bump the version:
oc exec pulse-openshift-sre-agent-postgresql-0 -n openshiftpulse -- \
  psql -U pulse pulse -c "INSERT INTO schema_migrations (version, name, applied_at) VALUES (N, 'manual_fix', NOW());"
```

### Stale test PostgreSQL

**Symptom:** Tests fail with "connection refused" on port 5433.

**Fix:** `podman start pulse-test-pg` or recreate:
```bash
podman rm -f pulse-test-pg
podman run -d --name pulse-test-pg \
  -p 5433:5432 \
  -e POSTGRES_USER=pulse \
  -e POSTGRES_PASSWORD=pulse \
  -e POSTGRES_DB=pulse_test \
  postgres:16-alpine
```

### PVC full

**Symptom:** PostgreSQL pod crashes with "No space left on device".

**Fix:** Expand the PVC (if storage class supports it):
```bash
oc patch pvc pg-data-pulse-openshift-sre-agent-postgresql-0 -n openshiftpulse \
  -p '{"spec":{"resources":{"requests":{"storage":"10Gi"}}}}'
```

Or clean old data:
```bash
oc exec pulse-openshift-sre-agent-postgresql-0 -n openshiftpulse -- \
  psql -U pulse pulse -c "DELETE FROM tool_usage WHERE timestamp < NOW() - INTERVAL '90 days';"
```
