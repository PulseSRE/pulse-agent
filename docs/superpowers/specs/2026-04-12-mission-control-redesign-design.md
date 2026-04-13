# Mission Control: Agent Settings Page Redesign

## Problem

The Agent Settings page (`/agent`) is a collection of disconnected toggles and read-only displays spread across 5 tabs (Settings, Scanners, Memory, Views, Evals). Three core operator questions require visiting 3 pages each:

- "Is the agent working well?" → Agent Settings (Evals) → Toolbox (Analytics) → Incidents (History)
- "What has the agent done for me?" → Toolbox (Usage Log) → Incidents (History) → Agent Settings (Memory)
- "What can I use that I'm not?" → Toolbox (Catalog) → Agent Settings (Scanners) → Toolbox (Skills)

Additionally, Welcome and Pulse duplicate cluster health data and AI briefing content, creating confusion about which page to check first.

## Goals

1. Every operator task completable in 1–2 page visits (down from 3)
2. Agent settings page actively teaches users about the trust spectrum, agent performance, and unused capabilities
3. Clear, non-overlapping page responsibilities — no data duplication across pages
4. Operator-first design; developer/admin concerns stay on Toolbox

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
- Click to expand: full eval suite breakdown (release, safety, integration, view designer), prompt token audit
- This replaces the current Evals tab content

**Data sources:**
- `GET /api/agent/eval/status` — quality gate, suite scores, outcomes, prompt audit
- `GET /api/agent/eval/trend?suite=release` — sparkline, delta

**Card 2: Coverage**

- Headline: "{N} of {total} scanners active — covering {X}% of common failure modes"
- Category breakdown: which failure categories are covered vs. not (pod health, node pressure, security audit, certificate expiry, etc.)
- Uncovered categories highlighted with brief explanation of what they catch
- Click to expand: detail drawer with full scanner list and toggles (replaces Scanners tab)
- Each scanner in the drawer shows contextual stats: "Found 8 issues this week" or "No findings yet"

**Data sources:**
- `GET /api/agent/monitor/scanners` — scanner list, enabled status
- Scanner finding counts from monitor data

**Card 3: Outcomes**

- Headline: "This week: {N} findings, {N} auto-fixed, {N} pending review, {N} self-resolved"
- Trend indicator: findings trending up/down vs. previous week
- Small memory indicator: "{N} patterns learned, {N} this week"
- Production readiness indicator: "{N}/{total} gates passing" with attention count
- Link to Incidents for full history
- Click memory indicator to open memory detail drawer

**Data sources:**
- Aggregated from monitor/incident data (new endpoint or computed client-side from existing data)
- `GET /api/agent/readiness/summary` or computed from readiness gate data
- Memory stats from memory API

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

No changes to: topology map, zone-based health report, insights rail.

### Incidents Page

**Current:** 5 tabs (Now, Timeline, Actions, History, Alerts).
**Proposed:** No structural changes. One addition:

- **Action reasoning:** Each action in History and Actions tabs includes a brief "why" line: "Deleted pod X because it crashlooped 14 times in 10 minutes (trust level 3, crashloop auto-fix enabled)." This eliminates the need to cross-reference Toolbox usage logs to understand agent behavior.

### Toolbox Page

**No changes.** Toolbox stays as the developer/admin deep-dive into tool schemas, skill definitions, MCP server configuration, chain analysis, and usage analytics.

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

| Endpoint | Purpose | Used By |
|----------|---------|---------|
| `GET /api/agent/monitor/fix-history/summary` | Aggregated fix counts for impact preview and outcomes card | Mission Control Sections 1 & 2 |
| `GET /api/agent/monitor/coverage` | Scanner coverage percentage and category breakdown | Mission Control Section 2 |
| `GET /api/agent/recommendations` | Contextual capability recommendations based on cluster profile and usage patterns | Mission Control Section 3 |
| `GET /api/agent/readiness/summary` | Lightweight readiness gate pass/fail counts | Mission Control Section 2 |

Existing endpoints used as-is:
- `GET /api/agent/version`
- `GET /api/agent/monitor/capabilities`
- `GET /api/agent/monitor/scanners`
- `GET /api/agent/eval/status`
- `GET /api/agent/eval/trend`

---

## Out of Scope

- Team-level settings and admin guardrails (future work)
- Toolbox page changes
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
