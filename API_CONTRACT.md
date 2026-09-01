# Pulse API Contract

**Protocol Version: 2**

Defines the REST and WebSocket protocol between the Pulse UI and Pulse Agent. Both repos must implement the same protocol version for compatibility.

> Source of truth for message schemas. When adding or changing a message type, update this file first, then implement in both repos.

---

## REST Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/healthz` | public | Liveness probe. Returns `{"status": "ok"}` |
| `GET` | `/version` | public | Protocol version, agent version (dynamic from package), tool count, feature flags |
| `GET` | `/health` | token | Circuit breaker state, error summary, investigation stats, autofix_paused status |
| `GET` | `/tools` | token | All tools grouped by mode (sre, security) with `requires_confirmation` flags and `category` |
| `GET` | `/agents` | token | All agent modes with metadata (name, description, tool count, categories, write capability) |
| `GET` | `/tools/usage` | token | Paginated audit log of tool invocations (query params: `tool_name`, `agent_mode`, `status`, `session_id`, `from`, `to`, `page`, `per_page`) |
| `GET` | `/tools/usage/stats` | token | Aggregated tool usage statistics (totals, by tool, by mode, by category, error rates) (query params: `from`, `to`) |
| `GET` | `/fix-history` | token | Paginated fix history with filters (`status`, `category`, `since`, `search`) |
| `GET` | `/fix-history/{id}` | token | Single action detail with before/after state |
| `POST` | `/fix-history/{id}/rollback` | token | Rollback a completed action. Actions with a pre-write snapshot (`restore_snapshot` rollback) are restored from it; `restart_deployment` actions roll back by revision; other action types return an error |
| `GET` | `/eval/status` | token | Cached quality gate snapshot (release, safety, integration, outcomes, view_designer) with `dimension_averages` and `prompt_audit` data |
| `GET` | `/eval/history` | token | Paginated eval run history for trend charts (query params: `suite`, `days`, `limit`) |
| `GET` | `/eval/trend` | token | Eval score trend summary with sparkline data (query params: `suite`, `days`) |
| `GET` | `/briefing` | token | Cluster activity summary for last N hours (greeting, actions, investigations) |
| `GET` | `/memory/export` | token | Export learned runbooks and patterns as JSON |
| `POST` | `/memory/import` | token | Import runbooks and patterns from another pod's export |
| `GET` | `/memory/stats` | token | Memory system stats: incident count, runbook count, pattern count |
| `GET` | `/memory/runbooks` | token | List learned runbooks sorted by success rate |
| `GET` | `/memory/incidents` | token | Search past incidents by query similarity |
| `GET` | `/memory/patterns` | token | List detected recurring patterns |
| `GET` | `/monitor/capabilities` | token | Max trust level and supported auto-fix categories |
| `POST` | `/monitor/pause` | token | Emergency kill switch — pause all auto-fix actions |
| `POST` | `/monitor/resume` | token | Resume auto-fix actions after a pause |
| `GET` | `/tools/usage/chains` | token | Discovered tool call chains (common sequences via bigram analysis) |
| `GET` | `/views` | token | List saved views. Query params: `view_type`, `visibility`, `exclude_status` |
| `GET` | `/views/:id` | token | Get a single saved view |
| `POST` | `/views` | token | Save a new view |
| `PUT` | `/views/:id` | token | Update view (title, layout, positions) |
| `DELETE` | `/views/:id` | token | Delete a view |
| `POST` | `/views/:id/clone` | token | Clone a view |
| `POST` | `/views/:id/share` | token | Generate 24h share link |
| `POST` | `/views/claim/:token` | token | Claim a shared view |
| `GET` | `/views/:id/versions` | token | List version history for a view |
| `POST` | `/views/:id/undo` | token | Undo last change to a view |
| `POST` | `/views/:id/actions` | token + owner | Execute a tool from an action_button component |
| `POST` | `/views/:id/status` | token + owner | Transition view status (incident/plan/assessment lifecycle) |
| `POST` | `/views/:id/claim` | token + owner | Claim a team view |
| `DELETE` | `/views/:id/claim` | token + owner | Release a claim |
| `GET` | `/fix-history/summary` | token | Aggregated fix stats: totals, success/rollback rates, by-category with auto_fixed/confirmation_required, trend (query: `days` 1-90) |
| `GET` | `/monitor/coverage` | token | Scanner coverage: active/total scanners, coverage %, category breakdown, per-scanner finding stats (query: `days` 1-90) |
| `GET` | `/monitor/scanners` | token | Full scanner registry with metadata, checks, and enabled state |
| `GET` | `/analytics/confidence` | token | Confidence calibration: Brier score, accuracy %, rating (good/fair/poor), prediction buckets (query: `days` 1-365) |
| `GET` | `/analytics/accuracy` | token | Agent accuracy: quality score trend, anti-patterns, learning stats, operator override rate (query: `days` 1-365) |
| `GET` | `/analytics/cost` | token | Token cost per incident with trending, by-mode breakdown, 30-day forecast (query: `days` 1-365) |
| `GET` | `/analytics/budget` | token | Investigation budget (used/remaining/max) and optional cost budget status |
| `GET` | `/metrics` | none | Prometheus metrics endpoint (tokens, cost, investigations, scanners, autofix) |
| `GET` | `/analytics/intelligence` | token | 8 intelligence sections as structured dicts: query reliability, error hotspots, token efficiency, harness effectiveness, routing accuracy, feedback analysis, token trending, dashboard patterns (query: `days` 1-90, `mode`) |
| `GET` | `/analytics/prompt` | token | Prompt section breakdown, cache hit rate, version drift history (query: `days` 1-365, `skill`) |
| `GET` | `/recommendations` | token | Contextual capability recommendations: unused scanners, untried features (max 4) |
| `GET` | `/analytics/readiness` | token | Readiness gate summary: pass/fail/attention counts, pass rate, attention items |
| `GET` | `/postmortems` | token | Auto-generated postmortems, newest first (query: `limit` 1-100) |
| `GET` | `/topology` | token | Dependency graph nodes + edges for visualization (query: `namespace` optional filter) |
| `GET` | `/plan-templates` | token | List investigation plan templates |
| `GET` | `/plan-templates/{type}` | token | Get a single plan template by incident type |
| `GET` | `/fix-history/resolutions` | token | Recent resolution outcomes with verification status (query: `days`, `limit`) |
| `GET` | `/slo` | token | Current SLO status with live Prometheus burn rates |
| `POST` | `/slo` | token | Register new SLO definition |
| `DELETE` | `/slo/{service}/{slo_type}` | token | Remove SLO definition |
| `GET` | `/analytics/fix-strategies` | token | Fix strategy effectiveness per category+tool (query: `days` 1-365) |
| `GET` | `/analytics/learning` | token | Agent learning feed: weight updates, scaffolded skills, routing decisions (query: `days` 1-365) |
| `PUT` | `/plan-templates/{type}` | token | Update plan template phases/timeouts |
| `DELETE` | `/plan-templates/{type}` | token | Delete a runtime-created plan template (bundled templates are protected) |
| `POST` | `/plan-templates` | token | Create a new plan template (versioned; phases accept depends_on, branch_on/branches, parallel_with, subplan) |
| `GET` | `/plan-templates/{type}/versions` | token | Version history for one plan template |
| `POST` | `/plan-templates/{type}/run` | token | Start a durable run of this plan on Temporal. 503 with the reason when durable execution is not configured |
| `GET` | `/workflow-runs` | token | Recent durable runs from Temporal's visibility store (query: `limit` 1-100). Each row carries a `memo` labelling what the run is acting on |
| `GET` | `/workflow-runs/{workflow_id}` | token | Status plus live progress for one run, queried from the workflow itself |
| `POST` | `/workflow-runs/{workflow_id}/approve` | token | Deliver a human verdict to a run waiting on approval (body: `phase_id`, `approved`) |
| `POST` | `/workflow-runs/{workflow_id}/cancel` | token | Stop a running workflow (body: optional `reason`). Cooperative: for an incident run this rolls the fix back from its snapshot and records a `cancelled` verdict rather than merely stopping |
| `GET` | `/metrics/fix-success-rate` | token | Auto-fix outcome success rate (query: `period` 1-365 days) |
| `GET` | `/metrics/response-latency` | token | Agent response p50/p95/p99 latency from tool_usage (query: `period` 1-365 days) |
| `GET` | `/metrics/eval-trend` | token | Eval score trend with sparkline (query: `suite`, `releases` 1-50) |
| `GET` | `/kpi` | token | 9 operational KPIs aligned with ORCA targets |
| `GET` | `/analytics/plans` | token | Plan template usage, phase success rate, and duration analytics |
| `GET` | `/activity` | token | Recent agent activity feed (used by the Admin Overview tab) |
| `POST` | `/analytics/events` | token | Fire-and-forget UI session event batch recorder |
| `GET` | `/analytics/sessions` | token | Page-view/session/feature-usage analytics |
| `GET` | `/eval/score` | token | Tool-selection accuracy scoring against static + learned eval prompts |
| `GET` | `/usage/summary` | token | Tool usage split by agent mode vs. pipeline/scanner mode |
| `GET` | `/interactions` | token | Query the `user_interactions` audit log |
| `GET` | `/query` | token | Direct PromQL -> ComponentSpec proxy for live widget refresh (no LLM call) |
| `GET` | `/log-counts` | token | Per-pod log pattern match counts for live-table enrichment |
| `GET` | `/topology/blast-radius` | token | Blast-radius analysis for a resource (query: `kind`, `name`, `namespace`) |
| `GET` | `/incidents/{finding_id}/impact` | token | Business/user impact estimate for a finding |
| `GET` | `/incidents/{finding_id}/learning` | token | What the agent learned from a resolved finding |
| `POST` | `/monitor/simulate` | token | Preview a proposed action's impact/risk/duration without executing it |

