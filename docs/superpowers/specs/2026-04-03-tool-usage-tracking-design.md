# Tool Usage Tracking & Tools/Agents UI

> Historical design/implementation record. This preserves the proposal and tasks at the date in the filename; unchecked tasks, paths, commands, and security claims are not evidence of the current implementation. Use [current documentation](../../README.md) and verify behavior against code before applying a procedure.


## Overview

Full audit logging of every tool invocation in PostgreSQL, plus UI for browsing all agents, tools, and usage history.

## Database Schema

New `tool_usage` table in PostgreSQL:

### `tool_usage` — per-invocation audit log

```sql
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

CREATE INDEX idx_tool_usage_timestamp ON tool_usage(timestamp DESC);
CREATE INDEX idx_tool_usage_tool_name ON tool_usage(tool_name);
CREATE INDEX idx_tool_usage_session ON tool_usage(session_id);
CREATE INDEX idx_tool_usage_mode ON tool_usage(agent_mode);
CREATE INDEX idx_tool_usage_status ON tool_usage(status);
```

- `turn_number`: sequential counter within a session (1, 2, 3...) — enables trivial sequence analysis
- `agent_mode`: one of sre, security, view_designer, both, agent
- `input_summary`: sanitized JSON (no secrets, truncated to 1KB) using existing sanitization patterns
- `status`: "success" or "error"
- `error_category`: from ToolError classification (7 categories)
- `result_bytes`: size of the tool result string — flags tools with oversized outputs that waste context
- `was_confirmed`: NULL for non-write tools, true/false for write tools

### `tool_turns` — per-turn context (what the user asked + what was offered)

```sql
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

CREATE INDEX idx_tool_turns_session ON tool_turns(session_id);
CREATE INDEX idx_tool_turns_feedback ON tool_turns(feedback) WHERE feedback IS NOT NULL;
```

- `query_summary`: first 200 chars of the user's message — connects tool calls to intent
- `tools_offered`: array of tool names the harness loaded into context for this turn
- `tools_called`: array of tool names Claude actually invoked (denormalized for fast queries)
- `feedback`: linked user feedback ("positive", "negative", NULL) — correlates tool patterns with satisfaction

## Backend API

### New Endpoints

#### `GET /agents`

Returns all agent modes with metadata.

```json
[
  {
    "name": "sre",
    "description": "Cluster diagnostics, incident triage, and resource management",
    "tools_count": 52,
    "has_write_tools": true,
    "categories": ["diagnostics", "workloads", "networking", "storage", "monitoring", "operations", "gitops"]
  },
  {
    "name": "security",
    "description": "Security scanning, RBAC analysis, and compliance checks",
    "tools_count": 9,
    "has_write_tools": false,
    "categories": ["security", "networking"]
  },
  {
    "name": "view_designer",
    "description": "Dashboard creation and component design",
    "tools_count": 12,
    "has_write_tools": false,
    "categories": ["diagnostics", "monitoring"]
  }
]
```

Auth: same `PULSE_AGENT_WS_TOKEN` check as existing endpoints.

#### `GET /tools/usage`

Paginated audit log of tool invocations.

**Query params:**
- `tool_name` — filter by tool
- `agent_mode` — filter by mode
- `status` — "success" or "error"
- `session_id` — filter by session
- `from` / `to` — ISO 8601 timestamp range
- `page` — page number (default 1)
- `per_page` — results per page (default 50, max 200)

**Response:**
```json
{
  "entries": [
    {
      "id": 1,
      "timestamp": "2026-04-03T10:30:00Z",
      "session_id": "abc123",
      "turn_number": 3,
      "agent_mode": "sre",
      "tool_name": "get_pod_logs",
      "tool_category": "diagnostics",
      "input_summary": {"pod_name": "web-1", "namespace": "prod"},
      "status": "success",
      "error_message": null,
      "error_category": null,
      "duration_ms": 342,
      "result_bytes": 4820,
      "requires_confirmation": false,
      "was_confirmed": null,
      "query_summary": "Show me the logs for the web pod in prod"
    }
  ],
  "total": 1284,
  "page": 1,
  "per_page": 50
}
```

#### `GET /tools/usage/stats`

Aggregated usage statistics.

**Query params:**
- `from` / `to` — ISO 8601 timestamp range (default: last 24h)

