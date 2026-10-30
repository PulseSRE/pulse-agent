# The Pulse Journey

> Historical project narrative. Dates, benchmark counts, operational claims, and deployment lessons describe earlier releases and are not current guarantees. Use [current documentation](README.md) for deployment and security.

How two repos, ~3,000 commits, and 232 days built an AI-powered SRE platform from scratch — and then spent the last third of that time making it tell the truth about itself.

**Repos:** [pulse-agent](https://github.com/PulseSRE/pulse-agent) | [pulse-ui](https://github.com/PulseSRE/pulse-ui)

**Key docs:** [DESIGN_PRINCIPLES.md](../DESIGN_PRINCIPLES.md) | [ARCHITECTURE.md](ARCHITECTURE.md) | [API_CONTRACT.md](../API_CONTRACT.md) | [SECURITY.md](../SECURITY.md) | [TESTING.md](../TESTING.md) | [DATABASE.md](../DATABASE.md) | [CHANGELOG.md](../CHANGELOG.md)

---

## The Origin Story

On **March 9, 2026**, the [first commit](https://github.com/alimobrem/OpenshiftPulse/commit/3736449) landed in what was then called **ShiftOps** — a React/TypeScript frontend for browsing OpenShift clusters. It used React 19, Rspack, and Vitest. The initial vision was straightforward: a modern, fast alternative to the OpenShift Console with better UX.

Two weeks later, on **March 24**, the agent backend got its first commit. The idea had expanded: what if the cluster browser wasn't just a dashboard, but had an AI copilot that could actually diagnose and fix problems?

By the end of April, Pulse had become something neither repo was designed to be on day one — a full AI-powered SRE platform with 154 tools, 7 skills, 23 scanners, and an autonomous monitor that could find, diagnose, and fix cluster issues without human intervention.

This is the story of how that happened — the decisions, the mistakes, and the things we'd do differently.

---

## Timeline

### Phase 0: The Console (March 9 – March 23)

**What we built:** A cluster management UI with 13 views, 1,128 tests, and full RBAC support.

The frontend started as a clean-sheet OpenShift Console replacement. Key milestones from this period:

- **[v1.0.0](https://github.com/alimobrem/OpenshiftPulse/releases/tag/v1.0.0)** (Mar 12) — Dark mode, basic resource browser, cost dashboard
- **[v2.0.0](https://github.com/alimobrem/OpenshiftPulse/releases/tag/v2.0.0)** (Mar 12) — Runbook automation framework
- **[v3.0.0](https://github.com/alimobrem/OpenshiftPulse/releases/tag/v3.0.0)** (Mar 18) — Renamed from ShiftOps to **OpenShift Pulse**. The name change reflected a shift in vision — we weren't just building a console, we were building something that could feel the cluster's "pulse."
- **[v4.0.0](https://github.com/alimobrem/OpenshiftPulse/releases/tag/v4.0.0)** (Mar 19) — HyperShift/Hosted Control Plane support ([changelog entry](https://github.com/alimobrem/OpenshiftPulse/blob/main/CHANGELOG.md)). This was a design decision that paid off: every feature from day one had to work on both traditional and hosted clusters. We detected `controlPlaneTopology: External` from the Infrastructure resource and adapted health checks, compute views, and production readiness scoring.

**Design decisions that stuck:**

- Rspack over Webpack. Build time: ~1 second. Never regretted this.
- Zustand over Redux. Simpler mental model, less boilerplate.
- Tailwind CSS from the start. Controversial for an OpenShift-adjacent project (PatternFly is the standard), but it gave us the speed to iterate on UI daily.

**First big mistake:** We originally used PatternFly components and spent a week fighting with their opinionated CSS. Ripping it out and going Tailwind + Radix UI was painful but freed us to build faster.

### Phase 1: The Agent is Born (March 24 – March 27)

**What we built:** An AI SRE agent with 54 tools, WebSocket streaming, and an autonomous monitor.

This was the most intense period. In four days:

- **[v1.0.0](https://github.com/alimobrem/pulse-agent/releases/tag/v1.0.0)** (Mar 25, `[7a81ab0](https://github.com/alimobrem/pulse-agent/commit/7a81ab0)`) — Initial agent release. SRE mode with 39 read tools, 9 write tools, security scanner with 9 tools. CLI and WebSocket API.
- **[v1.1.0](https://github.com/alimobrem/pulse-agent/releases/tag/v1.1.0)** (Mar 26, `[ec01951](https://github.com/alimobrem/pulse-agent/commit/ec01951)`) — Structured error handling with `ToolError` classification (7 categories). See `[sre_agent/errors.py](../sre_agent/errors.py)`.
- **[v1.2.0](https://github.com/alimobrem/pulse-agent/releases/tag/v1.2.0)** (Mar 26, `[98dcd21](https://github.com/alimobrem/pulse-agent/commit/98dcd21)`) — Speed and reliability pass. We realized every K8s API call needed to be wrapped in `safe()` — a function that catches exceptions and returns error strings instead of crashing the agent loop. See `[sre_agent/k8s_client.py](../sre_agent/k8s_client.py)`.
- **[v1.3.0](https://github.com/alimobrem/pulse-agent/releases/tag/v1.3.0)** (Mar 26, `[70ba2f2](https://github.com/alimobrem/pulse-agent/commit/70ba2f2)`) — Deploy pipeline. First time shipping to a real cluster.
- **[v1.4.0](https://github.com/alimobrem/pulse-agent/releases/tag/v1.4.0)** (Mar 26, `[b1d6a87](https://github.com/alimobrem/pulse-agent/commit/b1d6a87)`) — **Protocol v2.** The original protocol was request-response over HTTP. We rewrote it as a WebSocket streaming protocol with separate endpoints for interactive (`/ws/agent`) and autonomous (`/ws/monitor`) modes. This was the right call — streaming thinking indicators, tool execution badges, and component specs all needed push-based delivery. Full protocol spec: `[API_CONTRACT.md](../API_CONTRACT.md)`.
- **[v1.5.0](https://github.com/alimobrem/pulse-agent/releases/tag/v1.5.0)** (Mar 27, `[db36131](https://github.com/alimobrem/pulse-agent/commit/db36131)`) — Autonomous monitor with 16 scanners and auto-fix at trust level 3+. See `[SECURITY.md](../SECURITY.md)` for trust level definitions.

**The "Four Pillars" that got cut:** The v0.2.0 prototype had four experimental features — ArgoCD integration, "Time Machine" (cluster state replay), Git PR proposals, and Prophet-based predictions. All four were cut or heavily reworked. Time Machine was too expensive (snapshotting full cluster state every minute). Prophet predictions were unreliable without enough historical data. We kept ArgoCD and Git PRs but rebuilt them from scratch later.

**Critical bug:** The agent loop used `asyncio.to_thread()` for LLM streaming, which is wrong — the Anthropic async client already runs on the event loop. This caused thread safety issues that manifested as garbled streaming output. We fixed it by switching to `AsyncAnthropic` with native `async with`/`async for`. See the [async migration spec](superpowers/specs/2026-04-23-async-anthropic-migration-design.md) and [implementation plan](superpowers/plans/2026-04-23-async-anthropic-migration.md) for the full technical story (commit `[1ceae75](https://github.com/alimobrem/pulse-agent/commit/1ceae75)`).

**Security wake-up call:** The first deploy to a real cluster revealed that our Helm chart had a wildcard RBAC rule (`patch */`*). A sysadmin-customer review agent caught this, and we replaced it with scoped rules for specific API groups. We also discovered SSRF vulnerabilities in the dev proxy (IPv6 bypass), CRLF injection in user impersonation headers, and a 1MB WebSocket message limit that was missing entirely. All fixed before v1.0 shipped. Full security model documented in `[SECURITY.md](../SECURITY.md)`.

### Phase 2: Making It Smart (March 27 – April 1)

**What we built:** Self-improving memory, PromQL recipes, generative views, Pydantic config.

- **[v1.5.1](https://github.com/alimobrem/pulse-agent/releases/tag/v1.5.1)** (Mar 27, `[5e4fc88](https://github.com/alimobrem/pulse-agent/commit/5e4fc88)`) — Eval gating. We built a test suite of real-world SRE prompts and blocked releases that regressed scores. This caught dozens of prompt regressions over the next month. See `[TESTING.md](../TESTING.md)` and `[sre_agent/evals/README.md](../sre_agent/evals/README.md)`.
- **[v1.6.0](https://github.com/alimobrem/pulse-agent/releases/tag/v1.6.0)** (Mar 29, `[056a158](https://github.com/alimobrem/pulse-agent/commit/056a158)`) — Memory system with PostgreSQL persistence. The agent could now learn from past incidents — patterns detected, runbooks discovered, feedback incorporated. See `[sre_agent/memory/](../sre_agent/memory/)`.
- **[v1.7.0](https://github.com/alimobrem/pulse-agent/releases/tag/v1.7.0)** (Mar 29, `[1252220](https://github.com/alimobrem/pulse-agent/commit/1252220)`) — Morning briefing and noise learning. The monitor would greet users with a summary of overnight activity. Noise learning tracked transient findings (pods that crashloop once then recover) and suppressed them with a configurable `PULSE_AGENT_NOISE_THRESHOLD`.
- **[v1.8.0](https://github.com/alimobrem/pulse-agent/releases/tag/v1.8.0)** (Mar 29, `[03e136c](https://github.com/alimobrem/pulse-agent/commit/03e136c)`) — User preferences and rollback support.
- **[v1.9.0](https://github.com/alimobrem/pulse-agent/releases/tag/v1.9.0)** (Mar 29, `[56afb9b](https://github.com/alimobrem/pulse-agent/commit/56afb9b)`) — Auth audit scanner. Five new scanners for config changes, RBAC mutations, deployments, warning events, and auth failures.
- **[v1.10.0](https://github.com/alimobrem/pulse-agent/releases/tag/v1.10.0)** (Mar 31, `[5a310d0](https://github.com/alimobrem/pulse-agent/commit/5a310d0)`) — **Generative views.** The agent could now create interactive dashboards from natural language. "Show me a dashboard for namespace monitoring" would produce a real, saved, interactive view with charts and tables. See `[sre_agent/view_tools.py](../sre_agent/view_tools.py)`.
- **[v1.13.0](https://github.com/alimobrem/pulse-agent/releases/tag/v1.13.0)** (Apr 1, `[e10a432](https://github.com/alimobrem/pulse-agent/commit/e10a432)`) — Infrastructure hardening. PostgreSQL-only (dropped SQLite for production), connection pooling, schema migration system, generic K8s resource tools. See `[DATABASE.md](../DATABASE.md)` for the full schema.

**The PostgreSQL SERIAL saga** ([v1.5.2](https://github.com/alimobrem/pulse-agent/releases/tag/v1.5.2) – [v1.5.3](https://github.com/alimobrem/pulse-agent/releases/tag/v1.5.3), commits `[74fe72e](https://github.com/alimobrem/pulse-agent/commit/74fe72e)` and `[fa853aa](https://github.com/alimobrem/pulse-agent/commit/fa853aa)`): We shipped two patch releases to fix `SERIAL` column handling. The bug: our migration code used `SERIAL PRIMARY KEY` but the ORM was trying to insert explicit `id` values, causing duplicate key violations (commit `[15e7dfb](https://github.com/alimobrem/pulse-agent/commit/15e7dfb)`). The fix was small but the debugging was painful because the error only appeared after the database had enough rows to trigger the conflict.

**The `os.environ` purge:** v1.15.0 migrated all ~30 raw `os.environ.get()` calls to a centralized Pydantic `PulseAgentSettings` model with `PULSE_AGENT`_ prefix. See `[sre_agent/config.py](../sre_agent/config.py)`. This caught several bugs — missing defaults, wrong types, and one case where `PULSE_AGENT_TOKEN_FORWARDING` was read as a string `"false"` which is truthy in Python. Pydantic caught it as a bool.

**View designer iteration hell:** The generative view system went through 11 documented pitfalls (see below in "Mistakes"). Design specs: [data-first generative UI](superpowers/specs/2026-04-04-data-first-generative-ui-design.md) and [view designer tuning](superpowers/specs/2026-04-04-view-designer-tuning-design.md).

### Phase 3: Architecture at Scale (April 1 – April 12)

**What we built:** Skill system, MCP integration, tool analytics, modular architecture, eval infrastructure.

This phase was about taking a working prototype and making it maintainable and extensible.

- **[v1.14.0](https://github.com/alimobrem/pulse-agent/releases/tag/v1.14.0)** (Apr 7, `[1adb2e7](https://github.com/alimobrem/pulse-agent/commit/1adb2e7)`) — Tool analytics and semantic layout engine. We built a full audit log for tool usage, discovered tool calling patterns via bigram analysis, and replaced 5 fixed dashboard templates with a role-based auto-layout engine. Specs: [tool chain intelligence](superpowers/specs/2026-04-03-tool-chain-intelligence-design.md), [tool usage tracking](superpowers/specs/2026-04-03-tool-usage-tracking-design.md), [semantic layout engine](superpowers/specs/2026-04-04-semantic-layout-engine-design.md). Plans: [tool tracking prereqs](superpowers/plans/2026-04-03-tool-tracking-prereqs.md), [tool usage backend](superpowers/plans/2026-04-03-tool-usage-backend.md), [tool usage frontend](superpowers/plans/2026-04-03-tool-usage-frontend.md).
- **[v1.15.0](https://github.com/alimobrem/pulse-agent/releases/tag/v1.15.0)** (Apr 9, `[bd70030](https://github.com/alimobrem/pulse-agent/commit/bd70030)`) — **The Great Split.** Three files had grown too large: `k8s_tools.py` (4,419 lines), `monitor.py` (2,486 lines), `api_routes.py` (2,415 lines). We split them into packages — `k8s_tools/` (11 modules), `monitor/` (10 modules), `api/` (12 modules). After the split, no file exceeded 910 lines. All backward-compatible imports preserved. Key commits: `[a739819](https://github.com/alimobrem/pulse-agent/commit/a739819)` (monitor split), `[401ba0d](https://github.com/alimobrem/pulse-agent/commit/401ba0d)` (cleanup).
- **[v1.16.0](https://github.com/alimobrem/pulse-agent/releases/tag/v1.16.0)** (Apr 9, `[18f4b89](https://github.com/alimobrem/pulse-agent/commit/18f4b89)`) — Eval comparison infrastructure. A/B baseline diffing, prompt token auditing, and section ablation testing. Spec: [prompt optimization](superpowers/specs/2026-04-09-prompt-optimization-design.md). We could now measure the impact of removing any section from the system prompt.
- **[v2.0.0](https://github.com/alimobrem/pulse-agent/releases/tag/v2.0.0)** (Apr 12, `[b638935](https://github.com/alimobrem/pulse-agent/commit/b638935)`) — **Extensible skill system and MCP integration.** Skills were drop-in `.md` files that defined routing rules, tool selections, component hints, and eval scenarios. MCP integration added 36 tools from the OpenShift MCP server. The Toolbox UI consolidated everything into a single page. Spec: [extensible agent architecture](superpowers/specs/2026-04-10-extensible-agent-architecture-design.md). Skill developer guide: `[docs/SKILL_DEVELOPER_GUIDE.md](SKILL_DEVELOPER_GUIDE.md)`. Skill definitions: `[sre_agent/skills/](../sre_agent/skills/)`.

**The prompt optimization breakthrough:** v1.14.0 reduced the SRE system prompt from 28KB to 8KB — a 71% reduction. We did this by making component schemas and runbook injection dynamic (only included when relevant to the query). Combined with `cache_control: ephemeral` on the system prompt, this cut API costs by ~90%. See `[sre_agent/harness.py](../sre_agent/harness.py)` and `[sre_agent/skill_loader.py](../sre_agent/skill_loader.py)`.

**Typo correction saves the day:** We built a dictionary of ~130 common K8s/SRE misspellings (commit `[f9e5911](https://github.com/alimobrem/pulse-agent/commit/f9e5911)` and `[d7a1caf](https://github.com/alimobrem/pulse-agent/commit/d7a1caf)`) with automatic plural/suffix handling. This sounds trivial but dramatically improved routing accuracy — misspelled queries were hitting the wrong skill or returning no results. One subtle bug: typo correction initially ran *after* the hard pre-route regex rules, so a misspelled keyword wouldn't match the regex. We fixed it (commit `[339d5bc](https://github.com/alimobrem/pulse-agent/commit/339d5bc)`) to run **hard pre-route before typo correction** — the regex rules are more reliable and shouldn't be undermined by the correction algorithm. This took two routing eval failures to catch.

### Phase 4: Intelligence (April 12 – April 17)

**What we built:** ORCA routing, adaptive tool selection, Mission Control, plan runtime, SLO registry, postmortems.

- **[v2.1.0](https://github.com/alimobrem/pulse-agent/releases/tag/v2.1.0)** (Apr 12, `[1980d49](https://github.com/alimobrem/pulse-agent/commit/1980d49)`) — Mission Control analytics with Vertex AI cost tracking. Specs: [Mission Control redesign](superpowers/specs/2026-04-12-mission-control-redesign-design.md). Plans: [backend](superpowers/plans/2026-04-12-mission-control-backend.md), [frontend](superpowers/plans/2026-04-12-mission-control-frontend.md).
- **[v2.2.0](https://github.com/alimobrem/pulse-agent/releases/tag/v2.2.0)** (Apr 12, `[6519218](https://github.com/alimobrem/pulse-agent/commit/6519218)`) — **Adaptive tool selection engine.** TF-IDF prediction learned which tools were relevant for each query from real usage. An LLM fallback (Haiku) handled cold start. Co-occurrence bundles tracked tools called together. Negative signals actively suppressed tools that were offered but never used. The result: `ALWAYS_INCLUDE` dropped from 12 tools to 5. Spec: [adaptive tool selection](superpowers/specs/2026-04-12-adaptive-tool-selection-design.md). Plan: [implementation](superpowers/plans/2026-04-12-adaptive-tool-selection.md). Key file: `[sre_agent/tool_predictor.py](../sre_agent/tool_predictor.py)`.
- **[v2.3.0](https://github.com/alimobrem/pulse-agent/releases/tag/v2.3.0)** (Apr 14, `[b1a14ba](https://github.com/alimobrem/pulse-agent/commit/b1a14ba)`) — **ORCA multi-signal skill selector.** 6-channel fusion (keyword, temporal, memory, entity, feedback, LLM), bidirectional `conflicts_with` for skills that shouldn't run together, exclusive skills that bypass secondary selection, and parallel multi-skill execution (max 2 concurrent skills with Sonnet-powered synthesis). Key commits: ORCA wiring (`[c1ccad4](https://github.com/alimobrem/pulse-agent/commit/c1ccad4)`), plan templates (`[6076d70](https://github.com/alimobrem/pulse-agent/commit/6076d70)`), plan runtime (`[b69889c](https://github.com/alimobrem/pulse-agent/commit/b69889c)`), SLO registry (`[36614a3](https://github.com/alimobrem/pulse-agent/commit/36614a3)`), postmortem generation (`[5a3d311](https://github.com/alimobrem/pulse-agent/commit/5a3d311)`). Parallel multi-skill spec: [design](superpowers/specs/2026-04-19-parallel-multi-skill-execution-design.md), [plan](superpowers/plans/2026-04-19-parallel-multi-skill-execution.md). Key files: `[sre_agent/skill_router.py](../sre_agent/skill_router.py)`, `[sre_agent/synthesis.py](../sre_agent/synthesis.py)`.
- **[v2.4.0](https://github.com/alimobrem/pulse-agent/releases/tag/v2.4.0)** (Apr 17, `[98288c5](https://github.com/alimobrem/pulse-agent/commit/98288c5)`) — Live tables with K8s watches + PromQL metrics + log enrichment. Topology graph with 5 perspectives (Physical, Logical, Network, Multi-Tenant, Helm). 55/55 routing accuracy on the eval suite. Topology spec: [perspectives design](superpowers/specs/2026-04-19-topology-perspectives-design.md), [plan](superpowers/plans/2026-04-19-topology-perspectives.md). Key files: `[sre_agent/dependency_graph.py](../sre_agent/dependency_graph.py)`, `[sre_agent/k8s_tools/live_table.py](../sre_agent/k8s_tools/live_table.py)`.

**The ORCA evolution:** Skill routing went through three generations:

1. **Keyword matching** (v1.0) — simple regex in `[sre_agent/orchestrator.py](../sre_agent/orchestrator.py)`. Worked for obvious queries, failed on ambiguous ones like "create a capacity planning dashboard" (routes to capacity_planner, should route to view_designer).
2. **LLM classification** (v2.0) — used Haiku for intent classification. More accurate but added 200ms latency and API cost to every query.
3. **ORCA 6-channel fusion** (v2.3) — keyword scoring, temporal context (what skill was used recently), memory patterns, entity detection, feedback signals, and LLM as a tiebreaker. Zero-cost for clear queries (keyword match is definitive), LLM only invoked when channels disagree. Eval framework: `[a0dee88](https://github.com/alimobrem/pulse-agent/commit/a0dee88)` (23 routing scenarios).

**The pre-route handoff fix:** A subtle routing bug: "create a capacity planning dashboard" matched `capacity_planner` on keywords, but the user's *intent* was to create a *dashboard* (view_designer). We added `handoff_to` rules in skill definitions — if the keyword winner's handoff rules match the query, route to the handoff target instead. This required the handoff check to happen during routing, not after.

**Intelligent auto-fix:** The monitor originally fixed crashlooping pods by deleting them (letting the controller recreate). This is a blunt instrument — if the pod is crashlooping because of a bad image or missing config, deleting it just creates a new crashlooping pod. v2.2.0 added a fix planner that classifies root cause (bad_image, oom, missing_config, probe_failure, quota_exceeded, crash_exit, dependency) and selects a targeted strategy (patch_image, patch_resources). Blunt restarts were disabled entirely. Only targeted fixes with confidence >= 0.5 execute. Spec: [autonomous remediation](superpowers/specs/2026-04-17-autonomous-remediation-design.md). Plan: [intelligent autofix](superpowers/plans/2026-04-13-intelligent-autofix.md). Also: autofix eval suite (`[933f093](https://github.com/alimobrem/pulse-agent/commit/933f093)`).

### Phase 5: The Platform (April 17 – April 25)

**What we built:** Ops Inbox, investigation views, user token forwarding, async migration, security hardening.

- **[v2.5.0](https://github.com/alimobrem/pulse-agent/releases/tag/v2.5.0)** (Apr 20, `[e18cc40](https://github.com/alimobrem/pulse-agent/commit/e18cc40)`) — Next-gen phases 1-6 complete. View lifecycle with 3 view types (incident/plan/assessment), multi-user claims, finding dedup, recurrence handling, assessment-to-incident escalation. ViewEventBus for real-time broadcast. Performance tests and operational metrics endpoints. Full revised spec: [next-gen revised design](superpowers/specs/2026-04-20-nextgen-revised-design.md). Phase plans: [phase 1](superpowers/plans/2026-04-20-nextgen-phase1-cleanup.md), [phase 2](superpowers/plans/2026-04-20-phase2-view-components.md), [phase 3a](superpowers/plans/2026-04-20-phase3a-view-schema.md), [phase 3b](superpowers/plans/2026-04-20-phase3b-agent-auto-creation.md).

**Post-v2.5.0** (Apr 20-25) — 80+ commits:

- **Ops Inbox:** 13 proactive task generators (cert expiry, trend prediction, degraded operators, SLO burn, capacity, RBAC drift, etc.), 13 REST endpoints, full frontend with lifecycle transitions, WebSocket live updates. Specs: [v1](superpowers/specs/2026-04-20-ops-inbox-design.md), [v2](superpowers/specs/2026-04-21-ops-inbox-design.md). Plan: [implementation](superpowers/plans/2026-04-21-ops-inbox.md). Key files: `[sre_agent/inbox.py](../sre_agent/inbox.py)`, `[sre_agent/inbox_generators.py](../sre_agent/inbox_generators.py)`, `[sre_agent/api/inbox_rest.py](../sre_agent/api/inbox_rest.py)`.
- **User token forwarding:** All K8s API calls now use the user's OAuth token from `X-Forwarded-Access-Token`. Monitor scans stay on the ServiceAccount. We chose direct token forwarding over impersonation because OpenShift RBAC is heavily group-based — impersonation drops group permissions. Spec: [design](superpowers/specs/2026-04-22-user-token-forwarding-design.md). Plan: [implementation](superpowers/plans/2026-04-22-user-token-forwarding.md). Key commits: `[708db96](https://github.com/alimobrem/pulse-agent/commit/708db96)`, `[6a536be](https://github.com/alimobrem/pulse-agent/commit/6a536be)`.
- **Async Anthropic migration:** Moved the entire agent loop from sync `Anthropic()` to `AsyncAnthropic()`. The LLM streaming now runs natively on the event loop. Tool execution stays sync in `ThreadPoolExecutor` (the K8s Python client is synchronous). Spec: [design](superpowers/specs/2026-04-23-async-anthropic-migration-design.md). Plan: [implementation](superpowers/plans/2026-04-23-async-anthropic-migration.md). Commit: `[1ceae75](https://github.com/alimobrem/pulse-agent/commit/1ceae75)`.
- **Security fixes:** IDOR vulnerabilities in view endpoints, HMAC-based token comparison, ReDoS in input validation regex. See `[SECURITY.md](../SECURITY.md)`.
- **SRE prompt rewrite:** The system prompt was rewritten to emphasize "diagnose fast, act decisively" — the agent was being too cautious, requesting dry-runs before every action. The new prompt has a 5-tool-call budget for diagnosis before it must commit to a fix. Commits: `[ea6411a](https://github.com/alimobrem/pulse-agent/commit/ea6411a)`, `[c86d03e](https://github.com/alimobrem/pulse-agent/commit/c86d03e)`.
- **Investigation views:** Views auto-created from monitor findings. Spec: [design](superpowers/specs/2026-04-22-investigation-view-plan-design.md). Plan: [implementation](superpowers/plans/2026-04-22-investigation-view-plan.md).

### Phase 6: The Operator, and Trust You Can Audit (August 19 – August 25)

**What we built:** A release gate that runs the real model, restorable snapshots, verification contracts, verified-trajectory learning, and the move to an operator.

August was when Pulse stopped being judged by what it claimed and started being judged by what it did.

- **Live judged replay became the release gate** (pulse-agent #48, #55, #59). Until then the "release" eval suite scored hand-written scenario JSON whose `verification_passed` was set in the fixture — it could not fail. The replay harness was rebuilt to run Pulse's *real* configuration (routed skill prompt, real tool schemas) against recorded cluster responses, with an LLM judge for correctness and structure checks for safety. Regressions are confirmed by re-running before they block (#117), because judge variance, not agent variance, was flipping borderline fixtures.
- **Confidence derived from evidence** (#68). Investigations returned a confidence the model asserted about itself; nothing connected it to the evidence list. `evidence.derive_confidence` makes the model's figure a ceiling and caps unsupported claims at 0.35.
- **Learning gated on outcomes, not confidence** (#70, #105, #110). Investigations record a `LearningCandidate`; only a fix the monitor verified is promoted to a skill. The candidates were an in-memory dict until #105 — a pod restart erased everything the agent was about to learn.
- **Restorable snapshots and verification contracts** (#113, v2.24.0). Rollback used to restore from a human-readable sentence. Auto-fix now captures the resource before writing it (migration 034), and the five most-used interactive write tools gained preconditions and tool-specific postconditions.
- **The Helm chart was removed** (#99). [pulse-operator](https://github.com/PulseSRE/pulse-operator) owns deployment through an `OpenShiftPulse` CR. The first bug this surfaced: the CR's `trustLevel` had been a no-op on every deployment because the operator injected a different variable name than the agent read (v2.24.0).
- **Model migration to `claude-sonnet-5`** (v2.25.0) and the **SRE-Bench simulation lane**: the production agent against simulated clusters, scored by an observing backend rather than a judge. It caught the model swap quietly changing the agent's safety posture, which led to a deny policy the model cannot argue past (v2.26.0).

### Phase 7: Durable Execution (August 25 – October 3)

**What we built:** Runtime artifacts that survive restarts, Temporal-backed plans and fix lifecycles, and a full plan language.

- **Nothing written at runtime survived a restart** (v2.27.0). Skills, plans and eval drafts the agent authored lived on the container filesystem. They are now persisted in the database and restored at startup.
- **Plans and fixes on Temporal** (v2.28.0–v2.31.0): durable plan runs, an incident lifecycle workflow (snapshot → apply → verify → wait for recurrence → settle, compensating on failure or cancel), waves, branching and sub-plans, and the learning flywheel on Temporal Schedules. All of it is inert until `PULSE_AGENT_TEMPORAL_HOST` is set, and workflow changes are patch-guarded so histories already in flight replay unchanged.
- **Approval, caller identity and preconditions preserved across every path** (#132–#139): chat, plans, monitor and MCP. A plan-only turn is not offered write tools at all (#147, #156).

### Phase 8: Seeing Clearly (October 16 – October 22)

**What we built:** A view designer that looks at what it builds, a monitor that says what it cannot see, and a security boundary review.

- **The view designer sees the page** (v2.32.0). `preview_dashboard` renders the draft through a render service and hands the model a screenshot plus the widgets that came out empty or broken; every widget's live query runs before saving; dashboards moved to a 12-column grid (migration 037). A nightly rendered-page eval scores what a person would actually see.
- **"A scanner that cannot see is not clean"** (v2.33.0). Stable finding identity, honest resolution (only the owning scanner can resolve a finding), a `degraded` scanner that turns blind spots into findings with a remedy, per-workload baselines, and a ranked "what changed before this" list for every incident.
- **The UI → agent → cluster boundary** (v2.34.0): a non-admin could rewrite the system prompt through a view action; `/log-counts` and `/query` ran as the service account; `describe_resource` returned Secret values. Each fix shipped with a test that fails on the previous code. Chat sessions also learned to survive API errors: retries had never actually run, because the error is raised when the stream is entered, outside the retry block.

### Phase 9: Does It Really Do It? (October 23 – October 27)

**What we built:** Learning that waits for proof, a six-batch functionality review, and an eval for the monitor's own investigations. Released as v2.35.0.

- **Self-learning, end to end** (#183–#187). Approved fixes had never taught anything: a finding cleared by a human-approved fix was labelled *self-healed* and its learning candidate discarded, so at the default trust level nothing verified was ever learned. Skill routing and tool prediction claimed to learn and did not (the predictor learned from its own guesses). `fix_learning.py` (migration 039) records which fix holds for which cause and escalates to a person when a strategy keeps failing. A learning simulator then showed skills being learned from fixes that recurred, so learning now waits until a fix outlasts the recurrence window.
- **The functionality review** (kept in the Pulse project workspace, not in this repo). Six parallel reviews traced every claim in the README, docs and site to the code and reproduced each defect against a disposable database. Six batches of fixes followed (#188–#195, pulse-ui #151–#153), each with a test file that fails on the code before it:
  - **Safety:** rollback compared the live object with itself, so it silently reverted later hotfixes; pause did not stop approvals; an approval could run a different strategy than the one proposed.
  - **Evidence:** the common incident types took the plan path, which reported a fixed 0.7 confidence and no evidence, so the UI always showed "No supporting evidence".
  - **Memory:** memory never reached the model on the server — the agent loop rebuilt the prompt from the skill alone and threw the memory away.
  - **Detection:** a Prometheus blip cleared every firing alert as "self-healed"; every restart re-paged every critical finding (noise counts now persist, migration 040).
  - **Ops:** one failed API call auto-resolved open inbox items; the SLO burn generator could never produce anything.
  - **UI honesty:** failures and "memory switched off" rendered as empty or successful states.
- **Evals for what was not measured** (#196, pulse-ui #153). A new release gate replays the monitor's *own* investigations — the plan and freeform paths — with the real model and gates on the right cause, evidence citing tools actually called, no write attempts, injected instructions not followed, and calibration (Brier score; low confidence where every read fails). On its first run it found that OOM and latency plans reported the *verify* phase instead of the diagnosis, and that two templates ran remediation without approval. The agent now generates the investigation payloads the UI renders, and the UI renders that same file in its tests.

---

## The Numbers


| Metric | Value (measured at v2.35.0, Oct 27 2026) |
| --- | --- |
| Total commits (both repos) | 2,998 (1,386 agent + 1,612 UI) |
| Calendar days | 232 (UI: Mar 9 – Oct 27, Agent: Mar 24 – Oct 27) |
| Tagged releases | 164 (85 agent + 79 UI, including the UI's pre-rename v1–v6 line) |
| Backend tests | 4,225 collected |
| Frontend tests | 2,504 (215 files) |
| Registered native tools | 108 (19 writes) — see `/tools` on a running agent |
| MCP tools | 36 (OpenShift MCP server sidecar) |
| Skills | 7 built-in ([SRE](../sre_agent/skills/sre/skill.md), [Security](../sre_agent/skills/security/skill.md), [View Designer](../sre_agent/skills/view-designer/skill.md), [Capacity Planner](../sre_agent/skills/capacity-planner/skill.md), [Plan Builder](../sre_agent/skills/plan-builder/skill.md), [Postmortem](../sre_agent/skills/postmortem/skill.md), [SLO Management](../sre_agent/skills/slo-management/skill.md)), plus runtime-authored and learned skills |
| Scanners | 32 (reactive, liveness, audit and predictive trend) — see `/monitor/scanners` |
| Plan templates | 12 |
| Inbox generators | 16 |
| Live replay fixtures | 53 chat + 11 monitor-investigation, plus the SRE-Bench simulation lane |
| Fixture-scoring suites | 16 (reports, not gates) |
| Database tables / migrations | ~43 / 40 — see [DATABASE.md](../DATABASE.md) |
| Design specs / implementation plans | 24 / 23 — see [docs/superpowers/](superpowers/) |
| Lines of Python (agent package) | ~77,600 |
| Lines of TypeScript (UI `src/`, including tests) | ~118,500 |

These are a snapshot. Counts change with every release; measure them again rather than quoting them.

---

## Design Decisions and Why We Made Them

### 1. WebSocket over REST for agent communication

**Decision:** Use WebSocket streaming for all agent interactions instead of REST.

**Why:** The agent loop is inherently streaming — thinking indicators, partial text, tool execution progress, component specs, and confirmation gates all need push-based delivery. A REST endpoint would need polling, which adds latency and complexity.

**Trade-off:** WebSocket connections are stateful and harder to load-balance. We accepted this because each agent session is already stateful (conversation history).

**Ref:** [API_CONTRACT.md](../API_CONTRACT.md), `[sre_agent/api/ws_endpoints.py](../sre_agent/api/ws_endpoints.py)`

### 2. Protocol v2: Separate endpoints for interactive and autonomous

**Decision:** `/ws/agent` for user-driven queries, `/ws/monitor` for autonomous scanning.

**Why:** The interactive agent needs confirmation gates, streaming UI, and user context. The monitor needs none of that — it runs unattended with configurable trust levels. Sharing an endpoint would mean every monitor scan negotiates confirmation gates it doesn't use.

**Ref:** `[API_CONTRACT.md](../API_CONTRACT.md)`, Protocol v2 commit `[06951d0](https://github.com/alimobrem/pulse-agent/commit/06951d0)`

### 3. Tool results return `(text, component_spec)` tuples

**Decision:** Tools return both a text description (for Claude to reason about) and a structured component spec (for the UI to render).

**Why:** Claude needs text to reason about results. Users need visual tables, charts, and status indicators. Separating the two means the LLM never sees HTML/JSON meant for the UI, and the UI never parses natural language meant for the LLM.

**Ref:** `[sre_agent/decorators.py](../sre_agent/decorators.py)`, `[sre_agent/component_registry.py](../sre_agent/component_registry.py)`, [data-first generative UI spec](superpowers/specs/2026-04-04-data-first-generative-ui-design.md)

### 4. Prompt caching with `cache_control: ephemeral`

**Decision:** Cache the system prompt across turns within a session.

**Why:** The system prompt (including runbooks, component hints, and cluster context) was 8KB+ even after optimization. Caching it saved ~90% on API costs. The `ephemeral` flag means it's only cached for the session — no stale prompts across deploys.

**Ref:** `[sre_agent/harness.py](../sre_agent/harness.py)`, [prompt optimization spec](superpowers/specs/2026-04-09-prompt-optimization-design.md)

### 5. PostgreSQL over SQLite for everything

**Decision:** v1.13.0 dropped SQLite and went PostgreSQL-only, even for the StatefulSet in-cluster.

**Why:** SQLite can't handle concurrent access from the agent loop, monitor scanner, and API endpoints. We hit WAL locking issues under load. PostgreSQL with connection pooling (`ThreadedConnectionPool`) handles concurrency natively. The cost: a StatefulSet with a PVC, but we needed persistent storage anyway.

**Ref:** `[DATABASE.md](../DATABASE.md)`, `[sre_agent/db.py](../sre_agent/db.py)`, `[chart/templates/postgresql.yaml](https://github.com/PulseSRE/pulse-operator)`

### 6. Pydantic Settings over raw `os.environ`

**Decision:** All configuration flows through `PulseAgentSettings(BaseSettings)` with `PULSE_AGENT`_ prefix.

**Why:** Type safety. `os.environ.get("PULSE_AGENT_TOKEN_FORWARDING", "true")` returns a string — `"false"` is truthy. Pydantic validates types at startup and fails loudly. We also got `.env` file support, default documentation, and IDE autocomplete for free.

**Ref:** `[sre_agent/config.py](../sre_agent/config.py)`, commit `[a7e4673](https://github.com/alimobrem/pulse-agent/commit/a7e4673)`

### 7. Skills as markdown files, not code

**Decision:** Skill definitions are `.md` files with YAML frontmatter, not Python modules.

**Why:** Users should be able to create skills through the chat interface without writing code. The agent can `create_skill` and `edit_skill` by writing markdown. Hot reload means no restart needed. The trade-off: skills can't define custom tool implementations (they can only select from existing tools).

**Ref:** [SKILL_DEVELOPER_GUIDE.md](SKILL_DEVELOPER_GUIDE.md), `[sre_agent/skill_loader.py](../sre_agent/skill_loader.py)`, [extensible architecture spec](superpowers/specs/2026-04-10-extensible-agent-architecture-design.md)

### 8. ORCA multi-signal routing over single-classifier

**Decision:** 6-channel fusion instead of a single LLM classifier for skill routing.

**Why:** A single classifier (even Haiku) adds 200ms and costs tokens on every query. ORCA's keyword channel resolves ~80% of queries at zero cost. The LLM channel only fires when other channels disagree. Parallel multi-skill execution (max 2) handles queries that genuinely span skills.

**Ref:** `[sre_agent/skill_router.py](../sre_agent/skill_router.py)`, `[sre_agent/selector_learning.py](../sre_agent/selector_learning.py)`, routing eval commit `[a0dee88](https://github.com/alimobrem/pulse-agent/commit/a0dee88)`

### 9. Direct token forwarding over impersonation

**Decision:** Forward the user's OAuth token to K8s API calls instead of using `Impersonate-User` headers.

**Why:** OpenShift RBAC is heavily group-based. Impersonation only copies user identity, not group membership. The user would lose permissions they have through group bindings. Direct token forwarding preserves the full identity.

**Ref:** [Token forwarding spec](superpowers/specs/2026-04-22-user-token-forwarding-design.md), `[sre_agent/k8s_client.py](../sre_agent/k8s_client.py)`

### 10. Conversational-first, visual-second, code-third

**Decision:** The primary interface is natural language chat. Visuals appear to confirm understanding. YAML/code only surfaces when explicitly requested.

**Why:** SREs spend their time in terminals and dashboards. We're not replacing those — we're adding a layer that understands intent. "Why is my app slow?" should produce a diagnosis with supporting charts, not a wall of YAML.

**Ref:** [DESIGN_PRINCIPLES.md](../DESIGN_PRINCIPLES.md)

### 11. Live replay as the release gate, fixtures as reports

**Decision:** A release is gated by the real model running against recorded cluster state — chat replay, monitor-investigation replay and the SRE-Bench simulation lane — not by scored scenario JSON.

**Why:** The fixture suites could not fail: their outcome fields were written by hand. A gate that can only pass is not a gate. Live replay costs provider calls and has judge variance, so regressions are re-run before they count; safety checks (forbidden tools, repeated credentials, write attempts) are never forgiven by a retry.

**Ref:** [TESTING.md](../TESTING.md), `[.github/workflows/evals.yml](../.github/workflows/evals.yml)`, `[sre_agent/evals/acceptance_cli.py](../sre_agent/evals/acceptance_cli.py)`

### 12. Learn from outcomes, not from confidence

**Decision:** Nothing is learned from an investigation until the fix it led to has been verified and has outlasted the recurrence window. Which fix to use for a cause is learned from the same verdicts.

**Why:** Self-assessed confidence is exactly what a wrong model is confident about. Learning from it turns one bad diagnosis into a reusable bad skill. A symptom-treating fix passes verification and then comes back, so verification alone is not enough either.

**Ref:** `[sre_agent/trajectory.py](../sre_agent/trajectory.py)`, `[sre_agent/fix_learning.py](../sre_agent/fix_learning.py)`, `[sre_agent/evals/learning_sim.py](../sre_agent/evals/learning_sim.py)`

### 13. The operator owns deployment

**Decision:** Remove the Helm chart; deploy through pulse-operator and an `OpenShiftPulse` CR.

**Why:** Database credentials, the OAuth proxy, RBAC, NetworkPolicies, MCP and Temporal all need reconciliation, not a one-shot template. The trade-off is that every knob is now a contract between two repos, which is how the trust-level variable mismatch went unnoticed — so cross-repo settings get an end-to-end check.

**Ref:** [pulse-operator](https://github.com/PulseSRE/pulse-operator), [RELEASE.md](../RELEASE.md)

### 14. "Couldn't check" is never "fine"

**Decision:** A scanner, generator, list or query that could not read its source reports that it could not — as a `degraded` finding, an unavailable generator, or a load error in the UI — and never as an empty, healthy result.

**Why:** Most of the functionality review's high-severity findings were one bug in different clothes: a failure turned into an empty list, and an empty list read as "all clear". An SRE tool that says "nothing wrong" when it is blind is worse than one that says nothing.

**Ref:** `[sre_agent/monitor/scanner_health.py](../sre_agent/monitor/scanner_health.py)`, `[sre_agent/inbox_generators.py](../sre_agent/inbox_generators.py)`

---

## Mistakes We Made (and How We Fixed Them)

### 1. Silent exception swallowing

**The mistake:** Throughout the codebase, we had `except Exception: pass` blocks. This is standard defensive programming — catch errors, don't crash.

**The consequence:** During the Ops Inbox build, a Vertex AI client bug (`anthropic.Anthropic()` instead of `create_client()`) was hidden for an **entire session**. Every Claude API call failed, but the error handlers swallowed the exceptions silently. The triage pipeline appeared to work locally but produced no results. Three code review agents also missed it because the pattern looked "safe."

**The fix:** Banned `except Exception: pass` entirely. Every exception handler must call `logger.exception()`. Added pre-commit checks to flag bare `pass` in exception handlers.

**Lesson:** "Defensive programming" that hides errors is worse than crashing. At least a crash tells you something is wrong.

### 2. Dead-end UI states

**The mistake:** We shipped drawers and detail panels where certain statuses had no action buttons. An escalated inbox item would show its details but offer no way to do anything about it. An `agent_cleared` finding showed a green checkmark but no "restore" or "dismiss" button.

**The consequence:** The user caught this three separate times and had to ask for fixes each time. We checked the happy path but not all status variants.

**The fix:** Before committing any drawer/detail component, we now enumerate ALL possible values of `item.status` and verify each one renders at least one action button. If any status is missing buttons, the code is not done. Added a UX workflow reviewer agent (commit `[282afef](https://github.com/alimobrem/pulse-agent/commit/282afef)`) and lifecycle tests (commit `[66f3e12](https://github.com/alimobrem/pulse-agent/commit/66f3e12)`).

**Lesson:** UI completeness means every state, not just the common ones.

### 3. The view designer prompt engineering rabbit hole

**The mistake:** We spent multiple iterations trying to make the LLM produce perfect dashboards through prompt engineering alone. Rules like "ALWAYS call cluster_metrics() FIRST" and "NEVER forget time_range" were added to the system prompt.

**The consequence:** The LLM ignored the rules. Every dashboard had the same generic KPI row. Charts rendered as tables because `time_range` was omitted. Validation that blocked saves broke the agent loop (agent calls `create_dashboard`, gets success, then `critique_view` says "not found").

**The fix:** Code enforcement replaced prompt engineering. `verify_query` was removed from the view_designer tool list entirely (it was causing 8-14 Prometheus calls per dashboard, commit `[ad5c830](https://github.com/alimobrem/pulse-agent/commit/ad5c830)`). `time_range` defaults were set in code. Dashboard validation logs warnings but always saves. The generic `cluster_metrics` call was removed from the prompt. Full lessons documented in [view designer tuning spec](superpowers/specs/2026-04-04-view-designer-tuning-design.md).

**Lesson:** For structured output, code constraints beat prompt instructions every time. If you need the LLM to always do X, make X the default in code and let the LLM opt out, not opt in.

### 4. Recharts height collapse

**The mistake:** Charts in generated dashboards rendered as 0-height invisible elements. We used `flex-1` and `h-full` CSS on chart containers.

**The consequence:** Dashboards looked broken — empty white space where charts should be. The data was there, the queries worked, but nothing was visible.

**The fix:** Recharts' `ResponsiveContainer` needs an explicit pixel height, not a flex-based one. `react-grid-layout` cells don't propagate height to children. We switched to `style={{ height: 300 }}` and aligned `rowHeight` between backend (`[sre_agent/layout_engine.py](../sre_agent/layout_engine.py)`) and frontend (`generateDefaultLayout`).

**Lesson:** CSS layout assumptions don't hold inside grid layout libraries. Test visual rendering, not just data correctness.

### 5. Helm secret regeneration on every deploy

**The mistake:** The Helm chart used `randAlphaNum` to generate the WebSocket token and PostgreSQL password. This generates a new random value on every `helm template` / `helm upgrade`.

**The consequence:** Every deploy generated new credentials, breaking all active WebSocket connections and PostgreSQL authentication. The agent pod would restart with a new token while the nginx configmap still had the old one.

**The fix:** Used `lookup()` to check if a Secret already exists. If it does, reuse the existing value. Only generate a new one on fresh install. Also discovered a double-base64 bug: `lookup()` returns already-base64-encoded `.data` values, so running `b64enc` again double-encodes them. See `[chart/templates/_helpers.tpl](https://github.com/PulseSRE/pulse-operator)`.

**Lesson:** Helm's declarative model fights against stateful resources. Always use `lookup()` for secrets.

### 6. The Thanos/ACM compatibility gap

**The mistake:** All 73 PromQL recipes (see `[sre_agent/promql_recipes.py](../sre_agent/promql_recipes.py)`) were written and tested against vanilla OpenShift `prometheus-k8s`. We didn't test against ACM clusters that use Thanos.

**The consequence:** On an ACM cluster, queries with `on(instance) group_left(node) kube_node_info` joins returned 422 errors. Thanos adds external labels and federates metrics differently. Charts rendered NaN from null Prometheus data. The RBAC fix commit `[68a7ecf](https://github.com/alimobrem/pulse-agent/commit/68a7ecf)` added `cluster-monitoring-view` access but the Thanos query incompatibility remains.

**The fix:** Still a TODO. Options: detect Thanos and adjust queries automatically, add Thanos-compatible recipe variants, or query local Prometheus directly bypassing Thanos.

**Lesson:** Test against the actual deployment targets, not just dev environments.

### 7. The SRE prompt was too cautious

**The mistake:** The original SRE system prompt emphasized safety so heavily that the agent would request dry-runs before every action, ask for confirmation multiple times, and hedge its diagnoses with excessive caveats.

**The consequence:** Users felt the agent was slow and indecisive. "Just tell me what's wrong and fix it" was common feedback. The agent would take 10+ tool calls to diagnose what an experienced SRE would see in 2.

**The fix:** Rewrote the SRE prompt with a 5-tool-call budget for diagnosis. "Diagnose fast, act decisively, no dry-runs." Commits: `[ea6411a](https://github.com/alimobrem/pulse-agent/commit/ea6411a)`, `[c86d03e](https://github.com/alimobrem/pulse-agent/commit/c86d03e)`, `[7b2e08a](https://github.com/alimobrem/pulse-agent/commit/7b2e08a)`. The confirmation gate in code (not in the prompt) is the safety mechanism — the prompt doesn't need to be cautious because the code enforces approval for writes.

**Lesson:** Safety mechanisms belong in code, not in prompts. A cautious prompt on top of a code-enforced confirmation gate is redundant and makes the agent worse.

### 8. The MCP field manager conflict

**The mistake:** The toolset toggle API patched the MCP deployment via the K8s API using the default field manager. Helm uses server-side apply with its own field manager.

**The consequence:** After toggling a toolset from the UI, the next `helm upgrade` failed with "conflict with OpenAPI-Generator" because two field managers owned the same fields. The only fix was deleting the deployment and letting Helm recreate it.

**The fix:** All K8s API patches now use `field_manager="helm"` to match Helm's ownership. Commits: `[2e0d49e](https://github.com/alimobrem/pulse-agent/commit/2e0d49e)`, `[8cbc08b](https://github.com/alimobrem/pulse-agent/commit/8cbc08b)`, `[8ec6610](https://github.com/alimobrem/pulse-agent/commit/8ec6610)`.

**Lesson:** Never patch Helm-managed resources with a different field manager. If you need runtime mutation of Helm-managed resources, coordinate through Helm's field manager.

### 9. Thread safety in the agent loop

**The mistake:** The early agent loop used `asyncio.to_thread()` to dispatch LLM streaming, assuming the Anthropic client was synchronous.

**The consequence:** The `AsyncAnthropic` client was already async. Wrapping it in `to_thread()` created a second event loop inside a thread, causing race conditions in streaming output — garbled text, out-of-order tokens, and occasional hangs.

**The fix:** Removed `to_thread()` entirely. LLM streaming runs on the main event loop with `async with`/`async for`. Tool execution stays sync in `ThreadPoolExecutor` because the K8s Python client is synchronous. See [async migration spec](superpowers/specs/2026-04-23-async-anthropic-migration-design.md) and commit `[1ceae75](https://github.com/alimobrem/pulse-agent/commit/1ceae75)`.

**Lesson:** Know your client library's async model before wrapping it.

### 10. Scrollbar CSS specificity wars

**The mistake:** We added `scrollbar-color` (standard CSS) alongside `::-webkit-scrollbar` pseudo-elements for custom scrollbar styling. See the [v2.4.1 changelog](https://github.com/alimobrem/OpenshiftPulse/blob/main/CHANGELOG.md).

**The consequence:** Chrome 121+ ignores `::-webkit-scrollbar` when standard scrollbar properties are set (even via inheritance). Our custom 6px violet scrollbars reverted to 15px gray default scrollbars. This took hours to debug because the CSS looked correct and worked in older Chrome versions.

**The fix:** Removed all `scrollbar-color` and `scrollbar-width` properties. Used ONLY `::-webkit-scrollbar` pseudo-elements. Added a note in [CLAUDE.md](../CLAUDE.md) ("Frontend CSS Gotchas" section) so this doesn't happen again.

**Lesson:** CSS feature detection is not just about support — it's about interaction between old and new specifications.

### 11. Startup probe timing

**The mistake:** The Helm chart's startup probe had `initialDelaySeconds: 5` for the agent container.

**The consequence:** The agent loads 7 skill packages, connects to K8s and PostgreSQL, then starts the HTTP server — this takes ~10s. The probe would fire before uvicorn was listening, causing transient "connection refused" warnings on every deploy. Commit `[c2135f8](https://github.com/alimobrem/pulse-agent/commit/c2135f8)`.

**The fix:** Increased `initialDelaySeconds` to 15. Added a similar fix for PostgreSQL (commit `[05bf955](https://github.com/alimobrem/pulse-agent/commit/05bf955)`).

**Lesson:** Startup probes need to account for application initialization time, not just container startup.

### 12. Claims the code did not keep

**The mistake:** The README and docs described behaviour — rollback rejects concurrent changes, memory makes the agent better, investigations show their evidence — that unit tests "covered" because the tests mocked the private functions where the claim broke.

**The consequence:** Rollback could silently revert a hotfix made after the fix. Memory never reached the model on the server. The UI showed "No supporting evidence" for every monitor investigation. None of it failed a test.

**The fix:** The October functionality review traced each claim to the code and reproduced each defect before fixing it. Every fix ships with a test that fails on the previous code, checked by stashing the fix and re-running.

**Lesson:** A test that mocks the seam where a claim could break proves the mock. Test the claim.

### 13. Evals that could not fail

**The mistake:** The "release" suite scored hand-written scenarios with the outcome already in them; single-turn dashboard fixtures passed whether or not a dashboard was built; the "safety suite" never ran the agent.

**The consequence:** Green gates over real regressions — including a dashboard fix that made the model look up a view that did not exist yet, caught only when the live gate went red on `main`.

**The fix:** Live replay is the gate; fixtures that cannot fail are labelled as reports. Dashboard fixtures are two-turn and must call `create_dashboard`. Safety is gated live, on every attempt.

**Lesson:** Before trusting a gate, make it fail once on purpose.

### 14. A knob with no end-to-end check

**The mistake:** The operator set `PULSE_AGENT_TRUST_LEVEL`; the agent read `PULSE_AGENT_MAX_TRUST_LEVEL`. Both defaulted to 2.

**The consequence:** `spec.agent.trustLevel` was a no-op on every deployment, invisible because the defaults agreed.

**The fix:** The agent accepts both names, and settings that cross a repo boundary get a test that the value set is the value read.

**Lesson:** Matching defaults hide a broken wire.

### 15. A failure that looked like success

**The mistake:** `safe()` returns a `ToolError` object, and callers checked `isinstance(result, str)`. Fetchers returned `[]` on error; UI queries ignored `isError`.

**The consequence:** One 403 auto-resolved every open inbox item a generator owned and hid it for 24 hours; a Prometheus blip cleared every firing alert as self-healed; the UI said "No SLOs defined" when the request failed.

**The fix:** See design decision 14. Failures are typed (`GeneratorUnavailableError`, `report_failure`, `AgentHttpError`) and rendered as failures.

**Lesson:** "Couldn't check" and "checked, nothing there" must never share a representation.

---

## The AI-Assisted Development Story

This project was built almost entirely with AI assistance (Claude Code / Claude Opus). Some observations from the experience:

**What worked well:**

- **Spec-driven development.** We wrote [23 design specs](superpowers/specs/) and [23 implementation plans](superpowers/plans/). Claude executed against these plans with high fidelity. The specs served as both documentation and development instructions.
- **Eval-gated releases.** The eval suite (see [TESTING.md](../TESTING.md)) caught regressions that manual testing would have missed. Every prompt change was tested against 175 scenarios.
- **Memory files as institutional knowledge.** The 35 memory files captured every hard-won lesson. Each new session started with full context of past mistakes and decisions.
- **Parallel agent work.** Using worktrees and parallel agents for independent tasks (e.g., frontend component + backend endpoint) saved significant time.
- **8 specialized Claude Code agents** (see `[.claude/agents/](../.claude/agents/)`) — tool-writer, runbook-writer, protocol-checker, tool-auditor, memory-auditor, security-hardener, test-writer, deploy-validator — each with focused expertise and hooks in `[.claude/settings.json](../.claude/settings.json)`.

**What didn't work:**

- **Prompt-only behavior control.** As documented in the [view designer tuning spec](superpowers/specs/2026-04-04-view-designer-tuning-design.md), telling the LLM "ALWAYS do X" in the system prompt is unreliable. Code enforcement is the only reliable mechanism.
- **Silent failures from defensive coding.** Three code review agents missed the `except: pass` bug because it looked like correct defensive programming. AI reviewers need to be specifically instructed to flag this pattern.
- **UI dead-ends.** The AI consistently tested the happy path but missed edge-case UI states. We learned to explicitly enumerate all status variants before shipping.
- **CSS visual issues.** AI can write correct CSS that renders incorrectly due to library-specific quirks (Recharts height, scrollbar specificity). Visual testing in a real browser is non-negotiable.
- **Believing its own summaries.** An AI session will report a fix as done when the code it changed is not the code that runs. The October review worked because it did the opposite: trace the claim to the code, reproduce the failure, fix it, and prove the new test fails without the fix. It also caught the AI's own mistakes — including a misdiagnosis it had to retract, and a learning bug its own simulator exposed one PR after it was introduced.

---

## What We Can Do Next

### Near-Term (Weeks)

1. **Prove the review fixes on a real cluster.** v2.35.0 is tested against mocks, a disposable database and recorded cluster data. Five items need a real environment: the ACM proxy path for fleet tools, `X-Forwarded-User` spoofing when the UI is reachable without the OAuth proxy, OpenShift service-serving certificates in cert-expiry checks, whether prompt caching actually hits, and the cost of 7-day trend queries on large clusters.
2. **Measure whether memory helps.** Memory now reaches the model; nothing yet shows it improves investigations. Run the investigation replay with memory on and off, with a seeded past incident.
3. **Persist structured evidence.** Investigations stream evidence with its stance and source, but the saved record keeps prose only, so a reloaded card loses which evidence contradicted the cause.
4. **Thanos/ACM PromQL compatibility** and **MCP token forwarding** remain open from earlier phases.

### Medium-Term (Months)

1. **Calibration as a product feature.** The investigation gate now measures calibration offline; the inbox could show how often a confidence level has been right on *this* cluster.
2. **Multi-cluster topology and incidents** through ACM, once the fleet proxy path is verified.
3. **Natural-language alerting rules** that create real PrometheusRules.
4. **Collaborative incident response** on the existing ViewEventBus and live inbox events.

### Long-Term (Vision)

1. **Self-healing that earns its trust level.** Fix learning already escalates a strategy that keeps failing; the next step is promoting categories to automatic fixes only on a measured hold rate.
2. **A skill ecosystem** built on reviewed, outcome-verified skills rather than generated ones.
3. **Compliance-as-conversation** on top of the security tools.

---

## Appendix: All Design Specs


| Date      | Spec                                                                                                                   | Plan                                                                                                                                                                                                                                                                           |
| --------- | ---------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Apr 3     | [Tool Chain Intelligence](superpowers/specs/2026-04-03-tool-chain-intelligence-design.md)                              | [Plan](superpowers/plans/2026-04-03-tool-chain-intelligence.md)                                                                                                                                                                                                                |
| Apr 3     | [Tool Usage Tracking](superpowers/specs/2026-04-03-tool-usage-tracking-design.md)                                      | [Prereqs](superpowers/plans/2026-04-03-tool-tracking-prereqs.md), [Backend](superpowers/plans/2026-04-03-tool-usage-backend.md), [Frontend](superpowers/plans/2026-04-03-tool-usage-frontend.md)                                                                               |
| Apr 4     | [Data-First Generative UI](superpowers/specs/2026-04-04-data-first-generative-ui-design.md)                            | [Plan](superpowers/plans/2026-04-04-data-first-generative-ui.md)                                                                                                                                                                                                               |
| Apr 4     | [Intelligence Loop](superpowers/specs/2026-04-04-intelligence-loop-design.md)                                          | —                                                                                                                                                                                                                                                                              |
| Apr 4     | [Semantic Layout Engine](superpowers/specs/2026-04-04-semantic-layout-engine-design.md)                                | [Plan](superpowers/plans/2026-04-04-semantic-layout-engine.md)                                                                                                                                                                                                                 |
| Apr 4     | [View Designer Tuning](superpowers/specs/2026-04-04-view-designer-tuning-design.md)                                    | —                                                                                                                                                                                                                                                                              |
| Apr 7     | [New Dashboard Components](superpowers/specs/2026-04-07-new-dashboard-components-design.md)                            | [Plan](superpowers/plans/2026-04-07-new-dashboard-components.md)                                                                                                                                                                                                               |
| Apr 9     | [Prompt Optimization](superpowers/specs/2026-04-09-prompt-optimization-design.md)                                      | —                                                                                                                                                                                                                                                                              |
| Apr 10    | [Extensible Agent Architecture](superpowers/specs/2026-04-10-extensible-agent-architecture-design.md)                  | —                                                                                                                                                                                                                                                                              |
| Apr 12    | [Adaptive Tool Selection](superpowers/specs/2026-04-12-adaptive-tool-selection-design.md)                              | [Plan](superpowers/plans/2026-04-12-adaptive-tool-selection.md)                                                                                                                                                                                                                |
| Apr 12    | [Mission Control Redesign](superpowers/specs/2026-04-12-mission-control-redesign-design.md)                            | [Backend](superpowers/plans/2026-04-12-mission-control-backend.md), [Frontend](superpowers/plans/2026-04-12-mission-control-frontend.md)                                                                                                                                       |
| Apr 14    | [Slack-Claude Bridge](superpowers/specs/2026-04-14-slack-claude-bridge-design.md)                                      | [Plan](superpowers/plans/2026-04-14-slack-claude-bridge.md)                                                                                                                                                                                                                    |
| Apr 14    | [Unified Layout Engine](superpowers/specs/2026-04-14-unified-layout-engine-design.md)                                  | —                                                                                                                                                                                                                                                                              |
| Apr 17    | [Autonomous Remediation](superpowers/specs/2026-04-17-autonomous-remediation-design.md)                                | [Intelligent Autofix](superpowers/plans/2026-04-13-intelligent-autofix.md), [Resolution Tracking](superpowers/plans/2026-04-13-resolution-tracking.md)                                                                                                                         |
| Apr 19    | [Parallel Multi-Skill Execution](superpowers/specs/2026-04-19-parallel-multi-skill-execution-design.md)                | [Plan](superpowers/plans/2026-04-19-parallel-multi-skill-execution.md)                                                                                                                                                                                                         |
| Apr 19    | [Skill Executor Refactor](superpowers/specs/2026-04-19-skill-executor-refactor-design.md)                              | —                                                                                                                                                                                                                                                                              |
| Apr 19    | [Topology Perspectives](superpowers/specs/2026-04-19-topology-perspectives-design.md)                                  | [Plan](superpowers/plans/2026-04-19-topology-perspectives.md)                                                                                                                                                                                                                  |
| Apr 20    | [Next-Gen Revised Design](superpowers/specs/2026-04-20-nextgen-revised-design.md)                                      | [Phase 1](superpowers/plans/2026-04-20-nextgen-phase1-cleanup.md), [Phase 2](superpowers/plans/2026-04-20-phase2-view-components.md), [Phase 3a](superpowers/plans/2026-04-20-phase3a-view-schema.md), [Phase 3b](superpowers/plans/2026-04-20-phase3b-agent-auto-creation.md) |
| Apr 20-21 | [Ops Inbox](superpowers/specs/2026-04-20-ops-inbox-design.md) ([v2](superpowers/specs/2026-04-21-ops-inbox-design.md)) | [Plan](superpowers/plans/2026-04-21-ops-inbox.md)                                                                                                                                                                                                                              |
| Apr 22    | [Investigation View](superpowers/specs/2026-04-22-investigation-view-plan-design.md)                                   | [Plan](superpowers/plans/2026-04-22-investigation-view-plan.md)                                                                                                                                                                                                                |
| Apr 22    | [User Token Forwarding](superpowers/specs/2026-04-22-user-token-forwarding-design.md)                                  | [Plan](superpowers/plans/2026-04-22-user-token-forwarding.md)                                                                                                                                                                                                                  |
| Apr 23    | [Async Anthropic Migration](superpowers/specs/2026-04-23-async-anthropic-migration-design.md)                          | [Plan](superpowers/plans/2026-04-23-async-anthropic-migration.md)                                                                                                                                                                                                              |


---

## Final Thoughts

Pulse started as a cluster browser and became an AI SRE platform in 47 days. The next six months were about something harder: making it true. Velocity produced a long list of features; the August and October work found how many of them only appeared to work — fixes that taught nothing, memory that never reached the model, evidence that never reached the screen, gates that could not fail.

The most important design decision wasn't any single technical choice. It was the feedback loop, and it changed shape. Early on it was build → deploy → discover a bug → write a memory. Now it is claim → trace to code → reproduce → fix → prove the test fails without the fix → gate it live. The project improved not just in features but in how it knows whether a feature works.

The real output isn't the code. It's that when Pulse says a pod is crash-looping because a rollout removed `DB_HOST`, at 0.56 confidence, citing the log line it read — that sentence is something you can check.

---

*Last updated: October 27, 2026*
*Pulse Agent v2.35.0 | Pulse UI v2.35.0*
*1,386 + 1,612 = 2,998 commits*
