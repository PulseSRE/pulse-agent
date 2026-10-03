# Mission Control: Agent Settings Page Redesign

> Historical design/implementation record. This preserves the proposal and tasks at the date in the filename; unchecked tasks, paths, commands, and security claims are not evidence of the current implementation. Use [current documentation](../../README.md) and verify behavior against code before applying a procedure.


## Problem

The Agent Settings page (`/agent`) is a collection of disconnected toggles and read-only displays spread across 5 tabs (Settings, Scanners, Memory, Views, Evals). Three core operator questions require visiting 3 pages each:

- "Is the agent working well?" → Agent Settings (Evals) → Toolbox (Analytics) → Incidents (History)
- "What has the agent done for me?" → Toolbox (Usage Log) → Incidents (History) → Agent Settings (Memory)
- "What can I use that I'm not?" → Toolbox (Catalog) → Agent Settings (Scanners) → Toolbox (Skills)

Additionally, Welcome and Pulse duplicate cluster health data and AI briefing content, creating confusion about which page to check first.

The backend produces extensive analytics data (tool usage, chain intelligence, prompt logs, confidence calibration, memory patterns, PromQL reliability, token efficiency, harness effectiveness) that is either invisible to users or buried in developer-only views. Operators have no way to answer "can I trust this agent?" with data.

## Goals

1. Every operator task completable in 1–2 page visits (down from 3)
2. Agent settings page actively teaches users about the trust spectrum, agent performance, and unused capabilities
3. Clear, non-overlapping page responsibilities — no data duplication across pages
4. Operator-first design; developer/admin concerns stay on Toolbox
5. Surface trust-building analytics (accuracy, reliability, improvement trends) to operators on Mission Control
6. Surface tuning analytics (efficiency, waste, debugging) to developers on Toolbox
7. Surface real-time agent activity indicators on Pulse

## Audiences

- **Primary (this design):** Day-to-day SRE operators who use Pulse for incident response and want to tune their experience
- **Secondary (future work):** Platform engineers who deploy Pulse and set team-wide policies

Team-level controls (recommended defaults, admin guardrails beyond `max_trust_level`) are deferred.

## Design Principles Applied

- Conversational-first, visual-second (Principle 1): plain-English policy summaries over abstract level numbers
- Intent → Visibility → Trust → Action (Principle 2): show consequences before asking for commitment
- Zero training curve (Principle 3): impact previews teach the trust spectrum without documentation
- Minimal cognitive load & single pane of glass (Principle 8): one page answers "is my agent set up right?"
- Proactive intelligence without alert fatigue (Principle 7): capability recommendations are contextual, not a catalog dump
- Radical transparency & explainability (Principle 6): surface confidence calibration, recurring mistakes, and improvement trends
- Personalized & adaptive over time (Principle 10): analytics reflect the operator's cluster and usage patterns, not generic benchmarks

---

## Page Architecture

### Page Roles (Revised)

| Page | Route | Job | Operator Question |
|------|-------|-----|-------------------|
| **Pulse** | `/pulse` | Home. Cluster health + what happened + agent activity | "What's going on right now?" |
| **Mission Control** | `/agent` | Agent policy + agent health + capability gaps | "Is my agent set up right and working well?" |
| **Incidents** | `/incidents` | Active work. Triage, approve, investigate, history | "What needs my attention?" |
| **Toolbox** | `/toolbox` | Developer reference. Tool/skill/MCP internals | "How does the agent work under the hood?" |
| **Welcome** | `/welcome` | First-run onboarding only. Redirects to Pulse after setup. | "I'm new, where do I start?" |

### Content Migration

