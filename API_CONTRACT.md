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
| `GET` | `/tools` | token | All tools grouped by mode (sre, security) with `requires_confirmation` flags |
| `GET` | `/fix-history` | token | Paginated fix history with filters (`status`, `category`, `since`, `search`) |
| `GET` | `/fix-history/{id}` | token | Single action detail with before/after state |
| `POST` | `/fix-history/{id}/rollback` | token | Attempt rollback (returns error — rollback not currently supported) |
| `GET` | `/eval/status` | token | Cached quality gate snapshot (release, safety, integration, outcomes) |
| `GET` | `/predictions` | token | Returns empty — predictions are WebSocket-only (`/ws/monitor`) |
| `GET` | `/memory/export` | token | Export learned runbooks and patterns as JSON |
| `POST` | `/memory/import` | token | Import runbooks and patterns from another pod's export |
| `GET` | `/monitor/capabilities` | token | Max trust level and supported auto-fix categories |
| `POST` | `/monitor/pause` | token | Emergency kill switch — pause all auto-fix actions |
| `POST` | `/monitor/resume` | token | Resume auto-fix actions after a pause |
| `GET` | `/context` | token | View recent shared context bus entries across all agents |

**Authentication:** Token-authenticated endpoints accept `Authorization: Bearer <token>` header or `?token=<token>` query parameter. The token is `PULSE_AGENT_WS_TOKEN`. Unauthenticated requests return 401.

### `/version` Response

```json
{
  "protocol": "2",
  "agent": "1.5.0",
  "tools": 109,
  "features": ["component_specs", "ws_token_auth", "rate_limiting", "monitor", "fix_history", "predictions"]
}
```

The `agent` version is read dynamically from the installed package metadata. The `tools` count is the sum of SRE + Security tools.

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
    {"name": "list_pods", "description": "...", "requires_confirmation": false},
    {"name": "delete_pod", "description": "...", "requires_confirmation": true}
  ],
  "security": [
    {"name": "scan_pod_security", "description": "...", "requires_confirmation": false}
  ],
  "write_tools": ["apply_yaml", "cordon_node", "delete_pod", "..."]
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
  "timestamp": 1711540800
}
```

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

`action_report` may include optional verification fields once post-fix verification completes:
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
  "timestamp": 1711540800
}
```

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
| v5.13.0 | v1.5.0 | 2 | Current |
| v5.12.0 | v1.4.0 | 2 | Compatible |
| v5.11.0 | v1.3.0 | 1 | Compatible |
| v5.10.0 | v1.3.0 | 1 | Compatible |
| v5.8.0 | v1.2.0 | 1 | Compatible |
| v5.0.0-v5.7.0 | v1.0.0-v1.1.0 | 1 | Compatible |

> Both repos should tag releases together when protocol changes occur. Minor UI/Agent releases within the same protocol version are always compatible.
