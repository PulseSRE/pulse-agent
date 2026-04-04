# Tool Usage Tracking — Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build UI for browsing agents, tools, and usage history — a new `/tools` page plus a summary tab on Agent Settings.

**Architecture:** New Zustand store (`toolUsageStore`) fetches from `/api/agent/tools`, `/api/agent/agents`, `/api/agent/tools/usage`, and `/api/agent/tools/usage/stats`. New `ToolsView` page with tabs for catalog, audit log, and stats. Add "Tools" tab to `AgentSettingsView` with summary cards and link to full page.

**Tech Stack:** React 19, TypeScript, Zustand 5, TanStack React Query 5, Tailwind CSS, Lucide icons

**Working directory:** `/Users/amobrem/ali/OpenshiftPulse`

---

## File Structure

| File | Responsibility |
|------|---------------|
| `src/kubeview/store/toolUsageStore.ts` (create) | Zustand store for tool/agent data, API fetching |
| `src/kubeview/views/ToolsView.tsx` (create) | Main `/tools` page with tabs: Catalog, Usage, Stats |
| `src/kubeview/views/AgentSettingsView.tsx` (modify) | Add "Tools" tab with summary cards |
| `src/kubeview/routes/domainRoutes.tsx` (modify) | Register `/tools` route |
| `src/kubeview/engine/navRegistry.ts` (modify) | Add "Tools & Agents" nav entry |

---

### Task 1: Create toolUsageStore

**Files:**
- Create: `src/kubeview/store/toolUsageStore.ts`

- [ ] **Step 1: Create the store file**

Create `/Users/amobrem/ali/OpenshiftPulse/src/kubeview/store/toolUsageStore.ts`:

```typescript
/**
 * Tool Usage Store — fetches tool catalog, agent metadata, and usage analytics
 * from the Pulse Agent backend API.
 */

import { create } from 'zustand';

const AGENT_BASE = '/api/agent';

export interface ToolInfo {
  name: string;
  description: string;
  requires_confirmation: boolean;
  category: string | null;
}

export interface AgentInfo {
  name: string;
  description: string;
  tools_count: number;
  has_write_tools: boolean;
  categories: string[];
}

export interface ToolUsageEntry {
  id: number;
  timestamp: string;
  session_id: string;
  turn_number: number;
  agent_mode: string;
  tool_name: string;
  tool_category: string | null;
  input_summary: Record<string, unknown> | null;
  status: string;
  error_message: string | null;
  error_category: string | null;
  duration_ms: number;
  result_bytes: number;
  requires_confirmation: boolean;
  was_confirmed: boolean | null;
  query_summary: string | null;
}

export interface ToolStat {
  tool_name: string;
  count: number;
  error_count: number;
  avg_duration_ms: number;
  avg_result_bytes: number;
}

export interface UsageStats {
  total_calls: number;
  unique_tools_used: number;
  error_rate: number;
  avg_duration_ms: number;
  avg_result_bytes: number;
  by_tool: ToolStat[];
  by_mode: Array<{ mode: string; count: number }>;
  by_category: Array<{ category: string; count: number }>;
  by_status: Record<string, number>;
}

export interface UsageFilters {
  tool_name?: string;
  agent_mode?: string;
  status?: string;
  session_id?: string;
  from?: string;
  to?: string;
  page: number;
  per_page: number;
}

interface ToolUsageState {
  // Data
  tools: { sre: ToolInfo[]; security: ToolInfo[]; write_tools: string[] } | null;
  agents: AgentInfo[];
  usage: { entries: ToolUsageEntry[]; total: number; page: number; per_page: number } | null;
  stats: UsageStats | null;

  // Loading states
  toolsLoading: boolean;
  agentsLoading: boolean;
  usageLoading: boolean;
  statsLoading: boolean;

  // Filters
  filters: UsageFilters;

  // Actions
  loadTools: () => Promise<void>;
  loadAgents: () => Promise<void>;
  loadUsage: (filters?: Partial<UsageFilters>) => Promise<void>;
  loadStats: (from?: string, to?: string) => Promise<void>;
  setFilters: (filters: Partial<UsageFilters>) => void;
}

async function apiFetch<T>(path: string): Promise<T | null> {
  try {
    const res = await fetch(`${AGENT_BASE}${path}`);
    if (!res.ok) return null;
    return res.json();
  } catch {
    return null;
  }
}

export const useToolUsageStore = create<ToolUsageState>()((set, get) => ({
  tools: null,
  agents: [],
  usage: null,
  stats: null,
  toolsLoading: false,
  agentsLoading: false,
  usageLoading: false,
  statsLoading: false,
  filters: { page: 1, per_page: 50 },

  loadTools: async () => {
    set({ toolsLoading: true });
    const data = await apiFetch<{ sre: ToolInfo[]; security: ToolInfo[]; write_tools: string[] }>('/tools');
    set({ tools: data, toolsLoading: false });
  },

  loadAgents: async () => {
    set({ agentsLoading: true });
    const data = await apiFetch<AgentInfo[]>('/agents');
    set({ agents: data || [], agentsLoading: false });
  },

  loadUsage: async (overrides) => {
    const filters = { ...get().filters, ...overrides };
    set({ usageLoading: true, filters });

    const params = new URLSearchParams();
    if (filters.tool_name) params.set('tool_name', filters.tool_name);
    if (filters.agent_mode) params.set('agent_mode', filters.agent_mode);
    if (filters.status) params.set('status', filters.status);
    if (filters.session_id) params.set('session_id', filters.session_id);
    if (filters.from) params.set('from', filters.from);
    if (filters.to) params.set('to', filters.to);
    params.set('page', String(filters.page));
    params.set('per_page', String(filters.per_page));

    const data = await apiFetch<{ entries: ToolUsageEntry[]; total: number; page: number; per_page: number }>(
      `/tools/usage?${params}`
    );
    set({ usage: data, usageLoading: false });
  },

  loadStats: async (from, to) => {
    set({ statsLoading: true });
    const params = new URLSearchParams();
    if (from) params.set('from', from);
    if (to) params.set('to', to);
    const qs = params.toString();
    const data = await apiFetch<UsageStats>(`/tools/usage/stats${qs ? `?${qs}` : ''}`);
    set({ stats: data, statsLoading: false });
  },

  setFilters: (partial) => {
    const filters = { ...get().filters, ...partial };
    set({ filters });
  },
}));
```