| Content | Current Location | New Location |
|---------|-----------------|--------------|
| Cluster health overview | Welcome + Pulse (duplicated) | **Pulse only** |
| AI briefing | Welcome + Pulse (duplicated) | **Pulse only** |
| Agent activity highlights | Nowhere (spread across 3 pages) | **Pulse** (in activity feed) |
| Scan Now / monitoring toggle | Agent Settings | **Pulse** |
| Trust level + auto-fix categories | Agent Settings tab 1 | **Mission Control** Section 1 |
| Communication style | Agent Settings tab 1 | **Mission Control** Section 1 (inline) |
| Scanner config | Agent Settings tab 2 | **Mission Control** Section 2 (coverage card → drawer) |
| Eval quality gate | Agent Settings tab 5 | **Mission Control** Section 2 (quality card) |
| Outcomes summary | Nowhere | **Mission Control** Section 2 (outcomes card) |
| Capability recommendations | Nowhere | **Mission Control** Section 3 (new) |
| Production readiness summary | Nowhere | **Mission Control** Section 2 (small indicator) |
| Memory detail | Agent Settings tab 3 | **Mission Control** (detail drawer, not tab) |
| Views management | Agent Settings tab 4 | Accessible from Custom Views or chat |
| Onboarding wizard | Welcome (mixed with daily landing) | **Welcome** (first-run only) |
| Confidence calibration | Nowhere (computed but not surfaced) | **Mission Control** (Quality card) |
| Safety record (violations, hallucinations) | Evals tab (buried in suite detail) | **Mission Control** (Quality card, headline number) |
| Scanner finding counts + noise scores | Nowhere | **Mission Control** (Coverage card → drawer) |
| Auto-fix success/rollback rate | Nowhere (raw data in fix history) | **Mission Control** (Outcomes card) |
| Resolution time trending | Nowhere | **Mission Control** (Outcomes card) |
| Cost per incident | Nowhere | **Mission Control** (Outcomes card) |
| Interaction quality scores | Nowhere (internal memory eval) | **Mission Control** (Section 2b) |
| Anti-patterns / recurring mistakes | Nowhere (internal memory) | **Mission Control** (Section 2b) |
| Operator override rate | Nowhere | **Mission Control** (Section 2b) |
| Active skill / handoff events | Nowhere | **Pulse** (header + activity feed) |
| Harness effectiveness | Nowhere (system prompt only) | **Toolbox** (Analytics tab) |
| Routing accuracy | Nowhere (system prompt only) | **Toolbox** (Analytics tab) |
| Feedback analysis | Nowhere (system prompt only) | **Toolbox** (Analytics tab) |
| Prompt breakdown + version drift | Nowhere | **Toolbox** (Analytics tab) |
| PromQL reliability | Nowhere (system prompt only) | **Toolbox** (Analytics tab) |
| Token trending | Nowhere (system prompt only) | **Toolbox** (Analytics tab) |

---

## Mission Control Page Design

Mission Control is a single scrollable page with three sections and drill-through detail drawers. No tabs.

### Section 1: Agent Status & Trust Policy (Top)

**Layout:** Compact identity bar at top, trust slider as hero element, policy summary below.

**Components:**

1. **Identity Bar** (one line)
   - Agent version, protocol version, connection status indicator, model name
   - Compact — informational, not interactive

2. **Trust Level Selector** (hero element)
   - Horizontal segmented control, levels 0–4
   - Each level labeled: Monitor Only, Suggest, Ask First, Auto-fix Safe, Full Auto
   - Capped by server-side `max_trust_level` (levels beyond cap are disabled with tooltip explaining why)
   - Level 3+ selection triggers confirmation dialog (existing behavior)

3. **Plain-English Policy Summary** (live-updating text)
   - Updates immediately as trust level changes
   - Format: "Your agent monitors {N} scanners, auto-fixes {categories}, and asks before anything else. Communication: {style}."
   - Example at Level 2: "Your agent monitors 12 scanners and proposes fixes for your review before acting. Communication: detailed."
   - Example at Level 3: "Your agent monitors 12 scanners, auto-fixes crashloops and failed deployments, and asks before anything risky. Communication: detailed."

4. **Impact Preview** (hover/focus on unselected trust level)
   - Shows what would change if the user moved to that level
   - Uses historical data from fix history: "Moving to Level 3 would also auto-fix image pull errors. Last week, this would have resolved 4 additional incidents without asking."
   - If no historical data: falls back to generic description of what unlocks

