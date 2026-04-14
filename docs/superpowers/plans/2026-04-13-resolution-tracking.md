# Resolution Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add resolution/failure counts to Mission Control and a resolution history view showing every auto-fix outcome with what fixed it and how long it took.

**Architecture:** Extend the existing `/fix-history/summary` endpoint to include verification breakdown (resolved/still_failing/improved). Add a new `/fix-history/resolutions` endpoint returning individual resolution records. UI: add resolution stats to the Outcomes card and a Resolution History section to the Actions tab.

**Tech Stack:** Python/FastAPI (agent API), React/TypeScript (UI), PostgreSQL (existing actions table)

---

### Task 1: Extend fix-history/summary with verification breakdown

**Files:**
- Modify: `sre_agent/api/monitor_rest.py:142-234`
- Test: `tests/test_api_http.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_api_http.py or create tests/test_resolution_tracking.py
from unittest.mock import patch, MagicMock


class TestFixHistorySummaryVerification:
    @patch("sre_agent.api.monitor_rest.db")
    def test_summary_includes_verification_counts(self, mock_db_mod):
        db = MagicMock()
        mock_db_mod.get_database.return_value = db
        db.fetchall.return_value = [
            {"status": "completed", "category": "crashloop", "duration_ms": 100, "verification_status": "verified"},
            {"status": "completed", "category": "workloads", "duration_ms": 200, "verification_status": "still_failing"},
            {"status": "completed", "category": "crashloop", "duration_ms": 150, "verification_status": "verified"},
            {"status": "failed", "category": "image_pull", "duration_ms": None, "verification_status": None},
        ]
        db.fetchone.return_value = {"cnt": 4}

        from sre_agent.api.monitor_rest import get_fix_history_summary
        result = get_fix_history_summary(days=7)

        assert result["verification"]["resolved"] == 2
        assert result["verification"]["still_failing"] == 1
        assert result["verification"]["pending"] == 1
        assert result["verification"]["resolution_rate"] == 0.5  # 2 resolved / 4 total
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_resolution_tracking.py::TestFixHistorySummaryVerification -v`
Expected: FAIL — `verification` key not in result

- [ ] **Step 3: Add verification breakdown to get_fix_history_summary**

In `sre_agent/api/monitor_rest.py`, modify the `get_fix_history_summary` function. Change the SQL query to also select `verification_status`:

```python
        actions = database.fetchall(
            "SELECT status, category, duration_ms, verification_status FROM actions "
            "WHERE timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * ?)::BIGINT * 1000",
            (days,),
        )
```

After the existing aggregation, add:

```python
        # Verification breakdown
        resolved = sum(1 for a in actions if a.get("verification_status") == "verified")
        still_failing = sum(1 for a in actions if a.get("verification_status") == "still_failing")
        improved = sum(1 for a in actions if a.get("verification_status") == "improved")
        pending_verification = total_actions - resolved - still_failing - improved

        verification = {
            "resolved": resolved,
            "still_failing": still_failing,
            "improved": improved,
            "pending": pending_verification,
            "resolution_rate": round(resolved / total_actions, 2) if total_actions > 0 else 0.0,
        }
```

