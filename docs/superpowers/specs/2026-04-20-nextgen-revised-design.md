# Pulse Next-Gen: Revised Design Spec

## Context

The original next-gen plan (11 phases, 2700 lines) was overscoped. After brainstorming, we've made key decisions that simplify the path forward:

- **No feature flags** — hard cutover per phase, each must be fully baked
- **Agent views** — not a new UI layer; agent creates versioned custom views for any structured analysis (incidents, planning, readiness assessments), SRE opens side-by-side with chat
- **Cleanup before features** — refactor the large files and remove dead code first
- **Drop Go event processor and NATS** — K8s Python watches replace 60s polling without adding a new language or infrastructure
- **Drop Voyage AI / pgvector** — existing TF-IDF tool prediction + incident memory is sufficient until proven otherwise
- **6 phases, not 11** — cut mission board, capability discovery, fast path, meta-cognitive layer

## What This Plan Does NOT Include

These items from the original plan are deferred indefinitely. They're either premature optimization, premature abstraction, or scope creep:

- Go event processor + NATS JetStream (Phase 1 original)
- Medium path with Sonnet + pgvector similarity search (Phase 2 original)
- Fast path with compiled decision trees (Phase 7 original)
- Mission Board / task management (Phase 5 original)
- Intent Reconciler (Phase 6 original)
- Self-improvement / pattern promotion (Phase 8 original)
- Tool consolidation (Phase 9 original)
- Meta-cognitive dashboard (Phase 10 original)
- Capability discovery (Phase 11 original)

If any of these become necessary, they can be specced and built as standalone projects. They do not block the work below.

---

## Phase 1: Code Cleanup & Refactoring

**Goal:** Clean foundation before building anything new.

**Why:** 10 files over 1,000 lines, 4 backward-compat wrappers, dead config, mypy gaps. Building on top of this creates compounding tech debt.

### 1.1 Split Large Files

| File | Lines | Split Into |
|------|-------|------------|
| `api/monitor_rest.py` | 1,955 | `api/scanner_rest.py` (scanner coverage), `api/fix_rest.py` (fix history + rollback), `api/action_rest.py` (action details) |
| `skill_loader.py` | 1,590 | `skill_loader.py` (load + parse), `skill_router.py` (routing logic), `tool_categories.py` (category reference data) |
| `view_tools.py` | 1,554 | `view_tools.py` (CRUD + creation), `view_mutations.py` (widget update operations) |

Note: `evals/replay.py` was measured at 460 lines (not 16K as originally reported). No split needed.

Rules:
- Each split must preserve all existing tests (no test changes except imports)
- Each split must pass mypy
- No behavioral changes — pure structural refactoring

### 1.2 Remove Dead Code

| Item | Location | Action |
|------|----------|--------|
| `view_validator.py` | 43-line thin wrapper around quality_engine | Delete, update imports in `agent_ws.py` (line 372) and `test_view_validator.py` |
| `view_critic.py` | 151-line file containing `critique_view` tool | Move `critique_view` function to `quality_engine.py`, update imports in `view_tools.py` (line 1262), `ALL_TOOLS`, view_designer skill prompt, and 4 test files |
| `k8s_tools/__init__.py` re-exports | mixed active + dead re-exports | Keep `WRITE_TOOLS` re-export (actively imported by `agent.py`, `orchestrator.py`, `skill_loader.py`, `api/tools_rest.py`, `monitor/investigations.py`). Remove only genuinely unused re-exports. |
| `config.py` `prompt_experiment` | legacy A/B testing field | Verify usage in `agent.py` and `runbooks.py` first. Remove field and all references only if confirmed dead. |

Note: `_last_routing_decision` in `skill_loader.py` is actively used by `tool_usage.py` and `agent_ws.py` — it stays.

### 1.3 Security Hardening (Prerequisites)

These existing vulnerabilities must be fixed before Phase 2 ships action buttons. Action buttons on top of broken access control = privilege escalation.

#### 1.3.1 Fix View Ownership Bypass (IDOR) — HIGH

**Files:** `view_tools.py` lines 634, 681, 941, 1030, 1091, 1158

Multiple view tool functions (`get_view_details`, `update_view_widgets`, `remove_widget_from_view`, `undo_view_change`, `delete_dashboard`, `optimize_view`) fall back to `db.get_view(view_id)` without an owner filter when the owner check fails, then adopt the actual owner's identity for mutations. Any authenticated user can read, modify, or delete any other user's views.

**Fix:** Remove all ownership fallback paths. If `db.get_view(view_id, owner)` returns None, return "View not found" immediately. The identity drift concern should be solved by the existing `migrate_view_ownership()` in `auth.py`, not by bypassing access control.

#### 1.3.2 Fix `list_saved_views` Cross-User Leak — MEDIUM

**File:** `view_tools.py` lines 577-587

When `db.list_views(owner)` returns empty (identity drift), the fallback queries ALL views across ALL users with no owner filter. Remove the unscoped fallback query entirely.

#### 1.3.3 Fix Share Token HMAC Key Mismatch — HIGH

**File:** `api/views.py` lines 166, 202

Sign endpoint uses `os.environ.get("PULSE_SHARE_TOKEN_KEY", "") or get_settings().ws_token`, but verify endpoint always uses `get_settings().ws_token`. Use the same key derivation in both paths.

#### 1.3.4 Fix `clone_view` Post-Share Mutation — MEDIUM

**File:** `db.py` lines 390-423

Share token doesn't include content hash. Claimant gets whatever the view currently contains, not what was shared. Fix: snapshot the view at share time and clone from the snapshot, or include a content hash in the share token.

#### 1.3.5 Fix ReDoS in `log-counts` Endpoint — LOW-MEDIUM

**File:** `api/views.py` lines 299-349

`GET /log-counts` accepts arbitrary regex via `pattern` parameter and compiles it directly. Fix: validate `len(pattern) <= 100` and reject patterns with nested quantifiers.

#### 1.3.6 Namespace-Scope Topology/Blast Radius — MEDIUM

**Files:** `dependency_graph.py`, `api/monitor_rest.py` line 971, `view_tools.py` (`get_topology_graph`)

The dependency graph is a singleton containing ALL resources across ALL namespaces. Any authenticated user can enumerate the entire cluster topology. Fix: add namespace-scoping to `get_topology_graph` and the `blast-radius` REST endpoint based on the user's authorized namespaces.

### 1.4 Trend Scanners & Briefing Enhancement

**Trend scanners** — add 4 new scanners to `monitor/scanners.py` (~200 lines) using Prometheus `predict_linear()`:

