# Changelog

All notable changes to Pulse Agent are documented in this file.

## [Unreleased]

### Liveness monitoring

Every existing scanner measured the *health of state* — is this pod crashing, is this deployment short of replicas. None measured the *liveness of a process* — should this have finished by now. That gap is why a Kuadrant CRD finalizer hammered the API server for four months without producing a single finding: a stuck finalizer leaves no crashing pod, no degraded deployment and no firing alert, so all 23 scanners saw a healthy cluster.

- `stuck` scanner — resources whose deletion was requested but never completed: namespaces terminating past 15 minutes, pods past their grace period, PVCs, and CRDs mid-deletion. Namespace findings carry the API server's own `NamespaceContentRemaining` / `NamespaceFinalizersRemaining` messages, which name the exact resource types and finalizers holding the deletion open. Unlike the health scanners this one does not skip `openshift-*` and `kube-*`, because a wedged system namespace is precisely the case the others hide
- `hot_loop` scanner — the same failure seen from the symptom side, so a loop with any other cause is still caught: sustained work-queue retries (`workqueue_retries_total`), write amplification against one resource kind (`apiserver_request_total`), and pods retrying failing API calls (`rest_client_requests_total`). Thresholds are calibrated against a healthy production cluster, where the busiest legitimate controller sustains ~10 retries/s and the busiest non-lease write ~0.7/s; the scanner fires at 20/s and 5/s respectively and returns nothing on that cluster
- `diagnose_stuck_deletion` tool — explains why a deletion has not completed (finalizers, owner references, remaining namespace content). A read, because the diagnosis is mechanical and safe to automate
- `remove_finalizer` tool — confirmation-gated, and refuses more than `kubectl patch` would: it will not touch an object that is not already being deleted, will not force control-plane finalizers such as `pvc-protection`, and will not clear a namespace's `spec.finalizers` while the API server still reports content inside it, which is the case that orphans every object in the namespace
- Both categories are investigable by default (`stuck`, `hot_loop` added to `PULSE_AGENT_INVESTIGATION_CATEGORIES`)

### Fixes
- `describe_resource`, `explain_resource` and `list_api_resources` were dead. All three passed `response_type="object"` to `ApiClient.call_api()`, a kwarg the Kubernetes client removed several major versions ago, so every call raised `TypeError` — and every call site caught it and returned its own "Error fetching ..." string. Verified against a live cluster: the generic describe tool returned an error for every resource kind, including every CRD. They now share one `get_raw_json()` helper, and a test sweeps the source for `call_api()` keywords the installed client cannot accept
- The `describe_resource` tests stubbed `call_api` to return a bare dict, a shape the real client never produces, so they passed against code that could not work. They now stub the wire helper and assert on rendering, with the wire contract tested separately

## [2.9.0] - 2026-08-19

### Signal quality
- Simultaneous pod restarts are grouped into one finding instead of one per pod. Measured on a live cluster, 75 crashloop findings became 19: 39 of the 75 shared just two moments, so they were one event reported 39 times. Burst items carry every affected pod and are raised to critical — a correlated restart across namespaces matters more than any single pod in it
- Inbox ranking now favours novelty. `age_bonus` alone meant a week-old warning outranked one raised ten minutes ago, so permanent conditions ("1 cluster-admin binding to review") drifted to the top and stayed. The new bonus decays to zero over 24h, after which ranking behaves as before
- Conditions can be muted by `correlation_key` (`POST /inbox/mute`), with an optional expiry and a required reason. Muted conditions are dropped at creation rather than filtered on read, so they cannot resurface, be counted, or trigger an investigation

### Fixes
- Inbox items showed pod names that had been deleted days earlier: `resources` were re-pointed at the current pod on every scan while `title` and `summary` were frozen at creation
- Correlation keys omitted the namespace (`crashloop::Pod/name`), so same-named workloads in different namespaces shared one item
- Every inbox-triggered investigation was discarded on save — the report had no `id` and the column is `NOT NULL`, so the model was paid for work that was then thrown away
- The stale-findings digest counted and listed itself once it was 72h old
- `/metrics/*` REST routes were shadowed by the Prometheus scrape mount and had been unreachable
- Investigation view plans were dropped by the inbox (`viewPlan` read where the producer emits `view_plan`)

