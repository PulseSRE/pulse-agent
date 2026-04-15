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
| `POST` | `/fix-history/{id}/rollback` | token | Rollback a completed action (supported for `restart_deployment`; returns error for unsupported action types) |
| `GET` | `/eval/status` | token | Cached quality gate snapshot (release, safety, integration, outcomes, view_designer) with `dimension_averages` and `prompt_audit` data |
| `GET` | `/eval/history` | token | Paginated eval run history for trend charts (query params: `suite`, `days`, `limit`) |
| `GET` | `/eval/trend` | token | Eval score trend summary with sparkline data (query params: `suite`, `days`) |
| `GET` | `/briefing` | token | Cluster activity summary for last N hours (greeting, actions, investigations) |
| `GET` | `/predictions` | token | Returns empty — predictions are WebSocket-only (`/ws/monitor`) |
| `POST` | `/simulate` | token | Predict impact of a tool action without executing it |
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
| `GET` | `/context` | token | View recent shared context bus entries across all agents |
| `GET` | `/views` | token | List saved views for current user |
| `GET` | `/views/:id` | token | Get a single saved view |
| `POST` | `/views` | token | Save a new view |
| `PUT` | `/views/:id` | token | Update view (title, layout, positions) |
| `DELETE` | `/views/:id` | token | Delete a view |
| `POST` | `/views/:id/clone` | token | Clone a view |
| `POST` | `/views/:id/share` | token | Generate 24h share link |
| `POST` | `/views/claim/:token` | token | Claim a shared view |
| `GET` | `/views/:id/versions` | token | List version history for a view |
| `POST` | `/views/:id/undo` | token | Undo last change to a view |
| `GET` | `/fix-history/summary` | token | Aggregated fix stats: totals, success/rollback rates, by-category with auto_fixed/confirmation_required, trend (query: `days` 1-90) |
| `GET` | `/monitor/coverage` | token | Scanner coverage: active/total scanners, coverage %, category breakdown, per-scanner finding stats (query: `days` 1-90) |
| `GET` | `/monitor/history` | token | Paginated scan run history (query: `limit`, `offset`) |
| `GET` | `/analytics/confidence` | token | Confidence calibration: Brier score, accuracy %, rating (good/fair/poor), prediction buckets (query: `days` 1-365) |
| `GET` | `/analytics/accuracy` | token | Agent accuracy: quality score trend, anti-patterns, learning stats, operator override rate (query: `days` 1-365) |
| `GET` | `/analytics/cost` | token | Token cost per incident with trending, by-mode breakdown (query: `days` 1-365) |
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
| `DELETE` | `/plan-templates/{type}` | token | Delete auto-generated plan templates |

**Authentication:** Token-authenticated endpoints accept `Authorization: Bearer <token>` header or `?token=<token>` query parameter. The token is `PULSE_AGENT_WS_TOKEN`. Unauthenticated requests return 401.

### `/version` Response

```json
{
  "protocol": "2",
  "agent": "2.2.0",
  "tools": 122,
  "skills": 7,
  "features": ["component_specs", "ws_token_auth", "rate_limiting", "monitor", "fix_history", "predictions"]
}
```

The `agent` version is read dynamically from the installed package metadata. The `tools` count is dynamic from the package — the sum of all registered native tools plus discovered MCP tools at startup.

### `/health` Response