5. **Auto-fix Categories** (inline below trust slider)
   - Checkbox list: crashloop, workloads, image_pull
   - Grayed out at trust levels 0–1
   - Visually connected to the trust slider — they're part of the same policy decision

6. **Communication Style** (subtle segmented toggle)
   - Brief / Detailed / Technical
   - Positioned within the policy summary area, not a separate section
   - Small enough to not compete with trust for visual priority

**Data sources:**
- `GET /api/agent/version` — identity bar
- `GET /api/agent/monitor/capabilities` — max trust level, supported categories
- Fix history from `GET /api/agent/monitor/fix-history` or equivalent — impact preview
- `trustStore` (Zustand, localStorage) — current trust level, categories, communication style

### Section 2: Agent Health (Middle)

Three cards in a horizontal row, answering "is my agent working well?" without cross-referencing pages.

**Card 1: Quality Gate**

- Large pass/fail badge with overall score percentage
- Dimension breakdown: small colored bars for safety, relevance, clarity, etc.
  - Green ≥80%, amber 60–80%, red <60%
- Score trend sparkline (last N runs) with delta vs. previous
- **Confidence calibration** — "Confidence accuracy: 92%". Translates the Brier score into a percentage. When the agent says "high confidence," can you trust it? Displayed as good (≥85%) / fair (70–84%) / poor (<70%).
- **Safety record** — "0 policy violations, 0 hallucinated tools in 30 days." Pulled from eval blocker counts. A clean safety record builds trust; violations are flagged prominently.
- Click to expand: full eval suite breakdown (release, safety, integration, view designer), prompt token audit, confidence calibration detail
- This replaces the current Evals tab content

**Data sources:**
- `GET /api/agent/eval/status` — quality gate, suite scores, outcomes, prompt audit, blocker counts
- `GET /api/agent/eval/trend?suite=release` — sparkline, delta
- `GET /api/agent/analytics/confidence` — Brier score, calibration breakdown (new)

**Card 2: Coverage**

- Headline: "{N} of {total} scanners active — covering {X}% of common failure modes"
- Category breakdown: which failure categories are covered vs. not (pod health, node pressure, security audit, certificate expiry, etc.)
- Uncovered categories highlighted with brief explanation of what they catch
- Click to expand: detail drawer with full scanner list and toggles (replaces Scanners tab)
- Each scanner in the drawer shows contextual stats:
  - **Finding count with actionability:** "Found 12 issues (8 actionable)" — not just volume, but signal quality
  - **False positive rate:** "Noise score: 15%" — powered by existing `noiseScore` and resolution events (self-resolved without action = likely noise)
  - These stats help operators decide which scanners are worth enabling vs. which are noisy

**Data sources:**
- `GET /api/agent/monitor/scanners` — scanner list, enabled status
- `GET /api/agent/monitor/coverage` — coverage percentage, category breakdown, per-scanner stats (new)
- Scanner finding counts and noise scores from monitor data

**Card 3: Outcomes**

- Headline: "This week: {N} findings, {N} auto-fixed, {N} pending review, {N} self-resolved"
- **Auto-fix success rate** — "Auto-fixes: 94% successful, 2 rolled back." Directly answers "can I trust auto-fix?" Powered by action status (completed vs. rolled_back).
- **Resolution time trend** — "Avg time to resolution: 4.2 min (↓ from 6.1 min last week)." Shows agent is getting faster. Computed from finding timestamp → action completion timestamp.
- **Cost per incident** — "Avg tokens per resolved incident: 12K (↓ 18%)." Operators managing budgets see cost trajectory. Computed from tool_turns token data joined with incident resolution.
- Trend indicator: findings trending up/down vs. previous week
- Small memory indicator: "{N} patterns learned, {N} this week"
- Production readiness indicator: "{N}/{total} gates passing" with attention count
- Link to Incidents for full history
- Click memory indicator to open memory detail drawer

**Data sources:**
- `GET /api/agent/monitor/fix-history/summary` — aggregated fix counts, success rate, rollback count, resolution times (new)
- `GET /api/agent/analytics/cost` — tokens per incident, trending (new)
- `GET /api/agent/readiness/summary` or computed from readiness gate data
- Memory stats from memory API