### Security
- The agent could rewrite its own system prompt with no confirmation: the four skill-mutation tools were registered as reads, so they never reached the confirmation gate
- `/admin/skills` mutation required only the shared token — now needs a real authenticated user, with an optional `PULSE_AGENT_ADMIN_USERS` allowlist. `PUT` can no longer overwrite built-in skills, which `DELETE` already refused
- `PULSE_AGENT_DEV_USER` could mask a real authenticated identity; it is now a fallback for the no-proxy case only
- The auto-fix kill switch never reached the monitor loop (a by-value import), and once fixed still reset to armed on every restart. It is now persisted and fails closed

### CI
- The test suite runs again. It had not passed since 2026-06-26 — every scheduled and push run failed for seven weeks. The cause was tests building a real Anthropic client with no API key, which hung until the job cap
- Scheduled failures now open or update a GitHub issue, so an unattended run that breaks is not silent
- Two bug classes are mechanically blocked: tests that skip instead of failing, and `from mod import FLAG` where the module rebinds `FLAG` (the shape behind the inert kill switch)
- `register_tool`'s `is_write` is now required, so a tool cannot be added without deciding whether it needs confirmation

## [2.8.0] - 2026-08-07

Backfilled — this release shipped without a changelog entry.

### Features
- Intelligence bridge: component spec normalization and validation
- Targeted async DB migration for tool usage, monitor repo and REST endpoints
- `current_user` included in the `/inbox` list response

### Fixes
- Needs-attention exclusion narrowed to fully-closed statuses
- Component spec field names standardized across tools
- Source filter uses `created_by`; claim rejects terminal states
- Async DB fallbacks narrowed to specific errors, preventing fire-and-forget task GC
- Auto-detection uses the RHACM hub for multi-cluster rather than Submariner

## [2.7.1] - 2026-05-20

### Security
- Default `max_trust_level` lowered from 3 to 2 — autonomous auto-fix now requires explicit server-side opt-in via `PULSE_AGENT_MAX_TRUST_LEVEL=3`

### Fixes
- fix: add `threading.Lock` to `CircuitBreaker` — prevents race conditions when tools run in `ThreadPoolExecutor`
- fix: add `db.transaction()` context manager — prevents connection pool leaks when `commit()` is not called
- fix: `/health` endpoint now reports database connectivity (`"database": "ok"|"unavailable"`) and returns `"degraded"` when PG is down
- fix: graceful `ClusterMonitor` shutdown on SIGTERM — sets `running=False` and cancels pending investigation tasks

### Observability
- `COST_BUDGET_REMAINING_USD` Prometheus gauge for real-time cost budget alerting
- `COST_BUDGET_EXHAUSTION_TOTAL` counter for budget exhaustion events
- Wired cost budget metrics into monitor investigation gate

### Docs
- Updated all README badges (v2.7.1, 154 tools, 23 scanners, 2372 tests, 16 suites/192 scenarios, 99.6% gate, 83 PromQL)
- Fixed CLAUDE.md counts (118 native tools, 23 scanners with corrected breakdown, removed phantom eval prompt claim)
- Updated API_CONTRACT.md `/health` response schema with `database` field
- Updated GitHub Pages site with current stats, trust level description, and PulseSRE org links
- Migrated all links from `alimobrem/*` to `PulseSRE/*` across README, SECURITY.md, JOURNEY.md, index.html

### Tests
- 9 new tests: CircuitBreaker thread safety (3), trust level defaults (2), cost budget metrics (3), /health database field (1)

## [2.7.0] - 2026-05-12

### Features
- Prometheus `/metrics` endpoint — token usage, cost, investigations, scanner runs, autofix outcomes as counters/gauges
- `GET /analytics/budget` — real-time investigation budget (used/remaining) and optional cost budget status
- 30-day cost forecast in `/analytics/cost` based on 7-day daily token totals
- Optional daily dollar-amount budget enforcement (`PULSE_AGENT_COST_BUDGET_USD`) pauses investigations when exceeded
- ServiceMonitor Helm template for Prometheus Operator scraping
- `observability.py` — centralized Prometheus metrics registry with `record_token_metrics()` helper

### Fixes
- fix: exclude resolved items from Needs Attention list and count
- fix: atomic claim, trend degraded finding, MCP shutdown race
- fix: inbox dedup — reopen recently-resolved items instead of creating duplicates
- fix: auto-resolve inbox items when all referenced resources are gone
- fix: inbox resolution falls back to correlation_key when finding_id misses
- fix: MCP toolset 'observability' → 'metrics' + 'openshift'
- fix: disconnect_all unregisters tools, clear _mcp_shutdown on restart

