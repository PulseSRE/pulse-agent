# Observability

## Logging

All modules use named loggers under the `pulse_agent` namespace:

```
pulse_agent.api      — REST/WebSocket endpoints
pulse_agent.agent    — Agent loop, tool execution
pulse_agent.monitor  — Scanner loop, auto-fix, investigations
pulse_agent          — Root logger for utilities
```

### Log Levels

| Level | Use |
|-------|-----|
| ERROR | Scanner failures, DB errors, K8s API failures |
| WARNING | Degraded state (circuit breaker open, memory init failed, cluster unreachable) |
| INFO | Connection events, tool invocations, scan completions |
| DEBUG | Memory retrieval, feedback recording, internal state |

## Metrics Available

### `/health` endpoint (REST)
- Circuit breaker state (CLOSED/OPEN/HALF_OPEN)
- Error summary by category (last 500 errors)
- Recent errors with timestamps
- Investigation stats (total calls, tokens used)
- Auto-fix paused status

### `/memory/stats` endpoint (REST)
- Incident count
- Learned runbook count
- Detected pattern count
- Evaluation metrics (average scores)

### `/briefing` endpoint (REST)
- Actions taken in last N hours (completed, failed)
- Investigations completed
- Categories fixed

## Audit Trail

Every tool invocation is logged to `/tmp/pulse_agent_audit.log` in structured JSON:
```json
{"timestamp": "...", "tool": "scale_deployment", "input": {...}, "session_id": "..."}
```

Cluster-side audit via `record_audit_entry` tool writes to a ConfigMap.

## Error Tracking

Thread-safe ring buffer (`error_tracker.py`) stores last 500 errors:
- Per-category aggregation (permission, not_found, conflict, validation, server, network, quota)
- Top-tool breakdown
- Error rate trending
```

