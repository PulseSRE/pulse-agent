# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Pulse Agent — AI-powered OpenShift/Kubernetes SRE and Security agent built on Claude. Connects to live clusters via the K8s API and uses Claude Opus for diagnostics, incident triage, and automated remediation. v2.17.4, Protocol v2, 104 native tools plus whatever connected MCP servers add, 7 skills (built-in: sre, security, view_designer, capacity_planner, plan-builder, postmortem, slo-management), 27 scanners (11 reactive + 5 audit + 1 SLO burn + 1 security posture + 5 predictive trend + 4 liveness: stuck, hot_loop, control_plane, degraded), 2848 tests, 83 PromQL recipes, 16 eval suites, 192 scenarios. 54 modules (k8s_tools/11, monitor/18, api/21, plus decorators.py, tool_predictor.py, inbox.py, inbox_generators.py, observability.py) — no file over 910 lines. Python 3.11+, Mypy clean (0 errors), ruff clean. Migration version 031 (action correlation key). Auto-routing orchestrator with typo auto-correction (~130 K8s misspellings), hard pre-route regex rules, and pre-route handoff in skill classifier. Centralized Pydantic config (no raw os.environ). Generative views: tools return 25 component specs (including action_button, confidence_badge, resolution_tracker, blast_radius, status_pipeline) for rich UI rendering, user-scoped custom dashboards with share/clone, action execution endpoint. Intelligence bridge: `normalize_component_spec()` / `normalize_layout()` in `quality_engine.py` fix field aliases (label→name, values→data, props flattening) before validation or persistence; `validate_layout_kinds()` gates REST and WS save paths; frontend `normalizeAgentProps.ts` provides matching client-side normalization. Agent view lifecycle: 3 view types (incident/plan/assessment) with status state machines, multi-user claims, finding dedup, recurrence handling, assessment→incident escalation. ViewEventBus for real-time broadcast. Multi-datasource live tables with K8s watches + PromQL/log enrichment + sparkline charts. Tool usage tracking: full audit log with chain intelligence. Adaptive tool selection: TF-IDF + LLM fallback + chain expansion. ORCA multi-signal skill selector (6-channel fusion, 5 active by default, parallel multi-skill execution max 2 with Sonnet synthesis, exclusive skills bypass secondary selection, bidirectional conflicts_with), phased plan execution, dependency graph (17 resource types, 10 relationships, 5 topology perspectives with metrics enrichment), auto-postmortems, SLO registry. ALWAYS_INCLUDE trimmed from 12 to 5. Episodes: one event with a cause, not N findings that are wrong — `episodes`/`episode_symptoms` tables, causal layers (infrastructure → platform → workload → signal) where a finding may only be explained by one strictly beneath it, symptoms collapsed out of the inbox with a count, and detachment recorded as training signal. Only infrastructure/platform findings with `findingType: "current"` may head an episode; forecasts and posture findings never can. Firing alerts are layered by *what the alert is about* (`monitor/alert_layers.py`) rather than by the fact that they arrived as alerts — an unclassified alert stays at the signal layer, where it can be a symptom but never a cause. Findings may carry `layer`, `posture` and `startedAt`; `startedAt` is the condition's own onset (Prometheus `activeAt`) and episodes correlate on it in preference to the monitor's in-memory first-sight, which is lost on every restart. A symptom belongs to exactly one episode, and a finding something deeper already explains does not head its own. The monitor scan loop starts in the app lifespan, not on WebSocket connect. Scanners that swallow an exception must call `report_failure(e)` — enforced by the `no-silent-scanner-failure` discipline rule. Release gate: 99.6% (release suite avg).

**UI Repository:** `/Users/amobrem/ali/OpenshiftPulse` — React/TypeScript frontend (Zustand stores, incident views, admin dashboard).

**IMPORTANT:** Always run `python3 -m pytest tests/ -v` before committing. CI runs on every push — check with `gh run list --limit 1`.

## Design Principles (see [`DESIGN_PRINCIPLES.md`](DESIGN_PRINCIPLES.md))