### Tests
- 12 new observability tests (metric registration, counter increments, gauge operations, label cardinality)
- Total: 2372 backend tests, 2021 frontend tests

## [2.4.0] - 2026-04-17

### Features
- Multi-datasource live tables — K8s watches + PromQL metrics + log enrichment
- All K8s table tools emit datasources for live rendering
- ResourceTable shared component — unified rendering for live and static tables
- Chart editor modal — edit PromQL, title, axes, legend, thresholds, time range
- Chart threshold lines — warning (amber) and critical (red) reference lines
- Cross-chart hover synchronization in custom views
- Global time range selector for custom views (1h/6h/24h/3d/7d)
- Persist chart customizations to saved views
- Inline row actions — open detail, YAML, logs, delete with confirmation
- Column auto-detect for resources without enhancers
- Plans drawer (matches skills pattern)
- Topology graph component with Add to View
- Clickable component cards with detail drawer
- ORCA hard pre-route rules (55/55 routing accuracy)
- Release skill for coordinated dual-repo releases
- Chaos test WebSocket client + topology health overlay

### Fixes
- ORCA routing: hard pre-route before typo correction
- API group resolution for plural resource names
- Namespace "ALL" normalized for frontend watches
- optimize_view saves both layout and positions
- Dynamic table heights based on row count
- view_designer requires_tools includes editing tools
- tool_sequence crash in MemoryView
- K8s API proxy uses SA token

### Tests
- 43 new tests (topology, live table, useMultiSourceTable, ResourceTable, layout)
- 55 routing eval scenarios (100% pass)

## [2.3.0] - 2026-04-14

### ORCA UI Surfaces
- **Postmortems tab** — Auto-generated postmortem reports with timeline, root cause, blast radius, and prevention recommendations in the Incident Center.
- **Topology view** — Dependency graph visualization with blast radius analysis at `/topology` (Impact Analysis).
- **Plans tab** — View, edit, and delete investigation plan templates from the Toolbox Skills section.
- **SLOs tab** — CRUD for SLO definitions with live Prometheus burn-rate queries (`GET /slo`, `POST /slo`, `DELETE /slo/{service}/{slo_type}`).

### Analytics Restructured
- **Agent Intelligence section** — Unified analytics view with routing decisions, fix strategies, and learning feed.
- **Fix strategy effectiveness** — `GET /analytics/fix-strategies` shows per-category+tool success rates.
- **Learning feed** — `GET /analytics/learning` surfaces weight updates, scaffolded skills, and routing decisions.

### Unified Routing
- **Orchestrator delegates to ORCA selector** — ~200 lines of duplicate keyword routing removed from `orchestrator.py`. The ORCA 5-channel selector is now the single routing authority.

### Unified Layout Engine
- **Backend-authoritative layout** — Layout engine now owns all positioning with optional frontend hint support. Eliminates layout drift between backend generation and frontend rendering.

### Plan CRUD
- **Plan template management** — `PUT /plan-templates/{type}` and `DELETE /plan-templates/{type}` endpoints for editing and deleting plan templates from the UI.

### SLO Management
- **SLO registry** — `slo_registry.py` provides CRUD operations with live Prometheus burn-rate integration. Persisted to `slo_definitions` table (migration 016).

### Skill Enrichment
- **All skills have trigger_patterns, tool_sequences, investigation_framework** — Every built-in skill now declares regex trigger patterns, named tool sequences for phased execution, and structured investigation methodology.

### Live Investigation Phases
- **`investigation_progress` WebSocket event** — Real-time phase updates during multi-phase investigations. Each phase reports status (pending/running/complete/failed/skipped), skill name, summary, and confidence.

### Deploy Risk Badges
- **Change risk scoring** — `change_risk.py` correlates recent changes with incidents. Findings display deploy risk badges in the UI.

### Skill Badges
- **Tool catalog badges** — Tools in the catalog show which skill(s) they belong to.

### Node Dedup in Dependency Graph
- **Topology deduplication** — Duplicate nodes in the dependency graph are merged, reducing visual noise in large clusters.

### Tool Renames
- **`describe_agent` / `describe_tools`** — Self-description tools renamed for clarity (previously `self_describe` / `self_describe_tools`).

### Code Review Fixes
- **Crash bug** — Fixed null pointer in plan runtime when investigation has no phases.
- **SQL precedence** — Fixed operator precedence in selector learning weight query.
- **Duplicate computation** — Eliminated redundant burn-rate calculation in SLO status endpoint.
- **O(n^2) BFS** — Fixed quadratic performance in dependency graph traversal.

