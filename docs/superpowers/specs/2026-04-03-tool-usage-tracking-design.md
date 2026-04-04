# Tool Usage Tracking & Tools/Agents UI

## Overview

Full audit logging of every tool invocation in PostgreSQL, plus UI for browsing all agents, tools, and usage history.

## Database Schema

New `tool_usage` table in PostgreSQL:

```sql
CREATE TABLE IF NOT EXISTS tool_usage (
    id              SERIAL PRIMARY KEY,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    session_id      TEXT NOT NULL,
    agent_mode      TEXT NOT NULL,
    tool_name       TEXT NOT NULL,
    tool_category   TEXT,
    input_summary   JSONB,
    status          TEXT NOT NULL,
    error_message   TEXT,
    error_category  TEXT,
    duration_ms     INTEGER,
    requires_confirmation BOOLEAN DEFAULT FALSE,
    was_confirmed   BOOLEAN
);

CREATE INDEX idx_tool_usage_timestamp ON tool_usage(timestamp DESC);
CREATE INDEX idx_tool_usage_tool_name ON tool_usage(tool_name);
CREATE INDEX idx_tool_usage_session ON tool_usage(session_id);
CREATE INDEX idx_tool_usage_mode ON tool_usage(agent_mode);
CREATE INDEX idx_tool_usage_status ON tool_usage(status);
```

- `agent_mode`: one of sre, security, view_designer, both, agent
- `input_summary`: sanitized JSON (no secrets, truncated to 1KB) using existing sanitization patterns
- `status`: "success" or "error"
- `error_category`: from ToolError classification (7 categories)
- `was_confirmed`: NULL for non-write tools, true/false for write tools

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
      "agent_mode": "sre",
      "tool_name": "get_pod_logs",
      "tool_category": "diagnostics",
      "input_summary": {"pod_name": "web-1", "namespace": "prod"},
      "status": "success",
      "error_message": null,
      "error_category": null,
      "duration_ms": 342,
      "requires_confirmation": false,
      "was_confirmed": null
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
  "by_tool": [
    {"tool_name": "get_pod_logs", "count": 142, "error_count": 3, "avg_duration_ms": 310},
    {"tool_name": "list_resources", "count": 98, "error_count": 1, "avg_duration_ms": 220}
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
  "by_status": {"success": 1232, "error": 52}
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
3. Agent mode and session ID (available from WebSocket handler context)
4. Result status (success/error) and error details
5. Duration (end - start)

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
- Columns: timestamp, tool name, agent mode, status, duration, session ID
- Expandable rows showing input_summary and error details
- Filters: tool name (dropdown), mode, status, date range
- Pagination controls

### Stats Panel

- Top 10 tools bar chart
- Calls over time (line chart, hourly buckets)
- Error rate by tool (table sorted by error rate desc)
- Usage by category (pie/donut chart)

## Files to Create/Modify

### Backend (pulse-agent)
- `sre_agent/tool_usage.py` — new module: DB functions (create table, record, query, stats)
- `sre_agent/db.py` — add `ensure_tool_usage_table()` to init
- `sre_agent/api.py` — add `/agents`, `/tools/usage`, `/tools/usage/stats` endpoints; enhance `/tools`
- `sre_agent/agent.py` — wrap tool execution with audit recording
- `sre_agent/harness.py` — expose `get_tool_category(tool_name)` helper

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
- `tests/test_tool_usage.py` — DB functions, recording, query/stats
- `tests/test_api_tools.py` — new endpoint tests

## API Contract Updates

Add to `API_CONTRACT.md`:
- `GET /agents`
- `GET /tools/usage`
- `GET /tools/usage/stats`
- Enhanced `GET /tools` response schema
