# Mission Control — Frontend Redesign Implementation Plan

> Historical design/implementation record. This preserves the proposal and tasks at the date in the filename; unchecked tasks, paths, commands, and security claims are not evidence of the current implementation. Use [current documentation](../../README.md) and verify behavior against code before applying a procedure.


> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the 5-tab Agent Settings page with a single-page "Mission Control" view, add agent activity indicators to Pulse, expand Toolbox analytics, convert Welcome to onboarding-only, and add action reasoning to Incidents.

**Architecture:** Mission Control is a single scrollable page with 4 sections (Trust Policy, Agent Health, Agent Accuracy, Capability Discovery) and 3 slide-over drawers. New `engine/analyticsApi.ts` module provides typed fetch functions for all new backend endpoints. Existing Zustand stores (`trustStore`, `monitorStore`) are reused; monitorStore extended with `skill_activity` event handling.

**Tech Stack:** React 18, TypeScript, Tailwind CSS, Zustand, TanStack React Query, vitest, @testing-library/react, lucide-react icons

**Spec:** `docs/superpowers/specs/2026-04-12-mission-control-redesign-design.md`

**Backend dependency:** `docs/superpowers/plans/2026-04-12-mission-control-backend.md` — all new API endpoints must be deployed first. During frontend development, mock responses via MSW or inline stubs.

**UI Repo:** `/Users/amobrem/ali/OpenshiftPulse`

---

## File Structure

| File | Responsibility |
|------|---------------|
| `src/kubeview/engine/analyticsApi.ts` (create) | Typed fetch functions for all 9 new analytics endpoints |
| `src/kubeview/views/MissionControlView.tsx` (create) | Single-page layout: Trust Policy → Agent Health → Agent Accuracy → Capability Discovery |
| `src/kubeview/views/mission-control/TrustPolicy.tsx` (create) | Trust slider, policy summary, impact preview, auto-fix categories, communication style |
| `src/kubeview/views/mission-control/AgentHealth.tsx` (create) | 3 cards: Quality Gate, Coverage, Outcomes |
| `src/kubeview/views/mission-control/AgentAccuracy.tsx` (create) | Improvement trend, recurring mistakes, learning activity, override rate |
| `src/kubeview/views/mission-control/CapabilityDiscovery.tsx` (create) | Contextual recommendations with inline actions |
| `src/kubeview/views/mission-control/ScannerDrawer.tsx` (create) | Full scanner list with toggles and per-scanner stats |
| `src/kubeview/views/mission-control/EvalDrawer.tsx` (create) | Full eval suite breakdown, prompt audit, confidence detail |
| `src/kubeview/views/mission-control/MemoryDrawer.tsx` (create) | Learned patterns, runbooks, resolved incidents |
| `src/kubeview/views/AgentSettingsView.tsx` (delete/replace) | Replaced by MissionControlView — kept as redirect during transition |
| `src/kubeview/views/PulseView.tsx` (modify) | Add agent status indicator, monitoring controls, skill activity, token burn |
| `src/kubeview/views/ToolboxView.tsx` (modify) | Expand Analytics tab with 8 intelligence sections + prompt breakdown |
| `src/kubeview/views/WelcomeView.tsx` (modify) | Convert to first-run onboarding wizard |
| `src/kubeview/views/IncidentCenterView.tsx` (modify) | Add action reasoning text to History and Actions tabs |
| `src/kubeview/store/monitorStore.ts` (modify) | Handle `skill_activity` WebSocket event |
| `src/kubeview/engine/monitorClient.ts` (modify) | Add `skill_activity` to MonitorEvent type |
| `src/kubeview/routes/domainRoutes.tsx` (modify) | Update `/agent` route to MissionControlView |
| `src/kubeview/components/CommandPalette.tsx` (modify) | Update agent page description |
| `src/kubeview/App.tsx` (modify) | Change default redirect from `/welcome` to `/pulse` after onboarding |
| `src/kubeview/views/__tests__/MissionControlView.test.tsx` (create) | Tests for Mission Control page |

---

### Task 1: Analytics API client

**Files:**
- Create: `src/kubeview/engine/analyticsApi.ts`
- Create: `src/kubeview/engine/__tests__/analyticsApi.test.ts`

- [ ] **Step 1: Write failing test for API client**

Create `src/kubeview/engine/__tests__/analyticsApi.test.ts`:

```typescript
import { describe, it, expect, vi, afterEach } from 'vitest';
import {
  fetchFixHistorySummary,
  fetchScannerCoverage,
  fetchConfidenceCalibration,
  fetchAccuracyStats,
  fetchCostStats,
  fetchIntelligenceSections,
  fetchPromptStats,
  fetchRecommendations,
  fetchReadinessSummary,
} from '../analyticsApi';

const mockFetch = vi.fn();
vi.stubGlobal('fetch', mockFetch);

afterEach(() => vi.clearAllMocks());

describe('analyticsApi', () => {
  it('fetchFixHistorySummary calls correct endpoint', async () => {
    mockFetch.mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ total_actions: 5 }) });
    const result = await fetchFixHistorySummary(7);
    expect(mockFetch).toHaveBeenCalledWith('/api/agent/fix-history/summary?days=7');
    expect(result.total_actions).toBe(5);
  });

  it('fetchScannerCoverage calls correct endpoint', async () => {
    mockFetch.mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ coverage_pct: 78 }) });
    const result = await fetchScannerCoverage();
    expect(mockFetch).toHaveBeenCalledWith('/api/agent/monitor/coverage?days=7');
    expect(result.coverage_pct).toBe(78);
  });

  it('fetchConfidenceCalibration calls correct endpoint', async () => {
    mockFetch.mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ rating: 'good' }) });
    const result = await fetchConfidenceCalibration();
    expect(mockFetch).toHaveBeenCalledWith('/api/agent/analytics/confidence?days=30');
    expect(result.rating).toBe('good');
  });

  it('fetchRecommendations calls correct endpoint', async () => {
    mockFetch.mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ recommendations: [] }) });
    const result = await fetchRecommendations();
    expect(mockFetch).toHaveBeenCalledWith('/api/agent/recommendations');
    expect(result.recommendations).toEqual([]);
  });

  it('throws on non-ok response', async () => {
    mockFetch.mockResolvedValueOnce({ ok: false, status: 500 });
    await expect(fetchFixHistorySummary()).rejects.toThrow();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/amobrem/ali/OpenshiftPulse && npx vitest run src/kubeview/engine/__tests__/analyticsApi.test.ts 2>&1 | tail -10`
Expected: FAIL — module not found

- [ ] **Step 3: Implement analytics API client**

Create `src/kubeview/engine/analyticsApi.ts`:

```typescript
/** Typed fetch functions for Mission Control + Toolbox analytics endpoints. */

const AGENT_BASE = '/api/agent';

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`Analytics API error: ${res.status} on ${path}`);
  return res.json();
}

// --- Types ---

export interface FixHistorySummary {
  total_actions: number;
  completed: number;
  failed: number;
  rolled_back: number;
  success_rate: number;
  rollback_rate: number;
  avg_resolution_ms: number;
  by_category: Array<{ category: string; count: number; success_count: number; auto_fixed: number; confirmation_required: number }>;
  trend: { current_week: number; previous_week: number; delta: number };
}

export interface ScannerCoverage {
  active_scanners: number;
  total_scanners: number;
  coverage_pct: number;
  categories: Array<{ name: string; covered: boolean; scanners: string[] }>;
  per_scanner: Array<{ name: string; enabled: boolean; finding_count: number; actionable_count: number; noise_pct: number }>;
}

export interface ConfidenceCalibration {
  brier_score: number;
  accuracy_pct: number;
  rating: 'good' | 'fair' | 'poor' | 'insufficient_data';
  total_predictions: number;
  buckets: Array<{ range: string; predicted: number; actual: number; count: number }>;
}

export interface AccuracyStats {
  avg_quality_score: number;
  quality_trend: { current: number; previous: number; delta: number };
  dimensions: { resolution: number; efficiency: number; safety: number; speed: number };
  anti_patterns: Array<{ error_type: string; namespace: string; count: number; description: string }>;
  learning: {
    total_runbooks: number;
    new_this_month: number;
    runbook_success_rate: number;
    total_patterns: number;
    pattern_types: Record<string, number>;
  };
  override_rate: { overrides: number; total_proposed: number; rate: number };
}

export interface CostStats {
  avg_tokens_per_incident: number;
  trend: { current: number; previous: number; delta_pct: number };
  by_mode: Array<{ mode: string; avg_tokens: number; count: number }>;
  total_tokens: number;
  total_incidents: number;
}

export interface IntelligenceSections {
  query_reliability?: { preferred: Array<{ query: string; success_rate: number; total: number }>; unreliable: Array<{ query: string; success_rate: number; total: number }> };
  error_hotspots?: Array<{ tool: string; error_rate: number; total: number; common_error: string }>;
  token_efficiency?: { avg_input: number; avg_output: number; cache_hit_rate: number };
  harness_effectiveness?: { accuracy: number; wasted: Array<{ tool: string; offered: number; used: number }> };
  routing_accuracy?: { mode_switch_rate: number; total_sessions: number };
  feedback_analysis?: { negative: Array<{ tool: string; count: number }> };
  token_trending?: { input_delta_pct: number; output_delta_pct: number; cache_delta_pct: number };
  dashboard_patterns?: { top_components: Array<{ kind: string; count: number }>; avg_widgets: number };
}

export interface PromptAnalytics {
  stats: {
    total_prompts: number;
    avg_tokens: number;
    cache_hit_rate: number;
    section_avg: Record<string, number>;
    by_skill: Array<{ skill_name: string; count: number; avg_tokens: number; prompt_versions: number }>;
  };
  versions: Array<{ prompt_hash: string; count: number; first_seen: string | null; last_seen: string | null }>;
}

export interface Recommendation {
  type: 'scanner' | 'capability';
  title: string;
  description: string;
  action: { kind: string; scanner?: string; prompt?: string };
}

export interface ReadinessSummary {
  total_gates: number;
  passed: number;
  failed: number;
  attention: number;
  pass_rate: number;
  attention_items: Array<{ gate: string; message: string }>;
}

// --- Fetch functions ---

export const fetchFixHistorySummary = (days = 7) =>
  get<FixHistorySummary>(`${AGENT_BASE}/fix-history/summary?days=${days}`);

export const fetchScannerCoverage = (days = 7) =>
  get<ScannerCoverage>(`${AGENT_BASE}/monitor/coverage?days=${days}`);

export const fetchConfidenceCalibration = (days = 30) =>
  get<ConfidenceCalibration>(`${AGENT_BASE}/analytics/confidence?days=${days}`);

export const fetchAccuracyStats = (days = 30) =>
  get<AccuracyStats>(`${AGENT_BASE}/analytics/accuracy?days=${days}`);

export const fetchCostStats = (days = 30) =>
  get<CostStats>(`${AGENT_BASE}/analytics/cost?days=${days}`);

export const fetchIntelligenceSections = (days = 7, mode = 'sre') =>
  get<IntelligenceSections>(`${AGENT_BASE}/analytics/intelligence?days=${days}&mode=${mode}`);

export const fetchPromptStats = (days = 30, skill?: string) =>
  get<PromptAnalytics>(`${AGENT_BASE}/analytics/prompt?days=${days}${skill ? `&skill=${skill}` : ''}`);

export const fetchRecommendations = () =>
  get<{ recommendations: Recommendation[] }>(`${AGENT_BASE}/recommendations`);

export const fetchReadinessSummary = () =>
  get<ReadinessSummary>(`${AGENT_BASE}/analytics/readiness`);
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/amobrem/ali/OpenshiftPulse && npx vitest run src/kubeview/engine/__tests__/analyticsApi.test.ts 2>&1 | tail -10`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/amobrem/ali/OpenshiftPulse
git add src/kubeview/engine/analyticsApi.ts src/kubeview/engine/__tests__/analyticsApi.test.ts
git commit -m "feat: add typed analytics API client for Mission Control + Toolbox"
```

---

### Task 2: Mission Control page scaffold

**Files:**
- Create: `src/kubeview/views/MissionControlView.tsx`
- Modify: `src/kubeview/routes/domainRoutes.tsx`
- Modify: `src/kubeview/components/CommandPalette.tsx`

- [ ] **Step 1: Create Mission Control page scaffold**

Create `src/kubeview/views/MissionControlView.tsx`:

```tsx
import { useQuery } from '@tanstack/react-query';
import { Bot, Shield, ChevronRight } from 'lucide-react';
import { fetchFixHistorySummary, fetchScannerCoverage, fetchConfidenceCalibration, fetchAccuracyStats, fetchCostStats, fetchRecommendations, fetchReadinessSummary } from '../engine/analyticsApi';
import { fetchAgentEvalStatus } from '../engine/evalStatus';
import { useTrustStore } from '../store/trustStore';