#### Chat History (`chat_rest.py`)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/chat/sessions` | token | List chat sessions for the current user |
| `GET` | `/chat/sessions/{session_id}/messages` | token | Get all messages in a session |
| `POST` | `/chat/sessions` | token | Create a new chat session |
| `PUT` | `/chat/sessions/{session_id}` | token | Update session metadata (e.g. title) |
| `DELETE` | `/chat/sessions/{session_id}` | token | Delete a chat session |

#### Ops Inbox (`inbox_rest.py`)

Unified worklist for findings, alerts, and predictions -- replaces the old multi-tab Incident Center in the UI.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/inbox` | token | List inbox items (filters: status, priority, category, assignee). Returns `collapsedIntoEpisodes`: how many items an open episode already explains and were therefore left out  |
| `GET` | `/inbox/stats` | token | Aggregate inbox counts by status/priority |
| `GET` | `/inbox/mutes` | token | List active mutes (declared before `/inbox/{item_id}` so it is reachable) |
| `GET` | `/episodes` | token | Open episodes, newest first, each with cause, symptom count and affected namespaces |
| `GET` | `/episodes/{id}` | token | One episode: `episode`, `symptoms`, `changes` (config/RBAC/deployment activity in the 30m before it began, each with `seconds_before`), `recurrence` (`occurrences`, `window_seconds`, optional `interval_seconds` when the cadence is regular) |
| `POST` | `/episodes/{id}/detach` | token + owner | Record that a symptom was not caused by this episode (`{"correlationKey": "..."}`). Stored, never re-attached |
| `GET` | `/inbox/{item_id}` | token | Get a single inbox item |
| `POST` | `/inbox` | token | Create a new inbox item |
| `PATCH` | `/inbox/{item_id}` | token | Update an inbox item |
| `POST` | `/inbox/{item_id}/claim` | token | Claim an item |
| `DELETE` | `/inbox/{item_id}/claim` | token | Release a claim |
| `POST` | `/inbox/{item_id}/acknowledge` | token | Acknowledge an item |
| `POST` | `/inbox/{item_id}/snooze` | token | Snooze an item |
| `POST` | `/inbox/{item_id}/dismiss` | token | Dismiss an item |
| `POST` | `/inbox/{item_id}/investigate` | token | Trigger investigation for an item |
| `POST` | `/inbox/mute` | user | Mute a `correlation_key` (body: `correlation_key`, `reason`, optional `hours`) |
| `DELETE` | `/inbox/mute/{correlation_key}` | user | Clear a mute |
| `POST` | `/inbox/{item_id}/resolve` | token | Mark an item resolved |
| `POST` | `/inbox/{item_id}/escalate` | token | Escalate an item |
| `POST` | `/inbox/{item_id}/restore` | token | Restore a dismissed/resolved item |
| `POST` | `/inbox/{item_id}/step` | token | Append an investigation step |
| `GET` | `/inbox/{item_id}/investigation` | token | Get the investigation timeline for an item |
| `POST` | `/inbox/{item_id}/runbook` | admin | Draft a runbook skill from an investigated item via the skill lifecycle; lands unreviewed in the Toolbox approval gate. 409 when the item has no investigation |

Inbox item `metadata` optional keys added for queue explainability: `priority_factors` (the score's inputs — severity/layer weights, confidence, noise, age/novelty/due bonuses, total; refreshed on every re-rank), `slo_impact` (registered SLOs the item's namespace/resources back: `[{service, slo_type, target}]`), `recurrence_30d` (which visit this is for the correlation key in 30 days; 1 = first).
| `POST` | `/inbox/{item_id}/pin` | token | Pin an item |

#### Skills, Prompts, and MCP Admin (`skill_rest.py`)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/skills` | token | List all skills with routing rules and metadata |
| `GET` | `/skills/{name}` | token | Get skill detail (prompt, tools, routing, versions) |
| `GET` | `/skills/usage` | token | Aggregated skill usage stats |
| `GET` | `/skills/usage/handoffs` | token | Skill-to-skill handoff analytics |
| `GET` | `/skills/usage/{name}` | token | Per-skill usage stats |
| `GET` | `/skills/usage/{name}/trend` | token | Per-skill usage trend with sparkline |
| `POST` | `/admin/skills/reload` | token | Hot-reload skill packages from disk |
| `POST` | `/admin/skills/test` | token | Test routing -- returns which skill matches a given query |
| `PUT` | `/admin/skills/{name}` | token | Edit skill (prompt, tools, routing rules) |
| `DELETE` | `/admin/skills/{name}` | token | Delete a skill |
| `POST` | `/admin/skills/{name}/clone` | token | Clone a skill with a new name |
| `GET` | `/admin/skills/{name}/versions` | token | Version history for a skill |
| `GET` | `/admin/skills/{name}/diff` | token | Diff between two skill versions |
| `POST` | `/admin/skills/{name}/approve` | admin | Mark an agent-authored skill reviewed, restoring it to automatic routing |
| `POST` | `/admin/skills/{name}/quarantine` | admin | Pull a skill from automatic routing (ORCA + pre-route); stays loadable by name |
| `POST` | `/admin/skills/{name}/unquarantine` | admin | Restore a quarantined skill to automatic routing |
| `GET` | `/prompt/stats` | token | Prompt token cost breakdown |
| `GET` | `/prompt/versions/{skill}` | token | Prompt version history for a skill |
| `GET` | `/prompt/log` | token | Prompt audit log (hash, sections, tokens) |
| `GET` | `/admin/mcp` | token | List MCP server connections and status |
| `POST` | `/admin/mcp/toolsets` | token | Toggle MCP toolsets on/off |
| `POST` | `/admin/mcp` | token | Register a new MCP server connection |
| `DELETE` | `/admin/mcp/{name}` | token | Remove an MCP server connection |
| `POST` | `/admin/mcp/test` | token | Test an MCP server connection |
| `GET` | `/components` | token | Component registry -- list all 25 component kinds with schemas |

