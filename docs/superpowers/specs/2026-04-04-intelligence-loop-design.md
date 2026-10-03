# Intelligence Loop — Design Spec

> Historical design/implementation record. This preserves the proposal and tasks at the date in the filename; unchecked tasks, paths, commands, and security claims are not evidence of the current implementation. Use [current documentation](../../README.md) and verify behavior against code before applying a procedure.


**Goal:** Feed analytics data back into the agent's system prompt to improve query selection, reduce errors, and surface patterns that make dashboards better. The agent gets smarter over time based on what works on this cluster.

**Problem:** The agent has no memory of what worked before. It suggests PromQL queries that failed last time, uses tools that error frequently, and doesn't learn from successful dashboard patterns. All the analytics data we're collecting (tool_usage, promql_queries, tool_chains) goes unused.

**Approach:** New `intelligence.py` module computes 3 intelligence summaries from the last 7 days of analytics data and injects them into the system prompt via the existing `get_cluster_context()` injection point. Cached in-memory for 10 minutes.

---

## 1. Intelligence Module (`sre_agent/intelligence.py`)

### Public API

```python
def get_intelligence_context(mode: str = "sre", max_age_days: int = 7) -> str:
    """Compute intelligence summary from analytics data.
    
    Returns text for system prompt injection with 3 sections:
    - Query Reliability: preferred and avoid-listed PromQL queries
    - Dashboard Patterns: common tool flows and component combos
    - Error Hotspots: tools with high error rates and common errors
    
    Returns empty string if no data or DB unavailable.
    Cached in-memory for 10 minutes.
    """
```

### Output Format

```
## Agent Intelligence (last 7 days)

### Query Reliability
Preferred queries (high success rate on this cluster):
- sum by (namespace) (rate(container_cpu_usage_seconds_total...)): 47/47 success → USE THIS
- 100 - avg(rate(node_cpu_seconds_total{mode='idle'}...)): 23/24 success → USE THIS

Avoid these queries (high failure rate on this cluster):
- predict_linear(node_filesystem_avail_bytes[7d]...): 1/9 success → AVOID, try alternative
- etcd_server_proposals_pending: 0/5 success → AVOID

### Dashboard Patterns
Most used tools in view_designer mode:
- cluster_metrics (called 15 times)
- get_prometheus_query (called 42 times)
- namespace_summary (called 8 times)
Average tools per dashboard: 5

### Error Hotspots
- get_prometheus_query: 12% error rate (32/267) — common: "Query returned no results"
- describe_pod: 5% error rate (3/60) — common: "pod not found"
```

### Caching

Module-level cache with 10-minute TTL:

```python
_intelligence_cache: dict[str, tuple[str, float]] = {}  # {mode: (text, timestamp)}
```

---

## 2. Query Reliability (`_compute_query_reliability`)

Queries the `promql_queries` table for queries with activity in the last N days.

**Partitioning:**
- **Preferred** (success_rate > 0.8, total_attempts >= 3): Agent should use these
- **Avoid** (success_rate < 0.3, total_attempts >= 3): Agent should not use these
- Queries with < 3 total attempts are excluded (not enough signal)

**Limit:** Top 10 preferred, top 5 avoid (keep prompt concise).

**SQL:**
```sql
SELECT query_template, success_count, failure_count,
       success_count::float / NULLIF(success_count + failure_count, 0) as success_rate
FROM promql_queries
WHERE last_success > NOW() - INTERVAL '%s days'
   OR last_failure > NOW() - INTERVAL '%s days'
HAVING success_count + failure_count >= 3
ORDER BY success_count + failure_count DESC
LIMIT 20
```

**Output rules:**
- Truncate query_template to 80 chars for readability
- Show success/total count (e.g., "23/24 success")
- Add action: "USE THIS" for preferred, "AVOID, try alternative" for avoid

---

## 3. Dashboard Patterns (`_compute_dashboard_patterns`)

Queries `tool_usage` for view_designer sessions to find common tool flows.