### New Key Files
- `slo_registry.py` — SLO definition CRUD with live Prometheus burn rates
- `change_risk.py` — Deploy risk scoring for findings
- `plan_runtime.py` — Phased investigation plan execution engine
- `skill_scaffolder.py` — AI-generated skill packages from usage patterns
- `selector_learning.py` — ORCA selector weight learning from feedback signals

## v2.2.0 (2026-04-12)

### Adaptive Tool Selection Engine
- **TF-IDF Prediction** — Learns which tools are relevant for each query from real usage. Tokenizes queries into unigrams + bigrams, scores against `tool_predictions` table, returns top-K tools. Zero cost, sub-millisecond.
- **LLM Fallback** — When TF-IDF confidence is low (cold start), Haiku picks tools from names only (~200 tokens). Selections feed back into TF-IDF dictionary, making the LLM path self-eliminating.
- **Co-occurrence Bundles** — Tracks tools called together in the same turn (`tool_cooccurrence` table). When a tool is predicted, its co-occurring tools are automatically included.
- **Negative Signals** — Tools offered but never called get `miss_count` increments, actively suppressing wasted tools via `score - miss_count * 0.3`.
- **Mid-Turn Chain Expansion** — After each tool call, chain bigrams and co-occurrence data dynamically add predicted next-tools to the available set.
- **Daily Score Decay** — Scores multiplied by 0.95 daily, entries not seen in 30 days pruned. Prevents stale patterns from dominating.
- **ALWAYS_INCLUDE trimmed** — Reduced from 12 to 5 essential tools (list_pods, get_events, namespace_summary, record_audit_entry, list_my_skills).
- **Minimum set size** — Enforces at least 8 tools per query, padding from category fallback if needed.
- **New tables** — `tool_predictions` and `tool_cooccurrence` (migration 012).

### Pre-Route Handoff
- **Skill classifier checks handoff rules during routing** — If the keyword winner's `handoff_to` rules match the query, routes directly to the handoff target instead of routing to the winner first. Fixes queries like "create a capacity planning dashboard" routing to capacity_planner instead of view_designer.

### Type Safety
- **Typed `beta_tool` wrapper** — Created `sre_agent/decorators.py` with a properly-typed wrapper around `anthropic.beta_tool`. All 21 tool files import from `decorators` instead of `anthropic` directly. Single `type: ignore` in one file replaces 40+ across the codebase.
- **ToolLike Protocol** — Added `ToolLike` protocol to `tool_registry.py` for proper typing of tool object collections.
- **Mypy clean** — 0 errors across 115 source files with proper type annotations (no `type: ignore` in tool files).
- **Ruff clean** — 0 lint errors.

### Eval Improvements
- **Error suite fixed** — `completed: true` for error-handling scenarios, `should_block_release: false` for all 5 error scenarios.
- **Adversarial suite fixed** — `adversarial_resource_exhaustion` changed to `should_block_release: false` (agent correctly mitigated, not refused).
- **Synonym expansion** — Added "forbidden"/"exceeded" as synonyms for "quota" in replay scoring.
- **Error display** — Replay CLI now shows error messages in text output instead of silently swallowing exceptions.
- **All 9 eval suites pass** — 70/70 scenarios green.

## v2.1.0 (2026-04-12)

- Vertex AI cost analytics endpoint
- Coverage percentage returns 0-100 not 0-1
- Analytics router prefix fix
- Context bus timestamp test stabilization

## v2.0.0 (2026-04-12)

### Extensible Skill System
- Skill packages: drop-in .md files with routing, tools, evals, hot reload
- 6 skills: SRE, Security, View Designer, Capacity Planner + user-created
- Create/edit/delete/clone skills through chat or Toolbox UI
- Skill name routing (2x weight), keyword scoring, LLM fallback (haiku)
- User-created skills persist on PVC across restarts

### MCP Integration
- OpenShift MCP server (11 toolsets, 36 tools)
- SSE transport, prompt discovery, 3-tier rendering
- Toolset toggle from UI with crashloop detection
- Table parser for kubectl-style output

### Agent Intelligence
- Intent analysis prefix (think-before-acting)
- Dynamic prompt builder (centralized assembly)
- Skill-aware component hints (data-driven from registry)
- Edit-distance typo correction (catches novel misspellings)
- Synonym-based eval scoring
- Lazy skill validation (no false degradation at startup)
- ALWAYS_INCLUDE trimmed 23→12 (self-describe tools conditional)
- Runbook injection capped at 2000 chars