All features and UI decisions must follow these principles:
1. Conversational-first, visual-second, code-third
2. Intent -> Visibility -> Trust -> Action
3. Zero training curve; interface teaches itself
4. Delight: proactive, plain-English, confidence scores everywhere
5. Human-in-the-loop by default for anything that matters
6. Radical transparency & explainability
7. Proactive intelligence without alert fatigue
8. Minimal cognitive load & single pane of glass
9. Forgiving & resilient by design
10. Personalized & adaptive over time

## Commands

```bash
# Install
pip install -e .

# Run CLI
python -m sre_agent.main              # SRE agent
python -m sre_agent.main security     # Security scanner

# Run API server
pulse-agent-api                       # FastAPI on port 8080

# Tests
python3 -m pytest tests/ -v           # all tests (~tests)
python3 -m pytest tests/test_k8s_tools.py -v  # single file
make verify                           # lint + type-check + test
make test-everything                  # verify + ALL eval suites (deterministic + LLM-judged)
make chaos-test                       # chaos engineering (5 scenarios, needs cluster)

# Eval commands
python -m sre_agent.evals.cli --suite release --fail-on-gate   # run release eval gate
python -m sre_agent.evals.cli --suite core --save-baseline     # save eval baseline
python -m sre_agent.evals.cli --suite core --compare-baseline  # compare vs saved baseline
python -m sre_agent.evals.cli --audit-prompt --mode sre        # prompt token cost breakdown
python -m sre_agent.evals.ablation --suite release --mode sre  # (future) ablation test

# Release
make release VERSION=1.6.0            # bump version everywhere, commit, tag
# then: git push && git push --tags   # triggers build-push.yml

# Deploy (OpenShift) — uses umbrella script in UI repo
cd /Users/amobrem/ali/OpenshiftPulse && ./deploy/deploy.sh   # builds UI + Agent, pushes to Quay, Helm upgrade
cd /Users/amobrem/ali/OpenshiftPulse && ./deploy/deploy.sh --dry-run  # preview without applying
cd /Users/amobrem/ali/OpenshiftPulse && ./deploy/deploy.sh --set agent.mcp.enabled=true  # deploy with MCP enabled
```

## Architecture

### Entry Points
- `sre_agent/main.py` — Interactive CLI with Rich UI
- `sre_agent/serve.py` → `sre_agent/api/` — FastAPI WebSocket server

### Agent Loop
- `agent.py` — shared `run_agent_streaming()` async loop used by both SRE and Security agents
- Uses `AsyncAnthropic`/`AsyncAnthropicVertex` — LLM streaming runs natively on the event loop (`async with`/`async for`), no `asyncio.to_thread` dispatch
- `create_async_client()` for async callers, `create_client()` retained for sync callers (skill_router, tool_predictor, inbox)
- Tool execution: stays sync in `ThreadPoolExecutor` via `loop.run_in_executor()` (K8s client is sync)
- All 7 callbacks routed through `EventBus` (`event_bus.py`). Existing callers pass individual callbacks via `EventBus.from_callbacks()`. New callers implement `AgentEventHandler` protocol.
- Circuit breaker: `CircuitBreaker` class with CLOSED→OPEN→HALF_OPEN states, `threading.Lock` for thread safety across `ThreadPoolExecutor`
- Confirmation: `confirm_request` → `confirm_response` with JIT nonce for replay prevention

### WebSocket API (Protocol v2)
- `/ws/agent` — Auto-routing orchestrated agent (ORCA classifies intent per message, routes to appropriate skill)
- `/ws/monitor` — Autonomous cluster monitoring (push-based findings, investigations, actions)
- Auth: `PULSE_AGENT_WS_TOKEN` via query param, constant-time comparison