| Scanner | PromQL Pattern | Finding |
|---------|---------------|---------|
| `scan_memory_pressure_forecast` | `predict_linear(node_memory_MemAvailable_bytes[7d], 3*86400) < 0` | "node X will hit memory pressure in ~N days" |
| `scan_disk_pressure_forecast` | `predict_linear(kubelet_volume_stats_used_bytes[7d], 7*86400) > capacity` | "PVC X will fill in ~N days" |
| `scan_hpa_exhaustion_trend` | `avg_over_time(hpa_current/hpa_max[48h]) > 0.9` | "HPA X at max 18h/day — needs higher ceiling" |
| `scan_error_rate_acceleration` | `deriv(rate(http_requests_total{code=~"5.."}[1h])[24h:]) > 0` | "error rate in X is accelerating — was N%, now N%" |

These return findings with `severity: "warning"`, a timeframe prediction, and `finding_type: "trend"`. The monitor loop picks them up automatically via the existing scanner registry.

**Briefing enhancement** — fix `get_briefing()` in `monitor/actions.py` (~100 lines):

```python
# Add to existing briefing response:
current_findings: list    # run 4 fastest scanners live (crashloop, pending, oom, firing_alerts)
trend_findings: list      # from trend scanners above
priority_items: list      # all findings sorted by severity * blast_radius * recurrence
recent_changes: list      # from audit scanners (deployments, RBAC, config changes last 12h)
recurrences: list         # findings matching recently-resolved action finding_ids
```

This is a data-correctness fix — the briefing currently only shows historical DB data, not live cluster state. Fixing it here ensures the morning briefing drives action, not just informs.

### 1.5 Enable Mypy on Skills

- Remove skills directory from mypy exclude in `pyproject.toml`
- Fix all type errors in `sre_agent/skills/`
- Add type annotations where missing

### 1.5 Acceptance Criteria

- All files under 1,000 lines (except `promql_recipes.py` which is a data file)
- Zero backward-compat wrappers
- Mypy clean across entire codebase including skills
- All 6 security vulnerabilities fixed with regression tests
- View ownership bypass test: verify user B cannot read/modify/delete user A's views
- Share token test: verify sign + verify use same key when `PULSE_SHARE_TOKEN_KEY` is set
- All existing tests pass
- `make verify` green

---

## Phase 2: New View Components

**Goal:** Add the missing component types needed for investigation views.

**Why:** The existing 25 component types cover data display but lack action execution, confidence display, resolution tracking, and status lifecycle. This phase adds 4 components; Phase 4 adds 1 more (`status_pipeline`) for a total of 6 new types (30 total).

### 2.1 Action Button Component

**Backend:** `component_registry.py` — register `action_button` component type.

```
kind: action_button
props:
  label: str          — button text
  action: str         — tool name to execute (e.g., "patch_resource")
  action_input: dict  — tool parameters
  style: str          — "primary" | "danger" | "ghost"
  confirm_text: str   — optional confirmation tooltip
```

**Execution flow:**
1. User clicks button in frontend
2. If `action` is in `WRITE_TOOLS`, frontend shows a confirmation dialog with risk level, impact description, and "What If?" preview (same pattern as existing `ConfirmDialog` component)
3. If `action` is a read-only tool, execute immediately (no dialog)
4. Frontend sends `POST /views/{view_id}/actions` with `{action, action_input}`
5. Backend verifies:
   - User owns the view (ownership check)
   - User's trust level >= action's required trust level (`MAX_TRUST_LEVEL` enforcement)
   - `action` is in the allowed tool whitelist (only tools from `WRITE_TOOLS` + read tools; never `drain_node`, `delete_namespace`, or other high-risk cluster ops)
   - Circuit breaker is not open for this tool
6. Backend executes the tool through the standard tool execution infrastructure (records in `tool_usage`, respects circuit breaker, tracks in `error_tracker`)
7. Backend returns result + creates new view version with updated state
8. Frontend receives updated view

**Security note:** Shared/cloned views inherit action buttons from the original. The backend must validate the executing user's permissions at execution time, not at view creation time. This prevents privilege escalation via malicious shared views.

**Action input sanitization:** When saving `action_button` components to views (in `_sanitize_components` or a new validator), validate that:
- `action` is in the allowed tool whitelist
- `action_input` keys match the tool's parameter schema (reject unknown parameters)
- `action_input` values pass the same validators as direct tool calls (`_validate_k8s_namespace`, `_validate_k8s_name`, replica count 0-100, grace period 1-300s)
- For `apply_yaml`, enforce `_ALLOWED_KINDS` whitelist at save time, not just execution time

This prevents prompt injection attacks where a crafted K8s resource name tricks the LLM into generating malicious `action_input` parameters in auto-created views.

**Backend endpoint:** `api/views.py` — add `POST /views/{view_id}/actions`

**Frontend:** `AgentComponentRenderer.tsx` — add `AgentActionButton` renderer. Must use existing `ConfirmDialog` pattern for write tools.

**Accessibility:** Action buttons must have `role="button"`, `aria-label` describing the action and its impact, and keyboard support (Enter/Space to activate). Danger-style buttons must have `aria-description` with risk context.

### 2.2 Confidence Badge Component

```
kind: confidence_badge
props:
  score: float        — 0.0 to 1.0
  label: str          — optional label (default: percentage)
```

Renders as inline badge: green (>0.8), amber (0.5-0.8), red (<0.5). Can be embedded in any card header.

**Accessibility:** Must have `aria-label="Confidence: {score}%"`. Color alone must not convey meaning — include text percentage.

### 2.3 Resolution Tracker Component

```
kind: resolution_tracker
props:
  steps: list[ResolutionStep]
```

Where `ResolutionStep`:
```
title: str
status: "done" | "running" | "pending"
detail: str           — metadata line
output: str | null    — monospace output block (e.g., pod status)
timestamp: str | null
```

Renders as vertical checklist with status icons (checkmark, spinner, dot). Running steps show live output.

**States:** Must handle loading (skeleton), error (step failed with message), and empty (no steps yet).

**Accessibility:** Use `role="list"` with `role="listitem"` per step. Status icons must have `aria-label` ("completed", "in progress", "pending"). Spinner must have `aria-live="polite"` for screen reader updates.

### 2.4 Blast Radius Component

```
kind: blast_radius
props:
  items: list[BlastItem]
```

Where `BlastItem`:
```
kind_abbrev: str      — "Svc", "Ing", "Dep", "HPA", etc.
name: str
relationship: str     — "Service → payment-api (selector match)"
status: str           — "degraded" | "healthy" | "retrying" | "paused"
status_detail: str    — "0 endpoints", "502 errors", etc.
```

Data source: `dependency_graph.py` already has the relationships (17 resource types, 10 relationships). The component just needs to query it and add live status per dependency.

**Topology perspective support:** The blast radius component supports an optional `perspective` prop (`physical | logical | network | multi_tenant | helm`) that filters dependencies through the 5 existing topology perspectives. Default: show all. Examples:
- `perspective: "network"` → shows only Services, Ingresses, Routes, NetworkPolicies affected
- `perspective: "logical"` → shows Deployments, ReplicaSets, ConfigMaps, Secrets affected
- `perspective: "physical"` → shows Nodes, scheduling impact, resource pressure