#### Debug (`debug_rest.py`)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/debug/memory` | token | RSS/GC/cache/session diagnostics |

**Authentication:** Token-authenticated endpoints accept `Authorization: Bearer <token>` header or `?token=<token>` query parameter. The token is `PULSE_AGENT_WS_TOKEN`. Unauthenticated requests return 401.

### `/version` Response

```json
{
  "protocol": "2",
  "agent": "2.7.1",
  "tools": 138,
  "skills": 7,
  "features": ["component_specs", "ws_token_auth", "rate_limiting", "monitor", "fix_history", "predictions"]
}
```

The `agent` version is read dynamically from the installed package metadata. The `tools` count is dynamic from the package — the sum of all registered native tools plus discovered MCP tools at startup.

### `/health` Response

```json
{
  "status": "ok",
  "database": "ok",
  "circuit_breaker": {
    "state": "closed",
    "failure_count": 0,
    "recovery_timeout": 60
  },
  "errors": {
    "total": 0,
    "by_category": {},
    "recent": []
  },
  "investigations": {},
  "autofix_paused": false
}
```

### `/tools` Response

```json
{
  "sre": [
    {"name": "list_pods", "description": "...", "requires_confirmation": false, "category": "pods"},
    {"name": "delete_pod", "description": "...", "requires_confirmation": true, "category": "pods"}
  ],
  "security": [
    {"name": "scan_pod_security", "description": "...", "requires_confirmation": false, "category": "scanning"}
  ],
  "write_tools": ["apply_yaml", "cordon_node", "delete_pod", "..."]
}
```

### `/agents` Response

```json
[
  {
    "name": "sre",
    "description": "OpenShift cluster diagnostics, incident triage, remediation",
    "tools_count": 70,
    "has_write_tools": true,
    "categories": ["pods", "nodes", "deployments", "services", "config", "logs", "fleet"]
  },
  {
    "name": "security",
    "description": "Pod security, RBAC, network policies, compliance",
    "tools_count": 9,
    "has_write_tools": false,
    "categories": ["scanning", "rbac", "network"]
  }
]
```

### `/tools/usage` Response

```json
{
  "entries": [
    {
      "id": 123,
      "tool_name": "list_pods",
      "agent_mode": "sre",
      "category": "pods",
      "status": "success",
      "duration_ms": 245,
      "result_bytes": 1234,
      "session_id": "sess-abc123",
      "timestamp": "2026-04-03T10:15:30Z",
      "query_summary": "list all pods in default namespace"
    }
  ],
  "total": 1523,
  "page": 1,
  "per_page": 50
}
```

Query parameters:
- `tool_name`: Filter by tool name
- `agent_mode`: Filter by agent mode (`sre`, `security`, `orchestrated`)
- `status`: Filter by status (`success`, `error`)
- `session_id`: Filter by session ID
- `from`: ISO 8601 timestamp (start of time range)
- `to`: ISO 8601 timestamp (end of time range)
- `page`: Page number (default: 1)
- `per_page`: Items per page (default: 50, max: 200)

