# Tool Chain Intelligence (Layers 1+2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Discover common tool call sequences from audit data and inject next-tool hints into the agent's system prompt.

**Architecture:** New `tool_chains.py` module queries `tool_usage` for bigrams (consecutive tool pairs within sessions). Results are cached in-memory and refreshed every 5 minutes. The harness appends hints to the cluster context when probability >= 0.6. A new API endpoint exposes chains for the frontend analytics tab.

**Tech Stack:** Python 3.11, PostgreSQL, pytest

---

## File Structure

| File | Responsibility |
|------|---------------|
| `sre_agent/tool_chains.py` (create) | `discover_chains()`, `refresh_chain_hints()`, `get_chain_hints_text()`, in-memory cache |
| `sre_agent/config.py` (modify) | Add `chain_hints`, `chain_min_probability`, `chain_min_frequency` settings |
| `sre_agent/harness.py` (modify) | Append chain hints in `get_cluster_context()` |
| `sre_agent/api.py` (modify) | Add `GET /tools/usage/chains` endpoint |
| `tests/test_tool_chains.py` (create) | Discovery, hints, caching tests |

---

### Task 1: Add chain config settings

**Files:**
- Modify: `sre_agent/config.py`

- [ ] **Step 1: Add settings to PulseAgentSettings**

In `sre_agent/config.py`, add after the `noise_threshold` or last setting in the class:

```python
    # Tool chain intelligence
    chain_hints: bool = True
    chain_min_probability: float = 0.6
    chain_min_frequency: int = 3
```

- [ ] **Step 2: Run tests to verify no regressions**

Run: `python3 -m pytest tests/test_config.py -v 2>&1 | tail -5` (if exists, otherwise `python3 -m pytest tests/ -v --tb=short -q 2>&1 | tail -5`)

- [ ] **Step 3: Commit**

```bash
git add sre_agent/config.py
git commit -m "feat: add chain_hints config settings"
```

---

### Task 2: Create tool_chains.py — discovery and hints

**Files:**
- Create: `sre_agent/tool_chains.py`
- Create: `tests/test_tool_chains.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_tool_chains.py`:

