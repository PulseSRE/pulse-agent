# Tool Chain Intelligence — Layers 1 & 2

> Historical design/implementation record. This preserves the proposal and tasks at the date in the filename; unchecked tasks, paths, commands, and security claims are not evidence of the current implementation. Use [current documentation](../../README.md) and verify behavior against code before applying a procedure.


## Overview

Mine the `tool_usage` audit data to discover common tool call sequences and inject next-tool hints into the agent's system prompt. Two layers:

1. **Chain Discovery** — SQL queries that extract frequent 2-tool and 3-tool sequences from `tool_usage`, surfaced via a new stats endpoint and shown in the UI
2. **Next-Tool Hints** — When the agent calls tool A and historical data shows tool B follows 60%+ of the time, append a hint to the cluster context section of the system prompt

## Layer 1: Chain Discovery

### Query

Extract tool sequences from `tool_usage` by ordering within `(session_id, turn_number)`. A bigram is two consecutive tools in the same session. A trigram is three.

```sql
-- Bigrams: consecutive tool pairs within a session
WITH ordered AS (
    SELECT session_id, tool_name,
           LAG(tool_name) OVER (PARTITION BY session_id ORDER BY turn_number, id) AS prev_tool
    FROM tool_usage
    WHERE status = 'success'
)
SELECT prev_tool, tool_name AS next_tool, COUNT(*) AS frequency
FROM ordered
WHERE prev_tool IS NOT NULL
GROUP BY prev_tool, tool_name
HAVING COUNT(*) >= 3
ORDER BY frequency DESC
LIMIT 20;
```

### API

`GET /tools/usage/chains` — returns discovered chains.

```json
{
  "bigrams": [
    {"from": "list_resources", "to": "get_pod_logs", "frequency": 42, "probability": 0.78},
    {"from": "get_pod_logs", "to": "describe_resource", "frequency": 35, "probability": 0.65}
  ],
  "total_sessions_analyzed": 120
}
```

`probability` = frequency / total times `from` tool was called.

### UI

Add a "Common Patterns" section to the Analytics tab in ToolsView. Show bigrams as a simple flow list: `list_resources -> get_pod_logs (78%)`.

## Layer 2: Next-Tool Hints

### How it works

1. A background function `refresh_chain_hints()` runs periodically (every 5 minutes, or on-demand) and computes the top bigrams from `tool_usage`
2. Results are cached in-memory as a dict: `{tool_name: [(next_tool, probability), ...]}`
3. When the harness builds the system prompt (`build_cached_system_prompt`), it appends chain hints to the cluster context section if any exist
4. Hints are phrased as: `"Tool usage patterns: After calling list_resources, users typically need get_pod_logs (78%) or describe_resource (65%)."`
5. Only include hints where probability >= 0.6 (configurable)
6. Maximum 5 hints per prompt to avoid bloat

### Where to inject

The harness already has `get_cluster_context()` which returns a string appended to the system prompt. Chain hints are appended to this string — no change to the prompt caching structure.

### Refresh strategy

- On startup: compute from all historical data
- Every 5 minutes: recompute from last 24h of data (lightweight query)
- Cache invalidation: in-memory dict, replaced atomically

### Configuration

- `PULSE_AGENT_CHAIN_HINTS`: enable/disable (default: `1` = enabled)
- `PULSE_AGENT_CHAIN_MIN_PROBABILITY`: minimum probability threshold (default: `0.6`)
- `PULSE_AGENT_CHAIN_MIN_FREQUENCY`: minimum occurrence count (default: `3`)

## Files to Create/Modify

### Backend
- `sre_agent/tool_chains.py` (create) — `discover_chains()`, `refresh_chain_hints()`, `get_chain_hints_text()`, in-memory cache
- `sre_agent/harness.py` (modify) — append chain hints in `get_cluster_context()`
- `sre_agent/api.py` (modify) — add `GET /tools/usage/chains` endpoint
- `sre_agent/config.py` (modify) — add chain hint settings

### Frontend
- `src/kubeview/store/toolUsageStore.ts` (modify) — add `loadChains()` action
- `src/kubeview/views/ToolsView.tsx` (modify) — add "Common Patterns" section to Analytics tab

### Tests
- `tests/test_tool_chains.py` (create) — discovery queries, hint generation, caching