### Section 2b: Agent Accuracy (Below Health Cards)

A compact section focused on trust-building through radical transparency — showing where the agent is strong AND where it's weak.

**Components:**

1. **Improvement Trend**
   - Average interaction quality score (0–1 from memory evaluation rubric) over time
   - Simple line: "Agent quality: 0.82 avg (↑ from 0.74 last month)"
   - Dimensions: resolution rate, efficiency, safety, speed — shown as small sub-indicators
   - Data source: `memory/evaluation.py` scores aggregated over time

2. **Recurring Mistakes** (radical transparency)
   - Powered by anti-pattern detection from `memory/store.py` (low-score incidents, score < 0.4)
   - Shows 1–2 areas where the agent struggles: "The agent has had difficulty with {error_type} in {namespace} — 3 low-score interactions this month"
   - If no anti-patterns detected: section shows "No recurring issues detected" (positive signal)
   - Purpose: the operator should know the agent's blind spots, not just its strengths

3. **Learning Activity**
   - Runbook success rate: "Learned runbooks: 87% success rate (12 runbooks, 3 new this month)"
   - Pattern detection: "Detected 4 recurring patterns, 2 time-based correlations"
   - Cross-session improvement: "Agent resolves {category} issues 30% faster than first encounter" (compare scores on similar queries over time)
   - Click to open Memory drawer for full detail

4. **Operator Override Rate**
   - "You overrode the agent 2 of 14 times this week (14%)"
   - Tracks rejected actions / total proposed actions
   - High override rate = trust calibration signal (agent may be too aggressive, or trust level may be too high)
   - Low override rate at high trust level = agent is well-calibrated

**Data sources:**
- `GET /api/agent/analytics/accuracy` — interaction scores, anti-patterns, learning stats, override rate (new)
- Memory evaluation scores from `memory/evaluation.py`
- Anti-patterns from `memory/store.py` search_low_score_incidents
- Override rate from action history (rejected / total)

**Design note:** This section is collapsible. Operators who trust the agent implicitly can collapse it. New operators or those evaluating whether to increase trust level will find it valuable. Default: expanded for trust levels 0–2, collapsed for levels 3–4.

### Section 3: Capability Discovery (Bottom)

Contextual recommendations — not a catalog, not a settings form.

**Layout:** Section header "You could also be using..." with 3–4 recommendation cards.

**Recommendation Type 1: Unused Scanners Relevant to Cluster**

- Based on cluster workload profile (e.g., StatefulSets detected → recommend storage scanner)
- Format: "{context about your cluster}. Enable {scanner} to catch {failure mode}."
- One-click enable button inline
- Example: "You have 8 StatefulSets with PVCs. Enable the storage exhaustion scanner to catch capacity issues early. [Enable]"

**Recommendation Type 2: Capabilities You Haven't Tried**

- Based on conversation history and tool usage patterns
- Format: "You've asked about {topic} {N} times. The agent can {capability} — try asking '{example prompt}'."
- Links to chat, not to a config page
- Example: "You've asked about deployment rollbacks 3 times. The agent can propose Git PRs for rollback — try asking 'propose a rollback PR for deployment X'."

**Constraints:**
- Max 3–4 recommendations at a time
- Individually dismissable (persisted to localStorage)
- Refreshed periodically (not on every page load)
- If no recommendations available, section is hidden (not "nothing to show")

**Data sources:**
- Cluster workload profile from monitor/Pulse data
- Tool usage patterns from `GET /api/agent/tools/usage/stats`
- Conversation history patterns (may require new lightweight endpoint or client-side analysis)

### Detail Drawers

Instead of tabs, detailed content opens in slide-over drawers from the right:

- **Scanner Drawer** — full scanner list with toggles and per-scanner stats. Opens from Coverage card.
- **Eval Drawer** — full suite breakdown, prompt audit, dimension details. Opens from Quality card.
- **Memory Drawer** — learned patterns, runbooks, resolved incidents. Opens from Outcomes card memory indicator.