### `/tools/usage/stats` Response

```json
{
  "total_calls": 1523,
  "unique_tools_used": 42,
  "error_rate": 0.0079,
  "avg_duration_ms": 345,
  "avg_result_bytes": 5120,
  "by_tool": [
    {
      "tool_name": "list_pods",
      "count": 450,
      "error_count": 2,
      "avg_duration_ms": 230,
      "avg_result_bytes": 4800
    },
    {
      "tool_name": "get_pod_logs",
      "count": 380,
      "error_count": 5,
      "avg_duration_ms": 1250,
      "avg_result_bytes": 12500
    }
  ],
  "by_mode": [
    {"mode": "sre", "count": 1400},
    {"mode": "security", "count": 123}
  ],
  "by_category": [
    {"category": "pods", "count": 830},
    {"category": "nodes", "count": 210}
  ],
  "by_status": {
    "success": 1511,
    "error": 12
  }
}
```

Query parameters:
- `from`: ISO 8601 timestamp (start of time range)
- `to`: ISO 8601 timestamp (end of time range)

### `GET /eval/status`

Cached quality gate snapshot. Includes all suites (`release`, `safety`, `integration`, `outcomes`, `view_designer`), per-dimension averages, and prompt token audit data.

```json
{
  "suites": {
    "release": {"score": 0.82, "pass": true, "scenarios": 12},
    "safety": {"score": 0.95, "pass": true, "scenarios": 3},
    "view_designer": {"score": 0.78, "pass": true, "scenarios": 7}
  },
  "dimension_averages": {
    "resolution": 0.85,
    "efficiency": 0.79,
    "safety": 0.96,
    "speed": 0.88
  },
  "prompt_audit": {
    "total_tokens": 4200,
    "sections": [
      {"name": "system_prompt", "tokens": 2100, "pct": 50.0},
      {"name": "runbooks", "tokens": 1200, "pct": 28.6}
    ]
  },
  "timestamp": "2026-04-09T10:00:00Z"
}
```

### `GET /eval/history`

Paginated eval run history for trend charts.

Query parameters:
- `suite`: Filter by suite name (e.g., `release`, `safety`, `view_designer`)
- `days`: Number of days to look back (default: `30`)
- `limit`: Maximum number of results (default: `100`)

Auth: Bearer token.

```json
{
  "runs": [
    {
      "id": 42,
      "suite": "release",
      "score": 0.82,
      "pass": true,
      "scenarios": 12,
      "dimension_scores": {"resolution": 0.85, "safety": 0.96},
      "timestamp": "2026-04-09T10:00:00Z"
    }
  ],
  "total": 150
}
```

### `GET /eval/trend`

Eval score trend summary with sparkline data.

Query parameters:
- `suite`: Suite name (default: `"release"`)
- `days`: Number of days to look back (default: `30`)

Auth: Bearer token.

```json
{
  "suite": "release",
  "current_score": 0.82,
  "trend": "stable",
  "sparkline": [0.80, 0.81, 0.79, 0.82, 0.82],
  "min": 0.79,
  "max": 0.82,
  "runs_count": 5
}
```

---

## WebSocket Endpoints

| Path | Auth | Description |
|------|------|-------------|
| `/ws/agent?token=...` | token | Auto-routing orchestrated agent — classifies intent per message and routes to the appropriate skill |
| `/ws/monitor?token=...` | token | Autonomous cluster monitoring (Protocol v2) |

All WebSocket endpoints require `PULSE_AGENT_WS_TOKEN` via the `token` query parameter. Connections without a valid token are closed with code `4001`.

---

## Chat Protocol (`/ws/agent`)

### Client-to-Server Messages

#### `message` — Send a chat message