### Transparency & Observability
- Skill attribution footer on every chat response (skill, tools, duration, tokens)
- Prompt logging (hash, sections, tokens, version tracking)
- Hallucination detection (unknown tools, empty results)
- Confidence scoring in routing decisions
- Capability change toast notifications
- Welcome message with dynamic tool/skill counts

### Toolbox UI (/toolbox)
- Consolidated /tools + /extensions into 6-tab page
- Source badges (native/mcp) throughout
- Follow-up suggestion pills (context-aware)
- Prompt Audit section in Analytics
- Skill detail drawer with editor, versions, diff viewer
- MCP toolset toggles with checkboxes
- Clone + Delete buttons for skills
- Arrow key tab navigation, proper ARIA

### Self-Description Tools (12)
- list_my_skills, list_my_tools, list_ui_components
- list_promql_recipes, list_runbooks
- explain_resource, list_api_resources, list_deprecated_apis
- create_skill, edit_skill, delete_skill, create_skill_from_template

### Testing
- 1454 backend tests, 1934 frontend tests
- 9 multi-turn replay fixtures
- 15 security eval scenarios (3x increase)
- Prompt quality test suite
- 0.981 deterministic eval score, 19/21 judge pass

## v1.16.0 (2026-04-09)

### Added
- **Eval comparison infrastructure** — A/B baseline diffing with `--save-baseline`, `--compare-baseline`, `--fail-on-regression` CLI flags.
- **Prompt token audit** — `--audit-prompt` shows token cost per prompt section.
- **Section ablation framework** — test impact of removing prompt sections on eval scores.
- **View designer eval suite** — 6 scenarios + 4 new replay fixtures (17 total).
- **Eval history DB** — `eval_runs` table (migration 006) with trends API (`GET /eval/history`, `GET /eval/trend`).
- **CI automation** — live judge runs on releases, daily cron, prompt change triggers.
- **GitHub secrets** for Vertex AI (`VERTEX_PROJECT_ID`, `VERTEX_REGION`, `GCP_SA_KEY`).
- **UI Evals tab** on Agent Settings — quality gate, suite scores, dimension bars, prompt audit viz, sparkline trends.
- **ToolsView accessibility fixes** — aria-labels, keyboard nav, ToolCard extraction.
- **View designer prompt improvement** — specific commands, cautious writes.
- **bump-version.sh** auto-updates umbrella chart subchart.
- **Replay harness** — thinking parameter support, config singleton fix, model defaults to `claude-sonnet-4-6`.

## v1.15.0 (2026-04-09)

### Added
- **Modular package architecture** — Split 3 largest files into focused packages: `k8s_tools/` (11 modules, was 4419 lines), `monitor/` (10 modules, was 2486 lines), `api/` (12 modules, was 2415 lines). No file exceeds 910 lines. All backward-compatible imports preserved.
- **Typo auto-correction** — `fix_typos()` corrects ~130 common K8s/SRE misspellings (deployment, namespace, prometheus, vulnerability, etc.) with automatic plural/suffix handling. Applied before intent classification and tool selection.
- **Route safety tests** — 22 tests guard against broken GVR routes (leading tilde, wrong namespace wildcard, double slashes in API paths).
- **Centralized configuration** — All ~30 raw `os.environ.get()` calls migrated to `get_settings()`. Added 6 missing config fields: `db_pool_min/max`, `noise_threshold`, `max_trust_level`, `investigations_max_per_scan`, `investigation_cooldown`, `dev_user`.

### Removed
- **`layout_templates.py`** — deleted deprecated module (replaced by `layout_engine.py`), along with 4 backward-compat tests.
- **Dead code** — removed unused `DEFAULT_DB_PATH` constant, identity typo mapping, unused `os` imports from 4 files.

### Fixed
- **Nodes page 404** — `TopologyMap.tsx` used `/r/~v1~nodes/*` which decoded to empty API group (`/apis//v1/nodes`). Fixed to `/r/v1~nodes/_`.
- **CRDs route bug** — `CRDsView.tsx` produced leading tilde for CRDs with empty `spec.group`.
- **`MAX_RESULTS` duplication** — was defined identically in 8 k8s_tools submodules; centralized to `validators.py`.

## v1.14.0 (2026-04-03)