**Response:**
```json
{
  "total_calls": 1284,
  "unique_tools_used": 38,
  "error_rate": 0.04,
  "avg_duration_ms": 285,
  "avg_result_bytes": 3200,
  "by_tool": [
    {"tool_name": "get_pod_logs", "count": 142, "error_count": 3, "avg_duration_ms": 310, "avg_result_bytes": 8500},
    {"tool_name": "list_resources", "count": 98, "error_count": 1, "avg_duration_ms": 220, "avg_result_bytes": 2100}
  ],
  "by_mode": [
    {"mode": "sre", "count": 980},
    {"mode": "security", "count": 204},
    {"mode": "view_designer", "count": 100}
  ],
  "by_category": [
    {"category": "diagnostics", "count": 520},
    {"category": "workloads", "count": 310}
  ],
  "by_status": {"success": 1232, "error": 52},
  "harness_efficiency": {
    "avg_tools_offered": 22,
    "avg_tools_used": 4.2,
    "utilization_rate": 0.19,
    "never_used_tools": ["tool_x", "tool_y"],
    "always_used_tools": ["list_resources", "get_pod_logs"]
  },
  "feedback_correlation": {
    "positive_sessions": 42,
    "negative_sessions": 8,
    "top_tools_in_positive": ["get_pod_logs", "describe_resource"],
    "top_tools_in_negative": ["exec_command", "apply_yaml"]
  }
}
```

### Enhanced Existing Endpoint

#### `GET /tools` (enhanced)

Add `category` field to each tool entry:

```json
{
  "sre": [
    {
      "name": "get_pod_logs",
      "description": "Retrieve logs from a pod",
      "requires_confirmation": false,
      "category": "diagnostics"
    }
  ],
  "security": [...],
  "write_tools": [...]
}
```

## Recording Layer

### Location

Hook into `agent.py`'s tool execution path. The existing `on_tool_use(name)` callback is the insertion point.

### Implementation

Wrap each tool call to capture:
1. Start timestamp
2. Tool name and input parameters
3. Agent mode, session ID, and turn number (available from WebSocket handler context)
4. Result status (success/error) and error details
5. Duration (end - start)
6. Result size in bytes

At the turn level (before tool execution), record:
1. User query summary (first 200 chars)
2. Tools offered by the harness for this turn
3. Insert a `tool_turns` row at turn start, update `tools_called` after execution

### Feedback linking

When a `feedback` WebSocket message arrives (thumbs up/down), update the most recent `tool_turns` row for that session with the feedback value. This connects satisfaction signals to the exact tool sequence that produced the response.

### Write strategy

Fire-and-forget async insert to PostgreSQL. Tool execution is not blocked by the audit write. Failed audit writes are logged but do not affect tool execution.

### Input sanitization

Reuse `_sanitize_for_prompt()` patterns:
- Strip known secret fields (token, password, key, secret)
- Truncate string values longer than 256 chars
- Cap total JSON size at 1KB

### Tool category resolution

Use `TOOL_CATEGORIES` from `harness.py` to resolve tool name to category at recording time. Tools not in any category get `category: null`.

## UI: Agent Settings "Tools" Tab

New tab on `/agent` page alongside settings, memory, views.

### Summary cards (top row)
- Total tool calls (last 24h)
- Unique tools used
- Error rate percentage
- Most-used tool name + count

### Recent activity
- Mini table showing last 10 tool invocations (tool, mode, status, duration, timestamp)
- Link to full `/tools` page

## UI: `/tools` Route

New top-level route with dedicated nav entry.

### Tools Catalog Panel

- All tools listed, grouped by category
- Filter by agent mode (sre/security/view_designer)
- Each tool card shows: name, description, category badge, write-tool indicator
- Search/filter by name

### Agents Panel

- Card per agent mode: name, description, tool count, categories, write capability
- Visual indicator of which mode is currently active

### Audit Log Panel

- Full-width table of tool invocations
- Columns: timestamp, tool name, agent mode, status, duration, result size, session ID
- Expandable rows showing: input_summary, error details, user query that triggered it
- Filters: tool name (dropdown), mode, status, date range
- Pagination controls

### Stats Panel

- Top 10 tools bar chart
- Calls over time (line chart, hourly buckets)
- Error rate by tool (table sorted by error rate desc)
- Usage by category (pie/donut chart)
- Harness efficiency gauge (tools offered vs used)
- Feedback correlation: tools in positive vs negative sessions
- Context hogs: tools ranked by avg result_bytes

## System Improvement Use Cases

The audit log is not just observability — it feeds back into improving the agent system.

### Tool Selection Optimization

The harness currently picks tools per query using keyword matching against `TOOL_CATEGORIES`. Usage data enables:
- Track which tools actually get called per query type vs. which were loaded — identify tools the harness loads but Claude never picks (wasted context)
- Identify tools that should be in `ALWAYS_INCLUDE` because they're called across all categories
- Surface category misassignments (tool in "diagnostics" but mostly used in "workloads" queries)

### Tool Quality Feedback Loop

- Flag tools with error rates above a threshold — candidates for fixes or deprecation
- Identify tools that are never called — dead tools bloating the tool list
- Track duration outliers — tools that are slow and could benefit from optimization
- Surface common `error_category` patterns per tool to guide targeted hardening

### Agent Behavior Tuning