**Queries:**
```sql
-- Most used tools in view_designer mode
SELECT tool_name, COUNT(*) as call_count
FROM tool_usage
WHERE agent_mode = 'view_designer'
  AND timestamp > NOW() - INTERVAL '%s days'
  AND status = 'success'
GROUP BY tool_name
ORDER BY call_count DESC
LIMIT 10

-- Average tools per dashboard session
SELECT AVG(tool_count) FROM (
    SELECT session_id, COUNT(*) as tool_count
    FROM tool_usage
    WHERE agent_mode = 'view_designer'
      AND timestamp > NOW() - INTERVAL '%s days'
    GROUP BY session_id
) sub
```

**Output:** List of most-used tools with counts + average tools per dashboard.

---

## 4. Error Hotspots (`_compute_error_hotspots`)

Queries `tool_usage` for tools with high error rates.

**SQL:**
```sql
SELECT tool_name,
       COUNT(*) FILTER (WHERE status = 'error') as error_count,
       COUNT(*) as total_count,
       (SELECT error_message FROM tool_usage t2
        WHERE t2.tool_name = t1.tool_name AND t2.status = 'error'
        AND t2.timestamp > NOW() - INTERVAL '%s days'
        GROUP BY error_message ORDER BY COUNT(*) DESC LIMIT 1) as common_error
FROM tool_usage t1
WHERE timestamp > NOW() - INTERVAL '%s days'
GROUP BY tool_name
HAVING COUNT(*) > 5
   AND COUNT(*) FILTER (WHERE status = 'error')::float / COUNT(*) > 0.05
ORDER BY COUNT(*) FILTER (WHERE status = 'error')::float / COUNT(*) DESC
LIMIT 5
```

**Filters:**
- Minimum 5 total calls (filter noise)
- Minimum 5% error rate (don't flag rare errors)
- Show error_count/total_count and most common error message
- Truncate error messages to 100 chars

---

## 5. Injection Point

In `sre_agent/harness.py:get_cluster_context()`, after the existing tool chain hints:

```python
# Intelligence context
try:
    from .intelligence import get_intelligence_context
    intel = get_intelligence_context(mode=mode)
    if intel:
        context += "\n\n" + intel
except Exception:
    pass  # Never block on intelligence failure
```

Same fire-and-forget pattern as tool chain hints.

---

## 6. Test Framework (`tests/test_intelligence.py`)

~15 tests:

**Query reliability:**
- test_preferred_queries_high_success_rate — mock DB with 90%+ success queries → appears in "Preferred"
- test_avoid_queries_low_success_rate — mock DB with 20% success → appears in "Avoid"
- test_low_attempt_queries_excluded — queries with <3 attempts not shown
- test_query_template_truncated — long queries truncated to 80 chars

**Dashboard patterns:**
- test_most_used_tools_listed — mock DB with view_designer calls → tool names appear
- test_avg_tools_per_dashboard — mock DB → average computed correctly
- test_no_view_designer_data_skips_section — empty result → section not included

**Error hotspots:**
- test_high_error_rate_tool_flagged — 15% error rate tool appears
- test_low_error_rate_filtered — 2% error rate tool excluded
- test_low_volume_filtered — tool with 3 calls excluded
- test_common_error_shown — most frequent error message included

**Integration:**
- test_get_intelligence_context_returns_string — full function returns non-empty string (with mock data)
- test_empty_db_returns_empty_string — no data → ""
- test_db_error_returns_empty_string — exception → "" (fire-and-forget)
- test_caching_uses_cache — second call within 10 min doesn't query DB
- test_cache_expiry — call after TTL queries DB again

---

## 7. Files Created/Modified

| File | Action | Purpose |
|------|--------|---------|
| `sre_agent/intelligence.py` | CREATE | Intelligence computation + caching |
| `sre_agent/harness.py` | MODIFY | Add intelligence injection to get_cluster_context |
| `tests/test_intelligence.py` | CREATE | ~15 tests |
| `CLAUDE.md` | MODIFY | Add intelligence.py to key files |

---

## Out of Scope
- Per-user intelligence (tracking individual user preferences)
- Real-time intelligence updates (mid-conversation refresh)
- Intelligence dashboard in the UI (Phase 2 territory)
- Automatic recipe creation from successful ad-hoc queries