This reuses the perspective filtering already built in `view_tools.py` `get_topology_graph()`.

**States:** Must handle loading (fetching dependencies), error (dependency graph unavailable), and empty (no downstream dependencies found — show "No downstream dependencies detected").

**Accessibility:** Use `role="list"` with descriptive `aria-label` per item including kind, name, relationship, and status.

### 2.5 Frontend Rendering

Add to `AgentComponentRenderer.tsx` switch statement:
- `action_button` → `AgentActionButton`
- `confidence_badge` → `AgentConfidenceBadge`
- `resolution_tracker` → `AgentResolutionTracker`
- `blast_radius` → `AgentBlastRadius`

### 2.6 Acceptance Criteria

- All 4 components registered in `component_registry.py`
- All 4 rendered in frontend with correct styling
- Action button executes tool and creates new view version
- Blast radius pulls live data from dependency graph
- Unit tests for each component's backend spec generation
- Frontend renders correctly in browser (manual verification)

---

## Phase 3: Investigation View Lifecycle

**Goal:** Agent auto-creates structured views for any analysis (incidents, planning, readiness assessments), tracks status, supports version diffing.

### 3.1 Agent View Types

Agent views are custom views (`/custom/<viewId>`) created by the agent to present structured analysis. They reuse the existing view infrastructure (CRUD, versioning, sharing) but add status tracking and type classification.

**Three view types, same infrastructure:**

| Type | Created When | Status Lifecycle | Example |
|------|-------------|-----------------|---------|
| `incident` | Monitor finding or agent-detected problem | investigating → action_taken → verifying → resolved → archived | CrashLoop in payment-api |
| `plan` | User requests capability planning, upgrade, or multi-step change | analyzing → ready → executing → completed | "Support VMs for team B" |
| `assessment` | Agent proactive scan, trend alert, or readiness check | analyzing → ready → acknowledged | "Node worker-5 memory trending to pressure in 3 days" |

All three types use the same components (resolution_tracker, action_buttons, confidence_badge, blast_radius, metric_cards, etc.) — the difference is the status lifecycle and what triggers creation.

### 3.2 View Schema

**Migration 019** in `db_migrations.py` — `_migrate_019_agent_views`:

```python
def _migrate_019_agent_views(db: Database) -> None:
    for col, typ, default in [
        ("status", "TEXT", "'active'"),
        ("view_type", "TEXT", "'custom'"),
        ("trigger_source", "TEXT", "'user'"),
        ("finding_id", "TEXT", None),
        ("cluster_id", "TEXT", "''"),
        ("claimed_by", "TEXT", None),
        ("claimed_at", "TIMESTAMPTZ", None),
        ("visibility", "TEXT", "'private'"),
    ]:
        try:
            default_clause = f" DEFAULT {default}" if default else ""
            not_null = " NOT NULL" if default else ""
            db.execute(f"ALTER TABLE views ADD COLUMN {col} {typ}{not_null}{default_clause}")
        except Exception:
            pass  # column already exists
```

Column definitions:
- `view_type` — `'custom' | 'incident' | 'plan' | 'assessment'`. Default `'custom'` preserves existing user-created views.
- `status` — depends on view_type:
  - `custom`: `'active'` (no lifecycle)
  - `incident`: `'investigating' | 'action_taken' | 'verifying' | 'resolved' | 'archived'`
  - `plan`: `'analyzing' | 'ready' | 'executing' | 'completed'`
  - `assessment`: `'analyzing' | 'ready' | 'acknowledged'`
- `trigger_source` — `'user' | 'monitor' | 'agent'`. Note: `trigger` is a PostgreSQL reserved word, so we use `trigger_source`.
- `finding_id` — nullable, links to the monitor finding that created the view.
- `cluster_id` — empty string default, enables future multi-cluster support.
- `claimed_by` — nullable, username of admin currently working on this view.
- `claimed_at` — nullable, when the claim was made.
- `visibility` — `'private' | 'team'`. Default `'team'` for agent views (incident/plan/assessment), `'private'` for custom views. Team views are visible to all admins.

### 3.3 Agent Auto-Creation

Agent views are created in three scenarios:

**Incident views** — when the monitor detects a finding with severity in (`warning`, `error`, `critical`), or when the agent identifies a problem during investigation:

1. Agent calls `create_dashboard()` with:
   - `timeline` with correlated events
   - `metric_card` with relevant PromQL queries
   - `data_table` or `status_list` for affected resources
   - `confidence_badge` on the RCA section
   - `blast_radius` from dependency graph
   - `action_button` for recommended fixes
   - `resolution_tracker` with recovery steps
2. View created with `view_type: "incident"`, `trigger_source: "monitor"`, `status: "investigating"`

**Plan views** — when the user requests a capability change, upgrade, or multi-step operational task (e.g., "I need to support VMs for team B"):

1. Agent runs pre-check tools (check operators, node capabilities, storage, RBAC)
2. Calls `create_dashboard()` with:
   - `resolution_tracker` as a prerequisites checklist (done/pending per requirement)
   - `data_table` showing current state vs required state
   - `action_button` for each step (install operator, create namespace, configure storage)
   - `confidence_badge` on overall readiness assessment
   - `blast_radius` showing what the change affects
3. View created with `view_type: "plan"`, `trigger_source: "agent"`, `status: "analyzing"` → transitions to `"ready"` when pre-checks complete

**Assessment views** — when trend scanners detect a projected issue or the briefing surfaces a proactive item:

1. Agent calls `create_dashboard()` with:
   - `metric_card` with trend PromQL + `predict_linear` overlay
   - `timeline` showing when the trend started and projected breach
   - `resolution_tracker` with recommended preventive actions
   - `action_button` for preemptive fixes
2. View created with `view_type: "assessment"`, `trigger_source: "monitor"`, `status: "analyzing"`

**User-created plan views** — when a user adds a task via chat:

Two creation modes:

- **Quick add:** "add task: rotate TLS certs before Friday" → agent creates a minimal plan view (title + description, status `analyzing`, no pre-checks yet). Pre-checks run when someone opens it and the agent hydrates the view with actual cluster data.
- **Full plan:** "plan: rotate TLS certs" → agent does full pre-check analysis immediately, creates rich plan view with prerequisites checklist, steps, and action buttons.

Both create `view_type: "plan"`, `trigger_source: "user"`, `visibility: "team"`. All admins see it immediately.

**The shared task list is just a view filter:** `GET /views?view_type=plan&status!=completed&visibility=team` returns all open tasks across all admins. No separate task table, no Mission Board database — plan views ARE the tasks.