- Track tool call sequences within a session to find common chains (e.g., `list_resources` -> `get_pod_logs` -> `describe_resource`)
- Identify sequences that always end in errors — the agent is making bad choices
- Find tools that are frequently called with the same parameters — candidates for caching or preloading
- Compare tool selection across agent modes to inform the orchestrator's routing

### Tool Chain Intelligence

Four layers of chain optimization, built incrementally on the audit data:

**Layer 1: Chain Discovery**
Query `tool_usage` ordered by `(session_id, turn_number)` to extract frequent N-grams (2-tool, 3-tool sequences). Surface in the stats panel as "Common Patterns" — e.g., `list_resources -> get_pod_logs` occurs in 78% of SRE sessions. Also surface anti-patterns: chains that frequently end in errors.

**Layer 2: Next-Tool Suggestions**
When the agent calls tool A and historical data shows tool B follows 60%+ of the time, inject a hint into the system prompt: "After calling {A}, users typically need {B} next." This nudges Claude's tool selection without hardcoding behavior. Hints are generated from chain discovery data and refreshed periodically. Implementation: a `get_chain_hints(tool_name)` function queried after each tool call, results appended to the next assistant turn's context.

**Layer 3: Composite Tool Recipes**
For chains that co-occur 90%+ of the time with consistent parameter threading (output of A feeds into input of B), auto-generate composite tools. Example: `diagnose_pod(pod, namespace)` = `list_resources(kind=Pod) + get_pod_logs(pod) + describe_resource(pod)` in a single tool call. Fewer round trips, faster responses, less context consumed. Recipes are stored in a `tool_recipes` table and registered as real tools at startup.

```sql
CREATE TABLE IF NOT EXISTS tool_recipes (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL,
    chain       TEXT[] NOT NULL,
    frequency   INTEGER NOT NULL,
    co_occurrence_rate FLOAT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    enabled     BOOLEAN DEFAULT FALSE
);
```

Recipes start as `enabled=false` (discovered but not active). Admin reviews and enables them via the UI. Enabled recipes get registered as `@beta_tool` functions that execute the chain sequentially, threading parameters.

**Layer 4: Speculative Preloading**
When tool A is called and tool B follows with >80% probability, speculatively execute B in the background before Claude asks for it. If Claude does request B, the result is served instantly from cache. If not, the result is discarded. Preloading only applies to read-only tools — never speculatively execute write tools. Implementation: after each tool result, check `get_likely_next(tool_name)` and fire-and-forget the top candidate with the same namespace/context parameters.

### Schema Support

Both tables work together for all use cases:
- `tool_usage.turn_number` + `session_id` enables trivial sequence queries without timestamp math
- `tool_turns.tools_offered` vs `tools_called` directly measures harness efficiency
- `tool_turns.query_summary` connects tool patterns to user intent
- `tool_turns.feedback` correlates satisfaction with specific tool sequences
- `tool_usage.result_bytes` identifies tools that waste context with oversized outputs
- `tool_usage.error_category` + `duration_ms` enable quality analysis
- `tool_usage.input_summary` enables parameter pattern detection

## Files to Create/Modify

### Backend (pulse-agent)
- `sre_agent/tool_usage.py` — new module: DB functions (create tables, record, query, stats, chain analysis)
- `sre_agent/db.py` — add `ensure_tool_usage_tables()` to init (both `tool_usage` and `tool_turns` tables)
- `sre_agent/api.py` — add `/agents`, `/tools/usage`, `/tools/usage/stats` endpoints; enhance `/tools`
- `sre_agent/agent.py` — wrap tool execution with audit recording, turn-level tracking, feedback linking
- `sre_agent/harness.py` — expose `get_tool_category(tool_name)` helper
- `sre_agent/tool_chains.py` — chain discovery, next-tool hints, recipe generation, speculative preloading

### Frontend (OpenshiftPulse)
- `src/kubeview/store/toolUsageStore.ts` — new Zustand store for tool/agent data
- `src/kubeview/views/ToolsView.tsx` — new route: catalog + audit log + stats
- `src/kubeview/views/AgentSettingsView.tsx` — add "Tools" tab with summary
- `src/kubeview/components/tools/ToolsCatalog.tsx` — tools catalog component
- `src/kubeview/components/tools/AgentsPanel.tsx` — agents overview component
- `src/kubeview/components/tools/AuditLog.tsx` — audit log table component
- `src/kubeview/components/tools/UsageStats.tsx` — charts/stats component
- `src/kubeview/routes/domainRoutes.tsx` — add `/tools` route

### Tests
- `tests/test_tool_usage.py` — DB functions, recording, query/stats, turn tracking
- `tests/test_tool_chains.py` — chain discovery, hints, recipe generation
- `tests/test_api_tools.py` — new endpoint tests

## API Contract Updates

Add to `API_CONTRACT.md`:
- `GET /agents`
- `GET /tools/usage`
- `GET /tools/usage/stats`
- Enhanced `GET /tools` response schema