Add `"verification": verification` to the return dict.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_resolution_tracking.py::TestFixHistorySummaryVerification -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/api/monitor_rest.py tests/test_resolution_tracking.py
git commit -m "feat: add verification breakdown to fix-history/summary"
```

---

### Task 2: Add /fix-history/resolutions endpoint

**Files:**
- Modify: `sre_agent/api/monitor_rest.py`
- Test: `tests/test_resolution_tracking.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
class TestResolutionsEndpoint:
    def test_returns_resolution_list(self):
        from sre_agent.api import app
        from starlette.testclient import TestClient

        client = TestClient(app)
        resp = client.get(
            "/api/agent/fix-history/resolutions?days=7",
            headers={"Authorization": "Bearer test"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "resolutions" in data
        assert "total" in data
        assert isinstance(data["resolutions"], list)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_resolution_tracking.py::TestResolutionsEndpoint -v`
Expected: FAIL — 404 endpoint not found

- [ ] **Step 3: Implement the endpoint**

Add to `sre_agent/api/monitor_rest.py`:

```python
@router.get("/fix-history/resolutions")
async def rest_fix_history_resolutions(
    days: int = 7,
    limit: int = 50,
    _auth=Depends(verify_token),
):
    """Recent resolution outcomes — what was fixed, how, and whether it worked."""
    try:
        from .. import db

        database = db.get_database()
        rows = database.fetchall(
            "SELECT id, finding_id, category, tool, status, reasoning, "
            "verification_status, verification_evidence, verification_timestamp, "
            "timestamp, duration_ms "
            "FROM actions "
            "WHERE timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * %s)::BIGINT * 1000 "
            "AND verification_status IS NOT NULL "
            "ORDER BY timestamp DESC "
            "LIMIT %s",
            (days, limit),
        )

        resolutions = []
        for r in rows:
            time_to_verify_ms = None
            if r.get("verification_timestamp") and r.get("timestamp"):
                time_to_verify_ms = int(r["verification_timestamp"]) - int(r["timestamp"])

            resolutions.append({
                "id": r["id"],
                "findingId": r.get("finding_id", ""),
                "category": r.get("category", ""),
                "tool": r.get("tool", ""),
                "status": r.get("status", ""),
                "reasoning": r.get("reasoning", ""),
                "outcome": r.get("verification_status", ""),
                "evidence": r.get("verification_evidence", ""),
                "timestamp": r.get("timestamp"),
                "verifiedAt": r.get("verification_timestamp"),
                "durationMs": r.get("duration_ms"),
                "timeToVerifyMs": time_to_verify_ms,
            })

        return {"resolutions": resolutions, "total": len(resolutions)}
    except Exception as e:
        logger.debug("Failed to get resolutions: %s", e)
        return {"resolutions": [], "total": 0}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_resolution_tracking.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/api/monitor_rest.py tests/test_resolution_tracking.py
git commit -m "feat: add /fix-history/resolutions endpoint"
```

---

### Task 3: UI — Add resolution stats to Outcomes card

**Files:**
- Modify: `src/kubeview/engine/analyticsApi.ts` (extend FixHistorySummary type)
- Modify: `src/kubeview/views/mission-control/AgentHealth.tsx` (OutcomesCard)

- [ ] **Step 1: Extend FixHistorySummary type**

In `src/kubeview/engine/analyticsApi.ts`, add to the `FixHistorySummary` interface:

```typescript
  verification: {
    resolved: number;
    still_failing: number;
    improved: number;
    pending: number;
    resolution_rate: number;
  };
```

- [ ] **Step 2: Add resolution stats to OutcomesCard**

In `src/kubeview/views/mission-control/AgentHealth.tsx`, inside the `OutcomesCard` function, after the existing fix summary stats, add:

```tsx
        {fixSummary && fixSummary.verification && (
          <div className="flex items-center gap-3 text-xs">
            <span className="text-emerald-400 font-medium">{fixSummary.verification.resolved} resolved</span>
            <span className="text-slate-600">&middot;</span>
            <span className="text-amber-400">{fixSummary.verification.still_failing} still failing</span>
            {fixSummary.verification.pending > 0 && (
              <>
                <span className="text-slate-600">&middot;</span>
                <span className="text-slate-400">{fixSummary.verification.pending} pending</span>
              </>
            )}
          </div>
        )}

        {fixSummary && fixSummary.verification && fixSummary.total_actions > 0 && (
          <div className="text-xs text-slate-400">
            Resolution rate: <span className="text-slate-200 font-medium">
              {Math.round(fixSummary.verification.resolution_rate * 100)}%
            </span>
          </div>
        )}
```

- [ ] **Step 3: Run UI tests**

Run: `pnpm test`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add src/kubeview/engine/analyticsApi.ts src/kubeview/views/mission-control/AgentHealth.tsx
git commit -m "feat: show resolution stats (resolved/failing/rate) on Outcomes card"
```

---

### Task 4: UI — Resolution History section in Actions tab

**Files:**
- Modify: `src/kubeview/engine/analyticsApi.ts` (add fetch function)
- Modify: `src/kubeview/views/incidents/ActionsTab.tsx` (add history section)

- [ ] **Step 1: Add fetch function to analyticsApi.ts**

```typescript
export interface ResolutionRecord {
  id: string;
  findingId: string;
  category: string;
  tool: string;
  status: string;
  reasoning: string;
  outcome: 'verified' | 'still_failing' | 'improved';
  evidence: string;
  timestamp: number;
  verifiedAt: number | null;
  durationMs: number | null;
  timeToVerifyMs: number | null;
}

export interface ResolutionsResponse {
  resolutions: ResolutionRecord[];
  total: number;
}

export const fetchResolutions = (days = 7, limit = 50) =>
  get<ResolutionsResponse>(`${AGENT_BASE}/fix-history/resolutions?days=${days}&limit=${limit}`);
```

- [ ] **Step 2: Add Resolution History section to ActionsTab**

In `src/kubeview/views/incidents/ActionsTab.tsx`, after the recent actions section, add:

```tsx
      {/* Resolution History */}
      <Card>
        <div className="px-4 py-3 border-b border-slate-800">
          <h2 className="text-sm font-semibold text-slate-100 flex items-center gap-2">
            <CheckCircle className="w-4 h-4 text-emerald-400" />
            Resolution History
          </h2>
          <p className="text-[11px] text-slate-500 mt-0.5">
            Every auto-fix outcome — whether the fix resolved the issue, how long it took, and what was done.
          </p>
        </div>
        <ResolutionHistoryList />
      </Card>
```

Create `ResolutionHistoryList` as a new component inside the same file:

```tsx
function ResolutionHistoryList() {
  const [resolutions, setResolutions] = useState<ResolutionRecord[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchResolutions(7, 20)
      .then((data) => setResolutions(data.resolutions))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="px-4 py-6 text-xs text-slate-500 text-center">Loading resolution history...</div>;
  if (resolutions.length === 0) return <div className="px-4 py-6 text-xs text-slate-500 text-center">No resolution data yet. Auto-fix outcomes will appear here after the next scan cycle verifies fixes.</div>;

  return (
    <div className="divide-y divide-slate-800">
      {resolutions.map((r) => (
        <div key={r.id} className="px-4 py-3 flex items-center gap-3">
          <div className={cn(
            'flex items-center justify-center w-6 h-6 rounded-full shrink-0',
            r.outcome === 'verified' ? 'bg-emerald-500/15' :
            r.outcome === 'improved' ? 'bg-blue-500/15' : 'bg-amber-500/15',
          )}>
            {r.outcome === 'verified' ? (
              <CheckCircle className="w-3.5 h-3.5 text-emerald-400" />
            ) : r.outcome === 'improved' ? (
              <ArrowUp className="w-3.5 h-3.5 text-blue-400" />
            ) : (
              <XCircle className="w-3.5 h-3.5 text-amber-400" />
            )}
          </div>
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2">
              <span className="text-xs font-medium text-slate-200 truncate">{r.reasoning || r.tool}</span>
              <span className={cn(
                'text-[10px] px-1.5 py-0.5 rounded font-medium uppercase',
                r.outcome === 'verified' ? 'bg-emerald-900/40 text-emerald-400' :
                r.outcome === 'improved' ? 'bg-blue-900/40 text-blue-400' :
                'bg-amber-900/40 text-amber-400',
              )}>
                {r.outcome === 'verified' ? 'Resolved' : r.outcome === 'improved' ? 'Improved' : 'Failed'}
              </span>
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-800 text-slate-400">{r.category}</span>
            </div>
            {r.evidence && <div className="text-[11px] text-slate-500 mt-0.5 truncate">{r.evidence}</div>}
          </div>
          <div className="text-right shrink-0">
            <div className="text-xs text-slate-500">{formatRelativeTime(r.timestamp)}</div>
            {r.timeToVerifyMs != null && r.timeToVerifyMs > 0 && (
              <div className="text-[10px] text-slate-600">verified in {Math.round(r.timeToVerifyMs / 1000)}s</div>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
```

Add necessary imports at the top:

```typescript
import { fetchResolutions, type ResolutionRecord } from '../../engine/analyticsApi';
import { ArrowUp } from 'lucide-react';
```

- [ ] **Step 3: Run UI tests**

Run: `pnpm test`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add src/kubeview/engine/analyticsApi.ts src/kubeview/views/incidents/ActionsTab.tsx
git commit -m "feat: add Resolution History section to Actions tab"
```

---

### Task 5: Intelligence Feedback Loop — Fix Outcome Learning

**Files:**
- Modify: `sre_agent/intelligence.py`
- Test: `tests/test_intelligence.py` (extend or create)

- [ ] **Step 1: Write the failing test**

```python
from unittest.mock import patch, MagicMock
from sre_agent.intelligence import _compute_fix_outcomes


class TestFixOutcomes:
    @patch("sre_agent.intelligence.get_database")
    def test_returns_strategy_success_rates(self, mock_get_db):
        db = MagicMock()
        mock_get_db.return_value = db
        db.fetchall.return_value = [
            {"tool": "rollback_deployment", "category": "image_pull", "total": 10, "resolved": 8},
            {"tool": "restart_deployment", "category": "workloads", "total": 5, "resolved": 1},
            {"tool": "patch_resources", "category": "crashloop", "total": 3, "resolved": 3},
            {"tool": "delete_pod", "category": "crashloop", "total": 8, "resolved": 2},
        ]
        result = _compute_fix_outcomes(7)
        assert "rollback_deployment" in result
        assert "80%" in result
        assert "patch_resources" in result

    @patch("sre_agent.intelligence.get_database")
    def test_returns_empty_when_no_data(self, mock_get_db):
        db = MagicMock()
        mock_get_db.return_value = db
        db.fetchall.return_value = []
        result = _compute_fix_outcomes(7)
        assert result == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_intelligence.py::TestFixOutcomes -v`
Expected: FAIL — `_compute_fix_outcomes` not defined

- [ ] **Step 3: Implement _compute_fix_outcomes**

Add to `sre_agent/intelligence.py`:

```python
def _compute_fix_outcomes(days: int) -> str:
    """Compute fix strategy effectiveness from verification outcomes."""
    try:
        from .db import get_database

        db = get_database()
        rows = db.fetchall(
            "SELECT tool, category, "
            "COUNT(*) AS total, "
            "SUM(CASE WHEN verification_status = 'verified' THEN 1 ELSE 0 END) AS resolved "
            "FROM actions "
            "WHERE timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * %s)::BIGINT * 1000 "
            "AND tool IS NOT NULL AND tool != '' "
            "GROUP BY tool, category "
            "HAVING COUNT(*) >= 2 "
            "ORDER BY COUNT(*) DESC "
            "LIMIT 10",
            (days,),
        )
        if not rows:
            return ""

        lines = ["### Fix Strategy Effectiveness"]
        for r in rows:
            total = r["total"]
            resolved = r["resolved"]
            rate = round(resolved / total * 100) if total > 0 else 0
            indicator = "effective" if rate >= 60 else "weak" if rate >= 30 else "ineffective"
            lines.append(
                f"- {r['tool']} for {r['category']}: {rate}% resolved ({resolved}/{total}) — {indicator}"
            )

        lines.append("")
        lines.append("Prefer strategies marked 'effective'. Avoid repeating 'ineffective' strategies.")
        return "\n".join(lines)
    except Exception:
        logger.debug("Failed to compute fix outcomes", exc_info=True)
        return ""
```

- [ ] **Step 4: Wire into get_intelligence_context**

In `sre_agent/intelligence.py`, inside `get_intelligence_context()`, after the last section append (around line 85), add:

```python
        if "intelligence_fix_outcomes" not in excluded:
            fo = _compute_fix_outcomes(max_age_days)
            if fo:
                sections.append(fo)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_intelligence.py::TestFixOutcomes -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add sre_agent/intelligence.py tests/test_intelligence.py
git commit -m "feat: fix outcome learning — inject strategy effectiveness into system prompt"
```

---

### Task 6: Full Verification

- [ ] **Step 1: Run agent tests**

Run: `python3 -m pytest tests/ -v --tb=short`
Expected: All pass

- [ ] **Step 2: Run UI tests**

Run: `pnpm test`
Expected: All pass

- [ ] **Step 3: Run agent lint + types**

Run: `python3 -m ruff check sre_agent/ tests/ && python3 -m mypy sre_agent/ --ignore-missing-imports --exclude 'skills/(view-designer|capacity-planner)'`
Expected: Clean

- [ ] **Step 4: Run eval gate**

Run: `python3 -m sre_agent.evals.cli --suite release --fail-on-gate`
Expected: PASS

- [ ] **Step 5: Commit and push both repos**

```bash
cd /Users/amobrem/ali/pulse-agent && git push
cd /Users/amobrem/ali/OpenshiftPulse && git push
```