All four creation paths (incident, plan, assessment, user-created): chat message includes a "VIEW CREATED" card linking to the view.

### 3.4 Multi-User Collaboration

Agent views (incident, plan, assessment) default to `visibility: "team"` — all admins can see and interact with them. This prevents duplicate work and enables coordination.

**Claim mechanism:**
- When an admin opens an agent view and starts working (executes an action or explicitly claims), the view's `claimed_by` and `claimed_at` fields are updated.
- Other admins see in the view list: "Alex is working on this · 3 min ago"
- In the view header: "Claimed by Alex · 3 min ago"
- Not a hard lock — any admin can still act in an emergency. The claim is informational.
- Claims expire after 30 minutes of inactivity (no actions, no chat about this view).

**Action attribution:**
- Every action button execution records `executed_by` and `executed_at` in the view version.
- The resolution tracker shows who completed each step: "Step 1: ConfigMap patched by Alex at 14:36"
- The version history shows who made each change.

**Real-time sync:**
- When admin A claims a view or executes an action, all connected WebSocket clients receive a broadcast notification.
- Frontend updates the view list badges and view header in real-time.
- Uses existing monitor WebSocket channel — new event types: `view_claimed`, `view_action_executed`, `view_updated`.

**Visibility rules:**
- `custom` views: `private` by default (owner only). Can be shared via existing share token mechanism.
- `incident` / `plan` / `assessment` views: `team` by default (all admins). Owner field tracks who created it, but all admins can view and act.
- `system:monitor` views: `team` visibility, no owner needed.

**Conflict prevention:**
- When two admins have the same view open and one executes an action, the other sees a toast notification: "Alex just applied 'Revert CACHE_SIZE'. View updated."
- Action buttons that were already executed show as disabled with "Applied by Alex" label.
- The resolution tracker live-updates via WebSocket so both admins see the same state.

**Monitor view ownership:** Monitor-created views are owned by `"system:monitor"` (a reserved owner that cannot be impersonated by regular users). When a user clicks an action button in a monitor-created view, the action endpoint derives the user's trust level from the authenticated session, NOT from the view's owner or the monitor session that created it. This prevents low-trust users from executing high-trust actions just because the monitor created the view.

**Trust level on action buttons:** Add optional `min_trust_level: int` field to `action_button` component spec. When the monitor or agent creates action buttons, it sets the minimum trust level required to execute the action. The action endpoint enforces `max(button.min_trust_level, tool.required_trust_level) <= user.trust_level`.

### 3.4 Status Transitions

**Incident lifecycle:**
```
investigating → action_taken    (user clicks action button or approves fix in chat)
action_taken → verifying        (fix applied, agent starts verification checks)
verifying → resolved            (all verification checks pass)
verifying → investigating      (verification fails, agent re-investigates)
resolved → investigating       (finding recurs)
resolved → archived             (after 24h with no recurrence, or manually)
```

**Plan lifecycle:**
```
analyzing → ready               (all pre-checks complete, plan is actionable)
ready → executing               (user clicks an action button to start)
executing → ready               (step completes, next steps available)
executing → completed           (all steps done)
analyzing → ready               (if new info changes the plan, re-analyze)
```

**Assessment lifecycle:**
```
analyzing → ready               (trend analysis complete, recommendations available)
ready → acknowledged            (user reviews and acknowledges)
ready → investigating           (trend becomes an incident — transitions to incident lifecycle)
```

All lifecycles: status transitions create new view versions automatically. The `status_pipeline` component renders the appropriate pipeline based on `view_type`.

**Recurrence handling:** When the monitor detects a finding that matches a resolved view's `finding_id`, it transitions the existing view back to `investigating` (creating a new version) rather than creating a duplicate view. The timeline shows the recurrence as a new event.

**Assessment → Incident escalation:** When a trend assessment becomes an actual problem (e.g., predicted memory pressure becomes real node pressure), the view transitions from assessment lifecycle to incident lifecycle. `view_type` changes from `assessment` to `incident`, status resets to `investigating`, and the original trend data is preserved in the timeline.

### 3.4 Version Diffing

Add `GET /views/{view_id}/diff?from=1&to=2` endpoint:

- Compares two version snapshots
- Returns list of changes: added components, removed components, modified component props
- **Component matching:** Components are matched by `kind` + position index within the layout. If a component moves position, it shows as "moved" not "removed + added". Components with the same `kind` at the same position are compared prop-by-prop.
- Frontend renders as a simple diff view (what changed between versions)

### 3.5 View Update Flow

When the agent learns new information (e.g., user asks "what changed in v3.8.1?"):

1. Agent updates the view layout with new/modified components
2. `snapshot_view()` is called automatically (already exists)
3. Chat shows "VIEW UPDATED → v2" notification
4. Frontend refreshes the view

### 3.8 Historical Learning & Skill Graduation

When views reach terminal status (`resolved`, `completed`, `acknowledged`), the system extracts learnings and graduates them through three levels. Human-in-the-loop at every promotion — the agent proposes, the user approves.

#### 3.8.1 Similar Past Incidents

When creating an incident view, query resolved views for similar findings (text match on finding title + affected resource kind + namespace pattern):

```python
similar = db.query("""
    SELECT id, title, status, resolved_at FROM views
    WHERE view_type = 'incident' AND status = 'resolved'
    AND similarity(title, ?) > 0.6
    ORDER BY resolved_at DESC LIMIT 3
""", (new_finding_title,))
```

If matches found, add a "Similar Past Incidents" card to the Analysis tab:
- Previous occurrence title + date + link to resolved view
- What fix was applied + whether it worked
- Agent pre-selects the same fix as the recommended action

For plan views, query completed plans with similar titles: "You completed a similar plan 3 weeks ago — [View Previous Plan]"

~30 lines backend. Uses existing view data — no new tables.

#### 3.8.2 Learned Runbooks (Level 1 — automatic)

On every view resolution/completion, extract and store:
- Tool sequence from the resolution tracker (ordered list of tools + inputs that were executed)
- Finding pattern (resource kind, namespace pattern, severity, finding title keywords)
- Outcome (resolved successfully? how long did verification take?)

Store in existing memory system as a learned runbook. ~30 lines, extends existing `memory/` module.

**"Automate this" command:** User says "automate this fix in the future" in chat while viewing a resolved incident:
1. Agent extracts the fix sequence from the current view's resolution tracker
2. Creates a learned runbook with the finding pattern as the trigger condition
3. Registers it as a monitor auto-fix rule at the user's current trust level
4. Next occurrence: monitor detects → auto-fixes → creates incident view showing "Auto-resolved using learned fix from [date]. Verifying..." → verification watchlist runs → status transitions to resolved or escalates back

~50 lines. Connects resolution tracker data → learned runbooks → monitor auto-fix (all existing systems).

#### 3.8.3 Skill Plans (Level 2 — agent proposes)