Drawers maintain page context (user can see the cards behind the drawer) and close with Escape or clicking outside.

---

## Changes to Other Pages

### Welcome Page

**Current:** Daily landing page with cluster health, AI briefing, navigation grid, getting started checklist.
**Proposed:** First-run onboarding wizard only.

- First visit: guided setup flow — connect to cluster → set trust level → enable scanners → run readiness check
- After onboarding complete: `/` redirects to Pulse (not Welcome)
- Welcome remains accessible via command palette (`Cmd+K` → "Onboarding") for re-running setup
- Getting started checklist and navigation grid are onboarding tools — they stay on Welcome, not duplicated on Pulse

### Pulse Page

**Current:** Cluster health dashboard with topology, zones, briefing, insights rail.
**Proposed:** Absorbs daily briefing and agent activity. Becomes the sole daily landing page.

Additions:
- **Agent activity in overnight feed:** "Agent auto-fixed 3 crashlooping pods, investigated 2 node pressure alerts" — woven into existing activity feed, not a separate section
- **Monitoring controls:** "Scan Now" button and monitoring enable/disable toggle move here from Agent Settings
- **Agent status indicator in header:** Small "Agent: connected, Trust Level 3" badge — links to Mission Control
- **Active skill indicator:** "Agent is running: SRE diagnosis" or "Security scan in progress" — shows what the agent is doing right now, powered by skill_usage tracking via WebSocket
- **Skill handoff events in activity feed:** "Agent escalated from SRE → Security scan for namespace prod" — from skill handoff tracking (handoff_from/handoff_to fields)
- **Token burn rate** (subtle): small indicator showing agent activity level — heavy token usage = complex investigation in progress. Not a number, just a pulse animation intensity (idle / thinking / deep analysis)

No changes to: topology map, zone-based health report, insights rail.

### Incidents Page

**Current:** 5 tabs (Now, Timeline, Actions, History, Alerts).
**Proposed:** No structural changes. One addition:

- **Action reasoning:** Each action in History and Actions tabs includes a brief "why" line: "Deleted pod X because it crashlooped 14 times in 10 minutes (trust level 3, crashloop auto-fix enabled)." This eliminates the need to cross-reference Toolbox usage logs to understand agent behavior.

### Toolbox Page

**Current:** Developer reference with Catalog, Skills, Connections, Components, Usage Log, Analytics tabs.
**Proposed:** Stays as the developer/admin deep-dive. Existing tabs unchanged. Analytics tab expanded with new intelligence data.

Additions to **Analytics tab:**

- **Harness effectiveness** — "Tool selection accuracy: 73%. Wasted tools: `get_operator_status` offered 45 times, used twice." Directly actionable: highlights tools to remove from always-include. Source: `intelligence.py` `_compute_harness_effectiveness()`.
- **Routing accuracy** — "8% of multi-turn sessions switched modes mid-conversation." High percentage = intent classifier needs tuning. Source: `intelligence.py` `_compute_routing_accuracy()`.
- **Feedback analysis** — "Tools with negative feedback: `scale_deployment` (3 complaints), `delete_pod` (2)." Shows which tools frustrate users. Source: `intelligence.py` `_compute_feedback_analysis()`.
- **Prompt breakdown** — "System prompt: 14,200 tokens. Sections: base (4,200), tools (6,100), context (2,400), intelligence (1,500). Cache hit rate: 89%." Tracks prompt bloat over time. Source: `prompt_log.py` `get_prompt_stats()`.
- **Prompt version drift** — "Prompt hash changed 3 times this week." Timeline showing when prompt content changed and which sections grew/shrank. Source: `prompt_log.py` `get_prompt_versions()`.
- **PromQL reliability** — "Reliable queries: 58/73 (>80% success). Unreliable: `container_memory_rss` (22% success rate)." Helps fix broken monitoring queries. Source: `intelligence.py` `_compute_query_reliability()`.
- **Token trending** — "Week-over-week: input tokens +12%, output tokens −5%, cache rate +3%." Cost trajectory with weekly comparison. Source: `intelligence.py` `_compute_token_trending()`.
- **Dashboard patterns** — "Most used view components: metric_card (34×), line_chart (28×), data_table (22×). Avg widgets per dashboard: 5.2." Guides component development priorities. Source: `intelligence.py` `_compute_dashboard_patterns()`.