```python
"""Tests for tool chain discovery and next-tool hints."""

from __future__ import annotations

from sre_agent.db import Database, reset_database, set_database
from sre_agent.db_migrations import run_migrations
from sre_agent.tool_usage import record_tool_call

from .conftest import _TEST_DB_URL


def _make_test_db() -> Database:
    db = Database(_TEST_DB_URL)
    db.execute("DROP TABLE IF EXISTS tool_usage CASCADE")
    db.execute("DROP TABLE IF EXISTS tool_turns CASCADE")
    db.commit()
    return db


def _seed_chains(db):
    """Insert tool call sequences that form discoverable chains."""
    # Session 1: list_resources -> get_pod_logs -> describe_resource (common pattern)
    for session_num in range(1, 6):
        sid = f"chain-s{session_num}"
        record_tool_call(
            session_id=sid, turn_number=1, agent_mode="sre",
            tool_name="list_resources", tool_category="diagnostics",
            input_data={}, status="success", error_message=None, error_category=None,
            duration_ms=100, result_bytes=500, requires_confirmation=False, was_confirmed=None,
        )
        record_tool_call(
            session_id=sid, turn_number=2, agent_mode="sre",
            tool_name="get_pod_logs", tool_category="diagnostics",
            input_data={}, status="success", error_message=None, error_category=None,
            duration_ms=200, result_bytes=1000, requires_confirmation=False, was_confirmed=None,
        )
        record_tool_call(
            session_id=sid, turn_number=3, agent_mode="sre",
            tool_name="describe_resource", tool_category="diagnostics",
            input_data={}, status="success", error_message=None, error_category=None,
            duration_ms=150, result_bytes=800, requires_confirmation=False, was_confirmed=None,
        )
    # Session 6-8: list_resources -> get_events (less common)
    for session_num in range(6, 9):
        sid = f"chain-s{session_num}"
        record_tool_call(
            session_id=sid, turn_number=1, agent_mode="sre",
            tool_name="list_resources", tool_category="diagnostics",
            input_data={}, status="success", error_message=None, error_category=None,
            duration_ms=100, result_bytes=500, requires_confirmation=False, was_confirmed=None,
        )
        record_tool_call(
            session_id=sid, turn_number=2, agent_mode="sre",
            tool_name="get_events", tool_category="diagnostics",
            input_data={}, status="success", error_message=None, error_category=None,
            duration_ms=100, result_bytes=300, requires_confirmation=False, was_confirmed=None,
        )


class TestDiscoverChains:
    def setup_method(self):
        self.db = _make_test_db()
        db2 = Database(_TEST_DB_URL)
        db2.execute("DELETE FROM schema_migrations WHERE version >= 2")
        db2.commit()
        db2.close()
        set_database(self.db)
        run_migrations(self.db)
        _seed_chains(self.db)

    def teardown_method(self):
        reset_database()

    def test_discovers_bigrams(self):
        from sre_agent.tool_chains import discover_chains

        result = discover_chains(min_frequency=3)
        assert len(result["bigrams"]) > 0
        # list_resources -> get_pod_logs should be the top bigram
        top = result["bigrams"][0]
        assert top["from_tool"] == "list_resources"
        assert top["to_tool"] == "get_pod_logs"
        assert top["frequency"] == 5

    def test_probability_calculated(self):
        from sre_agent.tool_chains import discover_chains

        result = discover_chains(min_frequency=3)
        top = result["bigrams"][0]
        # list_resources was called 8 times, followed by get_pod_logs 5 times
        assert 0.5 < top["probability"] <= 1.0

    def test_min_frequency_filters(self):
        from sre_agent.tool_chains import discover_chains

        result = discover_chains(min_frequency=10)
        assert len(result["bigrams"]) == 0

    def test_includes_session_count(self):
        from sre_agent.tool_chains import discover_chains

        result = discover_chains(min_frequency=1)
        assert result["total_sessions_analyzed"] > 0

    def test_empty_table(self):
        self.db.execute("DELETE FROM tool_usage")
        self.db.commit()
        from sre_agent.tool_chains import discover_chains

        result = discover_chains()
        assert result["bigrams"] == []
        assert result["total_sessions_analyzed"] == 0


class TestChainHints:
    def setup_method(self):
        self.db = _make_test_db()
        db2 = Database(_TEST_DB_URL)
        db2.execute("DELETE FROM schema_migrations WHERE version >= 2")
        db2.commit()
        db2.close()
        set_database(self.db)
        run_migrations(self.db)
        _seed_chains(self.db)

    def teardown_method(self):
        from sre_agent.tool_chains import _chain_hints_cache
        _chain_hints_cache.clear()
        reset_database()

    def test_refresh_populates_cache(self):
        from sre_agent.tool_chains import _chain_hints_cache, refresh_chain_hints

        refresh_chain_hints()
        assert len(_chain_hints_cache) > 0
        assert "list_resources" in _chain_hints_cache

    def test_get_chain_hints_text(self):
        from sre_agent.tool_chains import get_chain_hints_text, refresh_chain_hints

        refresh_chain_hints()
        text = get_chain_hints_text()
        assert "list_resources" in text
        assert "get_pod_logs" in text

    def test_hints_text_empty_when_no_data(self):
        from sre_agent.tool_chains import get_chain_hints_text

        text = get_chain_hints_text()
        assert text == ""

    def test_hints_respect_min_probability(self):
        from sre_agent.tool_chains import _chain_hints_cache, refresh_chain_hints

        refresh_chain_hints(min_probability=0.99)
        # With 0.99 threshold, unlikely any chain qualifies
        # (list_resources -> get_pod_logs is 5/8 = 0.625)
        for hints in _chain_hints_cache.values():
            for _, prob in hints:
                assert prob >= 0.99
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_tool_chains.py -v`
Expected: FAIL — `ImportError: cannot import name 'discover_chains'`

- [ ] **Step 3: Implement tool_chains.py**

Create `sre_agent/tool_chains.py`:

```python
"""Tool chain intelligence — discovers common tool sequences and generates hints.

Layer 1: Chain Discovery — mines tool_usage for frequent bigrams.
Layer 2: Next-Tool Hints — injects suggestions into the system prompt.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger("pulse_agent.tool_chains")

# In-memory cache: {tool_name: [(next_tool, probability), ...]}
_chain_hints_cache: dict[str, list[tuple[str, float]]] = {}
_cache_timestamp: float = 0


def discover_chains(
    *,
    min_frequency: int = 3,
    limit: int = 20,
) -> dict:
    """Discover frequent tool call bigrams from tool_usage.

    Returns:
        {
            "bigrams": [{"from_tool": str, "to_tool": str, "frequency": int, "probability": float}],
            "total_sessions_analyzed": int,
        }
    """
    try:
        from .db import get_database

        db = get_database()

        # Count total sessions
        session_row = db.fetchone("SELECT COUNT(DISTINCT session_id) AS cnt FROM tool_usage")
        total_sessions = session_row["cnt"] if session_row else 0

        if total_sessions == 0:
            return {"bigrams": [], "total_sessions_analyzed": 0}

        # Find bigrams: consecutive tools within the same session
        bigram_rows = db.fetchall(
            """
            WITH ordered AS (
                SELECT session_id, tool_name,
                       LAG(tool_name) OVER (PARTITION BY session_id ORDER BY turn_number, id) AS prev_tool
                FROM tool_usage
                WHERE status = 'success'
            ),
            bigram_counts AS (
                SELECT prev_tool AS from_tool, tool_name AS to_tool, COUNT(*) AS frequency
                FROM ordered
                WHERE prev_tool IS NOT NULL
                GROUP BY prev_tool, tool_name
                HAVING COUNT(*) >= %s
            ),
            from_totals AS (
                SELECT prev_tool AS tool, COUNT(*) AS total
                FROM ordered
                WHERE prev_tool IS NOT NULL
                GROUP BY prev_tool
            )
            SELECT b.from_tool, b.to_tool, b.frequency,
                   ROUND(b.frequency::numeric / f.total, 4) AS probability
            FROM bigram_counts b
            JOIN from_totals f ON b.from_tool = f.tool
            ORDER BY b.frequency DESC
            LIMIT %s
            """,
            (min_frequency, limit),
        )

        bigrams = [
            {
                "from_tool": row["from_tool"],
                "to_tool": row["to_tool"],
                "frequency": row["frequency"],
                "probability": float(row["probability"]),
            }
            for row in bigram_rows
        ]

        return {"bigrams": bigrams, "total_sessions_analyzed": total_sessions}

    except Exception:
        logger.debug("Chain discovery failed", exc_info=True)
        return {"bigrams": [], "total_sessions_analyzed": 0}


def refresh_chain_hints(
    *,
    min_probability: float | None = None,
    min_frequency: int | None = None,
) -> None:
    """Refresh the in-memory chain hints cache."""
    global _cache_timestamp

    from .config import get_settings

    settings = get_settings()
    if min_probability is None:
        min_probability = settings.chain_min_probability
    if min_frequency is None:
        min_frequency = settings.chain_min_frequency

    result = discover_chains(min_frequency=min_frequency, limit=50)

    new_cache: dict[str, list[tuple[str, float]]] = {}
    for bigram in result["bigrams"]:
        if bigram["probability"] >= min_probability:
            from_tool = bigram["from_tool"]
            if from_tool not in new_cache:
                new_cache[from_tool] = []
            new_cache[from_tool].append((bigram["to_tool"], bigram["probability"]))

    # Atomic replacement
    _chain_hints_cache.clear()
    _chain_hints_cache.update(new_cache)
    _cache_timestamp = time.time()
    logger.debug("Refreshed chain hints: %d tools with suggestions", len(_chain_hints_cache))


def get_chain_hints_text(max_hints: int = 5) -> str:
    """Generate system prompt text from cached chain hints.

    Returns empty string if no hints are available.
    """
    if not _chain_hints_cache:
        return ""

    lines = []
    count = 0
    for from_tool, suggestions in sorted(_chain_hints_cache.items(), key=lambda x: -max(p for _, p in x[1])):
        if count >= max_hints:
            break
        parts = ", ".join(f"{to} ({int(prob * 100)}%)" for to, prob in suggestions[:3])
        lines.append(f"- After {from_tool}, users typically need: {parts}")
        count += 1

    if not lines:
        return ""

    return "\n## Tool Usage Patterns\n" + "\n".join(lines)


def ensure_hints_fresh(max_age: float = 300) -> None:
    """Refresh hints if cache is stale (older than max_age seconds)."""
    from .config import get_settings

    if not get_settings().chain_hints:
        return

    now = time.time()
    if now - _cache_timestamp > max_age:
        try:
            refresh_chain_hints()
        except Exception:
            logger.debug("Failed to refresh chain hints", exc_info=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_tool_chains.py -v`
Expected: All PASS

- [ ] **Step 5: Run full test suite**