### Monitor System (`monitor/` package — 12 modules)
- `MonitorSession` (session.py) — periodic cluster scanning (default 60s interval)
- Scanner protocol: `ScannerMeta` dataclass + `Scanner` protocol in `scanner_protocol.py`. `FunctionScanner` wraps existing functions. `cluster_monitor._run_scan_locked()` iterates `get_all_scanner_instances()` with `shared_resources` dict for pod-sharing. Adding a scanner = adding a class, no changes to cluster_monitor.
- 24 scanners: 13 reactive (crashlooping pods, pending pods, failed deployments, node pressure, expiring certs, firing alerts, OOM-killed pods, image pull errors, degraded operators, DaemonSet gaps, HPA saturation, SLO burn rate, security posture) + 5 audit (config changes, RBAC, deployments, warning events, auth) + 4 predictive trend (memory pressure forecast, disk pressure forecast, HPA exhaustion trend, error rate acceleration) using Prometheus `predict_linear()`
- Auto-fix at trust level 3+: deletes crashlooping pods, restarts failed deployments
- Confidence scores on all findings, investigations, and action proposals
- `resolution` events emitted when findings resolve (auto-fix or self-healed)
- Reasoning chains: investigation prompt requests evidence + alternatives_considered
- Noise learning: tracks transient findings, assigns `noiseScore` to suppress flaky alerts
- `findings_snapshot` event for stale finding cleanup
- Morning briefing: `GET /briefing` endpoint aggregates recent activity with time-aware greeting, live scanner data, and trend findings with priority items
- Simulation preview: `POST /simulate` predicts impact, risk, duration, reversibility
- WebSocket `feedback` message type for UI-driven memory learning (thumbs up/down)
- Approved confirmations recorded as implicit positive feedback for memory
- Fix history persisted to the database (`PULSE_AGENT_DATABASE_URL`)
- `_sanitize_for_prompt()` on all cluster data in investigation prompts with `--- BEGIN/END CLUSTER DATA ---` delimiters

### Orchestrator (`orchestrator.py`)
- `classify_intent()` — keyword-based SRE/Security/Both classification
- `fix_typos()` — corrects ~130 common K8s/SRE misspellings before classification
- `build_orchestrated_config()` — returns system_prompt, tool_defs, tool_map, write_tools for the classified mode
- Used by `/ws/agent` endpoint for auto-routing
- Skill routing: ALL trigger patterns in `skill.md` YAML (`trigger_patterns` + `route_priority`), zero hardcoded patterns in `skill_router.py`. `reset_hard_pre_route()` on skill reload. OOM pattern uses `\boom` (start boundary) to avoid matching "headroom".

### Tools
- `k8s_tools/` — 12-module package with 42 K8s tools (`@beta_tool` decorated). Write tools in `WRITE_TOOLS` set require confirmation. Submodules: validators, pods, nodes, deployments, workloads, monitoring, diagnostics, generic, advanced, audit, live_table.
- `security_tools.py` — 9 security scanning tools (read-only)
- `fleet_tools.py` — 5 multi-cluster tools
- `gitops_tools.py` — 6 ArgoCD tools
- `predict_tools.py` — 3 predictive analytics tools
- `timeline_tools.py` — 1 incident correlation tool
- `git_tools.py` — 1 Git PR proposal tool
- `handoff_tools.py` — 2 agent-to-agent handoff tools (`request_security_scan`, `request_sre_investigation`)
- `tool_registry.py` — central registry with `TOOL_CATEGORIES` and `WRITE_TOOL_NAMES`. `@beta_tool(category="views", is_write=True)` for auto-registration with metadata. Plain `@beta_tool` unchanged.
- `tool_discovery.py` — `discover_tools()` imports all tool modules, populates `TOOL_REGISTRY`. Called in `app.py` lifespan.

### Tool Pattern
```python
@beta_tool
def tool_name(param: str, namespace: str = "") -> str:
    """One-line description used by Claude to decide when to call it."""
    err = _validate_k8s_namespace(namespace)
    if err:
        return err
    result = safe(lambda: get_core_client().list_namespaced_pod(namespace))
    if isinstance(result, str):
        return result  # Error from safe()
    # Format and return
```

Rules: validate inputs with `_validate_k8s_name()`/`_validate_k8s_namespace()`, wrap K8s calls in `safe()`, write tools must be in `WRITE_TOOLS` set, never return secret values.