export default function MissionControlView() {
  const trustLevel = useTrustStore((s) => s.trustLevel);

  // Data queries — all analytics endpoints
  const { data: evalStatus } = useQuery({
    queryKey: ['agent', 'eval-status'],
    queryFn: () => fetchAgentEvalStatus().catch(() => null),
    refetchInterval: 60_000,
  });

  const { data: fixSummary } = useQuery({
    queryKey: ['agent', 'fix-history-summary'],
    queryFn: () => fetchFixHistorySummary().catch(() => null),
    staleTime: 60_000,
  });

  const { data: coverage } = useQuery({
    queryKey: ['agent', 'scanner-coverage'],
    queryFn: () => fetchScannerCoverage().catch(() => null),
    staleTime: 60_000,
  });

  const { data: confidence } = useQuery({
    queryKey: ['agent', 'confidence'],
    queryFn: () => fetchConfidenceCalibration().catch(() => null),
    staleTime: 60_000,
  });

  const { data: accuracy } = useQuery({
    queryKey: ['agent', 'accuracy'],
    queryFn: () => fetchAccuracyStats().catch(() => null),
    staleTime: 60_000,
  });

  const { data: costStats } = useQuery({
    queryKey: ['agent', 'cost'],
    queryFn: () => fetchCostStats().catch(() => null),
    staleTime: 60_000,
  });

  const { data: recommendations } = useQuery({
    queryKey: ['agent', 'recommendations'],
    queryFn: () => fetchRecommendations().catch(() => null),
    staleTime: 5 * 60_000,
  });

  const { data: readiness } = useQuery({
    queryKey: ['agent', 'readiness-summary'],
    queryFn: () => fetchReadinessSummary().catch(() => null),
    staleTime: 60_000,
  });

  const { data: capabilities } = useQuery({
    queryKey: ['monitor', 'capabilities'],
    queryFn: async () => {
      const res = await fetch('/api/agent/monitor/capabilities');
      if (!res.ok) return { max_trust_level: 4 };
      return res.json();
    },
    staleTime: 60_000,
  });

  const { data: version } = useQuery({
    queryKey: ['agent', 'version'],
    queryFn: async () => {
      const res = await fetch('/api/agent/version');
      if (!res.ok) return null;
      return res.json();
    },
    staleTime: 5 * 60_000,
  });

  return (
    <div className="h-full overflow-auto bg-slate-950 p-6">
      <div className="max-w-5xl mx-auto space-y-6">
        {/* Page header */}
        <div className="flex items-center gap-3">
          <Bot className="w-6 h-6 text-violet-400" />
          <h1 className="text-lg font-semibold text-slate-100">Mission Control</h1>
          {version && (
            <span className="text-xs text-slate-500">
              v{version.agent} · Protocol v{version.protocol} · {version.tools} tools
            </span>
          )}
        </div>

        {/* Section 1: Trust Policy */}
        <div className="text-sm text-slate-400">Trust Policy section — placeholder</div>

        {/* Section 2: Agent Health */}
        <div className="text-sm text-slate-400">Agent Health section — placeholder</div>

        {/* Section 2b: Agent Accuracy */}
        <div className="text-sm text-slate-400">Agent Accuracy section — placeholder</div>

        {/* Section 3: Capability Discovery */}
        <div className="text-sm text-slate-400">Capability Discovery section — placeholder</div>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Update route to use MissionControlView**

In `src/kubeview/routes/domainRoutes.tsx`, replace the AgentSettingsView import and route:

```tsx
// Replace:
// import AgentSettingsView from '../views/AgentSettingsView';
// <Route path="agent" element={<AgentSettingsView />} />

// With:
const MissionControlView = lazy(() => import('../views/MissionControlView'));
// ...
<Route path="agent" element={<Lazy><MissionControlView /></Lazy>} />
```

- [ ] **Step 3: Update Command Palette description**

In `src/kubeview/components/CommandPalette.tsx`, update the agent entry:

```typescript
{ type: 'nav', id: 'agent', title: 'Mission Control', subtitle: 'Agent policy, health, accuracy, capability discovery', icon: 'Bot', path: '/agent' },
```

- [ ] **Step 4: Verify page renders**

Run: `cd /Users/amobrem/ali/OpenshiftPulse && npm run dev`
Navigate to `/agent` — should see the page scaffold with placeholder sections.

- [ ] **Step 5: Commit**

```bash
cd /Users/amobrem/ali/OpenshiftPulse
git add src/kubeview/views/MissionControlView.tsx src/kubeview/routes/domainRoutes.tsx src/kubeview/components/CommandPalette.tsx
git commit -m "feat: Mission Control page scaffold with data queries"
```

---

### Task 3: Trust Policy section (Section 1)

**Files:**
- Create: `src/kubeview/views/mission-control/TrustPolicy.tsx`
- Modify: `src/kubeview/views/MissionControlView.tsx`

- [ ] **Step 1: Create TrustPolicy component**

Create `src/kubeview/views/mission-control/TrustPolicy.tsx`:

```tsx
import { useState } from 'react';
import { Shield, Eye, MessageSquare, Zap, Activity, AlertTriangle, Info } from 'lucide-react';
import { cn } from '@/lib/utils';
import { Card } from '../../components/primitives/Card';
import { ConfirmDialog } from '../../components/feedback/ConfirmDialog';
import { useTrustStore, TRUST_LABELS, TRUST_DESCRIPTIONS, type TrustLevel, type CommunicationStyle } from '../../store/trustStore';
import type { FixHistorySummary } from '../../engine/analyticsApi';

const TRUST_ICONS = [Eye, MessageSquare, Shield, Zap, Activity] as const;

const AUTOFIX_CATEGORIES = [
  { id: 'crashloop', label: 'Crashlooping pods', description: 'Delete crashlooping pods (controller recreates)' },
  { id: 'workloads', label: 'Failed deployments', description: 'Rolling restart for degraded deployments' },
  { id: 'image_pull', label: 'Image pull errors', description: 'Restart deployment to clear image pull errors' },
] as const;

const COMM_STYLES: Array<{ id: CommunicationStyle; label: string; description: string }> = [
  { id: 'brief', label: 'Brief', description: 'Short, actionable answers' },
  { id: 'detailed', label: 'Detailed', description: 'Full explanations with context' },
  { id: 'technical', label: 'Technical', description: 'Deep detail, CLI examples' },
];

// Which auto-fix categories unlock at each trust level
const LEVEL_CATEGORIES: Record<number, string[]> = {
  0: [],
  1: [],
  2: [],
  3: ['crashloop', 'workloads'],
  4: ['crashloop', 'workloads', 'image_pull'],
};

interface TrustPolicyProps {
  maxTrustLevel: number;
  scannerCount: number;
  fixSummary: FixHistorySummary | null;
}

export function TrustPolicy({ maxTrustLevel, scannerCount, fixSummary }: TrustPolicyProps) {
  const trustLevel = useTrustStore((s) => s.trustLevel);
  const setTrustLevel = useTrustStore((s) => s.setTrustLevel);
  const autoFixCategories = useTrustStore((s) => s.autoFixCategories);
  const setAutoFixCategories = useTrustStore((s) => s.setAutoFixCategories);
  const communicationStyle = useTrustStore((s) => s.communicationStyle);
  const setCommunicationStyle = useTrustStore((s) => s.setCommunicationStyle);

  const [confirmLevel, setConfirmLevel] = useState<TrustLevel | null>(null);
  const [hoveredLevel, setHoveredLevel] = useState<TrustLevel | null>(null);

  const handleLevelClick = (level: TrustLevel) => {
    if (level > maxTrustLevel) return;
    if (level >= 3 && trustLevel < 3) {
      setConfirmLevel(level);
    } else {
      setTrustLevel(level);
    }
  };

  const previewLevel = hoveredLevel ?? trustLevel;

  // Build plain-English policy summary
  const policySummary = buildPolicySummary(previewLevel, scannerCount, autoFixCategories, communicationStyle);

  // Build impact preview when hovering a different level
  const impactPreview = hoveredLevel !== null && hoveredLevel !== trustLevel
    ? buildImpactPreview(trustLevel, hoveredLevel, fixSummary)
    : null;

  return (
    <Card>
      <div className="p-5 space-y-5">
        {/* Trust Level Selector */}
        <div>
          <h2 className="text-sm font-semibold text-slate-200 mb-3">Trust Level</h2>
          <div className="flex gap-1">
            {([0, 1, 2, 3, 4] as TrustLevel[]).map((level) => {
              const Icon = TRUST_ICONS[level];
              const disabled = level > maxTrustLevel;
              const active = level === trustLevel;
              return (
                <button
                  key={level}
                  onClick={() => handleLevelClick(level)}
                  onMouseEnter={() => !disabled && setHoveredLevel(level)}
                  onMouseLeave={() => setHoveredLevel(null)}
                  disabled={disabled}
                  className={cn(
                    'flex-1 flex flex-col items-center gap-1 rounded-lg px-3 py-3 text-xs transition-all border',
                    active
                      ? 'bg-violet-500/20 border-violet-500 text-violet-300'
                      : disabled
                        ? 'opacity-30 cursor-not-allowed border-slate-800'
                        : 'border-slate-800 hover:border-slate-600 text-slate-400 hover:text-slate-200 cursor-pointer',
                  )}
                  title={disabled ? `Capped by server (max: ${maxTrustLevel})` : TRUST_DESCRIPTIONS[level]}
                >
                  <Icon className="w-4 h-4" />
                  <span className="font-medium">{TRUST_LABELS[level]}</span>
                </button>
              );
            })}
          </div>
        </div>

        {/* Policy Summary */}
        <p className="text-sm text-slate-300 leading-relaxed">{policySummary}</p>

        {/* Impact Preview (on hover) */}
        {impactPreview && (
          <div className="flex items-start gap-2 text-xs text-amber-300/80 bg-amber-500/5 rounded-md px-3 py-2 border border-amber-500/10">
            <Info className="w-3.5 h-3.5 mt-0.5 shrink-0" />
            <span>{impactPreview}</span>
          </div>
        )}

        {/* Auto-fix Categories (inline) */}
        <div className={cn('space-y-2', trustLevel < 2 && 'opacity-40')}>
          <h3 className="text-xs font-medium text-slate-400 uppercase tracking-wider">Auto-fix categories</h3>
          <div className="flex flex-wrap gap-2">
            {AUTOFIX_CATEGORIES.map((cat) => {
              const enabled = autoFixCategories.includes(cat.id);
              const disabled = trustLevel < 2;
              return (
                <button
                  key={cat.id}
                  disabled={disabled}
                  onClick={() => {
                    if (disabled) return;
                    setAutoFixCategories(
                      enabled
                        ? autoFixCategories.filter((c) => c !== cat.id)
                        : [...autoFixCategories, cat.id],
                    );
                  }}
                  className={cn(
                    'px-3 py-1.5 rounded-md text-xs border transition-colors',
                    enabled && !disabled
                      ? 'bg-violet-500/20 border-violet-500/50 text-violet-300'
                      : 'border-slate-700 text-slate-500',
                    !disabled && 'hover:border-slate-500 cursor-pointer',
                  )}
                  title={cat.description}
                >
                  {cat.label}
                </button>
              );
            })}
          </div>
        </div>

        {/* Communication Style (subtle) */}
        <div className="flex items-center gap-3">
          <span className="text-xs text-slate-500">Style:</span>
          <div className="flex gap-1">
            {COMM_STYLES.map((style) => (
              <button
                key={style.id}
                onClick={() => setCommunicationStyle(style.id)}
                className={cn(
                  'px-2.5 py-1 rounded text-xs transition-colors',
                  communicationStyle === style.id
                    ? 'bg-slate-700 text-slate-200'
                    : 'text-slate-500 hover:text-slate-300',
                )}
                title={style.description}
              >
                {style.label}
              </button>
            ))}
          </div>
        </div>

        {/* Warning banner for trust ≥ 3 */}
        {trustLevel >= 3 && (
          <div className="flex items-start gap-2 text-xs text-amber-400 bg-amber-500/5 rounded-md px-3 py-2 border border-amber-500/10">
            <AlertTriangle className="w-3.5 h-3.5 mt-0.5 shrink-0" />
            <span>Auto-fixes execute automatically and are recorded in Fix History. Some actions cannot be rolled back.</span>
          </div>
        )}
      </div>

      {/* Confirmation dialog for level 3+ */}
      {confirmLevel !== null && (
        <ConfirmDialog
          title="Enable Auto-Fix?"
          message={`At Trust Level ${confirmLevel}, the agent will automatically fix certain issues without asking. You can configure which categories below.`}
          onConfirm={() => { setTrustLevel(confirmLevel); setConfirmLevel(null); }}
          onCancel={() => setConfirmLevel(null)}
        />
      )}
    </Card>
  );
}

function buildPolicySummary(level: TrustLevel, scanners: number, categories: string[], style: CommunicationStyle): string {
  const catNames = categories.length > 0 ? categories.join(', ').replace(/_/g, ' ') : 'none';
  const styleName = style === 'brief' ? 'brief' : style === 'technical' ? 'technical' : 'detailed';

  switch (level) {
    case 0:
      return `Your agent monitors ${scanners} scanners and reports findings. It takes no actions. Communication: ${styleName}.`;
    case 1:
      return `Your agent monitors ${scanners} scanners and suggests fixes with dry-run previews. It never acts without your approval. Communication: ${styleName}.`;
    case 2:
      return `Your agent monitors ${scanners} scanners and proposes fixes for your review before acting. Communication: ${styleName}.`;
    case 3:
      return `Your agent monitors ${scanners} scanners, auto-fixes ${catNames}, and asks before anything risky. Communication: ${styleName}.`;
    case 4:
      return `Your agent monitors ${scanners} scanners and auto-fixes all enabled categories (${catNames}). All actions are logged. Communication: ${styleName}.`;
    default:
      return '';
  }
}

function buildImpactPreview(current: TrustLevel, target: TrustLevel, fixSummary: FixHistorySummary | null): string | null {
  if (!fixSummary || fixSummary.total_actions === 0) {
    // No historical data — generic preview
    if (target > current) {
      const newCats = LEVEL_CATEGORIES[target]?.filter((c) => !(LEVEL_CATEGORIES[current] || []).includes(c)) || [];
      if (newCats.length > 0) {
        return `Moving to Level ${target} would also auto-fix: ${newCats.join(', ').replace(/_/g, ' ')}.`;
      }
    }
    return `Level ${target}: ${TRUST_DESCRIPTIONS[target]}`;
  }

  // Historical data available — compute impact
  if (target > current) {
    const newCats = LEVEL_CATEGORIES[target]?.filter((c) => !(LEVEL_CATEGORIES[current] || []).includes(c)) || [];
    const additionalFixes = fixSummary.by_category
      .filter((c) => newCats.includes(c.category))
      .reduce((sum, c) => sum + c.confirmation_required, 0);

    if (additionalFixes > 0) {
      return `Moving to Level ${target} would also auto-fix ${newCats.join(', ').replace(/_/g, ' ')}. Last week, this would have resolved ${additionalFixes} additional incidents without asking.`;
    }
  }
  return `Level ${target}: ${TRUST_DESCRIPTIONS[target]}`;
}
```

- [ ] **Step 2: Wire TrustPolicy into MissionControlView**

In `MissionControlView.tsx`, replace the Section 1 placeholder:

```tsx
import { TrustPolicy } from './mission-control/TrustPolicy';

// In the JSX, replace the placeholder:
<TrustPolicy
  maxTrustLevel={capabilities?.max_trust_level ?? 4}
  scannerCount={coverage?.active_scanners ?? 0}
  fixSummary={fixSummary ?? null}
/>
```

- [ ] **Step 3: Verify in browser**

Run dev server, navigate to `/agent`. Trust slider should render with all 5 levels, policy summary should update on click, impact preview on hover.

- [ ] **Step 4: Commit**

```bash
cd /Users/amobrem/ali/OpenshiftPulse
git add src/kubeview/views/mission-control/TrustPolicy.tsx src/kubeview/views/MissionControlView.tsx
git commit -m "feat: Mission Control Trust Policy section with impact preview"
```

---

### Task 4: Agent Health cards (Section 2)

**Files:**
- Create: `src/kubeview/views/mission-control/AgentHealth.tsx`
- Modify: `src/kubeview/views/MissionControlView.tsx`

- [ ] **Step 1: Create AgentHealth component with 3 cards**

Create `src/kubeview/views/mission-control/AgentHealth.tsx`:

```tsx
import { useState } from 'react';
import { CheckCircle2, XCircle, Shield, Radar, TrendingUp, TrendingDown, Minus, Brain, ChevronRight } from 'lucide-react';
import { cn } from '@/lib/utils';
import { Card } from '../../components/primitives/Card';
import type { FixHistorySummary, ScannerCoverage, ConfidenceCalibration, CostStats, ReadinessSummary } from '../../engine/analyticsApi';

interface AgentHealthProps {
  evalStatus: any | null;
  coverage: ScannerCoverage | null;
  fixSummary: FixHistorySummary | null;
  confidence: ConfidenceCalibration | null;
  costStats: CostStats | null;
  readiness: ReadinessSummary | null;
  onOpenScannerDrawer: () => void;
  onOpenEvalDrawer: () => void;
  onOpenMemoryDrawer: () => void;
  memoryPatternCount?: number;
}

export function AgentHealth({
  evalStatus, coverage, fixSummary, confidence, costStats, readiness,
  onOpenScannerDrawer, onOpenEvalDrawer, onOpenMemoryDrawer,
  memoryPatternCount = 0,
}: AgentHealthProps) {
  return (
    <div>
      <h2 className="text-sm font-semibold text-slate-200 mb-3">Agent Health</h2>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <QualityCard evalStatus={evalStatus} confidence={confidence} onClick={onOpenEvalDrawer} />
        <CoverageCard coverage={coverage} onClick={onOpenScannerDrawer} />
        <OutcomesCard
          fixSummary={fixSummary}
          costStats={costStats}
          readiness={readiness}
          memoryPatternCount={memoryPatternCount}
          onMemoryClick={onOpenMemoryDrawer}
        />
      </div>
    </div>
  );
}

function QualityCard({ evalStatus, confidence, onClick }: { evalStatus: any | null; confidence: ConfidenceCalibration | null; onClick: () => void }) {
  const passed = evalStatus?.quality_gate_passed;
  const avgScore = evalStatus?.release?.average_overall;
  const dims = evalStatus?.release?.dimension_averages || {};

  return (
    <Card onClick={onClick} className="group">
      <div className="p-4 space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="text-xs font-medium text-slate-400 uppercase tracking-wider">Quality Gate</h3>
          <ChevronRight className="w-4 h-4 text-slate-600 group-hover:text-slate-400 transition-colors" />
        </div>

        {/* Pass/fail badge */}
        <div className="flex items-center gap-2">
          {passed === true && <CheckCircle2 className="w-5 h-5 text-emerald-400" />}
          {passed === false && <XCircle className="w-5 h-5 text-red-400" />}
          <span className={cn('text-2xl font-bold', passed ? 'text-emerald-400' : 'text-red-400')}>
            {avgScore != null ? `${Math.round(avgScore * 100)}%` : '—'}
          </span>
        </div>

        {/* Dimension bars */}
        <div className="space-y-1">
          {Object.entries(dims).slice(0, 4).map(([dim, score]) => (
            <DimensionBar key={dim} label={dim} score={score as number} />
          ))}
        </div>

        {/* Confidence calibration */}
        {confidence && confidence.rating !== 'insufficient_data' && (
          <div className="flex items-center gap-2 text-xs text-slate-400 pt-1 border-t border-slate-800">
            <Shield className="w-3 h-3" />
            <span>Confidence accuracy: {confidence.accuracy_pct}%</span>
            <span className={cn(
              'px-1.5 py-0.5 rounded text-[10px] font-medium',
              confidence.rating === 'good' ? 'bg-emerald-500/10 text-emerald-400' :
              confidence.rating === 'fair' ? 'bg-amber-500/10 text-amber-400' :
              'bg-red-500/10 text-red-400',
            )}>
              {confidence.rating}
            </span>
          </div>
        )}

        {/* Safety record */}
        {evalStatus?.release?.blocker_counts && (
          <div className="text-xs text-slate-500">
            {(evalStatus.release.blocker_counts.policy_violation || 0) + (evalStatus.release.blocker_counts.hallucinated_tool || 0) === 0
              ? '0 policy violations, 0 hallucinated tools'
              : `${evalStatus.release.blocker_counts.policy_violation || 0} violations, ${evalStatus.release.blocker_counts.hallucinated_tool || 0} hallucinated tools`}
          </div>
        )}
      </div>
    </Card>
  );
}

function CoverageCard({ coverage, onClick }: { coverage: ScannerCoverage | null; onClick: () => void }) {
  return (
    <Card onClick={onClick} className="group">
      <div className="p-4 space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="text-xs font-medium text-slate-400 uppercase tracking-wider">Coverage</h3>
          <ChevronRight className="w-4 h-4 text-slate-600 group-hover:text-slate-400 transition-colors" />
        </div>

        <div className="flex items-baseline gap-2">
          <span className="text-2xl font-bold text-slate-100">
            {coverage?.coverage_pct != null ? `${coverage.coverage_pct}%` : '—'}
          </span>
          <span className="text-xs text-slate-500">
            {coverage ? `${coverage.active_scanners} of ${coverage.total_scanners} scanners` : ''}
          </span>
        </div>

        {/* Category breakdown */}
        <div className="space-y-1">
          {(coverage?.categories || []).map((cat) => (
            <div key={cat.name} className="flex items-center gap-2 text-xs">
              <div className={cn('w-1.5 h-1.5 rounded-full', cat.covered ? 'bg-emerald-400' : 'bg-slate-600')} />
              <span className={cn(cat.covered ? 'text-slate-300' : 'text-slate-500')}>
                {cat.name.replace(/_/g, ' ')}
              </span>
            </div>
          ))}
        </div>
      </div>
    </Card>
  );
}

function OutcomesCard({
  fixSummary, costStats, readiness, memoryPatternCount, onMemoryClick,
}: {
  fixSummary: FixHistorySummary | null;
  costStats: CostStats | null;
  readiness: ReadinessSummary | null;
  memoryPatternCount: number;
  onMemoryClick: () => void;
}) {
  const trend = fixSummary?.trend;
  const trendDir = trend && trend.delta > 0 ? 'up' : trend && trend.delta < 0 ? 'down' : 'flat';

  return (
    <Card>
      <div className="p-4 space-y-3">
        <h3 className="text-xs font-medium text-slate-400 uppercase tracking-wider">Outcomes</h3>

        {/* Headline stats */}
        {fixSummary && (
          <div className="text-sm text-slate-200">
            <span className="font-semibold">{fixSummary.total_actions}</span> findings ·{' '}
            <span className="text-emerald-400">{fixSummary.completed} fixed</span> ·{' '}
            <span className="text-amber-400">{fixSummary.rolled_back} rolled back</span>
          </div>
        )}

        {/* Auto-fix success rate */}
        {fixSummary && fixSummary.total_actions > 0 && (
          <div className="text-xs text-slate-400">
            Auto-fix success: <span className="text-slate-200">{Math.round(fixSummary.success_rate * 100)}%</span>
          </div>
        )}

        {/* Resolution time */}
        {fixSummary && fixSummary.avg_resolution_ms > 0 && (
          <div className="text-xs text-slate-400">
            Avg resolution: <span className="text-slate-200">{(fixSummary.avg_resolution_ms / 60000).toFixed(1)} min</span>
          </div>
        )}

        {/* Cost per incident */}
        {costStats && costStats.avg_tokens_per_incident > 0 && (
          <div className="text-xs text-slate-400">
            Avg tokens/incident: <span className="text-slate-200">{(costStats.avg_tokens_per_incident / 1000).toFixed(1)}K</span>
            {costStats.trend.delta_pct !== 0 && (
              <TrendBadge delta={costStats.trend.delta_pct} invertColor />
            )}
          </div>
        )}

        {/* Memory + Readiness footer */}
        <div className="flex items-center justify-between pt-2 border-t border-slate-800">
          <button onClick={onMemoryClick} className="text-xs text-violet-400 hover:text-violet-300">
            <Brain className="w-3 h-3 inline mr-1" />{memoryPatternCount} patterns learned
          </button>
          {readiness && (
            <span className="text-xs text-slate-500">
              Readiness: {readiness.passed}/{readiness.total_gates}
            </span>
          )}
        </div>
      </div>
    </Card>
  );
}

function DimensionBar({ label, score }: { label: string; score: number }) {
  const pct = Math.round(score * 100);
  return (
    <div className="flex items-center gap-2">
      <span className="text-[10px] text-slate-500 w-20 truncate capitalize">{label.replace(/_/g, ' ')}</span>
      <div className="flex-1 h-1.5 bg-slate-800 rounded-full overflow-hidden">
        <div
          className={cn('h-full rounded-full', pct >= 80 ? 'bg-emerald-500' : pct >= 60 ? 'bg-amber-500' : 'bg-red-500')}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="text-[10px] text-slate-500 w-8 text-right">{pct}%</span>
    </div>
  );
}

function TrendBadge({ delta, invertColor = false }: { delta: number; invertColor?: boolean }) {
  const positive = invertColor ? delta < 0 : delta > 0;
  const Icon = delta > 0 ? TrendingUp : delta < 0 ? TrendingDown : Minus;
  return (
    <span className={cn('inline-flex items-center gap-0.5 ml-1 text-[10px]', positive ? 'text-emerald-400' : 'text-red-400')}>
      <Icon className="w-3 h-3" />{Math.abs(delta).toFixed(1)}%
    </span>
  );
}
```

- [ ] **Step 2: Wire AgentHealth into MissionControlView**

Add drawer state and the component:

```tsx
import { AgentHealth } from './mission-control/AgentHealth';

// Inside MissionControlView component, add drawer state:
const [drawerOpen, setDrawerOpen] = useState<'scanner' | 'eval' | 'memory' | null>(null);

// Replace Section 2 placeholder:
<AgentHealth
  evalStatus={evalStatus}
  coverage={coverage ?? null}
  fixSummary={fixSummary ?? null}
  confidence={confidence ?? null}
  costStats={costStats ?? null}
  readiness={readiness ?? null}
  onOpenScannerDrawer={() => setDrawerOpen('scanner')}
  onOpenEvalDrawer={() => setDrawerOpen('eval')}
  onOpenMemoryDrawer={() => setDrawerOpen('memory')}
  memoryPatternCount={accuracy?.learning?.total_patterns ?? 0}
/>
```

- [ ] **Step 3: Verify in browser**

Three cards should render in a row: Quality Gate (pass/fail + dimension bars), Coverage (percentage + category dots), Outcomes (headline stats + cost trend). Cards should be clickable (hover effect).

- [ ] **Step 4: Commit**

```bash
cd /Users/amobrem/ali/OpenshiftPulse
git add src/kubeview/views/mission-control/AgentHealth.tsx src/kubeview/views/MissionControlView.tsx
git commit -m "feat: Mission Control Agent Health cards (quality, coverage, outcomes)"
```

---

### Task 5: Agent Accuracy section (Section 2b)

**Files:**
- Create: `src/kubeview/views/mission-control/AgentAccuracy.tsx`
- Modify: `src/kubeview/views/MissionControlView.tsx`

- [ ] **Step 1: Create AgentAccuracy component**

Create `src/kubeview/views/mission-control/AgentAccuracy.tsx`:

```tsx
import { useState } from 'react';
import { TrendingUp, AlertCircle, BookOpen, UserX, ChevronDown, ChevronRight, CheckCircle2 } from 'lucide-react';
import { cn } from '@/lib/utils';
import { Card } from '../../components/primitives/Card';
import { useTrustStore } from '../../store/trustStore';
import type { AccuracyStats } from '../../engine/analyticsApi';

interface AgentAccuracyProps {
  accuracy: AccuracyStats | null;
  onOpenMemoryDrawer: () => void;
}

export function AgentAccuracy({ accuracy, onOpenMemoryDrawer }: AgentAccuracyProps) {
  const trustLevel = useTrustStore((s) => s.trustLevel);
  const [expanded, setExpanded] = useState(trustLevel <= 2);

  if (!accuracy) return null;

  const hasAntiPatterns = accuracy.anti_patterns.length > 0;

  return (
    <Card>
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center justify-between px-5 py-3 text-left"
      >
        <h2 className="text-sm font-semibold text-slate-200">Agent Accuracy</h2>
        {expanded ? <ChevronDown className="w-4 h-4 text-slate-500" /> : <ChevronRight className="w-4 h-4 text-slate-500" />}
      </button>

      {expanded && (
        <div className="px-5 pb-5 space-y-4 border-t border-slate-800 pt-4">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            {/* Improvement Trend */}
            <div className="space-y-1">
              <div className="flex items-center gap-2 text-xs text-slate-400">
                <TrendingUp className="w-3.5 h-3.5" />
                <span>Quality Score</span>
              </div>
              <div className="flex items-baseline gap-2">
                <span className="text-lg font-bold text-slate-100">
                  {(accuracy.avg_quality_score * 100).toFixed(0)}%
                </span>
                {accuracy.quality_trend.delta !== 0 && (
                  <span className={cn('text-xs', accuracy.quality_trend.delta > 0 ? 'text-emerald-400' : 'text-red-400')}>
                    {accuracy.quality_trend.delta > 0 ? '+' : ''}{(accuracy.quality_trend.delta * 100).toFixed(1)}%
                  </span>
                )}
              </div>
            </div>

            {/* Override Rate */}
            <div className="space-y-1">
              <div className="flex items-center gap-2 text-xs text-slate-400">
                <UserX className="w-3.5 h-3.5" />
                <span>Override Rate</span>
              </div>
              <div className="text-lg font-bold text-slate-100">
                {accuracy.override_rate.total_proposed > 0
                  ? `${(accuracy.override_rate.rate * 100).toFixed(0)}%`
                  : '—'}
              </div>
              {accuracy.override_rate.total_proposed > 0 && (
                <div className="text-xs text-slate-500">
                  {accuracy.override_rate.overrides} of {accuracy.override_rate.total_proposed} actions overridden
                </div>
              )}
            </div>
          </div>

          {/* Recurring Mistakes */}
          <div className="space-y-2">
            <div className="flex items-center gap-2 text-xs text-slate-400">
              <AlertCircle className="w-3.5 h-3.5" />
              <span>Recurring Issues</span>
            </div>
            {hasAntiPatterns ? (
              <div className="space-y-1">
                {accuracy.anti_patterns.map((ap, i) => (
                  <div key={i} className="text-xs text-amber-300/80 bg-amber-500/5 rounded px-3 py-1.5 border border-amber-500/10">
                    {ap.description} — {ap.count} incidents this month
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-xs text-emerald-400 flex items-center gap-1">
                <CheckCircle2 className="w-3 h-3" /> No recurring issues detected
              </div>
            )}
          </div>

          {/* Learning Activity */}
          <div className="space-y-2">
            <div className="flex items-center gap-2 text-xs text-slate-400">
              <BookOpen className="w-3.5 h-3.5" />
              <span>Learning</span>
            </div>
            <div className="flex items-center gap-4 text-xs text-slate-300">
              <span>{accuracy.learning.total_runbooks} runbooks ({accuracy.learning.new_this_month} new)</span>
              <span>·</span>
              <span>Success rate: {(accuracy.learning.runbook_success_rate * 100).toFixed(0)}%</span>
              <span>·</span>
              <span>{accuracy.learning.total_patterns} patterns</span>
            </div>
            <button onClick={onOpenMemoryDrawer} className="text-xs text-violet-400 hover:text-violet-300">
              View learned patterns →
            </button>
          </div>
        </div>
      )}
    </Card>
  );
}
```

- [ ] **Step 2: Wire into MissionControlView**

```tsx
import { AgentAccuracy } from './mission-control/AgentAccuracy';

// Replace Section 2b placeholder:
<AgentAccuracy
  accuracy={accuracy ?? null}
  onOpenMemoryDrawer={() => setDrawerOpen('memory')}
/>
```

- [ ] **Step 3: Verify in browser**

Section should render collapsed at trust 3+, expanded at trust 0-2. Shows quality score, override rate, recurring issues, and learning activity.

- [ ] **Step 4: Commit**

```bash
cd /Users/amobrem/ali/OpenshiftPulse
git add src/kubeview/views/mission-control/AgentAccuracy.tsx src/kubeview/views/MissionControlView.tsx
git commit -m "feat: Mission Control Agent Accuracy section with anti-patterns"
```

---

### Task 6: Capability Discovery section (Section 3)

**Files:**
- Create: `src/kubeview/views/mission-control/CapabilityDiscovery.tsx`
- Modify: `src/kubeview/views/MissionControlView.tsx`

- [ ] **Step 1: Create CapabilityDiscovery component**

Create `src/kubeview/views/mission-control/CapabilityDiscovery.tsx`:

```tsx
import { useState, useCallback } from 'react';
import { Lightbulb, X, Radar, MessageSquare } from 'lucide-react';
import { cn } from '@/lib/utils';
import { Card } from '../../components/primitives/Card';
import type { Recommendation } from '../../engine/analyticsApi';

const DISMISSED_KEY = 'openshiftpulse-dismissed-recommendations';

interface CapabilityDiscoveryProps {
  recommendations: Recommendation[];
}

export function CapabilityDiscovery({ recommendations }: CapabilityDiscoveryProps) {
  const [dismissed, setDismissed] = useState<Set<string>>(() => {
    try {
      const stored = localStorage.getItem(DISMISSED_KEY);
      return new Set(stored ? JSON.parse(stored) : []);
    } catch {
      return new Set();
    }
  });

  const dismiss = useCallback((title: string) => {
    setDismissed((prev) => {
      const next = new Set(prev);
      next.add(title);
      localStorage.setItem(DISMISSED_KEY, JSON.stringify([...next]));
      return next;
    });
  }, []);

  const visible = recommendations.filter((r) => !dismissed.has(r.title));

  if (visible.length === 0) return null;

  return (
    <div>
      <h2 className="text-sm font-semibold text-slate-200 mb-3 flex items-center gap-2">
        <Lightbulb className="w-4 h-4 text-amber-400" />
        You could also be using...
      </h2>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        {visible.slice(0, 4).map((rec) => (
          <Card key={rec.title}>
            <div className="p-4 space-y-2">
              <div className="flex items-start justify-between">
                <div className="flex items-center gap-2">
                  {rec.type === 'scanner' ? (
                    <Radar className="w-4 h-4 text-blue-400 shrink-0" />
                  ) : (
                    <MessageSquare className="w-4 h-4 text-violet-400 shrink-0" />
                  )}
                  <h3 className="text-sm font-medium text-slate-200">{rec.title}</h3>
                </div>
                <button
                  onClick={() => dismiss(rec.title)}
                  className="text-slate-600 hover:text-slate-400 p-0.5"
                  aria-label="Dismiss"
                >
                  <X className="w-3.5 h-3.5" />
                </button>
              </div>
              <p className="text-xs text-slate-400 leading-relaxed">{rec.description}</p>
              {rec.action.kind === 'enable_scanner' && (
                <button className="text-xs text-blue-400 hover:text-blue-300 font-medium">
                  Enable scanner →
                </button>
              )}
              {rec.action.kind === 'chat_prompt' && (
                <button className="text-xs text-violet-400 hover:text-violet-300 font-medium">
                  Try in chat →
                </button>
              )}
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Wire into MissionControlView**

```tsx
import { CapabilityDiscovery } from './mission-control/CapabilityDiscovery';

// Replace Section 3 placeholder:
{recommendations?.recommendations && (
  <CapabilityDiscovery recommendations={recommendations.recommendations} />
)}
```

- [ ] **Step 3: Verify in browser**

Recommendation cards should render with dismiss buttons. Dismissed state persists via localStorage.

- [ ] **Step 4: Commit**

```bash
cd /Users/amobrem/ali/OpenshiftPulse
git add src/kubeview/views/mission-control/CapabilityDiscovery.tsx src/kubeview/views/MissionControlView.tsx
git commit -m "feat: Mission Control Capability Discovery section with dismissable recommendations"
```

---

### Task 7: Detail drawers (Scanner, Eval, Memory)

**Files:**
- Create: `src/kubeview/views/mission-control/ScannerDrawer.tsx`
- Create: `src/kubeview/views/mission-control/EvalDrawer.tsx`
- Create: `src/kubeview/views/mission-control/MemoryDrawer.tsx`
- Modify: `src/kubeview/views/MissionControlView.tsx`

- [ ] **Step 1: Create shared Drawer shell**

Add to the top of `ScannerDrawer.tsx` (or extract to a shared component):

```tsx
function DrawerShell({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  return (
    <div
      className="fixed inset-0 z-50 flex justify-end"
      onClick={onClose}
      onKeyDown={(e) => { if (e.key === 'Escape') onClose(); }}
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
      <div className="absolute inset-0 bg-black/50" />
      <div
        className="relative w-full max-w-2xl bg-slate-950 border-l border-slate-800 h-full overflow-auto shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="sticky top-0 z-10 bg-slate-950 border-b border-slate-800 px-5 py-4 flex items-center justify-between">
          <h2 className="text-base font-semibold text-slate-100">{title}</h2>
          <button onClick={onClose} className="text-slate-500 hover:text-slate-300">
            <X className="w-5 h-5" />
          </button>
        </div>
        <div className="p-5">{children}</div>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Create ScannerDrawer**

Create `src/kubeview/views/mission-control/ScannerDrawer.tsx`:

```tsx
import { X } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useQuery } from '@tanstack/react-query';
import { fetchScannerCoverage, type ScannerCoverage } from '../../engine/analyticsApi';

interface ScannerDrawerProps {
  onClose: () => void;
}

export function ScannerDrawer({ onClose }: ScannerDrawerProps) {
  const { data: coverage } = useQuery({
    queryKey: ['agent', 'scanner-coverage-detail'],
    queryFn: () => fetchScannerCoverage(30),
    staleTime: 60_000,
  });

  return (
    <DrawerShell title="Scanner Coverage" onClose={onClose}>
      <div className="space-y-4">
        {(coverage?.per_scanner || []).map((scanner) => (
          <div key={scanner.name} className="flex items-center justify-between py-2 border-b border-slate-800">
            <div>
              <div className="text-sm text-slate-200">{scanner.name.replace(/^scan_/, '').replace(/_/g, ' ')}</div>
              {scanner.finding_count > 0 && (
                <div className="text-xs text-slate-500">
                  Found {scanner.finding_count} issues ({scanner.actionable_count} actionable)
                  {scanner.noise_pct > 0 && ` · ${scanner.noise_pct}% noise`}
                </div>
              )}
              {scanner.finding_count === 0 && (
                <div className="text-xs text-slate-600">No findings yet</div>
              )}
            </div>
            <div className={cn('w-2 h-2 rounded-full', scanner.enabled ? 'bg-emerald-400' : 'bg-slate-600')} />
          </div>
        ))}
      </div>
    </DrawerShell>
  );
}

// DrawerShell defined here or imported from shared
function DrawerShell({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  return (
    <div className="fixed inset-0 z-50 flex justify-end" onClick={onClose} onKeyDown={(e) => { if (e.key === 'Escape') onClose(); }} role="dialog" aria-modal="true" aria-label={title}>
      <div className="absolute inset-0 bg-black/50" />
      <div className="relative w-full max-w-2xl bg-slate-950 border-l border-slate-800 h-full overflow-auto shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="sticky top-0 z-10 bg-slate-950 border-b border-slate-800 px-5 py-4 flex items-center justify-between">
          <h2 className="text-base font-semibold text-slate-100">{title}</h2>
          <button onClick={onClose} className="text-slate-500 hover:text-slate-300"><X className="w-5 h-5" /></button>
        </div>
        <div className="p-5">{children}</div>
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Create EvalDrawer and MemoryDrawer**

Create `src/kubeview/views/mission-control/EvalDrawer.tsx` — renders full eval suite breakdown (reuse existing eval display logic from old AgentSettingsView EvalsTab):

```tsx
import { X } from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import { fetchAgentEvalStatus } from '../../engine/evalStatus';

export function EvalDrawer({ onClose }: { onClose: () => void }) {
  const { data: evalStatus } = useQuery({
    queryKey: ['agent', 'eval-status'],
    queryFn: () => fetchAgentEvalStatus().catch(() => null),
    refetchInterval: 60_000,
  });

  return (
    <DrawerShell title="Quality Gate Details" onClose={onClose}>
      {/* Render suite cards: release, safety, integration, view_designer */}
      {/* Reuse existing EvalsTab rendering logic from AgentSettingsView */}
      <div className="space-y-4 text-sm text-slate-300">
        {evalStatus ? (
          <>
            {['release', 'safety', 'integration', 'view_designer'].map((suite) => {
              const s = (evalStatus as any)[suite];
              if (!s) return null;
              return (
                <div key={suite} className="bg-slate-900 rounded-lg border border-slate-800 p-4">
                  <div className="flex items-center justify-between mb-2">
                    <h3 className="font-medium text-slate-200 capitalize">{suite.replace('_', ' ')}</h3>
                    <span className={s.gate_passed ? 'text-emerald-400' : 'text-red-400'}>
                      {s.gate_passed ? 'PASS' : 'FAIL'}
                    </span>
                  </div>
                  <div className="text-xs text-slate-400">
                    {s.scenario_count} scenarios · avg {Math.round((s.average_overall || 0) * 100)}%
                  </div>
                </div>
              );
            })}
          </>
        ) : (
          <div className="text-slate-500">Loading eval data...</div>
        )}
      </div>
    </DrawerShell>
  );
}

// Same DrawerShell as ScannerDrawer
function DrawerShell({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  return (
    <div className="fixed inset-0 z-50 flex justify-end" onClick={onClose} onKeyDown={(e) => { if (e.key === 'Escape') onClose(); }} role="dialog" aria-modal="true" aria-label={title}>
      <div className="absolute inset-0 bg-black/50" />
      <div className="relative w-full max-w-2xl bg-slate-950 border-l border-slate-800 h-full overflow-auto shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="sticky top-0 z-10 bg-slate-950 border-b border-slate-800 px-5 py-4 flex items-center justify-between">
          <h2 className="text-base font-semibold text-slate-100">{title}</h2>
          <button onClick={onClose} className="text-slate-500 hover:text-slate-300"><X className="w-5 h-5" /></button>
        </div>
        <div className="p-5">{children}</div>
      </div>
    </div>
  );
}
```

Create `src/kubeview/views/mission-control/MemoryDrawer.tsx`:

```tsx
import { Suspense, lazy, X } from 'react';

const MemoryView = lazy(() => import('../MemoryView'));

export function MemoryDrawer({ onClose }: { onClose: () => void }) {
  return (
    <DrawerShell title="Agent Memory" onClose={onClose}>
      <Suspense fallback={<div className="text-sm text-slate-500">Loading memory...</div>}>
        <MemoryView embedded />
      </Suspense>
    </DrawerShell>
  );
}

function DrawerShell({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  return (
    <div className="fixed inset-0 z-50 flex justify-end" onClick={onClose} onKeyDown={(e) => { if (e.key === 'Escape') onClose(); }} role="dialog" aria-modal="true" aria-label={title}>
      <div className="absolute inset-0 bg-black/50" />
      <div className="relative w-full max-w-2xl bg-slate-950 border-l border-slate-800 h-full overflow-auto shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="sticky top-0 z-10 bg-slate-950 border-b border-slate-800 px-5 py-4 flex items-center justify-between">
          <h2 className="text-base font-semibold text-slate-100">{title}</h2>
          <button onClick={onClose} className="text-slate-500 hover:text-slate-300"><X className="w-5 h-5" /></button>
        </div>
        <div className="p-5">{children}</div>
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Wire drawers into MissionControlView**

Add at the bottom of MissionControlView's JSX, before the closing `</div>`:

```tsx
import { ScannerDrawer } from './mission-control/ScannerDrawer';
import { EvalDrawer } from './mission-control/EvalDrawer';
import { MemoryDrawer } from './mission-control/MemoryDrawer';

// At end of JSX:
{drawerOpen === 'scanner' && <ScannerDrawer onClose={() => setDrawerOpen(null)} />}
{drawerOpen === 'eval' && <EvalDrawer onClose={() => setDrawerOpen(null)} />}
{drawerOpen === 'memory' && <MemoryDrawer onClose={() => setDrawerOpen(null)} />}
```

- [ ] **Step 5: Verify in browser**

Click Quality card → Eval drawer slides in. Click Coverage card → Scanner drawer. Click memory link → Memory drawer. Escape or click backdrop closes.

- [ ] **Step 6: Commit**

```bash
cd /Users/amobrem/ali/OpenshiftPulse
git add src/kubeview/views/mission-control/ScannerDrawer.tsx src/kubeview/views/mission-control/EvalDrawer.tsx src/kubeview/views/mission-control/MemoryDrawer.tsx src/kubeview/views/MissionControlView.tsx
git commit -m "feat: Mission Control detail drawers (scanner, eval, memory)"
```

---

### Task 8: Pulse page — agent indicators and monitoring controls

**Files:**
- Modify: `src/kubeview/views/PulseView.tsx`
- Modify: `src/kubeview/store/monitorStore.ts`
- Modify: `src/kubeview/engine/monitorClient.ts`

- [ ] **Step 1: Add skill_activity event to MonitorClient**

In `src/kubeview/engine/monitorClient.ts`, add to the MonitorEvent type:

```typescript
| ({ type: 'skill_activity' } & { skill_name: string; status: string; timestamp: number; handoff_from?: string; handoff_to?: string })
```

- [ ] **Step 2: Handle skill_activity in monitorStore**

In `src/kubeview/store/monitorStore.ts`, add state fields and handler:

```typescript
// Add to interface:
activeSkill: string | null;
skillHandoffs: Array<{ from: string; to: string; timestamp: number }>;

// Add to initial state:
activeSkill: null,
skillHandoffs: [],

// Add to event handler switch:
case 'skill_activity':
  set({
    activeSkill: event.skill_name,
    ...(event.handoff_from && event.handoff_to ? {
      skillHandoffs: [...get().skillHandoffs.slice(-19), { from: event.handoff_from, to: event.handoff_to, timestamp: event.timestamp }],
    } : {}),
  });
  break;
```

- [ ] **Step 3: Add agent status indicator to Pulse header**

In `src/kubeview/views/PulseView.tsx`, add an agent status pill in the header stat area:

```tsx
import { useMonitorStore } from '../store/monitorStore';
import { useNavigate } from 'react-router-dom';
import { useTrustStore } from '../store/trustStore';

// Inside PulseView, add:
const connected = useMonitorStore((s) => s.connected);
const activeSkill = useMonitorStore((s) => s.activeSkill);
const trustLevel = useTrustStore((s) => s.trustLevel);
const monitorEnabled = useMonitorStore((s) => s.monitorEnabled);
const setMonitorEnabled = useMonitorStore((s) => s.setMonitorEnabled);
const triggerScan = useMonitorStore((s) => s.triggerScan);
const navigate = useNavigate();

// In the header area, add agent status pill:
<button
  onClick={() => navigate('/agent')}
  className="flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-slate-800 text-xs text-slate-300 hover:bg-slate-700 transition-colors"
>
  <div className={cn('w-1.5 h-1.5 rounded-full', connected ? 'bg-emerald-400' : 'bg-slate-600')} />
  Agent · Trust {trustLevel}
  {activeSkill && <span className="text-violet-400">· {activeSkill}</span>}
</button>
```

- [ ] **Step 4: Add monitoring controls to Pulse**

Add Scan Now button and monitor toggle near the agent status:

```tsx
{/* Monitor controls — moved from Agent Settings */}
<div className="flex items-center gap-2">
  <button
    onClick={triggerScan}
    disabled={!connected}
    className="px-2.5 py-1 rounded bg-violet-500/10 text-xs text-violet-400 hover:bg-violet-500/20 disabled:opacity-40"
  >
    Scan Now
  </button>
  <button
    onClick={() => setMonitorEnabled(!monitorEnabled)}
    className={cn('px-2.5 py-1 rounded text-xs', monitorEnabled ? 'bg-emerald-500/10 text-emerald-400' : 'bg-slate-800 text-slate-500')}
  >
    {monitorEnabled ? 'Monitoring On' : 'Monitoring Off'}
  </button>
</div>
```

- [ ] **Step 5: Verify in browser**

Navigate to `/pulse`. Agent status pill should show connection status and trust level. Scan Now and monitor toggle should work.

- [ ] **Step 6: Commit**

```bash
cd /Users/amobrem/ali/OpenshiftPulse
git add src/kubeview/views/PulseView.tsx src/kubeview/store/monitorStore.ts src/kubeview/engine/monitorClient.ts
git commit -m "feat: Pulse agent status indicator, monitoring controls, skill activity"
```

---

### Task 9: Toolbox Analytics tab expansion

**Files:**
- Modify: `src/kubeview/views/ToolboxView.tsx`

- [ ] **Step 1: Add intelligence analytics to Analytics tab**

In the AnalyticsTab function inside `ToolboxView.tsx`, add queries and display for the new intelligence sections:

```tsx
import { fetchIntelligenceSections, fetchPromptStats } from '../engine/analyticsApi';

// Inside AnalyticsTab, add queries:
const { data: intelligence } = useQuery({
  queryKey: ['analytics', 'intelligence'],
  queryFn: () => fetchIntelligenceSections().catch(() => null),
  staleTime: 60_000,
});

const { data: promptAnalytics } = useQuery({
  queryKey: ['analytics', 'prompt'],
  queryFn: () => fetchPromptStats().catch(() => null),
  staleTime: 60_000,
});

// Add sections after existing analytics content:

{/* Harness Effectiveness */}
{intelligence?.harness_effectiveness && (
  <Card>
    <CardHeader title="Harness Effectiveness" icon={<Target className="w-4 h-4 text-blue-400" />} />
    <CardBody>
      <div className="text-sm text-slate-200">
        Tool selection accuracy: <span className="font-semibold">{(intelligence.harness_effectiveness.accuracy * 100).toFixed(0)}%</span>
      </div>
      {intelligence.harness_effectiveness.wasted.length > 0 && (
        <div className="mt-2 space-y-1">
          <div className="text-xs text-slate-400">Wasted tools (offered frequently, rarely used):</div>
          {intelligence.harness_effectiveness.wasted.map((w) => (
            <div key={w.tool} className="text-xs text-amber-400/80">
              {w.tool}: offered {w.offered}×, used {w.used}×
            </div>
          ))}
        </div>
      )}
    </CardBody>
  </Card>
)}

{/* Prompt Breakdown */}
{promptAnalytics?.stats && (
  <Card>
    <CardHeader title="Prompt Analytics" icon={<FileText className="w-4 h-4 text-green-400" />} />
    <CardBody>
      <div className="text-sm text-slate-200">
        Avg tokens: {promptAnalytics.stats.avg_tokens.toLocaleString()} · Cache hit rate: {(promptAnalytics.stats.cache_hit_rate * 100).toFixed(0)}%
      </div>
      {Object.keys(promptAnalytics.stats.section_avg).length > 0 && (
        <div className="mt-2 space-y-1">
          {Object.entries(promptAnalytics.stats.section_avg).map(([section, avg]) => (
            <div key={section} className="flex items-center gap-2 text-xs">
              <span className="w-24 text-slate-500 truncate">{section}</span>
              <div className="flex-1 h-1.5 bg-slate-800 rounded-full overflow-hidden">
                <div className="h-full bg-green-500/60 rounded-full" style={{ width: `${Math.min((avg as number) / 100, 100)}%` }} />
              </div>
              <span className="w-12 text-right text-slate-500">{(avg as number).toLocaleString()}</span>
            </div>
          ))}
        </div>
      )}
    </CardBody>
  </Card>
)}

{/* PromQL Reliability */}
{intelligence?.query_reliability && (
  <Card>
    <CardHeader title="PromQL Reliability" icon={<Database className="w-4 h-4 text-cyan-400" />} />
    <CardBody>
      <div className="text-xs text-slate-400 mb-2">
        {intelligence.query_reliability.preferred.length} reliable · {intelligence.query_reliability.unreliable.length} unreliable
      </div>
      {intelligence.query_reliability.unreliable.map((q) => (
        <div key={q.query} className="text-xs text-red-400/80 truncate">
          {q.query} — {(q.success_rate * 100).toFixed(0)}% success ({q.total} calls)
        </div>
      ))}
    </CardBody>
  </Card>
)}

{/* Token Trending + Routing Accuracy */}
{intelligence?.token_trending && (
  <Card>
    <CardHeader title="Token Trending (week-over-week)" icon={<TrendingUp className="w-4 h-4 text-amber-400" />} />
    <CardBody>
      <div className="grid grid-cols-3 gap-4 text-center text-xs">
        <div>
          <div className="text-slate-400">Input</div>
          <div className={intelligence.token_trending.input_delta_pct > 0 ? 'text-red-400' : 'text-emerald-400'}>
            {intelligence.token_trending.input_delta_pct > 0 ? '+' : ''}{intelligence.token_trending.input_delta_pct.toFixed(1)}%
          </div>
        </div>
        <div>
          <div className="text-slate-400">Output</div>
          <div className={intelligence.token_trending.output_delta_pct > 0 ? 'text-red-400' : 'text-emerald-400'}>
            {intelligence.token_trending.output_delta_pct > 0 ? '+' : ''}{intelligence.token_trending.output_delta_pct.toFixed(1)}%
          </div>
        </div>
        <div>
          <div className="text-slate-400">Cache</div>
          <div className={intelligence.token_trending.cache_delta_pct > 0 ? 'text-emerald-400' : 'text-red-400'}>
            {intelligence.token_trending.cache_delta_pct > 0 ? '+' : ''}{intelligence.token_trending.cache_delta_pct.toFixed(1)}%
          </div>
        </div>
      </div>
    </CardBody>
  </Card>
)}
```

- [ ] **Step 2: Verify in browser**

Navigate to `/toolbox?tab=analytics`. New sections should render below existing analytics.

- [ ] **Step 3: Commit**

```bash
cd /Users/amobrem/ali/OpenshiftPulse
git add src/kubeview/views/ToolboxView.tsx
git commit -m "feat: Toolbox Analytics tab expansion with intelligence + prompt analytics"
```

---

### Task 10: Welcome page → onboarding-only + default redirect

**Files:**
- Modify: `src/kubeview/views/WelcomeView.tsx`
- Modify: `src/kubeview/App.tsx`

- [ ] **Step 1: Add onboarding completion flag**

In `WelcomeView.tsx`, add a localStorage check and redirect:

```tsx
import { useNavigate } from 'react-router-dom';
import { useEffect } from 'react';

const ONBOARDING_COMPLETE_KEY = 'openshiftpulse-onboarding-complete';

// At the top of WelcomeView:
const navigate = useNavigate();
const onboardingComplete = localStorage.getItem(ONBOARDING_COMPLETE_KEY) === 'true';

// If onboarding is complete and user navigates to /welcome, show a "re-run onboarding?" banner
// but don't auto-redirect (they may have navigated here intentionally via command palette)
```

- [ ] **Step 2: Update default redirect**

In `src/kubeview/App.tsx`, change the default redirect:

```tsx
// Change:
// <Route index element={<Navigate to="/welcome" replace />} />
// To:
<Route index element={<Navigate to={localStorage.getItem('openshiftpulse-onboarding-complete') === 'true' ? '/pulse' : '/welcome'} replace />} />
```

Note: This is a simplified approach. For a cleaner solution, create a small `<DefaultRedirect />` component that reads localStorage and redirects accordingly.

- [ ] **Step 3: Verify navigation**

- New user (no localStorage): `/` → `/welcome`
- Returning user: `/` → `/pulse`
- Command palette: "Onboarding" still navigates to `/welcome`

- [ ] **Step 4: Commit**

```bash
cd /Users/amobrem/ali/OpenshiftPulse
git add src/kubeview/views/WelcomeView.tsx src/kubeview/App.tsx
git commit -m "feat: Welcome page becomes onboarding-only, default redirect to Pulse"
```

---

### Task 11: Incidents page — action reasoning

**Files:**
- Modify: `src/kubeview/views/IncidentCenterView.tsx` (or the relevant sub-component for action/history items)

- [ ] **Step 1: Add reasoning text to action items**

Find the component that renders individual actions in the History and Actions tabs. Each action item should display a "why" line using the `reasoning` field from the action data:

```tsx
{/* After the action title/description, add: */}
{action.reasoning && (
  <p className="text-xs text-slate-500 mt-1 italic">
    {action.reasoning}
  </p>
)}
```

If the `reasoning` field doesn't exist in the current action data type, add it to the TypeScript interface and ensure the backend's fix-history endpoint returns it (it's already stored in the `actions` table).

- [ ] **Step 2: Verify in browser**

Navigate to `/incidents?tab=history`. Actions should show reasoning text below each entry.

- [ ] **Step 3: Commit**

```bash
cd /Users/amobrem/ali/OpenshiftPulse
git add src/kubeview/views/IncidentCenterView.tsx
git commit -m "feat: Incidents page shows action reasoning text"
```

---

### Task 12: Clean up old AgentSettingsView + update redirects

**Files:**
- Modify: `src/kubeview/views/AgentSettingsView.tsx`

- [ ] **Step 1: Replace AgentSettingsView with redirect**

Replace the entire content of `AgentSettingsView.tsx` with a redirect to MissionControlView. This preserves backward compatibility for any direct links or bookmarks:

```tsx
import MissionControlView from './MissionControlView';

/** @deprecated Use MissionControlView. Kept for backward compatibility. */
export default function AgentSettingsView() {
  return <MissionControlView />;
}
```

Or if the route was already updated in Task 2, this file can be deleted and any remaining imports updated.

- [ ] **Step 2: Update legacy redirects**

Ensure these legacy routes still work:
- `/memory` → `/agent` (memory drawer opens automatically via URL param)
- `/agent?tab=views` → redirect to `/custom` or equivalent

- [ ] **Step 3: Commit**

```bash
cd /Users/amobrem/ali/OpenshiftPulse
git add src/kubeview/views/AgentSettingsView.tsx
git commit -m "refactor: replace AgentSettingsView with MissionControlView redirect"
```

---

### Task 13: Tests for Mission Control

**Files:**
- Create: `src/kubeview/views/__tests__/MissionControlView.test.tsx`

- [ ] **Step 1: Write component tests**

Create `src/kubeview/views/__tests__/MissionControlView.test.tsx`:

```tsx
// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

vi.mock('../../store/trustStore', () => ({
  useTrustStore: Object.assign(
    (selector: any) => selector({
      trustLevel: 2,
      autoFixCategories: ['crashloop'],
      communicationStyle: 'detailed',
      setTrustLevel: vi.fn(),
      setAutoFixCategories: vi.fn(),
      setCommunicationStyle: vi.fn(),
    }),
    { getState: () => ({ trustLevel: 2 }) },
  ),
  TRUST_LABELS: { 0: 'Monitor Only', 1: 'Suggest', 2: 'Ask First', 3: 'Auto-fix Safe', 4: 'Full Auto' },
  TRUST_DESCRIPTIONS: { 0: '', 1: '', 2: '', 3: '', 4: '' },
}));

vi.mock('../../store/monitorStore', () => ({
  useMonitorStore: Object.assign(
    (selector: any) => selector({ connected: true, findings: [] }),
    { getState: () => ({ findings: [] }) },
  ),
}));

vi.mock('@/lib/utils', () => ({ cn: (...args: any[]) => args.filter(Boolean).join(' ') }));

// Mock all analytics API calls
vi.mock('../../engine/analyticsApi', () => ({
  fetchFixHistorySummary: vi.fn().mockResolvedValue({ total_actions: 10, completed: 8, failed: 1, rolled_back: 1, success_rate: 0.8, rollback_rate: 0.1, avg_resolution_ms: 120000, by_category: [], trend: { current_week: 10, previous_week: 7, delta: 3 } }),
  fetchScannerCoverage: vi.fn().mockResolvedValue({ active_scanners: 12, total_scanners: 17, coverage_pct: 78, categories: [], per_scanner: [] }),
  fetchConfidenceCalibration: vi.fn().mockResolvedValue({ accuracy_pct: 92, rating: 'good', brier_score: 0.08, total_predictions: 45, buckets: [] }),
  fetchAccuracyStats: vi.fn().mockResolvedValue({ avg_quality_score: 0.82, quality_trend: { current: 0.82, previous: 0.74, delta: 0.08 }, anti_patterns: [], learning: { total_runbooks: 5, new_this_month: 1, runbook_success_rate: 0.9, total_patterns: 3, pattern_types: {} }, override_rate: { overrides: 1, total_proposed: 10, rate: 0.1 }, dimensions: {} }),
  fetchCostStats: vi.fn().mockResolvedValue({ avg_tokens_per_incident: 12000, trend: { current: 12000, previous: 14000, delta_pct: -14.3 }, by_mode: [], total_tokens: 0, total_incidents: 0 }),
  fetchRecommendations: vi.fn().mockResolvedValue({ recommendations: [] }),
  fetchReadinessSummary: vi.fn().mockResolvedValue({ total_gates: 30, passed: 28, failed: 1, attention: 1, pass_rate: 0.93, attention_items: [] }),
}));

vi.mock('../../engine/evalStatus', () => ({
  fetchAgentEvalStatus: vi.fn().mockResolvedValue({ quality_gate_passed: true, release: { average_overall: 0.85, dimension_averages: { safety: 0.9, relevance: 0.8 }, blocker_counts: { policy_violation: 0, hallucinated_tool: 0 }, gate_passed: true, scenario_count: 20 } }),
}));

function createQueryClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
}

async function renderView() {
  const MissionControlView = (await import('../MissionControlView')).default;
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={['/agent']}>
        <MissionControlView />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('MissionControlView', () => {
  afterEach(cleanup);

  it('renders page header', async () => {
    await renderView();
    expect(screen.getByText('Mission Control')).toBeDefined();
  });

  it('renders trust level selector', async () => {
    await renderView();
    expect(screen.getByText('Trust Level')).toBeDefined();
    expect(screen.getByText('Monitor Only')).toBeDefined();
    expect(screen.getByText('Full Auto')).toBeDefined();
  });

  it('renders agent health section', async () => {
    await renderView();
    expect(screen.getByText('Agent Health')).toBeDefined();
    expect(screen.getByText('Quality Gate')).toBeDefined();
    expect(screen.getByText('Coverage')).toBeDefined();
    expect(screen.getByText('Outcomes')).toBeDefined();
  });

  it('renders agent accuracy section', async () => {
    await renderView();
    expect(screen.getByText('Agent Accuracy')).toBeDefined();
  });
});
```

- [ ] **Step 2: Run tests**

Run: `cd /Users/amobrem/ali/OpenshiftPulse && npx vitest run src/kubeview/views/__tests__/MissionControlView.test.tsx 2>&1 | tail -15`
Expected: PASS

- [ ] **Step 3: Run full test suite**

Run: `cd /Users/amobrem/ali/OpenshiftPulse && npx vitest run 2>&1 | tail -20`
Expected: All tests pass

- [ ] **Step 4: Commit**

```bash
cd /Users/amobrem/ali/OpenshiftPulse
git add src/kubeview/views/__tests__/MissionControlView.test.tsx
git commit -m "test: Mission Control view tests"
```