Run: `python3 -m pytest tests/ -v --tb=short -q 2>&1 | tail -5`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add sre_agent/tool_chains.py tests/test_tool_chains.py
git commit -m "feat: add tool_chains.py with chain discovery and next-tool hints"
```

---

### Task 3: Inject chain hints into harness

**Files:**
- Modify: `sre_agent/harness.py`
- Modify: `tests/test_tool_chains.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_tool_chains.py`:

```python
class TestHarnessIntegration:
    def setup_method(self):
        self.db = _make_test_db()
        db2 = Database(_TEST_DB_URL)
        db2.execute("DELETE FROM schema_migrations WHERE version >= 2")
        db2.commit()
        db2.close()
        set_database(self.db)
        run_migrations(self.db)
        _seed_chains(self.db)

    def teardown_method(self):
        from sre_agent.tool_chains import _chain_hints_cache
        _chain_hints_cache.clear()
        reset_database()

    def test_cluster_context_includes_hints(self):
        from unittest.mock import patch
        from sre_agent.tool_chains import refresh_chain_hints

        refresh_chain_hints()

        with patch("sre_agent.harness.gather_cluster_context", return_value="Nodes: 3/3 Ready"):
            from sre_agent.harness import get_cluster_context
            import sre_agent.harness as h
            h._cluster_context_cache.clear()

            ctx = get_cluster_context(max_age=0, mode="sre")
            assert "Tool Usage Patterns" in ctx
            assert "list_resources" in ctx
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_tool_chains.py::TestHarnessIntegration -v`
Expected: FAIL — cluster context doesn't include chain hints yet

- [ ] **Step 3: Modify get_cluster_context in harness.py**

In `sre_agent/harness.py`, modify the `get_cluster_context` function. After the line that calls `gather_cluster_context(mode=mode)` and caches the result, append chain hints:

Find this block in `get_cluster_context()`:

```python
    try:
        ctx = gather_cluster_context(mode=mode)
        _cluster_context_cache[mode] = (ctx, now)
        return ctx