### Configuration (`config.py`)
- `PulseAgentSettings(BaseSettings)` — Pydantic v2 Settings with `PULSE_AGENT_` env prefix
- 6 frozen sub-models: `AgentConfig`, `DatabaseConfig`, `MonitorConfig`, `RoutingConfig`, `ServerConfig`, `PrometheusConfig`
- Access: `get_settings().monitor.scan_interval` (nested). Flat env vars (`PULSE_AGENT_MODEL`) synced to nested via `model_post_init`
- `.env` file support, type validation at startup. All config via settings instance, not raw `os.environ`

### Harness (`harness.py`)
- Prompt caching: `cache_control: ephemeral` on system prompt
- Cluster context injection: pre-fetches node count, namespaces, OCP version
- Component hints for selected tools (tool selection moved to skill_loader)

### Logging
- structlog wraps stdlib (`logging_config.py`). Both `logging.getLogger()` and `get_logger()` from `sre_agent/log.py` emit structured JSON.
- `CorrelationMiddleware` in `api/app.py` injects `request_id` into all log entries via `contextvars`.
- Log format/level configured via `PULSE_AGENT_LOG_FORMAT` / `PULSE_AGENT_LOG_LEVEL` (Pydantic settings, not raw `os.environ`).

### Security
- Non-root container (UID 1001) on UBI9
- RBAC: read-only by default, write ops opt-in via `rbac.allowWriteOperations`
- Confirmation gate enforced in code (not just prompt)
- Prompt injection defense in system prompt
- Input validation: replicas 0-100, log lines 1-1000, grace period 1-300s
- Token forwarding: user OAuth token from `X-Forwarded-Access-Token` forwarded to K8s API calls for per-user RBAC enforcement. Monitor scans use SA. Toggle: `PULSE_AGENT_TOKEN_FORWARDING`

### MCP Server
- OpenShift MCP server (github.com/openshift/openshift-mcp-server) deployed as sidecar pod
- Image: `quay.io/amobrem/pulse-agent:mcp-server` (built from openshift fork)
- SSE transport, auto-discovery, 3-tier rendering
- 11 toolsets: core, config, helm, observability, openshift, ossm, netedge, tekton, kiali, kubevirt, kcp
- 36 MCP tools registered alongside native tools
- Health probes (readiness + liveness on `/healthz`)
- Toggle toolsets from the Toolbox UI
- CI (`build-push.yml`) builds MCP image on release tags