### All Domain Pages

**No changes.** Workloads, Compute, Networking, Storage, Security, Admin, Identity, GitOps, Fleet, Readiness, and all resource views (Table, Detail, YAML, Logs, Metrics, Dependencies) are unaffected.

### Readiness Page

**No structural changes.** The full 30-gate checklist stays as-is. Two new connections:

1. **Welcome onboarding** links to Readiness as the final setup step
2. **Mission Control** shows a readiness summary indicator in the Outcomes card

---

## Task Journey Improvements

| # | Task | Before (hops) | After (hops) |
|---|------|---------------|--------------|
| 1 | "What happened overnight?" | 3 (Welcome → Pulse → Incidents) | **1** (Pulse) |
| 2 | "Is my cluster healthy?" | 1 (Pulse) | **1** (Pulse) |
| 3 | "Something is broken" | 3-4 (Incidents → Detail → Logs) | **3-4** (same, good flow) |
| 4 | "Approve a fix" | 1 (Incidents) | **1** (Incidents) |
| 5 | "Silence an alert" | 1 (Incidents) | **1** (Incidents) |
| 6 | "Change agent autonomy" | 1 (Agent Settings) | **1** (Mission Control, with impact preview) |
| 7 | "Enable a scanner" | 1 (Agent Settings) | **1** (Mission Control, with coverage context) |
| 8 | "Trigger a cluster scan" | 1 (Agent Settings) | **1** (Pulse, where you're watching) |
| 9 | "Is the agent working well?" | 3 (Settings → Toolbox → Incidents) | **1** (Mission Control) |
| 10 | "What has the agent done for me?" | 3 (Toolbox → Incidents → Settings) | **1** (Pulse) |
| 11 | "What am I not using?" | 3 (Toolbox → Settings → Toolbox) | **1** (Mission Control) |
| 12 | "Why did the agent do that?" | 3 (Incidents → Toolbox → Settings) | **1** (Incidents, reasoning inline) |
| 13 | "What has the agent learned?" | 1 (Agent Settings) | **1** (Mission Control, drawer) |
| 14 | "First-time setup" | 3 (Welcome → Settings → Readiness) | **1-2** (Welcome wizard) |
| 15 | "Are we production-ready?" | 1 (Readiness) | **1-2** (Mission Control summary → Readiness detail) |
| 16 | "Create a custom dashboard" | 1 (Agent Settings Views tab) | **1** (Chat or /custom) |

---

## New API Endpoints Required

### Mission Control Endpoints

| Endpoint | Purpose | Used By | Data Sources |
|----------|---------|---------|--------------|
| `GET /api/agent/monitor/fix-history/summary` | Aggregated fix counts, success/rollback rates, resolution times for impact preview and outcomes card | Mission Control Sections 1 & 2 | `actions` table |
| `GET /api/agent/monitor/coverage` | Scanner coverage percentage, category breakdown, per-scanner finding counts and noise scores | Mission Control Section 2 | `scan_runs`, scanner metadata, finding resolution data |
| `GET /api/agent/analytics/confidence` | Confidence calibration (Brier score as percentage), predicted vs. actual outcome breakdown | Mission Control Section 2 (Quality card) | `actions` table (confidence field vs. verification_status) |
| `GET /api/agent/analytics/accuracy` | Interaction quality scores, anti-patterns, learning stats, override rate | Mission Control Section 2b | `memory/evaluation.py`, `memory/store.py`, `actions` table |
| `GET /api/agent/analytics/cost` | Tokens per incident, cost trending week-over-week | Mission Control Section 2 (Outcomes card) | `tool_turns` joined with incident/action data |
| `GET /api/agent/recommendations` | Contextual capability recommendations based on cluster workload profile and tool usage patterns | Mission Control Section 3 | `tool_usage`, scanner data, cluster workload profile |
| `GET /api/agent/readiness/summary` | Lightweight readiness gate pass/fail counts | Mission Control Section 2 | Readiness gate evaluation |

### Toolbox Analytics Endpoints

| Endpoint | Purpose | Used By | Data Sources |
|----------|---------|---------|--------------|
| `GET /api/agent/analytics/intelligence` | All 8 intelligence loop sections (harness effectiveness, routing accuracy, feedback analysis, token trending, query reliability, dashboard patterns, error hotspots, token efficiency) | Toolbox Analytics tab | `intelligence.py` computed sections |
| `GET /api/agent/analytics/prompt` | Prompt section breakdown, cache hit rate, version history | Toolbox Analytics tab | `prompt_log.py` `get_prompt_stats()`, `get_prompt_versions()` |

### Pulse Indicators (WebSocket)

| Event | Purpose | Used By | Data Sources |
|-------|---------|---------|--------------|
| `skill_activity` on `/ws/monitor` | Active skill name, handoff events, activity level | Pulse header & activity feed | `skill_usage` tracking, real-time session state |

### Existing Endpoints (used as-is)

- `GET /api/agent/version`
- `GET /api/agent/monitor/capabilities`
- `GET /api/agent/monitor/scanners`
- `GET /api/agent/eval/status`
- `GET /api/agent/eval/trend`
- `GET /api/agent/tools/usage/stats`
- `GET /api/agent/tools/usage/chains`

---

## New Tracking Required

Some analytics need new data collection, not just new endpoints over existing data:

| Metric | What to Track | Implementation |
|--------|--------------|----------------|
| **Conversation depth** | Turns to resolution per session | Count turns in sessions where outcome=resolved. Low effort — data exists in `tool_turns`, just needs aggregation. |
| **User satisfaction ratio** | Thumbs up/down aggregated | Feedback field exists in `tool_turns` but isn't aggregated. Add a `GET /api/agent/analytics/satisfaction` endpoint. |
| **Time-to-first-action** | Duration from finding detection to first proposed action | Timestamp diff: finding.timestamp → first action.timestamp for that finding_id. Data exists, needs join query. |
| **Scanner dwell time** | How long findings stay open before resolution | Track finding created_at → resolved_at. Partially exists via resolution events; may need explicit timestamp pairing. |
| **Cross-session learning** | Performance improvement on repeated issue types | Compare memory evaluation scores on queries with similar keywords over time. Requires new aggregation query against `incidents` table. |
| **Operator intervention rate** | Rejected actions / total proposed actions | Data exists in actions table (status field). Needs aggregation endpoint. |

## Analytics Distribution Summary

| Page | Analytics Purpose | What's Shown |
|------|-------------------|--------------|
| **Mission Control** | Trust-building | Confidence calibration, safety record, scanner ROI, false positive rate, auto-fix success rate, resolution time trend, cost per incident, learning rate, recurring mistakes, improvement trend, override rate |
| **Pulse** | Real-time awareness | Active skill, handoff events, agent activity level (token burn pulse) |
| **Toolbox** | Agent tuning | Harness effectiveness, routing accuracy, feedback analysis, prompt breakdown, prompt drift, PromQL reliability, token trending, dashboard patterns, error hotspots |
| **Incidents** | Action context | Per-action reasoning text (inline, not a separate analytics view) |

## Out of Scope

- Team-level settings and admin guardrails (future work)
- Domain page changes (Workloads, Compute, etc.)
- Fleet-specific Mission Control views
- Mobile/responsive layout specifics
- Visual design (colors, spacing, typography) — follows existing design system

---

## Migration Notes

- Views management (`/agent?tab=views`) gets a redirect to `/custom` or equivalent
- Memory route (`/memory`) redirects to `/agent` (drawer opens automatically)
- Existing `trustStore`, `monitorStore` Zustand stores remain — no data migration needed
- localStorage keys unchanged
- The 5-tab structure is removed in favor of single-page with drawers
- All existing API contracts preserved; new endpoints are additive