Periodically (daily or on-demand), the agent compares recent resolved views for pattern similarity:
- Same investigation tool sequence across 3+ different resources/namespaces
- Example: 3 OOM incidents all followed: check limits → check config changes → check deploys → correlate → fix

When pattern detected, agent proposes in chat:
> "I've resolved 3 similar OOM incidents using the same investigation pattern. Want me to create a reusable plan template for OOM diagnostics?"

User approves → `skill_scaffolder.py` creates a plan template (generalized tool sequence with variable resource/namespace). Next time a similar incident occurs, the agent pre-populates the investigation view using the template.

~50 lines in `skill_scaffolder.py`. Pattern detection uses tool chain similarity (cosine similarity on tool sequence vectors from `tool_chains.py`).

#### 3.8.4 Full Skills (Level 3 — agent proposes)

When 3+ validated skill plans accumulate in the same domain (e.g., memory diagnostics, cert management, capacity planning):

Agent proposes:
> "I have 4 OOM-related plan templates that have been validated across 12 incidents. Want me to create a full OOM Diagnostics skill?"

User approves → `skill_scaffolder.py` generates:
- `skills/oom-diagnostics/skill.md` — system prompt, tool selection, investigation template
- `eval_scaffolder.py` generates eval scenarios from the resolved incidents
- ORCA selector learns to route memory-related queries to the new skill via `selector_learning.py`

Uses existing `skill_scaffolder.py` and `eval_scaffolder.py`. The new part is the trigger logic (~30 lines) that counts validated plans per domain.

#### Graduation Summary

| Level | What | Trigger | Approval | Existing Code |
|-------|------|---------|----------|---------------|
| Runbook | Specific fix for specific problem | View resolved/completed | Auto (or explicit "automate this") | `memory/`, monitor auto-fix |
| Skill Plan | Generalized investigation template | 3+ similar runbooks detected | Agent proposes, user approves | `skill_scaffolder.py` |
| Full Skill | Complete skill package + evals | 3+ validated plans in same domain | Agent proposes, user approves | `skill_scaffolder.py`, `eval_scaffolder.py`, `selector_learning.py` |

### 3.9 Auto-Fix Pipeline

**Remove blunt auto-fixes.** The current system deletes crashlooping pods and restarts failed deployments without understanding root cause. This undermines trust and masks real issues. Replace with a skill-based pipeline that always understands before acting, or asks the user if it doesn't.

#### 3.9.1 Fix Pipeline

```
Finding detected by monitor
    ↓
1. Learned runbook match? (confidence >= 0.85, success_rate >= 0.90)
    → Execute runbook tool sequence
    → Create incident view showing: "Auto-resolved using learned fix from [date]"
    → Run verification watchlist (5 scan cycles)
    → If verification fails → escalate to user (transition to investigating)
    ↓ No match
2. Skill plan match? (matching investigation template exists)
    → Run skill plan: pre-checks → root cause analysis → targeted fix
    → Create incident view with full analysis
    → Run verification watchlist
    → If verification fails → escalate to user
    ↓ No match
3. Full skill match? (ORCA routes to a relevant skill)
    → Route through skill for investigation + fix recommendation
    → Create incident view with RCA + action buttons
    → WAIT for user approval (do NOT auto-execute)
    ↓ No match
4. No known fix — investigate and present
    → Create incident view with RCA, evidence, blast radius
    → Action buttons for recommended fixes (user must approve)
    → Notify via chat: "New incident needs your attention"
    → Notify via WebSocket: active incident banner in CommandBar
```

**Key principle:** The agent only auto-executes fixes it has learned and validated (Level 1-2). For novel issues (Level 3-4), it always investigates and presents — the user decides. No more blunt "delete pod and hope."

#### 3.9.2 Dynamic Auto-Fix Budget

Replace the fixed daily cap (`max_daily_investigations`) with a dynamic budget based on track record:

```python
def calculate_autofix_budget() -> int:
    rate = get_fix_success_rate(period="30d")
    if rate >= 0.95:   return 20   # proven reliability → high autonomy
    if rate >= 0.85:   return 10   # good track record → moderate
    if rate >= 0.70:   return 5    # learning → conservative  
    return 2                        # new or struggling → minimal
```

**Per-pattern exemptions:** A runbook that has succeeded 10+ times with 100% success rate is exempt from the daily budget — it's a proven fix. Novel or low-confidence fixes always count against the budget.

**Self-regulating:** New agent starts at budget 2. As it proves itself through successful fixes, budget increases. If fixes start failing (rollbacks, escalations), budget automatically decreases. The agent earns trust over time.

**Config:** Dynamic budget is the only mode. `max_daily_investigations` config is removed. Budget is calculated from the 30-day success rate — starts at 0 for new deployments.

#### 3.9.3 Blunt Fix Removal

Remove from `monitor/session.py`:
- Delete crashlooping pod action (replace with: create incident view + investigate root cause)
- Restart failed deployment action (replace with: create incident view + investigate root cause)

These blunt actions become the agent's last resort ONLY if a user explicitly approves them via an action button in the incident view — never autonomous.

**Migration path:** Existing auto-fix rules in the database remain but are re-classified:
- Rules with matching learned runbooks → continue auto-executing via runbook pipeline
- Rules without learned runbooks → downgraded to "investigate and present" (Level 4)

#### 3.9.4 Zero-Start Budget

No separate shadow mode. The dynamic budget starts at 0 for new deployments — every fix requires user approval. This IS shadow mode, just simpler:

1. Agent detects issue → investigates → creates incident view with action buttons → user approves
2. Each successful user-approved fix builds the agent's track record
3. When 30-day success rate crosses 70%, budget increases to 2 (first autonomous fixes)
4. Budget continues scaling with success rate (70% → 2, 85% → 5, 95% → 10+)

The agent earns autonomy through demonstrated competence on this specific cluster with these specific workloads. No config toggle — just math.

#### 3.9.5 View Auto-Creation Safeguards

The agent auto-creation flow (Phase 3.3) will encounter known view designer pitfalls. These must be enforced in code, not prompts:

1. **Save with warnings, never block** — if validation finds issues, save the view and log warnings. Don't fail the creation.
2. **No `verify_query` calls during auto-creation** — causes Thanos/Prometheus overload (8-14 calls per view).
3. **Always include `time_range` on PromQL metric cards** — omitting it produces tables instead of charts. Default to `"1h"`.
4. **Clear `session_components` after each view save** — prevents component leakage between auto-created views.
5. **Fetch full view layout on merge** — `get_view_by_title` must return the full layout, not just id+title.
6. **Grid components need explicit titles** — backend must emit `title` field on all grid/section components.
7. **Chart containers need explicit pixel height** — `style.height: 300` not `flex-1` (react-grid-layout doesn't propagate height).

These are code-enforced in `view_tools.py` and `layout_engine.py`, not in system prompts.

### 3.10 Acceptance Criteria

- Monitor auto-creates incident views for severity in (warning, error, critical)
- Agent creates plan views when user requests capability planning or multi-step changes
- User can create plan views via "add task:" or "plan:" chat commands; all admins see them
- Trend scanners create assessment views for projected issues
- Status bar renders correctly for all three lifecycles
- Assessment → incident escalation works when trend becomes real problem
- Status transitions create new versions
- Multi-user: claim mechanism works, action attribution shows who did what, WebSocket sync across clients
- Version diff endpoint returns meaningful diffs
- Chat notifications for view creation and updates
- Resolution tracker updates in real-time as fix progresses
- Post-fix verification watchlist drives status transitions automatically
- Similar past incidents surfaced when creating new incident/plan views
- "Automate this" creates learned runbook and registers auto-fix rule
- Blunt auto-fixes removed — agent never deletes pods or restarts deployments autonomously
- Auto-fix pipeline: runbook match → skill plan → skill → investigate & present (never guess)
- Dynamic budget adjusts based on 30-day success rate
- Proven runbooks (10+ successes, 100% rate) exempt from daily budget
- End-to-end tests:
  - Incident: finding → view created → action taken → verified → resolved → runbook extracted
  - Plan: user request → pre-checks → ready → execute steps → completed
  - Assessment: trend detected → view created → acknowledged (or escalated to incident)
  - Task: "add task: X" → plan view created → visible to all admins → claimed → completed
  - Learned auto-fix: known pattern recurs → runbook executes → view created → verified → resolved
  - Novel issue: unknown pattern → view created with RCA + action buttons → user approves → verified
  - Failed auto-fix: runbook executes → verification fails → escalates to user → view status back to investigating

---

## Phase 4: Tabbed View Layout

**Goal:** Organize investigation views into tabs (Resolution, Analysis, Impact, Timeline) with a persistent hero header.

### 4.1 Agent View Layout Templates

Add layout templates to `layout_engine.py` that trigger automatically based on `view_type`. Regular user-created views (`view_type: "custom"`) continue to use the existing layout logic.

The existing `tabs` component type needs enhancement to support a persistent header above the tabs.

**Incident view layout:**
```
kind: section (hero header — always visible)
  children:
    - confidence_badge
    - status_pipeline (Detected → Investigated → Action Taken → Verifying → Resolved)
    - metric_card (inline strip: memory, restarts, pods, latency)

kind: tabs
  children:
    - tab "Resolution": resolution_tracker + action_button (fallbacks)
    - tab "Analysis": key_value (root cause) + data_table (affected pods)
    - tab "Impact": blast_radius
    - tab "Timeline": timeline
```

**Plan view layout:**
```
kind: section (hero header)
  children:
    - confidence_badge (overall readiness %)
    - status_pipeline (Analyzing → Ready → Executing → Completed)
    - metric_card (inline strip: prerequisites met, steps remaining, estimated effort)

kind: tabs
  children:
    - tab "Prerequisites": resolution_tracker (checklist of what's ready/missing)
    - tab "Steps": resolution_tracker (ordered execution steps) + action_button per step
    - tab "Impact": blast_radius (what this change affects)
    - tab "Current State": data_table + metric_card (relevant cluster state)
```

**Assessment view layout:**
```
kind: section (hero header)
  children:
    - confidence_badge (prediction confidence)
    - status_pipeline (Analyzing → Ready → Acknowledged)
    - metric_card (inline strip: current value, predicted value, time to breach)

kind: tabs
  children:
    - tab "Trend": metric_card with predict_linear overlay + timeline
    - tab "Recommendations": resolution_tracker + action_button (preventive actions)
    - tab "Impact": blast_radius (what will be affected if trend continues)
```

### 4.2 Status Pipeline Component

Simple horizontal status display:

```
kind: status_pipeline
props:
  steps: list[str]           — ["Detected", "Investigated", "Action Taken", "Verifying", "Resolved"]
  current: int               — index of current step (0-based)
```

### 4.3 Hero Metrics Strip

Use existing `metric_card` components in a horizontal `grid` with `columns: 6` to create the metrics strip (Memory, Restarts, Pods, p99 Latency, Namespace, Trigger).

### 4.4 Acceptance Criteria

- Investigation views render with hero header + tabs
- Hero header stays visible when switching tabs
- Impact tab shows badge count for degraded resources
- Layout engine positions hero above tabs correctly
- Manual browser verification of all 4 tabs

---

## Phase 5: Performance Testing & Coverage Gates

**Goal:** Know how fast the system is and enforce test quality.

### 5.1 Performance Test Suite

New directory: `tests/perf/`

**What to measure:**

| Metric | How | Target |
|--------|-----|--------|
| Agent response p95 | Replay eval scenarios, measure wall-clock time | < 15s for single-tool, < 30s for multi-tool |
| Tool execution p95 | Instrument `@beta_tool` decorator with timing | < 2s per tool call |
| WebSocket connect-to-first-message | Load test with N concurrent connections | < 500ms |
| Monitor scan cycle time | Instrument `MonitorSession._scan()` | < 60s (must finish within interval) |
| View creation latency | Time `create_dashboard()` end-to-end | < 1s |

**Implementation:**
- Add timing instrumentation to `decorators.py` (`@beta_tool` wrapper records execution time)
- Add `tests/perf/test_tool_latency.py` — runs each tool with mock K8s data, asserts < 2s
- Add `tests/perf/test_scan_cycle.py` — runs full scan with mock clients, asserts < 60s
- Add `tests/perf/test_view_creation.py` — creates investigation view, asserts < 1s
- CI runs perf tests on every PR; failures block merge

### 5.2 Coverage Gates

- Add `pytest-cov` to dev dependencies
- Do NOT add coverage to `pyproject.toml` `addopts` (slows every test run including single-file developer iterations)
- Add `make coverage` target:
  ```makefile
  coverage:
  	python3 -m pytest tests/ --cov=sre_agent --cov-fail-under=80 --cov-report=term-missing
  ```
- CI runs `make coverage` (not `make test`) to enforce the gate
- New code must have coverage (no `# pragma: no cover` without justification)

### 5.2.1 Performance Baseline Storage

- Perf baselines stored in `tests/perf/baselines/` as JSON (same pattern as `evals/baselines/`)
- `make perf-baseline` saves current measurements
- `make perf-check` compares current vs baseline, fails on > 20% regression
- Baselines committed to git and updated on each release

### 5.3 WebSocket Contract Tests

- New file: `tests/test_ws_contract.py`
- Validates all message types from `API_CONTRACT.md` are handled
- Validates message schemas match documented format
- Validates message sequences (e.g., `confirm_request` must be followed by `confirm_response` before next tool use)
- Runs against the FastAPI test client (no live server needed)

### 5.4 Acceptance Criteria

- `tests/perf/` directory with latency tests for tools, scan, view creation
- CI blocks on perf regression (> 20% slower than baseline)
- Coverage gate at 80% enforced in CI
- WebSocket contract tests cover all documented message types
- `make verify` includes perf + coverage checks

---

## Phase 6: Operational Metrics & Improvement Tracking

**Goal:** Answer "is the system getting better?" with data, not vibes.

### 6.1 Three Key Metrics

Track per release:

1. **Eval gate score** — already exists (99.6% release suite). Continue tracking. Add trend sparkline to eval history endpoint.

2. **Auto-fix success rate** — ratio of fixes that resolved the finding vs fixes that needed rollback or escalation.
   - Data source: `actions` table has `status`, `rollback_available`, `rollback_action` but no outcome tracking
   - **New migration** (part of Phase 6): add `outcome TEXT NOT NULL DEFAULT 'unknown'` column to `actions` table. Values: `resolved | rolled_back | escalated | unknown`
   - Outcome is set automatically: `resolved` when linked finding resolves, `rolled_back` when rollback action is executed
   - New endpoint: `GET /metrics/fix-success-rate?period=30d`

3. **Agent response p95 latency** — from the perf instrumentation in Phase 5.
   - Record per-request latency in `tool_usage` table (already fire-and-forget)
   - New endpoint: `GET /metrics/response-latency?period=30d`

### 6.2 Release Dashboard

Add a release health view (static, not investigation) that shows:
- Eval score trend (last 10 releases)
- Auto-fix success rate trend
- Response latency trend
- Test count + coverage trend
- Active findings count trend

This view is created once and updated by CI on each release.

### 6.3 Eval Baseline Enforcement

- Every release must run `python -m sre_agent.evals.cli --suite release --compare-baseline`
- Regression > 2% blocks the release
- New baseline saved automatically when release passes

### 6.4 Acceptance Criteria

- Auto-fix outcome tracking in actions table
- Three metric endpoints returning trend data
- Release dashboard view with sparkline trends
- CI enforces eval baseline comparison on release
- `make release` fails if eval gate regresses > 2%

---

## Cross-Cutting Requirements (All Phases)

These apply to every phase, not just one:

### Eval Scenarios Per Phase

Major features need eval scenarios + replay fixtures, not just unit tests. Each phase must add:
- Deterministic eval scenarios for new tools/endpoints (in appropriate eval suite)
- Replay fixtures for key workflows (agent creates view, agent auto-fixes, etc.)
- LLM-judged scenarios where correctness requires reasoning assessment
- Run `python -m sre_agent.evals.cli --suite release --fail-on-gate` before merging each phase

### Doc Updates Per Phase

Every code change must update relevant documentation:
- `CLAUDE.md` — new tools, config vars, architecture changes
- `API_CONTRACT.md` — new endpoints, message types, WebSocket events
- `SECURITY.md` — new attack surfaces, trust level changes, auth flows
- `README.md` — user-facing feature descriptions
- `TESTING.md` — new test patterns, eval suites

Phase acceptance criteria are not met until docs are updated.

### Design Principle Alignment

Every feature must align with the 10 design principles (`DESIGN_PRINCIPLES.md`). Key checkpoints:
- Confidence scores visible on all agent outputs
- Plain-English explanations (no jargon-only displays)
- Human-in-the-loop for anything that matters (destructive actions, novel fixes)
- Rollback available for every action
- Audit trail for every agent decision

---

## Execution Order

```
Phase 1 (Cleanup)  →  Phase 2 (Components)  →  Phase 3 (Lifecycle)  →
Phase 4 (Tabs)     →  Phase 5 (Perf/Coverage) → Phase 6 (Metrics)
```

**Rationale:** Cleanup first (clean foundation). Components second (building blocks). Lifecycle third (wiring it together). Tabs fourth (polish). Perf and metrics last (measuring what we built).

Each phase is a hard cutover — fully tested, fully deployed, no feature flags.

---

## Frontend Integration Model

Investigation views are NOT new views — they are `CustomView` instances at `/custom/<viewId>` rendered by the existing `AgentComponentRenderer` + `react-grid-layout`. No new view components, routes, or layouts are needed. The work is connecting existing views with links and data.

### Existing Views (no changes to structure)

| View | Role in Investigation Workflow |
|------|-------------------------------|
| `IncidentCenterView` (`/incidents`) | Entry point — shows active findings, links to investigation views |
| `IncidentLifecycleDrawer` | Quick lifecycle summary (7 stages) — links TO investigation views |
| `CustomView` (`/custom/<viewId>`) | The investigation view itself — tabbed layout with new components |
| `AISidebar` | Chat panel — shows VIEW CREATED / VIEW UPDATED notifications |
| `MorningSummaryCard` | Morning briefing — enhanced with live data + investigate actions |
| `NowTab` | Active findings list — shows "View Investigation" badge for findings with views |

### Drawer ↔ Investigation View Relationship

These are complementary, not overlapping:
- **Drawer** = quick glance at lifecycle status (which of the 7 stages is this finding in?)
- **Investigation view** = full workspace (metrics, blast radius, action buttons, resolution tracking)

The drawer gets an "Open Investigation" button that navigates to `/custom/<viewId>`. If no investigation view exists yet, the button reads "Create Investigation" and triggers the auto-creation flow (Phase 3.2).

### Frontend Changes by Phase

**Phase 2 (Components):**
- Add 4 new renderers to `AgentComponentRenderer.tsx` switch statement: `action_button`, `confidence_badge`, `resolution_tracker`, `blast_radius`
- Action button renderer uses existing `ConfirmDialog` component for write tools

**Phase 3 (Lifecycle):**
- `NowTab`: query `GET /views?status=investigating` and show "View Investigation" badge on findings that have linked views
- `IncidentLifecycleDrawer`: add "Open Investigation" / "Create Investigation" button
- `CommandBar`: on app load, if `GET /views?status=investigating&severity=critical` returns results, show banner: "CRITICAL: [title] — [Open Investigation]"
- `AISidebar` `DashboardMode`: add VIEW CREATED / VIEW UPDATED card type linking to `/custom/<viewId>`
- `monitorStore`: add `investigationViews` state populated from new endpoint

**Phase 4 (Tabs):**
- Add `status_pipeline` renderer to `AgentComponentRenderer.tsx`
- Investigation layout template auto-applies to views with `status` in (`investigating`, `action_taken`, `verifying`, `resolved`)

---

## Workflow Support

### Workflow 1: Reactive Incident Response

**User journey:** PagerDuty pages at 3am → user opens Pulse → sees critical incident banner in CommandBar → clicks → investigation view opens in main content with AISidebar chat alongside → user sees status pipeline (Verifying), live metrics, blast radius → clicks "Approve" on recommended fix → resolution tracker shows progress → verification passes → status transitions to Resolved → user goes back to sleep.

**Next morning colleague catch-up:** Opens `/incidents` → sees resolved investigation in Activity tab → clicks → IncidentLifecycleDrawer shows full 7-stage lifecycle → clicks "Open Investigation" → sees versioned investigation view with diff showing how analysis evolved.

**Post-fix verification (automated):**
When an action button is executed, the monitor session adds the affected resources to a verification watchlist:
- Backend: add `verification_watchlist: list[dict]` to `MonitorSession`
- On action execution, add `{resource_kind, resource_name, namespace, finding_id, checks_remaining: 5}`
- Each scan cycle (60s), check watchlisted resources specifically
- If finding recurs, transition view status back to `investigating`, create new version
- If 5 consecutive clean checks pass, transition to `resolved`, create new version
- Chat notification: "Fix verified — payment-api stable for 5 minutes. Status: Resolved."

### Workflow 2: Intent-Driven Task Execution

**The model:** User shares intent → agent breaks it into tasks (plan views) → all admins see the tasks → team divides and conquers.

**User journey:**
1. Admin A types: "This week: upgrade ingress to v1.9, set up GitOps for team B, tune HPA for frontend, rotate TLS certs"
2. Agent creates 4 plan views (one per task), each with pre-checks, steps, and action buttons. All `visibility: "team"`.
3. Chat shows 4 "VIEW CREATED" cards. All admins see 4 new tasks in their view list.
4. Admin A claims "upgrade ingress" and starts working through the steps.
5. Admin B sees 3 unclaimed tasks, claims "rotate TLS certs", executes the steps.
6. Both see each other's progress in real-time. Resolution trackers update via WebSocket.
7. Admin A finishes ingress upgrade → status transitions to `completed`. 3 tasks remaining.

**Quick add vs full plan:**
- "add task: rotate TLS certs before Friday" → minimal plan view (title + description, status `analyzing`). Pre-checks run when someone opens it.
- "plan: upgrade ingress to v1.9" → full pre-check analysis immediately, rich plan view with prerequisites, steps, action buttons.
- "This week: task1, task2, task3" → agent creates multiple plan views, one per task, with suggested execution order in each view's description.

**Shared task list:**
- `GET /views?view_type=plan&status!=completed&visibility=team` — returns all open tasks across all admins.
- Frontend: new filter in IncidentCenterView or a "Tasks" tab showing plan views sorted by priority.
- No separate task table, no Mission Board database — plan views ARE the tasks.

**Implementation (Phase 3):**
- Agent creates plan views via existing `create_dashboard()` — no new tool needed. Claude parses the multi-task intent naturally and calls `create_dashboard()` once per task.
- View list endpoint gains filters: `view_type`, `status`, `visibility`, `claimed_by`
- WebSocket broadcasts for `view_claimed`, `view_action_executed` keep all clients in sync

### Workflow 3: Proactive Agent Intelligence

**User journey:** User opens Pulse Monday morning → `MorningSummaryCard` shows enhanced briefing with live cluster state + priority items → items sorted by urgency: "FIX NOW: 2 certs expiring in 3 days" / "WATCH: node worker-5 memory trending up" / "FYI: 3 deploys rolled out overnight, all healthy" → user clicks "Investigate" on the cert item → investigation view auto-created → user clicks "Rotate Certs" action button → resolution tracked.

**Backend changes (Phase 1 — data-correctness fix):**

Enhance `get_briefing()` in `monitor/actions.py`:
```python
# Add to existing briefing response:
current_findings = []  # run 4 fastest scanners: crashloop, pending, oom, firing_alerts
priority_items = []    # sorted by severity * blast_radius * recurrence
recent_changes = []    # from audit scanners (deployments, RBAC, config changes)
recurrences = []       # findings matching recently-resolved action finding_ids
```

~100 lines of backend code. No new views — `MorningSummaryCard` renders the additional data.

**Backend changes (Phase 3):**

- `POST /findings/{finding_id}/ack` — persists acknowledgment in DB (currently client-only Zustand)
- `POST /findings/{finding_id}/investigate` — creates investigation view on demand, returns view_id
- Frontend: `NowTab` finding cards get "Ack" and "Investigate" action buttons

---

## What Stays Unchanged

- All 99 native tools + 36 MCP tools
- 73 PromQL recipes
- 18 scanners
- 7 skills + ORCA selector
- `agent.py` `run_agent_streaming()` loop
- Memory system
- Circuit breaker, confirmation gates, trust levels
- Monitor polling loop (enhanced with status lifecycle, not replaced)
- Existing 25 component types (4 new ones added)
- Existing view versioning system (enhanced with diffing)

---

## Decisions Log

| Decision | Chosen | Rejected | Why |
|----------|--------|----------|-----|
| Feature flags | Hard cutover | Feature flags | User preference; forces each phase to be complete |
| Surfaces UX | Custom views side-by-side with chat | Embedded surfaces with own chat | Avoids second AI channel; reuses existing view system |
| Action confirmation | Click for reads; confirm dialog for writes | Click-only for all / Nonce for all | Balances safety (write tools need confirmation per trust level) with speed (reads are instant). Reviewers flagged click-only as bypassing safety architecture. |
| Event processing | Python K8s watches | Go + NATS JetStream | Avoids new language/infra for single-cluster system |
| Similarity search | Existing TF-IDF + incident memory | Voyage AI + pgvector | Prove need before adding external dependency |
| View organization | Tabbed (Resolution/Analysis/Impact/Timeline) | Flat grid | Better information hierarchy; user confirmed |
| Plan scope | 6 phases | 11 phases | Cut premature optimization and scope creep |
| Coverage gate | 80% minimum | No gate | Enforced in CI; new code must be covered |
| Perf gate | 20% regression blocks merge | No gate | Prevents silent degradation |
| Agent views | 3 types (incident/plan/assessment) on CustomView | Single investigation type / New view components | Same infrastructure, different layouts and lifecycles. Covers reactive + proactive + planning. |
| Drawer vs view | Complementary (drawer=summary, view=workspace) | Replace drawer / Embed view in drawer | Drawer links TO agent view. Different purposes. |
| Task planning | Agent creates plan views with pre-checks | Full Mission Board / Chat-only | Plan views give structure; agent does pre-work using existing tools. Not a task management system. |
| Morning briefing | Enhance existing MorningSummaryCard + get_briefing() | New briefing view | ~100 lines backend. Existing card renders richer data. |
| Post-fix verification | Automated watchlist in MonitorSession (5 scan cycles) | Manual verification / No verification | Drives status transitions automatically. Enables "fix it and go back to sleep." |
| Trend anticipation | predict_linear scanners in existing monitor loop | ML prediction engine / External service | 4 scanners, ~200 lines. Uses Prometheus built-in forecasting. |