### Deployment
Deployment is owned by the [Pulse Operator](https://github.com/PulseSRE/pulse-operator), installed via OLM. The agent version it runs is `spec.agent.image` on the `OpenShiftPulse` CR. `oc patch openshiftpulse pulse -n openshiftpulse --type=merge -p '{"spec":{"agent":{"image":"..."}}}'` rolls a new version; patching the Deployment directly is reverted by the operator.

### Frontend CSS Gotchas
- **Scrollbar styling**: Uses ONLY `::-webkit-scrollbar` pseudo-elements (in `index.css`). Do NOT add `scrollbar-color` or `scrollbar-width` — Chrome 121+ ignores `::-webkit-scrollbar` when standard scrollbar properties are set (even via inheritance).
- **`.openshiftpulse` class**: Must be on the Shell root div (`Shell.tsx`). All global CSS rules are scoped to this class.

### Key Files
- `config.py` — Pydantic v2 Settings (`PulseAgentSettings` with `PULSE_AGENT_` prefix)
- `errors.py` — `ToolError` classification (7 categories + suggestions)
- `error_tracker.py` — thread-safe ring buffer for error aggregation
- `runbooks.py` — 10 built-in SRE runbooks injected into system prompt
- `memory/` — self-improving agent (PostgreSQL, pattern detection, learned runbooks)
- `memory/environment.py` — memory about *this* cluster, kept separate from memory about Kubernetes. `EnvironmentFact` (stated/established, changes rarely: ownership, retention, GitOps topology) and `WorkloadBaseline` (measured, recomputed on a window) so an observation can be reported as "3x this workload's normal" rather than a raw number. Tools: `remember_environment_fact`, `get_environment_facts`, `compare_to_baseline`, `search_conversations` (cross-session search over `chat_messages`, scoped to one owner). Migration 032
- `view_tools.py` — `namespace_summary` + `create_dashboard` (accepts view_type, trigger_source, finding_id, visibility for agent view lifecycle) + `get_topology_graph` tools for generative views. Topology supports 5 perspectives (Physical, Logical, Network, Multi-Tenant, Helm) via `kinds`, `relationships`, `layout_hint`, `include_metrics`, `group_by` params
- `view_mutations.py` — dispatches to `mutations/` package (13 typed `ViewMutation` classes with validate/apply). `MUTATION_REGISTRY` with lazy registration. Tool signature unchanged.
- `view_executor.py` — executes viewPlan widget specs at claim time (tool-backed + props-only, timeout, staleness, security gate)
- `dependency_graph.py` — live K8s resource dependency graph (17 types, 10 relationships), `_fetch_metrics()` with 30s TTL cache for metrics-server enrichment
- `quality_engine.py` — unified dashboard validation + quality scoring (includes `critique_view` moved from view_critic.py). `normalize_component_spec()` fixes field aliases, `normalize_layout()` recurses into containers, `validate_layout_kinds()` checks kind validity
- `db.py` — Database class (PostgreSQL pool, `?`→`%s` translation) + thin wrappers delegating to repositories
- `repositories/` — domain-specific DB access: `ViewRepository` (27 methods), `InboxRepository` (24 methods), `MonitorRepository` (17 methods), `ToolUsageRepository`, `IntelligenceRepository`, `PromptLogRepository`, `ChatHistoryRepository`, `SelectorLearningRepository`. All extend `BaseRepository` with lazy `self.db` property.
- `async_db.py` — `AsyncDatabase` with asyncpg pool (`$1` placeholders), transaction context manager (`async with db.transaction() as conn`), connection-scoped helpers (`execute_in_tx`, `fetchone_in_tx`, `fetchall_in_tx`). Used by tool_usage (fire-and-forget recording), monitor repo (8 async methods), inbox/views REST endpoints. JSONB operators preserved by regex.
- `k8s_client.py` — lazy-initialized K8s client with `safe()` wrapper
- `context_bus.py` — shared context bus for cross-agent communication
- `orchestrator.py` — intent classification + typo correction + agent routing for `/ws/agent`
- `tool_usage.py` — tool invocation audit log (PostgreSQL, fire-and-forget recording, query/stats)
- `tool_predictor.py` — adaptive tool selection engine (TF-IDF prediction, LLM fallback, co-occurrence expansion, real-time learning)
- `decorators.py` — typed `beta_tool` wrapper with optional `category`/`is_write` kwargs for auto-registration
- `event_bus.py` — `EventBus` with `from_callbacks()` factory, replaces 7 individual callback params in agent loop
- `loop_budget.py` — inner-loop bounds and honesty about them. `LoopBudget` tracks iterations, input tokens and wall-clock; warns the model to conclude at 80% of a limit while a turn remains to do it in; and appends a cutoff notice to the *returned text* when a limit ends the turn, so a truncated investigation cannot be mistaken for a complete one. `compact_tool_results()` shrinks tool results older than 6 turns, leaving a note saying how much was dropped rather than shrinking context silently
- `async_k8s.py` — async K8s client wrappers using `kubernetes_asyncio` (scaffolding, no production consumer yet)
- `tool_chains.py` — tool chain discovery and next-tool hints (bigram analysis, system prompt injection)
- `promql_recipes.py` — 83 production-tested PromQL recipes + learned queries DB (sources: OpenShift console, cluster-monitoring-operator, kube-state-metrics, node_exporter, ACM)
- `layout_engine.py` — semantic auto-layout engine (role-based row packing, replaces fixed templates)
- `intelligence.py` — analytics feedback loop (query reliability, dashboard patterns, error hotspots → system prompt); supports `PULSE_PROMPT_EXCLUDE_SECTIONS` env var for ablation testing
- `evals/compare.py` — A/B comparison of eval suite results (baseline vs current, regression detection)
- `evals/ablation.py` — prompt section ablation framework (tests impact of removing prompt sections on scores)
- `evals/history.py` — eval history DB (eval_runs table, trend queries, migration 006)
- `skill_loader.py` — skill package loader, tool selection, MCP inclusion (consolidates harness tool selection); ALWAYS_INCLUDE trimmed to 5 (adaptive) plus `skill_search`/`skill_load`, self-describe tools conditional. `_rank_by_relevance()` orders candidates by query relevance before the MAX_TOOL_BUDGET cut, so tools named in the query survive truncation
- `skill_tools.py` — progressive skill disclosure: `skill_search` (tier 0, names + descriptions), `skill_load` (tier 1, one skill's procedure), `skill_load(name, reference=...)` (tier 2, a document from the skill's `references/` directory). Lets the agent reach expertise mid-investigation instead of being fixed to whatever routing chose from the first sentence
- `skill_router.py` — query routing, pre-route handoff, exclusive skill enforcement, bidirectional conflict checking, multi-skill secondary selection
- `tool_categories.py` — tool category definitions extracted from skill_loader.py
- `mcp_client.py` — MCP server connections (SSE transport), tool/prompt discovery, registration
- `self_tools.py` — 12 self-description + 4 skill management + 3 K8s API introspection tools
- `observability.py` — Prometheus metrics registry (tokens, cost, investigations, scanners, autofix); `/metrics` endpoint mounted in `app.py`
- `prompt_log.py` — prompt logging (hash, sections, tokens, version tracking) for observability and debugging
- `component_registry.py` — 25 component kinds registered (metrics, data, visualization, status, detail, layout, action); data-driven prompt hints
- `slo_registry.py` — SLO definition CRUD, live Prometheus burn-rate queries, persistence to `slo_definitions` table
- `inbox.py` — Ops Inbox CRUD, lifecycle transitions, priority scoring, dedup, snooze, monitor bridge, generator cycle, `create_inbox_task` agent tool
- `inbox_generators.py` — 13 proactive task generators (cert expiry, trend prediction, degraded operators, upgrades, SLO burn, capacity, stale findings, privileged workloads, RBAC drift, network policy gaps, route certs, endpoint gaps, readiness regressions)
- `api/inbox_rest.py` — 13 inbox REST endpoints (list, get, create, update, claim, acknowledge, snooze, dismiss, investigate, resolve, escalate, pin, stats)
- `change_risk.py` — deploy risk scoring for findings (correlates recent changes with incidents)
- `plan_runtime.py` — phased investigation plan execution engine (plan templates, phase lifecycle, progress events). `_execute_phase()` holds each phase to its declared `produces` contract via `phase_judge`, retrying once with the missing fields named before downgrading status to `partial`
- `phase_judge.py` — deterministic contract check for plan phases. `produces` was previously only shown to the model as "Expected outputs" and never verified, so a diagnose phase could return no root cause and remediation would still run. Treats `unknown`/`n/a`/empty collections as unfilled, but a measured `0` or `False` as real
- `skill_scaffolder.py` — AI-generated skill packages from conversation patterns and usage data
- `trajectory.py` — verified-trajectory learning gate. Investigations record a `LearningCandidate` instead of scaffolding immediately; `monitor/verification_pipeline.py` promotes it to a skill/plan/eval only once the finding is confirmed resolved, discards it if the fix did not hold, and the daily flywheel expires candidates whose verification never arrived. Learning is gated on outcome, not on self-assessed confidence
- `eval_scaffolder.py` — auto-generates eval scenarios when skills are scaffolded (`scaffold_eval_from_plan()` for full scenario + replay fixture, `scaffold_eval_from_investigation()` for scenario only); writes to non-gating `scaffolded` eval suite
- `selector_learning.py` — ORCA selector weight learning from feedback signals (routing decisions, overrides, outcomes)
- `synthesis.py` — parallel skill output merging with Sonnet-powered conflict detection and fallback concatenation
- `trend_scanners.py` — 4 predictive scanners (memory pressure, disk pressure, HPA exhaustion, error rate acceleration) using Prometheus predict_linear()
- `api/views.py` — view CRUD, sharing, version history, action execution (`POST /views/{id}/actions`), status transitions (`POST /views/{id}/status`), claim/unclaim (`POST/DELETE /views/{id}/claim`)
- `api/view_events.py` — ViewEventBus pub/sub for real-time broadcast of view_claimed, view_action_executed, view_status_changed events
- `api/metrics_rest.py` — operational metrics endpoints: fix success rate, response latency (percentile_cont), eval trend with sparkline
- `api/scanner_rest.py` — scanner REST endpoints extracted from monitor_rest.py
- `api/fix_rest.py` — fix history REST endpoints extracted from monitor_rest.py
- `api/topology_rest.py` — topology REST endpoints extracted from monitor_rest.py

**Frontend:** `/toolbox` consolidates tools, skills, MCP, components, usage, analytics into single page.

**Testing:** See [`TESTING.md`](TESTING.md) for full testing strategy, eval prompts, CI pipeline, and release process.

### Claude Code Agents (`.claude/agents/`)
8 specialized agents with hooks in `.claude/settings.json`:
- `tool-writer` — writes new `@beta_tool` functions
- `runbook-writer` — writes diagnostic runbooks
- `protocol-checker` — validates WebSocket protocol vs API_CONTRACT.md
- `tool-auditor` — audits tools for input validation, security
- `memory-auditor` — audits memory system integrity
- `security-hardener` — reviews security across code, containers, Helm
- `test-writer` — writes pytest tests following conftest patterns
- `deploy-validator` — validates deploy config before rollout

### Environment Variables
| Variable | Description | Default |
|----------|-------------|---------|
| `ANTHROPIC_VERTEX_PROJECT_ID` | GCP project for Vertex AI | required* |
| `CLOUD_ML_REGION` | GCP region | required* |
| `ANTHROPIC_API_KEY` | Direct Anthropic API key | required* |
| `PULSE_AGENT_MODEL` | Claude model | `claude-opus-4-6` |
| `PULSE_AGENT_MULTI_SKILL` | Enable parallel multi-skill routing | `true` |
| `PULSE_AGENT_MULTI_SKILL_MAX` | Max concurrent skills | `2` |
| `PULSE_AGENT_MULTI_SKILL_THRESHOLD` | ORCA score gap for multi-skill activation | `0.15` |
| `PULSE_AGENT_NOISE_THRESHOLD` | Noise score threshold for suppressing findings | `0.7` |
| `PULSE_AGENT_SCAN_INTERVAL` | Monitor scan interval (seconds) | `60` |
| `PULSE_AGENT_TEMPORAL_CACHE_TTL` | Temporal signal cache TTL (seconds) | `60` |
| `PULSE_AGENT_TOKEN_FORWARDING` | Forward user OAuth token to K8s API | `true` |
| `PULSE_AGENT_WS_TOKEN` | WebSocket auth token | auto-generated |
| `PULSE_AGENT_AUTOFIX_ENABLED` | Enable monitor auto-fix | `true` |
| `PULSE_AGENT_CB_THRESHOLD` | Circuit breaker failure threshold | `3` |
| `PULSE_AGENT_CB_TIMEOUT` | Circuit breaker recovery (seconds) | `60` |
| `PULSE_AGENT_DATABASE_URL` | Database URL (PostgreSQL) | required |
| `PULSE_AGENT_HARNESS` | Enable harness optimizations | `1` |
| `PULSE_AGENT_MAX_TRUST_LEVEL` | Server-side max trust level (0-4) | `2` |
| `PULSE_AGENT_MEMORY` | Enable self-improving memory | `1` (enabled) |
| `PULSE_AGENT_COST_BUDGET_USD` | Daily cost budget in USD (0=disabled) | `0` |
| `PULSE_AGENT_COST_BUDGET_WARNING_PCT` | Cost budget warning threshold (%) | `80` |

*One of Vertex AI or Anthropic API key is required.