### Added
- **Tool eval prompts** -- 84 real-world user queries mapped to expected tool calls, covering all 82 registered tools. CI enforces eval prompt coverage for new tools.
- **`delete_dashboard` and `clone_dashboard` tools** -- manage saved views from the agent conversation.
- **Token usage tracking** -- records input/output/cache tokens per turn from the Claude API for cost visibility.
- **Semantic layout engine** -- role-based auto-layout replaces 5 fixed dashboard templates. Widgets arranged by role (KPI, chart, table, status) and content relationships.
- **Intelligence loop** -- `intelligence.py` feeds tool analytics (query reliability, dashboard patterns, error hotspots) back into the system prompt.
- **Plan validation** -- `plan_dashboard` now validates plans before execution, catching missing components early.
- **Tool analytics** -- full audit log with chain intelligence (bigram discovery, next-tool hints), usage stats API, chains endpoint.
- **View versioning** -- version history with undo support for saved views.

### Changed
- **Prompt optimization** -- SRE system prompt reduced from 28KB to 8KB (71% reduction) via selective component schema and runbook injection.
- **View designer prompt** rewritten -- 50% smaller, workflow-first approach.
- `verify_query` made optional for recipe-based PromQL queries to reduce Prometheus round-trips.
- `time_range` defaults to 1h for chart components.

### Fixed
- NaN values in chart data causing JSON parse failures and 500 errors on view endpoints.
- Connection leak, context validation, and ApiClient reuse issues.
- View designer bugs: generic `cluster_metrics` forced on every dashboard, missing title exemptions for grid/tabs/section.
- SQL interval syntax, category tracking, and Prometheus client issues.
- Invalid PromQL, dead code, session leak, and KPI sizing issues from code review.

### Docs
- Updated tool count from 105 to 82 across all documentation.
- Updated test count to 1078.
- Added EVAL_PROMPTS.md, DESIGN_PRINCIPLES.md, and CHANGELOG.md.
- Added Tool Analytics section to README.
- Updated API_CONTRACT.md with view REST endpoints and tool chain endpoint.

## v1.13.1 (2026-03-28)

### Added
- 88 unit tests for all 11 monitor scanner functions.
- Startup probes for agent (60s) and PostgreSQL (30s).
- PodDisruptionBudget for zero-downtime rollouts.

### Changed
- Deployment strategy changed to RollingUpdate with maxUnavailable=1/maxSurge=0.
- Removed standalone WS token generator; umbrella chart owns the token.

### Fixed
- Share endpoint JSONResponse bug.
- Memory timing bug, async pattern detection, version history diffs.

## v1.13.0 (2026-03-25)

### Added
- Generic `list_resources` and `describe_resource` tools for any K8s resource type via the Table API.
- 14 smart column renderers for resource tables.
- Resource relationship tracer (`get_resource_relationships`).
- View auto-save: `create_dashboard` saves directly to PostgreSQL.
- View versioning with undo and share/clone support.
- Structured JSON logging.
- Connection pooling with `ThreadedConnectionPool`.
- Schema migration system (`db_migrations.py`).
- Warning-severity investigation in monitor (not just critical).
- Default namespace scanning (removed from skip list).
- Showcase eval scenarios for all 10 component types.

### Changed
- PostgreSQL-only database layer (SQLite removed for production).
- Removed 9 redundant tools, consolidated into generic resource tools.
- Context helper extraction, tool parallelization.

### Fixed
- 401 on views API.
- Prometheus table labels.
- NaN in chart data causing JSON parse failures.

## v1.12.0 (2026-03-18)

### Added
- Auto-routing orchestrator (`/ws/agent` endpoint).
- Agent-to-agent handoff tools (`request_security_scan`, `request_sre_investigation`).
- Shared context bus for cross-agent communication.
- Noise learning for monitor findings.
- Morning briefing endpoint (`GET /briefing`).
- Simulation preview endpoint (`POST /simulate`).

## v1.9.0 (2026-03-01)

### Added
- Self-improving agent with incident memory, learned runbooks, and pattern detection.
- 73 production-tested PromQL recipes across 16 categories.
- `discover_metrics` and `verify_query` tools.
- Pydantic v2 configuration (`PulseAgentSettings`).

## v1.4.0 (2026-02-01)

### Added
- Protocol v2: `/ws/monitor` for autonomous scanning.
- 16 scanners (11 cluster + 5 audit).
- Auto-fix at trust levels 3 and 4.
- Fix history with rollback support.
- Confidence scores on all findings.

## v1.0.0 (2026-01-15)

- Initial release: SRE agent, security scanner, 9 security tools, CLI and WebSocket API.