```json
{
  "status": "ok",
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
    "task_success": 0.85,
    "safety": 0.96,
    "tool_efficiency": 0.79,
    "operational_quality": 0.81,
    "reliability": 0.88
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
      "dimension_scores": {"task_success": 0.85, "safety": 0.96},
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
| `/ws/sre?token=...` | token | SRE agent chat |
| `/ws/security?token=...` | token | Security scanner chat |
| `/ws/monitor?token=...` | token | Autonomous cluster monitoring (Protocol v2) |
| `/ws/agent?token=...` | token | Auto-routing orchestrated agent — classifies intent per message and routes to SRE or Security |

All WebSocket endpoints require `PULSE_AGENT_WS_TOKEN` via the `token` query parameter. Connections without a valid token are closed with code `4001`.

---

## Chat Protocol (`/ws/sre`, `/ws/security`, `/ws/agent`)

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

### Server-to-Client Events

#### `finding` — Cluster issue detected

```json
{
  "type": "finding",
  "id": "f-abc123",
  "severity": "warning",
  "category": "crash_loop",
  "resource": {"kind": "Pod", "name": "api-server-xyz", "namespace": "production"},
  "summary": "Pod crash-looping: CrashLoopBackOff (5 restarts in 10m)",
  "details": "...",
  "confidence": 0.95,
  "timestamp": 1711540800
}
```

The optional `confidence` field (0.0–1.0) indicates how confident the scanner is that this is a real issue. The optional `noiseScore` field (0.0–1.0) indicates how likely this finding is transient noise (based on historical self-resolution patterns). Findings with `noiseScore >= 0.5` are dimmed in the UI.

#### `prediction` — Predicted future issue

```json
{
  "type": "prediction",
  "id": "p-abc123",
  "category": "resource_pressure",
  "resource": {"kind": "Node", "name": "worker-03"},
  "summary": "Node memory predicted to exceed 90% within 2 hours",
  "confidence": 0.87,
  "horizon": "2h",
  "timestamp": 1711540800
}
```

#### `action_report` — Result of an autonomous or approved action

```json
{
  "type": "action_report",
  "actionId": "a-abc123",
  "findingId": "f-abc123",
  "action": "restart_pod",
  "status": "applied",
  "summary": "Restarted pod api-server-xyz",
  "before": {},
  "after": {},
  "timestamp": 1711540800
}
```

`action_report` may include optional fields:
- `confidence`: `number` (0.0–1.0) — agent's confidence that this action will resolve the issue
- `verificationStatus`: `"verified"` | `"still_failing"`
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

Optional fields: `evidence` (list of facts supporting the diagnosis), `alternativesConsidered` (hypotheses checked and ruled out).

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

The UI shows a "Save Dashboard" prompt. Saved views are accessible at `/custom/:viewId` and persist in localStorage.

#### `view_validation_warning` — Dashboard saved with quality issues

Emitted when the agent's `create_dashboard` call produces components with validation issues (missing structure, generic titles, etc.). The view IS saved (after dedup) so the agent can critique and fix it. Duplicates are silently removed.

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

#### `investigation_progress` — Live investigation phase updates

Emitted during multi-phase investigations to show real-time progress of each phase (tool calls, skill transitions, etc.).

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

#### `skill_activity` — Active skill change or handoff

Emitted when the agent switches skills or a handoff occurs between agent modes.

```json
{
  "type": "skill_activity",
  "data": {
    "skill_name": "sre",
    "status": "active",
    "timestamp": 1711540800000,
    "handoff_from": "security",
    "handoff_to": "sre"
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `skill_name` | `string` | Currently active skill |
| `status` | `string` | Skill status (`"active"`, `"idle"`) |
| `timestamp` | `number` | Unix epoch milliseconds |
| `handoff_from` | `string?` | Previous skill (only on handoff) |
| `handoff_to` | `string?` | New skill (only on handoff) |

#### `error` — Rate limit or other errors

```json
{
  "type": "error",
  "message": "Rate limited. Max 10 messages per minute."
}
```

---

## Agent Protocol (`/ws/agent`)

The `/ws/agent` endpoint uses the same client-to-server and server-to-client message types as the chat protocol (`/ws/sre`, `/ws/security`). The difference is that each incoming `message` is classified by an intent classifier (`orchestrator.py`) and automatically routed to the appropriate agent (SRE or Security) with the correct system prompt and tool set.

### Client-to-Server Messages

- `message`: `{type, content, context?, fleet?}` — same as chat protocol
- `confirm_response`: `{type, approved, nonce}` — same as chat protocol
- `clear`: `{type}` — clears conversation history

### Server-to-Client Events

- `text_delta`, `thinking_delta`, `tool_use`, `component`, `confirm_request` (with nonce), `done`, `error`, `cleared` — same as chat protocol

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
| Pending confirmation TTL | 5 minutes | Agent |
| Context field validation | `^[a-zA-Z0-9\-._/: ]{0,253}$` | Agent |
| Reconnect attempts | 5 max, exponential backoff + jitter | UI |

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
| v5.16.2+ | v2.2.0 | 2 | Current |
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