- [ ] **Step 2: Verify it compiles**

Run: `cd /Users/amobrem/ali/OpenshiftPulse && npx tsc --noEmit src/kubeview/store/toolUsageStore.ts 2>&1 | head -20`

If there are import path issues, fix them. The project uses path aliases — `@/` maps to `src/`.

- [ ] **Step 3: Commit**

```bash
cd /Users/amobrem/ali/OpenshiftPulse
git add src/kubeview/store/toolUsageStore.ts
git commit -m "feat: add toolUsageStore for tool/agent data fetching"
```

---

### Task 2: Register nav entry and route

**Files:**
- Modify: `src/kubeview/engine/navRegistry.ts`
- Modify: `src/kubeview/routes/domainRoutes.tsx`

- [ ] **Step 1: Add nav item to navRegistry.ts**

In `/Users/amobrem/ali/OpenshiftPulse/src/kubeview/engine/navRegistry.ts`, add a new entry to `NAV_ITEMS` array after the existing `agent` entry (line 36):

```typescript
  { id: 'tools', label: 'Tools & Agents', icon: 'Wrench', path: '/tools', group: 'agent', subtitle: 'Tool catalog, usage analytics, agent modes', color: 'text-fuchsia-400' },
```

- [ ] **Step 2: Add lazy import to domainRoutes.tsx**

In `/Users/amobrem/ali/OpenshiftPulse/src/kubeview/routes/domainRoutes.tsx`, add a lazy import at the top with the other lazy imports:

```typescript
const ToolsView = lazy(() => import('../views/ToolsView'));
```

- [ ] **Step 3: Add route to domainRoutes function**

In the `domainRoutes()` function, add after the `agent` route:

```typescript
      <Route path="tools" element={<Lazy><ToolsView /></Lazy>} />
```

- [ ] **Step 4: Create placeholder ToolsView**

Create `/Users/amobrem/ali/OpenshiftPulse/src/kubeview/views/ToolsView.tsx`:

```tsx
import { Wrench } from 'lucide-react';

export default function ToolsView() {
  return (
    <div className="h-full overflow-auto bg-slate-950 p-6">
      <div className="max-w-6xl mx-auto space-y-6">
        <div>
          <h1 className="text-2xl font-bold text-slate-100 flex items-center gap-2">
            <Wrench className="w-6 h-6 text-fuchsia-400" />
            Tools & Agents
          </h1>
          <p className="text-sm text-slate-400 mt-1">Tool catalog, usage analytics, and agent modes</p>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 5: Verify it compiles and route works**

Run: `cd /Users/amobrem/ali/OpenshiftPulse && npx tsc --noEmit 2>&1 | head -20`

- [ ] **Step 6: Commit**

```bash
cd /Users/amobrem/ali/OpenshiftPulse
git add src/kubeview/engine/navRegistry.ts src/kubeview/routes/domainRoutes.tsx src/kubeview/views/ToolsView.tsx
git commit -m "feat: register /tools route and nav entry"
```

---

### Task 3: Build ToolsView with tabs — Catalog, Usage, Stats

**Files:**
- Modify: `src/kubeview/views/ToolsView.tsx`

- [ ] **Step 1: Implement the full ToolsView with three tabs**

Replace `/Users/amobrem/ali/OpenshiftPulse/src/kubeview/views/ToolsView.tsx`:

```tsx
import { useState, useEffect } from 'react';
import { useSearchParams } from 'react-router-dom';
import {
  Wrench, List, BarChart3, History, Search, ChevronLeft, ChevronRight,
  AlertTriangle, CheckCircle2, Clock, Database, Bot, Shield, Palette,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { useToolUsageStore } from '../store/toolUsageStore';
import type { ToolInfo, AgentInfo, ToolUsageEntry, ToolStat } from '../store/toolUsageStore';

type ToolsTab = 'catalog' | 'usage' | 'stats';

const MODE_ICONS: Record<string, React.ReactNode> = {
  sre: <Bot className="w-4 h-4 text-violet-400" />,
  security: <Shield className="w-4 h-4 text-red-400" />,
  view_designer: <Palette className="w-4 h-4 text-emerald-400" />,
};

export default function ToolsView() {
  const [searchParams, setSearchParams] = useSearchParams();
  const initialTab = (searchParams.get('tab') as ToolsTab) || 'catalog';
  const [activeTab, setActiveTabState] = useState<ToolsTab>(initialTab);

  const setActiveTab = (tab: ToolsTab) => {
    setActiveTabState(tab);
    const next = new URLSearchParams(searchParams);
    if (tab === 'catalog') next.delete('tab'); else next.set('tab', tab);
    setSearchParams(next, { replace: true });
  };

  const tabs: Array<{ id: ToolsTab; label: string; icon: React.ReactNode; activeIcon: React.ReactNode }> = [
    { id: 'catalog', label: 'Catalog', icon: <List className="w-3.5 h-3.5 text-fuchsia-400" />, activeIcon: <List className="w-3.5 h-3.5" /> },
    { id: 'usage', label: 'Usage Log', icon: <History className="w-3.5 h-3.5 text-amber-400" />, activeIcon: <History className="w-3.5 h-3.5" /> },
    { id: 'stats', label: 'Analytics', icon: <BarChart3 className="w-3.5 h-3.5 text-cyan-400" />, activeIcon: <BarChart3 className="w-3.5 h-3.5" /> },
  ];

  return (
    <div className="h-full overflow-auto bg-slate-950 p-6">
      <div className="max-w-6xl mx-auto space-y-6">
        <div>
          <h1 className="text-2xl font-bold text-slate-100 flex items-center gap-2">
            <Wrench className="w-6 h-6 text-fuchsia-400" />
            Tools & Agents
          </h1>
          <p className="text-sm text-slate-400 mt-1">Tool catalog, usage analytics, and agent modes</p>
        </div>

        <div className="flex gap-1 bg-slate-900 rounded-lg border border-slate-800 p-1" role="tablist" aria-label="Tools tabs">
          {tabs.map((t) => (
            <button
              key={t.id}
              role="tab"
              aria-selected={activeTab === t.id}
              tabIndex={activeTab === t.id ? 0 : -1}
              onClick={() => setActiveTab(t.id)}
              className={cn(
                'flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-md transition-colors whitespace-nowrap focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
                activeTab === t.id ? 'bg-blue-600 text-white' : 'text-slate-400 hover:text-slate-200',
              )}
            >
              {activeTab === t.id ? t.activeIcon : t.icon}{t.label}
            </button>
          ))}
        </div>

        {activeTab === 'catalog' && <CatalogTab />}
        {activeTab === 'usage' && <UsageTab />}
        {activeTab === 'stats' && <StatsTab />}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Catalog Tab                                                         */
/* ------------------------------------------------------------------ */

function CatalogTab() {
  const { tools, agents, toolsLoading, agentsLoading, loadTools, loadAgents } = useToolUsageStore();
  const [search, setSearch] = useState('');
  const [modeFilter, setModeFilter] = useState<string>('all');

  useEffect(() => { loadTools(); loadAgents(); }, [loadTools, loadAgents]);

  const allTools: Array<ToolInfo & { mode: string }> = [];
  if (tools) {
    for (const t of tools.sre) allTools.push({ ...t, mode: 'sre' });
    for (const t of tools.security) {
      if (!allTools.some((x) => x.name === t.name)) allTools.push({ ...t, mode: 'security' });
    }
  }

  const filtered = allTools.filter((t) => {
    if (modeFilter !== 'all' && t.mode !== modeFilter) return false;
    if (search && !t.name.toLowerCase().includes(search.toLowerCase()) && !t.description.toLowerCase().includes(search.toLowerCase())) return false;
    return true;
  });

  const categories = [...new Set(filtered.map((t) => t.category).filter(Boolean))] as string[];

  return (
    <div className="space-y-6">
      {/* Agents overview */}
      {!agentsLoading && agents.length > 0 && (
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          {agents.map((a) => (
            <div key={a.name} className="bg-slate-900 border border-slate-800 rounded-lg p-4 space-y-2">
              <div className="flex items-center gap-2">
                {MODE_ICONS[a.name] || <Bot className="w-4 h-4 text-slate-400" />}
                <span className="text-sm font-medium text-slate-100 capitalize">{a.name === 'view_designer' ? 'View Designer' : a.name.toUpperCase()}</span>
              </div>
              <p className="text-xs text-slate-400">{a.description}</p>
              <div className="flex items-center gap-3 text-xs text-slate-500">
                <span>{a.tools_count} tools</span>
                {a.has_write_tools && <span className="text-amber-400">write ops</span>}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Filters */}
      <div className="flex items-center gap-3">
        <div className="relative flex-1 max-w-xs">
          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-500" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search tools..."
            className="w-full pl-8 pr-3 py-1.5 text-xs bg-slate-900 border border-slate-700 rounded-md text-slate-200 placeholder:text-slate-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
          />
        </div>
        <select
          value={modeFilter}
          onChange={(e) => setModeFilter(e.target.value)}
          className="px-2 py-1.5 text-xs bg-slate-900 border border-slate-700 rounded-md text-slate-200 focus:outline-none focus:ring-1 focus:ring-blue-500"
        >
          <option value="all">All modes</option>
          <option value="sre">SRE</option>
          <option value="security">Security</option>
        </select>
        <span className="text-xs text-slate-500">{filtered.length} tools</span>
      </div>

      {/* Tool list by category */}
      {toolsLoading ? (
        <div className="flex justify-center py-12"><div className="kv-skeleton w-8 h-8 rounded-full" /></div>
      ) : (
        <div className="space-y-4">
          {categories.sort().map((cat) => (
            <div key={cat}>
              <h3 className="text-xs font-medium text-slate-400 uppercase tracking-wider mb-2">{cat}</h3>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
                {filtered.filter((t) => t.category === cat).map((t) => (
                  <div key={t.name} className="bg-slate-900/50 border border-slate-800/50 rounded-md px-3 py-2 space-y-0.5">
                    <div className="flex items-center gap-1.5">
                      <span className="text-xs font-mono text-slate-200">{t.name}</span>
                      {t.requires_confirmation && (
                        <span className="text-[10px] px-1 py-0.5 rounded bg-amber-900/30 text-amber-400 border border-amber-800/30">write</span>
                      )}
                    </div>
                    <p className="text-[11px] text-slate-500 line-clamp-1">{t.description}</p>
                  </div>
                ))}
              </div>
            </div>
          ))}
          {/* Uncategorized */}
          {filtered.filter((t) => !t.category).length > 0 && (
            <div>
              <h3 className="text-xs font-medium text-slate-400 uppercase tracking-wider mb-2">uncategorized</h3>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
                {filtered.filter((t) => !t.category).map((t) => (
                  <div key={t.name} className="bg-slate-900/50 border border-slate-800/50 rounded-md px-3 py-2 space-y-0.5">
                    <div className="flex items-center gap-1.5">
                      <span className="text-xs font-mono text-slate-200">{t.name}</span>
                      {t.requires_confirmation && (
                        <span className="text-[10px] px-1 py-0.5 rounded bg-amber-900/30 text-amber-400 border border-amber-800/30">write</span>
                      )}
                    </div>
                    <p className="text-[11px] text-slate-500 line-clamp-1">{t.description}</p>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Usage Log Tab                                                       */
/* ------------------------------------------------------------------ */

function UsageTab() {
  const { usage, usageLoading, filters, loadUsage, setFilters } = useToolUsageStore();
  const [toolFilter, setToolFilter] = useState(filters.tool_name || '');
  const [modeFilter, setModeFilter] = useState(filters.agent_mode || '');
  const [statusFilter, setStatusFilter] = useState(filters.status || '');

  useEffect(() => { loadUsage(); }, [loadUsage]);

  const applyFilters = () => {
    loadUsage({
      tool_name: toolFilter || undefined,
      agent_mode: modeFilter || undefined,
      status: statusFilter || undefined,
      page: 1,
    });
  };

  const totalPages = usage ? Math.ceil(usage.total / usage.per_page) : 0;

  return (
    <div className="space-y-4">
      {/* Filters row */}
      <div className="flex items-center gap-2 flex-wrap">
        <input
          value={toolFilter}
          onChange={(e) => setToolFilter(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && applyFilters()}
          placeholder="Tool name..."
          className="px-2 py-1.5 text-xs bg-slate-900 border border-slate-700 rounded-md text-slate-200 placeholder:text-slate-500 w-36 focus:outline-none focus:ring-1 focus:ring-blue-500"
        />
        <select
          value={modeFilter}
          onChange={(e) => { setModeFilter(e.target.value); loadUsage({ agent_mode: e.target.value || undefined, page: 1 }); }}
          className="px-2 py-1.5 text-xs bg-slate-900 border border-slate-700 rounded-md text-slate-200 focus:outline-none focus:ring-1 focus:ring-blue-500"
        >
          <option value="">All modes</option>
          <option value="sre">SRE</option>
          <option value="security">Security</option>
          <option value="view_designer">View Designer</option>
        </select>
        <select
          value={statusFilter}
          onChange={(e) => { setStatusFilter(e.target.value); loadUsage({ status: e.target.value || undefined, page: 1 }); }}
          className="px-2 py-1.5 text-xs bg-slate-900 border border-slate-700 rounded-md text-slate-200 focus:outline-none focus:ring-1 focus:ring-blue-500"
        >
          <option value="">All statuses</option>
          <option value="success">Success</option>
          <option value="error">Error</option>
          <option value="denied">Denied</option>
        </select>
        {usage && <span className="text-xs text-slate-500 ml-auto">{usage.total} total</span>}
      </div>

      {/* Table */}
      {usageLoading ? (
        <div className="flex justify-center py-12"><div className="kv-skeleton w-8 h-8 rounded-full" /></div>
      ) : usage && usage.entries.length > 0 ? (
        <div className="bg-slate-900 border border-slate-800 rounded-lg overflow-hidden">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-slate-800">
                <th className="text-left py-2 px-3 text-slate-400 font-medium">Time</th>
                <th className="text-left py-2 px-3 text-slate-400 font-medium">Tool</th>
                <th className="text-left py-2 px-3 text-slate-400 font-medium">Mode</th>
                <th className="text-left py-2 px-3 text-slate-400 font-medium">Status</th>
                <th className="text-right py-2 px-3 text-slate-400 font-medium">Duration</th>
                <th className="text-right py-2 px-3 text-slate-400 font-medium">Size</th>
              </tr>
            </thead>
            <tbody>
              {usage.entries.map((e) => (
                <UsageRow key={e.id} entry={e} />
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="text-center py-12 text-sm text-slate-500">No tool usage recorded yet</div>
      )}

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-center gap-2">
          <button
            disabled={filters.page <= 1}
            onClick={() => loadUsage({ page: filters.page - 1 })}
            className="p-1 rounded text-slate-400 hover:text-slate-200 disabled:opacity-30"
          >
            <ChevronLeft className="w-4 h-4" />
          </button>
          <span className="text-xs text-slate-400">
            Page {filters.page} of {totalPages}
          </span>
          <button
            disabled={filters.page >= totalPages}
            onClick={() => loadUsage({ page: filters.page + 1 })}
            className="p-1 rounded text-slate-400 hover:text-slate-200 disabled:opacity-30"
          >
            <ChevronRight className="w-4 h-4" />
          </button>
        </div>
      )}
    </div>
  );
}

function UsageRow({ entry: e }: { entry: ToolUsageEntry }) {
  const [expanded, setExpanded] = useState(false);
  const time = new Date(e.timestamp).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' });

  return (
    <>
      <tr
        className="border-b border-slate-800/50 hover:bg-slate-800/30 cursor-pointer"
        onClick={() => setExpanded(!expanded)}
      >
        <td className="py-1.5 px-3 text-slate-400">{time}</td>
        <td className="py-1.5 px-3 font-mono text-slate-200">{e.tool_name}</td>
        <td className="py-1.5 px-3 text-slate-400 capitalize">{e.agent_mode}</td>
        <td className="py-1.5 px-3">
          {e.status === 'success' ? (
            <span className="text-emerald-400 flex items-center gap-1"><CheckCircle2 className="w-3 h-3" /> ok</span>
          ) : e.status === 'denied' ? (
            <span className="text-amber-400 flex items-center gap-1"><AlertTriangle className="w-3 h-3" /> denied</span>
          ) : (
            <span className="text-red-400 flex items-center gap-1"><AlertTriangle className="w-3 h-3" /> error</span>
          )}
        </td>
        <td className="py-1.5 px-3 text-right text-slate-400">{e.duration_ms}ms</td>
        <td className="py-1.5 px-3 text-right text-slate-500">{e.result_bytes > 0 ? `${(e.result_bytes / 1024).toFixed(1)}KB` : '-'}</td>
      </tr>
      {expanded && (
        <tr className="border-b border-slate-800/50">
          <td colSpan={6} className="px-3 py-2 bg-slate-900/50">
            <div className="space-y-1 text-[11px]">
              {e.query_summary && <div><span className="text-slate-500">Query:</span> <span className="text-slate-300">{e.query_summary}</span></div>}
              {e.input_summary && <div><span className="text-slate-500">Input:</span> <code className="text-slate-400">{JSON.stringify(e.input_summary)}</code></div>}
              {e.error_message && <div><span className="text-slate-500">Error:</span> <span className="text-red-400">{e.error_message}</span></div>}
              <div className="text-slate-600">Session: {e.session_id} | Turn: {e.turn_number} | Category: {e.tool_category || 'none'}</div>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

/* ------------------------------------------------------------------ */
/* Stats Tab                                                           */
/* ------------------------------------------------------------------ */

function StatsTab() {
  const { stats, statsLoading, loadStats } = useToolUsageStore();

  useEffect(() => { loadStats(); }, [loadStats]);

  if (statsLoading) {
    return <div className="flex justify-center py-12"><div className="kv-skeleton w-8 h-8 rounded-full" /></div>;
  }

  if (!stats || stats.total_calls === 0) {
    return <div className="text-center py-12 text-sm text-slate-500">No usage data yet. Tool calls will appear here once the agent is used.</div>;
  }

  const maxCount = Math.max(...stats.by_tool.slice(0, 10).map((t) => t.count), 1);

  return (
    <div className="space-y-6">
      {/* Summary cards */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <StatCard label="Total Calls" value={stats.total_calls.toLocaleString()} icon={<Database className="w-4 h-4 text-blue-400" />} />
        <StatCard label="Unique Tools" value={String(stats.unique_tools_used)} icon={<Wrench className="w-4 h-4 text-fuchsia-400" />} />
        <StatCard label="Error Rate" value={`${(stats.error_rate * 100).toFixed(1)}%`} icon={<AlertTriangle className="w-4 h-4 text-red-400" />} />
        <StatCard label="Avg Duration" value={`${stats.avg_duration_ms}ms`} icon={<Clock className="w-4 h-4 text-emerald-400" />} />
      </div>

      {/* Top tools bar chart */}
      <div className="bg-slate-900 border border-slate-800 rounded-lg p-4">
        <h3 className="text-xs font-medium text-slate-300 mb-3">Top Tools</h3>
        <div className="space-y-1.5">
          {stats.by_tool.slice(0, 10).map((t) => (
            <div key={t.tool_name} className="flex items-center gap-2 text-xs">
              <span className="w-36 truncate font-mono text-slate-300">{t.tool_name}</span>
              <div className="flex-1 h-4 bg-slate-800 rounded-sm overflow-hidden">
                <div
                  className="h-full bg-blue-600/60 rounded-sm"
                  style={{ width: `${(t.count / maxCount) * 100}%` }}
                />
              </div>
              <span className="w-10 text-right text-slate-400">{t.count}</span>
              {t.error_count > 0 && <span className="text-red-400 text-[10px]">{t.error_count} err</span>}
            </div>
          ))}
        </div>
      </div>

      {/* By mode + by category */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div className="bg-slate-900 border border-slate-800 rounded-lg p-4">
          <h3 className="text-xs font-medium text-slate-300 mb-3">By Mode</h3>
          <div className="space-y-1">
            {stats.by_mode.map((m) => (
              <div key={m.mode} className="flex items-center justify-between text-xs">
                <span className="text-slate-300 capitalize flex items-center gap-1.5">
                  {MODE_ICONS[m.mode] || <Bot className="w-3 h-3 text-slate-500" />}
                  {m.mode}
                </span>
                <span className="text-slate-400">{m.count}</span>
              </div>
            ))}
          </div>
        </div>
        <div className="bg-slate-900 border border-slate-800 rounded-lg p-4">
          <h3 className="text-xs font-medium text-slate-300 mb-3">By Category</h3>
          <div className="space-y-1">
            {stats.by_category.map((c) => (
              <div key={c.category} className="flex items-center justify-between text-xs">
                <span className="text-slate-300">{c.category}</span>
                <span className="text-slate-400">{c.count}</span>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Context hogs */}
      <div className="bg-slate-900 border border-slate-800 rounded-lg p-4">
        <h3 className="text-xs font-medium text-slate-300 mb-3">Largest Results (Context Usage)</h3>
        <div className="space-y-1">
          {[...stats.by_tool].sort((a, b) => b.avg_result_bytes - a.avg_result_bytes).slice(0, 5).map((t) => (
            <div key={t.tool_name} className="flex items-center justify-between text-xs">
              <span className="font-mono text-slate-300">{t.tool_name}</span>
              <span className="text-slate-400">{(t.avg_result_bytes / 1024).toFixed(1)} KB avg</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function StatCard({ label, value, icon }: { label: string; value: string; icon: React.ReactNode }) {
  return (
    <div className="bg-slate-900 border border-slate-800 rounded-lg p-3">
      <div className="flex items-center gap-1.5 mb-1">{icon}<span className="text-[11px] text-slate-400">{label}</span></div>
      <div className="text-lg font-semibold text-slate-100">{value}</div>
    </div>
  );
}
```

- [ ] **Step 2: Verify it compiles**

Run: `cd /Users/amobrem/ali/OpenshiftPulse && npx tsc --noEmit 2>&1 | head -20`

- [ ] **Step 3: Commit**

```bash
cd /Users/amobrem/ali/OpenshiftPulse
git add src/kubeview/views/ToolsView.tsx
git commit -m "feat: build ToolsView with catalog, usage log, and analytics tabs"
```

---

### Task 4: Add "Tools" tab to AgentSettingsView

**Files:**
- Modify: `src/kubeview/views/AgentSettingsView.tsx`

- [ ] **Step 1: Add the Tools tab and summary content**

In `/Users/amobrem/ali/OpenshiftPulse/src/kubeview/views/AgentSettingsView.tsx`:

1. Add import for the `Wrench` icon and `useNavigate`:

```typescript
import {
  Bot, Shield, MessageSquare, Activity, Play, Eye, Brain,
  Zap, AlertTriangle, CheckCircle2, XCircle, Settings, LayoutDashboard, Wrench,
} from 'lucide-react';
```

2. Change the `AgentTab` type to include `'tools'`:

```typescript
type AgentTab = 'settings' | 'memory' | 'views' | 'tools';
```

3. Add the tools tab to the `agentTabs` array (after the `views` tab entry):

```typescript
    { id: 'tools', label: 'Tools', icon: <Wrench className="w-3.5 h-3.5 text-fuchsia-400" />, activeIcon: <Wrench className="w-3.5 h-3.5" /> },
```

4. Add the tab content rendering after the views tab content block:

```typescript
        {activeTab === 'tools' && <ToolsSummaryTab />}
```

5. Add the `ToolsSummaryTab` component at the bottom of the file (before the last closing of the file or after the `SettingsTabContent` function):

```typescript
function ToolsSummaryTab() {
  const navigate = useNavigate();
  const { data: stats } = useQuery({
    queryKey: ['tools', 'stats'],
    queryFn: async () => {
      const res = await fetch('/api/agent/tools/usage/stats');
      if (!res.ok) return null;
      return res.json();
    },
    staleTime: 30_000,
  });

  const { data: versionInfo } = useQuery({
    queryKey: ['agent', 'version'],
    queryFn: async () => {
      const res = await fetch('/api/agent/version');
      if (!res.ok) return null;
      return res.json();
    },
    staleTime: 300_000,
  });

  return (
    <div className="space-y-4">
      {/* Summary cards */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <div className="bg-slate-900 border border-slate-800 rounded-lg p-3">
          <div className="text-[11px] text-slate-400 mb-1">Total Tools</div>
          <div className="text-lg font-semibold text-slate-100">{versionInfo?.tools ?? '...'}</div>
        </div>
        <div className="bg-slate-900 border border-slate-800 rounded-lg p-3">
          <div className="text-[11px] text-slate-400 mb-1">Calls (all time)</div>
          <div className="text-lg font-semibold text-slate-100">{stats?.total_calls?.toLocaleString() ?? '...'}</div>
        </div>
        <div className="bg-slate-900 border border-slate-800 rounded-lg p-3">
          <div className="text-[11px] text-slate-400 mb-1">Error Rate</div>
          <div className="text-lg font-semibold text-slate-100">
            {stats?.error_rate != null ? `${(stats.error_rate * 100).toFixed(1)}%` : '...'}
          </div>
        </div>
        <div className="bg-slate-900 border border-slate-800 rounded-lg p-3">
          <div className="text-[11px] text-slate-400 mb-1">Tools Used</div>
          <div className="text-lg font-semibold text-slate-100">{stats?.unique_tools_used ?? '...'}</div>
        </div>
      </div>

      {/* Top tools mini-list */}
      {stats?.by_tool && stats.by_tool.length > 0 && (
        <div className="bg-slate-900 border border-slate-800 rounded-lg p-4">
          <h3 className="text-xs font-medium text-slate-300 mb-2">Most Used Tools</h3>
          <div className="space-y-1">
            {stats.by_tool.slice(0, 5).map((t: { tool_name: string; count: number; error_count: number }) => (
              <div key={t.tool_name} className="flex items-center justify-between text-xs">
                <span className="font-mono text-slate-300">{t.tool_name}</span>
                <div className="flex items-center gap-2">
                  <span className="text-slate-400">{t.count} calls</span>
                  {t.error_count > 0 && <span className="text-red-400">{t.error_count} errors</span>}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Link to full page */}
      <button
        onClick={() => navigate('/tools')}
        className="flex items-center gap-2 px-4 py-2 text-xs bg-slate-900 border border-slate-700 rounded-md text-slate-300 hover:text-slate-100 hover:border-slate-600 transition-colors"
      >
        <Wrench className="w-3.5 h-3.5 text-fuchsia-400" />
        Open full Tools & Agents page
      </button>
    </div>
  );
}
```

- [ ] **Step 2: Verify it compiles**

Run: `cd /Users/amobrem/ali/OpenshiftPulse && npx tsc --noEmit 2>&1 | head -20`

- [ ] **Step 3: Commit**

```bash
cd /Users/amobrem/ali/OpenshiftPulse
git add src/kubeview/views/AgentSettingsView.tsx
git commit -m "feat: add Tools summary tab to AgentSettingsView"
```

---

### Task 5: Final verification

**Files:** None (verification only)

- [ ] **Step 1: Type check the full project**

Run: `cd /Users/amobrem/ali/OpenshiftPulse && npx tsc --noEmit 2>&1 | head -30`
Expected: No errors

- [ ] **Step 2: Build the project**

Run: `cd /Users/amobrem/ali/OpenshiftPulse && npm run build 2>&1 | tail -20`
Expected: Build succeeds

- [ ] **Step 3: Verify all new files exist**

```bash
ls -la src/kubeview/store/toolUsageStore.ts
ls -la src/kubeview/views/ToolsView.tsx
```

- [ ] **Step 4: Commit (if any fixes needed)**

```bash
cd /Users/amobrem/ali/OpenshiftPulse
git add -A && git commit -m "chore: final cleanup for tools UI"
```