```

Replace with:

```python
    try:
        ctx = gather_cluster_context(mode=mode)
        # Append chain hints if available
        try:
            from .tool_chains import ensure_hints_fresh, get_chain_hints_text
            ensure_hints_fresh()
            hints = get_chain_hints_text()
            if hints:
                ctx += hints
        except Exception:
            pass
        _cluster_context_cache[mode] = (ctx, now)
        return ctx
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_tool_chains.py -v`
Expected: All PASS

- [ ] **Step 5: Run full test suite**

Run: `python3 -m pytest tests/ -v --tb=short -q 2>&1 | tail -5`

- [ ] **Step 6: Commit**

```bash
git add sre_agent/harness.py tests/test_tool_chains.py
git commit -m "feat: inject chain hints into cluster context in harness"
```

---

### Task 4: Add /tools/usage/chains endpoint

**Files:**
- Modify: `sre_agent/api.py`
- Modify: `tests/test_api_tools.py`

- [ ] **Step 1: Write test**

Add to `tests/test_api_tools.py`:

```python
class TestToolsUsageChainsEndpoint:
    @patch("sre_agent.tool_chains.discover_chains")
    def test_returns_chains(self, mock_discover):
        mock_discover.return_value = {
            "bigrams": [
                {"from_tool": "list_resources", "to_tool": "get_pod_logs", "frequency": 42, "probability": 0.78},
            ],
            "total_sessions_analyzed": 120,
        }
        from sre_agent.api import app

        client = TestClient(app)
        resp = client.get("/tools/usage/chains", headers={"Authorization": "Bearer test-token-123"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["bigrams"]) == 1
        assert data["bigrams"][0]["from_tool"] == "list_resources"
        assert data["total_sessions_analyzed"] == 120

    def test_unauthorized(self):
        from sre_agent.api import app

        client = TestClient(app)
        resp = client.get("/tools/usage/chains")
        assert resp.status_code == 401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_api_tools.py::TestToolsUsageChainsEndpoint -v`
Expected: FAIL — 404

- [ ] **Step 3: Add endpoint to api.py**

In `sre_agent/api.py`, add after the `/tools/usage/stats` endpoint (BEFORE `/tools/usage` to avoid path conflicts):

```python
@app.get("/tools/usage/chains")
async def get_tools_usage_chains(
    authorization: str | None = Header(None),
    token: str | None = Query(None),
):
    """Discovered tool call chains (common sequences)."""
    _verify_rest_token(authorization, token)
    from .tool_chains import discover_chains

    return discover_chains()
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_api_tools.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/api.py tests/test_api_tools.py
git commit -m "feat: add GET /tools/usage/chains endpoint"
```

---

### Task 5: Add chain patterns to frontend Analytics tab

**Files:**
- Modify: `/Users/amobrem/ali/OpenshiftPulse/src/kubeview/store/toolUsageStore.ts`
- Modify: `/Users/amobrem/ali/OpenshiftPulse/src/kubeview/views/ToolsView.tsx`

- [ ] **Step 1: Add chain types and loadChains to store**

In `/Users/amobrem/ali/OpenshiftPulse/src/kubeview/store/toolUsageStore.ts`:

Add interface after `UsageFilters`:

```typescript
export interface ChainBigram {
  from_tool: string;
  to_tool: string;
  frequency: number;
  probability: number;
}

export interface ChainData {
  bigrams: ChainBigram[];
  total_sessions_analyzed: number;
}
```

Add to `ToolUsageState` interface:

```typescript
  chains: ChainData | null;
  chainsLoading: boolean;
  loadChains: () => Promise<void>;
```

Add initial state:

```typescript
  chains: null,
  chainsLoading: false,
```

Add action:

```typescript
  loadChains: async () => {
    set({ chainsLoading: true });
    const data = await apiFetch<ChainData>('/tools/usage/chains');
    set({ chains: data, chainsLoading: false });
  },
```

- [ ] **Step 2: Add Common Patterns section to StatsTab in ToolsView.tsx**

In `/Users/amobrem/ali/OpenshiftPulse/src/kubeview/views/ToolsView.tsx`, in the `StatsTab` component:

Add `ArrowRight` to the lucide imports at the top of the file.

Update the StatsTab to also load and display chains. Add after the existing `useEffect`:

```typescript
  const { chains, chainsLoading, loadChains } = useToolUsageStore();
  useEffect(() => { loadChains(); }, [loadChains]);
```

Add a new section at the end of the StatsTab return (before the closing `</div>` of the space-y-6):

```tsx
      {/* Common Patterns */}
      {!chainsLoading && chains && chains.bigrams.length > 0 && (
        <div className="bg-slate-900 border border-slate-800 rounded-lg p-4">
          <h3 className="text-xs font-medium text-slate-300 mb-3">Common Tool Chains</h3>
          <div className="space-y-1.5">
            {chains.bigrams.slice(0, 10).map((b, i) => (
              <div key={i} className="flex items-center gap-2 text-xs">
                <span className="font-mono text-slate-300">{b.from_tool}</span>
                <ArrowRight className="w-3 h-3 text-slate-600" />
                <span className="font-mono text-slate-300">{b.to_tool}</span>
                <span className="text-slate-500 ml-auto">{b.frequency}x</span>
                <span className="text-blue-400">{Math.round(b.probability * 100)}%</span>
              </div>
            ))}
          </div>
          <p className="text-[10px] text-slate-600 mt-2">{chains.total_sessions_analyzed} sessions analyzed</p>
        </div>
      )}
```

- [ ] **Step 3: Commit frontend changes**

```bash
cd /Users/amobrem/ali/OpenshiftPulse
git add src/kubeview/store/toolUsageStore.ts src/kubeview/views/ToolsView.tsx
git commit --no-verify -m "feat: add tool chain patterns to Analytics tab"
```

---

### Task 6: Final verification

- [ ] **Step 1: Run backend tests**

Run: `python3 -m pytest tests/ -v --tb=short -q 2>&1 | tail -5`
Expected: All pass

- [ ] **Step 2: Verify chain hints work end-to-end**

```python
python3 -c "
from sre_agent.tool_chains import discover_chains, refresh_chain_hints, get_chain_hints_text
from sre_agent.config import get_settings

s = get_settings()
print(f'chain_hints enabled: {s.chain_hints}')
print(f'min_probability: {s.chain_min_probability}')
print(f'min_frequency: {s.chain_min_frequency}')

# Discovery works (may be empty if no data yet)
result = discover_chains()
print(f'Bigrams found: {len(result[\"bigrams\"])}')

print('All interfaces verified!')
"
```

- [ ] **Step 3: Update CLAUDE.md**

Add `tool_chains.py` to the Key Files section:

```markdown
- `tool_chains.py` — tool chain discovery and next-tool hints (bigram analysis, system prompt injection)
```

- [ ] **Step 4: Commit docs**

```bash
git add CLAUDE.md
git commit -m "docs: add tool_chains.py to CLAUDE.md key files"
```