```json
{
  "type": "message",
  "content": "Why are pods crash-looping in production?",
  "context": {
    "kind": "Deployment",
    "name": "api-server",
    "namespace": "production",
    "gvr": "apps~v1~deployments"
  },
  "fleet": false
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | `"message"` | yes | |
| `content` | `string` | yes | User's message text |
| `context` | `ResourceContext` | no | Resource the user is viewing |
| `fleet` | `boolean` | no | Enable fleet/multi-cluster mode |

#### `ResourceContext`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `kind` | `string` | yes | K8s resource kind (e.g., `"Deployment"`) |
| `name` | `string` | yes | Resource name |
| `namespace` | `string` | no | Resource namespace (omit for cluster-scoped) |
| `gvr` | `string` | no | GVR key (`group~version~plural`) |

#### `confirm_response` — Respond to a confirmation request

```json
{
  "type": "confirm_response",
  "approved": true,
  "nonce": "abc123..."
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | `"confirm_response"` | yes | |
| `approved` | `boolean` | yes | Whether the user approved the action |
| `nonce` | `string` | yes | Must match the nonce from `confirm_request` (replay prevention) |

#### `clear` — Clear conversation history

```json
{
  "type": "clear"
}
```

#### `feedback` — Thumbs up/down on the last response

```json
{
  "type": "feedback",
  "resolved": true,
  "messageId": "msg-abc123"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | `"feedback"` | yes | |
| `resolved` | `boolean` | yes | Whether the response resolved the user's issue |
| `messageId` | `string` | no | ID of the message being rated |

Triggers memory learning -- the server responds with `feedback_ack`.

### Server-to-Client Events

#### `text_delta` — Streaming text chunk

```json
{
  "type": "text_delta",
  "text": "The pods are crash-looping because"
}
```

#### `thinking_delta` — Streaming thinking/reasoning chunk

```json
{
  "type": "thinking_delta",
  "thinking": "Let me check the pod logs first..."
}
```

#### `tool_use` — Tool execution started

```json
{
  "type": "tool_use",
  "tool": "get_pod_logs"
}
```

#### `component` — Structured UI component from tool result

```json
{
  "type": "component",
  "tool": "list_pods",
  "spec": {
    "kind": "data_table",
    "title": "Pods in production",
    "columns": [
      {"id": "name", "header": "Name"},
      {"id": "status", "header": "Status"}
    ],
    "rows": [
      {"name": "api-server-abc", "status": "Running"}
    ]
  }
}
```

See [Component Specs](#component-specs) for all `spec.kind` values.

#### `confirm_request` — Request user confirmation for a dangerous action

```json
{
  "type": "confirm_request",
  "tool": "delete_resource",
  "input": {"kind": "Pod", "name": "my-pod", "namespace": "default"},
  "nonce": "abc123..."
}
```

| Field | Type | Description |
|-------|------|-------------|
| `tool` | `string` | Tool name requiring confirmation |
| `input` | `object` | Tool input parameters (shown to user) |
| `nonce` | `string` | JIT nonce for replay prevention — client must echo this back |

#### `done` — Agent turn complete

```json
{
  "type": "done",
  "full_response": "The pods are crash-looping because..."
}
```

#### `error` — Error message

```json
{
  "type": "error",
  "message": "Rate limited. Max 10 messages per minute."
}
```

#### `cleared` — Conversation history cleared

```json
{
  "type": "cleared"
}
```

#### `feedback_ack` — Response to a `feedback` message

```json
{
  "type": "feedback_ack",
  "resolved": true,
  "score": 0.92,
  "runbookExtracted": false
}
```

| Field | Type | Description |
|-------|------|-------------|
| `resolved` | `boolean` | Echoes the client's `resolved` value |
| `score` | `number` | Memory score assigned to the interaction |
| `runbookExtracted` | `boolean` | Whether a new learned runbook was extracted from this interaction |

#### `session_expired` — The user's OAuth token expired mid-turn

```json
{
  "type": "session_expired"
}
```

Sent when a tool call fails with a 401 (`error_category == "unauthorized"`), signaling the user needs to re-authenticate.

#### `view_updated` — An existing view was modified mid-turn

```json
{
  "type": "view_updated",
  "viewId": "cv-abc123"
}
```

Sent when the agent merges new widgets into an already-saved view (as opposed to creating a new one via `view_spec`). The UI should refetch the view.

---

## Monitor Protocol (`/ws/monitor`)

### Client-to-Server Messages

#### `subscribe_monitor` — Subscribe to cluster monitoring

Sent as the first message after connecting to `/ws/monitor`. Configures the monitoring session.

```json
{
  "type": "subscribe_monitor",
  "trustLevel": 1,
  "autoFixCategories": ["crash_loop", "resource_pressure"]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | `"subscribe_monitor"` | yes | |
| `trustLevel` | `integer` | no | Autonomous action trust level (0-4). Clamped to server-configured max. Default: `1` |
| `autoFixCategories` | `string[]` | no | Categories the agent may auto-fix without prompting |

#### `trigger_scan` — Trigger an immediate cluster scan

```json
{
  "type": "trigger_scan"
}
```

Triggers an immediate cluster scan. If a scan is already in progress, returns an error. Results are pushed as `finding` and `monitor_status` events.

#### `action_response` — Respond to an autonomous action proposal

```json
{
  "type": "action_response",
  "actionId": "abc123",
  "approved": true
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | `"action_response"` | yes | |
| `actionId` | `string` | yes | ID of the proposed action |
| `approved` | `boolean` | yes | Whether the user approved the action |

#### `get_fix_history` — Request fix history

```json
{
  "type": "get_fix_history",
  "page": 1,
  "filters": {"status": "applied", "category": "crash_loop"}
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | `"get_fix_history"` | yes | |
| `page` | `integer` | no | Page number (default: `1`) |
| `filters` | `object` | no | Optional filters (`status`, `category`, `since`, `search`) |

#### `set_disabled_scanners` — Dynamically disable specific scanners

```json
{
  "type": "set_disabled_scanners",
  "scannerIds": ["daemonsets", "audit_events"]
}
```

Server acknowledges with an `ack` event.

### Server-to-Client Events

#### `finding` — Cluster issue detected

```json
{
  "type": "finding",
  "id": "f-abc123",
  "severity": "warning",
  "category": "crash_loop",
  "title": "Pod crash-looping",
  "resources": [{"kind": "Pod", "name": "api-server-xyz", "namespace": "production"}],
  "summary": "Pod crash-looping: CrashLoopBackOff (5 restarts in 10m)",
  "confidence": 0.95,
  "autoFixable": true,
  "runbookId": "crashloop",
  "findingType": "current",
  "timestamp": 1711540800
}
```

**Note:** `resources` is a plural array (not a single `resource` object) since a finding can span multiple resources. There is no `details` field. The optional `confidence` field (0.0–1.0) indicates how confident the scanner is that this is a real issue. The optional `noiseScore` field (0.0–1.0) indicates how likely this finding is transient noise (based on historical self-resolution patterns) -- findings with `noiseScore >= 0.5` are dimmed in the UI. `autoFixable`, `runbookId`, and `findingType` (`"current"` | predictive variants) are also present on the real payload.

#### `prediction` — Predicted future issue

```json
{
  "type": "prediction",
  "id": "p-abc123",
  "category": "resource_pressure",
  "title": "Node memory pressure predicted",
  "resources": [{"kind": "Node", "name": "worker-03"}],
  "detail": "Node memory predicted to exceed 90% within 2 hours",
  "confidence": 0.87,
  "eta": "2h",
  "recommendedAction": "Consider adding a worker node or reducing requests",
  "timestamp": 1711540800
}
```

**Note:** the real field names are `resources` (plural array), `detail` (not `summary`), and `eta` (not `horizon`). `title` and `recommendedAction` are also present on the real payload but were previously undocumented.

#### `action_report` — Result of an autonomous or approved action

```json
{
  "type": "action_report",
  "id": "a-abc123",
  "findingId": "f-abc123",
  "tool": "restart_deployment",
  "input": {"namespace": "production", "name": "api-server"},
  "status": "applied",
  "beforeState": "replicas=3, ready=1",
  "afterState": "replicas=3, ready=3",
  "reasoning": "Pod was crash-looping; restart cleared the bad in-memory state",
  "durationMs": 842,
  "timestamp": 1711540800
}
```

**Note:** the ID field is `id`, not `actionId`; the tool name field is `tool`, not `action`; there is no `summary` field; `beforeState`/`afterState` are **strings** (human-readable snapshots), not objects. `input` (the tool's input dict), `error` (on failure), `reasoning`, and `durationMs` are also present on the real payload but were previously undocumented.

`status` values: `proposed` (awaiting operator approval), `executing`, `completed`, `failed` (execution failed, or the operator rejected the proposal), `expired` (nobody answered within the approval window — the fix never ran), `rolled_back`.

`action_report` may include optional fields:
- `confidence`: `number` (0.0–1.0) — agent's confidence that this action will resolve the issue
- `error`: `string` — present when `status` is `failed` or `expired`
- `verificationStatus`: `"verified"` | `"still_failing"` | `"improved"` | `"unverifiable"` | `"verified_then_recurred"`
- `verificationEvidence`: `string`
- `verificationTimestamp`: `number`

#### `investigation_report` — Proactive root-cause analysis for critical findings

```json
{
  "type": "investigation_report",
  "id": "i-abc123",
  "findingId": "f-abc123",
  "category": "crashloop",
  "status": "completed",
  "summary": "Crashloop due to missing ConfigMap key",
  "suspectedCause": "ConfigMap key removed in recent rollout",
  "recommendedFix": "Restore key and restart deployment",
  "confidence": 0.82,
  "evidence": ["ConfigMap 'app-config' key 'DB_HOST' missing since rollout at 14:32", "Pod logs show KeyError on startup"],
  "alternativesConsidered": ["Image pull failure ruled out — image exists and pulled successfully"],
  "timestamp": 1711540800
}
```

Optional fields (per design): `evidence` (list of facts supporting the diagnosis), `alternativesConsidered` (hypotheses checked and ruled out).

> **Known gap:** as of this writing, `investigations.py` computes `evidence`
> and `alternatives_considered` internally, but `investigation_runner.py`'s
> outbound payload construction does not copy them onto the broadcast
> `investigation_report` event -- they are silently dropped before reaching
> the UI. Either wire them through or remove them from this doc; tracked
> here so it isn't lost.

#### `verification_report` — Next-scan validation after a fix action

```json
{
  "type": "verification_report",
  "id": "v-abc123",
  "actionId": "a-abc123",
  "findingId": "f-abc123",
  "status": "verified",
  "evidence": "No active crashloop findings for affected resources",
  "timestamp": 1711540800
}
```

`status` values: `verified`, `still_failing`, `improved` (namespace-level count dropped), `unverifiable` (the cluster could not be read — a fact about the check, not the fix), and `verified_then_recurred`. The last is emitted retroactively: a verdict of `verified` has a time horizon, and when the same condition (same correlation key) returns as a new finding within the recurrence window (`PULSE_AGENT_RECURRENCE_WINDOW`, default 1800s) after verification, the original action's verdict is downgraded and a fresh `verification_report` with `verified_then_recurred` is broadcast carrying the original `actionId` and the *new* finding's `findingId`. The action row's `outcome` becomes `recurred`, which counts against the fix success rate.

#### `resolution` — Issue resolved (proactive win)

Emitted when a previously active finding disappears from the scan results. Enables the UI to celebrate wins and track resolution attribution.

```json
{
  "type": "resolution",
  "findingId": "f-abc123",
  "category": "crashloop",
  "title": "Pod api-server-xyz crash-looping resolved",
  "resolvedBy": "auto-fix",
  "timestamp": 1711540800
}
```

`resolvedBy` values: `"auto-fix"` (monitor applied a fix), `"self-healed"` (issue disappeared without intervention).

#### `view_spec` — AI-generated custom dashboard

Emitted when the agent calls `create_dashboard`. Contains a collection of component specs that the UI can save as a persistent custom view.

```json
{
  "type": "view_spec",
  "spec": {
    "id": "cv-abc123",
    "title": "SRE Overview",
    "description": "Node health, crashlooping pods, RBAC risks",
    "layout": [
      {"kind": "data_table", "title": "...", "columns": [...], "rows": [...]},
      {"kind": "chart", "title": "...", "series": [...]}
    ],
    "generatedAt": 1711540800000
  }
}
```

The UI shows a "Save Dashboard" prompt. Saved views are accessible at `/custom/:viewId` and persist in PostgreSQL.

#### `view_validation_warning` — Dashboard saved with quality issues

Emitted when the agent's `create_dashboard` call produces components with validation issues (missing structure, generic titles, etc.). The view IS saved (after dedup) so the agent can critique and fix it. Duplicates are silently removed.

**Normalization**: Before validation, all component specs are normalized via `normalize_layout()` — field aliases are fixed automatically (e.g. `label`→`name` for status_list items, `values`→`data` for chart series, `props` wrappers are flattened). This runs on both WS and REST save paths.

```json
{
  "type": "view_validation_warning",
  "errors": ["Dashboard must include at least one chart.", "Generic title 'Table' — provide a descriptive title."],
  "warnings": ["PromQL has unbalanced braces {} in: rate(cpu[5m]"],
  "deduped_count": 2
}
```

| Field | Type | Description |
|-------|------|-------------|
| `errors` | `string[]` | Quality issues detected (view saved anyway) |
| `warnings` | `string[]` | Non-blocking PromQL or quality warnings |
| `deduped_count` | `number` | Number of duplicate components that were removed |

#### `findings_snapshot` — Active findings reconciliation

Sent after each scan cycle. Contains the IDs of all currently active findings. The UI removes any locally-held findings whose IDs are not in `activeIds`, preventing stale entries from accumulating after issues are resolved.

```json
{
  "type": "findings_snapshot",
  "activeIds": ["f-abc123", "f-def456"],
  "timestamp": 1711540800
}
```

| Field | Type | Description |
|-------|------|-------------|
| `activeIds` | `string[]` | IDs of all findings that are still active |
| `timestamp` | `number` | Unix timestamp of the snapshot |

#### `monitor_status` — Scan cycle status update

```json
{
  "type": "monitor_status",
  "activeWatches": ["crashloop", "pending", "workloads", "nodes", "cert_expiry", "alerts", "oom", "image_pull", "operators", "daemonsets", "hpa"],
  "lastScan": 1711540800,
  "findingsCount": 3,
  "nextScan": 1711540860
}
```

#### `scan_report` — Per-scanner timing and results

Emitted after each scan cycle completes. Includes per-scanner timing, findings count, and status.

```json
{
  "type": "scan_report",
  "scanId": 42,
  "duration_ms": 1234,
  "total_findings": 5,
  "scanners": [
    {
      "name": "crashloop",
      "displayName": "Crashlooping Pods",
      "description": "Detects pods with restart count above threshold",
      "duration_ms": 123,
      "findings_count": 2,
      "checks": ["restart count > threshold", "container state = CrashLoopBackOff"],
      "status": "warning"
    },
    {
      "name": "pending",
      "displayName": "Pending Pods",
      "description": "Finds pods stuck in Pending state for >5 minutes",
      "duration_ms": 89,
      "findings_count": 0,
      "checks": ["pod phase = Pending", "age > 5 minutes"],
      "status": "clean"
    },
    {
      "name": "security",
      "displayName": "Security Posture",
      "description": "Comprehensive security check: pod security, resource limits, network policies, RBAC, service accounts",
      "duration_ms": 567,
      "findings_count": 3,
      "checks": ["privileged containers", "missing resource limits", "missing health probes", "default service account", "untrusted registries", "missing network policies", "cluster-admin bindings", "secret rotation > 90 days"],
      "status": "warning"
    }
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `scanId` | `number` | Sequential scan counter (increments each scan) |
| `duration_ms` | `number` | Total scan duration in milliseconds |
| `total_findings` | `number` | Total findings across all scanners |
| `scanners` | `array` | Per-scanner results |
| `scanners[].name` | `string` | Scanner identifier (matches SCANNER_REGISTRY key) |
| `scanners[].displayName` | `string` | Human-readable scanner name |
| `scanners[].description` | `string` | Scanner description |
| `scanners[].duration_ms` | `number` | Scanner execution time in milliseconds |
| `scanners[].findings_count` | `number` | Number of findings from this scanner |
| `scanners[].checks` | `array` | List of checks performed by this scanner |
| `scanners[].status` | `string` | Scanner status: `"clean"`, `"warning"`, or `"error"` |
| `scanners[].error` | `string?` | Error message if scanner failed (status = "error") |

**Notes:**
- The security scanner runs every 3rd scan (scanId % 3 == 0) to reduce overhead
- Scan reports are persisted to the `scan_runs` table with session_id for historical analysis
- Scanner timing can be used to identify slow scanners and optimize scan cycles

#### `fix_history` — Response to `get_fix_history`

```json
{
  "type": "fix_history",
  "items": [],
  "total": 0,
  "page": 1,
  "pageSize": 20
}
```

#### `ack` — Acknowledgment of `set_disabled_scanners`

```json
{
  "type": "ack",
  "message": "Disabled 2 scanners"
}
```

#### `investigation_progress` — Live investigation phase updates

Emitted during multi-phase investigations to show real-time progress of each phase (tool calls, skill transitions, etc.).

`status` is one of `pending`, `running`, `complete`, `partial`, `failed`, `skipped`.

`partial` means the phase ran but did not produce the output fields it declared in
its plan template's `produces` list — it was asked again with the gap named and
still did not supply them. When that happens the phase carries `unmetContract`,
listing what is missing. A `partial` phase is not a less confident `complete`
one: the plan advanced without something it was supposed to have, so anything
downstream that depended on those fields was working without them.

```json
{
  "type": "investigation_progress",
  "findingId": "f-abc123",
  "phases": [
    {
      "id": "phase-1",
      "status": "complete",
      "skill_name": "sre",
      "summary": "Gathered pod logs and events",
      "confidence": 0.85
    },
    {
      "id": "phase-2",
      "status": "running",
      "skill_name": "security",
      "summary": "Scanning RBAC permissions",
      "confidence": 0.0
    },
    {
      "id": "phase-3",
      "status": "pending",
      "skill_name": "sre",
      "summary": "",
      "confidence": 0.0
    }
  ],
  "planId": "plan-abc123",
  "planName": "Crashloop Investigation",
  "timestamp": 1711540800
}
```

| Field | Type | Description |
|-------|------|-------------|
| `findingId` | `string` | ID of the finding being investigated |
| `phases` | `array` | Ordered list of investigation phases |
| `phases[].id` | `string` | Phase identifier |
| `phases[].status` | `string` | Phase status: `"pending"`, `"running"`, `"complete"`, `"failed"`, `"skipped"` |
| `phases[].skill_name` | `string` | Skill executing this phase |
| `phases[].summary` | `string` | Human-readable phase summary (empty while pending) |
| `phases[].confidence` | `number` | Confidence score (0.0–1.0) for phase result |
| `planId` | `string` | Investigation plan identifier |
| `planName` | `string` | Human-readable plan name |
| `timestamp` | `number` | Unix timestamp |

#### `error` — Rate limit or other errors

```json
{
  "type": "error",
  "message": "Rate limited. Max 10 messages per minute."
}
```

---

## Agent Protocol (`/ws/agent`)

The `/ws/agent` endpoint is the primary chat endpoint. Each incoming `message` is classified by the ORCA skill selector and automatically routed to the appropriate skill with the correct system prompt and tool set.

### Client-to-Server Messages

- `message`: `{type, content, context?, fleet?}` — same as chat protocol
- `confirm_response`: `{type, approved, nonce}` — same as chat protocol
- `clear`: `{type}` — clears conversation history

### Server-to-Client Events

- `text_delta`, `thinking_delta`, `tool_use`, `component`, `confirm_request` (with nonce), `done`, `error`, `cleared` — same as chat protocol

#### `multi_skill_start` — Parallel multi-skill execution started

Emitted when ORCA detects that two skills should run in parallel (score gap <= threshold and no conflicts).

```json
{
  "type": "multi_skill_start",
  "skills": ["sre", "security"]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `skills` | `string[]` | Names of the skills running in parallel (always 2) |

#### `skill_progress` — Individual skill status update

Emitted during parallel execution for tool activity, tool completion, skill completion, and synthesis phase.

```json
{
  "type": "skill_progress",
  "skill": "sre",
  "status": "tool_use",
  "tool": "list_pods"
}
```

```json
{
  "type": "skill_progress",
  "skill": "sre",
  "status": "tool_complete",
  "tool": "list_pods",
  "duration_ms": 2300
}
```

| Field | Type | Description |
|-------|------|-------------|
| `skill` | `string` | Skill name or `"synthesis"` for the merge step |
| `status` | `string` | `"tool_use"`, `"tool_complete"`, `"complete"`, or `"running"` |
| `tool` | `string?` | Tool name (present for `tool_use` and `tool_complete`) |
| `duration_ms` | `number?` | Tool execution duration (present for `tool_complete`) |

#### `done` (multi-skill extended) — Merged response with conflict metadata

When multi-skill execution completes, the `done` event includes additional fields:

```json
{
  "type": "done",
  "full_response": "Merged analysis from both skills...",
  "skill_name": "sre",
  "multi_skill": {
    "skills": ["sre", "security"],
    "conflicts": [
      {
        "topic": "root cause",
        "skill_a": "sre",
        "position_a": "OOM kill due to memory leak",
        "skill_b": "security",
        "position_b": "Pod evicted by resource quota policy"
      }
    ],
    "empty_skill": null
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `multi_skill` | `object` | Present only during multi-skill turns |
| `multi_skill.skills` | `string[]` | Skills that ran in parallel |
| `multi_skill.conflicts` | `Conflict[]` | Contradictions detected during synthesis (may be empty) |
| `multi_skill.empty_skill` | `string?` | If one skill returned no output, its name appears here |

**Empty output handling:** If one skill returns empty output (timeout, failure), synthesis is skipped. The non-empty skill's output is returned directly with a note, and `multi_skill.empty_skill` identifies the failed skill.

---

## Component Specs

Structured UI components returned by agent tools via the `component` event. The UI renders these inline in the chat.

| `kind` | Description | Key Fields |
|--------|-------------|------------|
| `data_table` | Sortable table | `columns[]`, `rows[]` |
| `info_card_grid` | Metric cards | `cards[]{label, value, sub?}` |
| `badge_list` | Colored badges | `badges[]{text, variant}` |
| `status_list` | Health status items | `items[]{name, status, detail?}` |
| `key_value` | Key-value pairs | `pairs[]{key, value}` |
| `chart` | Time-series chart | `series[]{label, data[][], color?}` |
| `tabs` | Tabbed content | `tabs[]{label, content: ComponentSpec}` |
| `grid` | Grid layout | `columns`, `items: ComponentSpec[]` |
| `section` | Titled section | `title`, `content: ComponentSpec` |
| `relationship_tree` | Resource hierarchy | `nodes[]`, `rootId` |
| `log_viewer` | Pod log stream | `lines[]{timestamp?, level?, message}` |
| `yaml_viewer` | YAML/JSON viewer | `content`, `language?` |
| `metric_card` | KPI with sparkline | `title`, `value`, `query?`, `status?` |
| `node_map` | Node topology | `nodes[]{name, status, cpuPct?, memPct?}` |
| `bar_list` | Horizontal ranked bars | `items[]{label, value, badge?, href?}` |
| `progress_list` | Utilization bars | `items[]{label, value, max, unit?}`, `thresholds?` |
| `stat_card` | Single big KPI | `title`, `value`, `unit?`, `trend?`, `trendValue?`, `status?` |

### Badge Variants

`success` | `warning` | `error` | `info` | `default`

### Status Values

`healthy` | `warning` | `error` | `pending` | `unknown`

---

## Constraints

| Constraint | Value | Enforced By |
|------------|-------|-------------|
| Max message size | 1 MB | Agent |
| Rate limit | 10 messages/minute per connection | Agent |
| Confirmation timeout | 120 seconds | Agent |
| Pending confirmation TTL | 120 seconds | Agent |
| Context field validation | `^[a-zA-Z0-9\-._/: ]{0,253}$` | Agent |
| Reconnect attempts | 5 max, linear backoff + jitter | UI |

---

## Version Compatibility

The UI sends a `GET /version` request before connecting. If the agent's `protocol` field doesn't match the UI's `EXPECTED_PROTOCOL`, the UI shows a warning but still connects (graceful degradation).

### Protocol Version History

| Version | Changes | UI Version | Agent Version |
|---------|---------|------------|---------------|
| `2` | `/ws/monitor` for autonomous scanning, `/ws/agent` for auto-routing orchestration, `subscribe_monitor` / `trigger_scan` / `action_response` / `get_fix_history` client messages, `finding` / `prediction` / `action_report` / `investigation_report` / `verification_report` / `findings_snapshot` / `monitor_status` server events, fix history / predictions / memory / context REST endpoints, monitor pause/resume, nonce-based confirmation replay prevention | v5.12.0+ | v1.4.0+ |
| `1` | Initial protocol: text/thinking streaming, tool use, components, confirmations | v5.0.0+ | v1.0.0+ |

### Release Compatibility Matrix

| UI Version | Agent Version | Protocol | Status |
|------------|--------------|----------|--------|
| v2.7.1 | v2.7.1 | 2 | Current |
| v6.2.0 | v2.3.0 | 2 | Compatible (pre versioning reset) |
| v5.16.2+ | v2.2.0 | 2 | Compatible |
| v5.16.2+ | v2.1.0 | 2 | Compatible |
| v5.16.2+ | v2.0.0 | 2 | Compatible |
| v5.16.2+ | v1.13.1 | 2 | Compatible |
| v5.16.2+ | v1.13.0 | 2 | Compatible |
| v5.16.2+ | v1.12.0 | 2 | Compatible |
| v5.14.0+ | v1.9.0 | 2 | Compatible |
| v5.14.0+ | v1.7.0-v1.8.0 | 2 | Compatible |
| v5.13.0+ | v1.5.3-v1.6.1 | 2 | Compatible |
| v5.12.0 | v1.4.0 | 2 | Compatible |
| v5.6.0+ | v1.1.0-v1.3.0 | 1 | Compatible |
| v5.3.0+ | v1.0.0 | 1 | Compatible |

> Both repos should tag releases together when protocol changes occur. Minor UI/Agent releases within the same protocol version are always compatible.
>
> **Versioning reset:** the UI's version numbering was reset from the `v6.x` line to a fresh `v2.x` line (coinciding with the org move to PulseSRE / rename to `pulse-ui`). Both repos now share the same version number for each release (e.g. `v2.7.1` / `v2.7.1`).
