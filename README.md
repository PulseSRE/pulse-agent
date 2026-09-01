<p align="center">
  <img src="docs/logo.svg" alt="Pulse Agent" width="120" height="120">
</p>

# Pulse Agent

<p>
  <a href="https://github.com/PulseSRE/pulse-agent/releases/tag/v2.28.0"><img src="https://img.shields.io/badge/release-v2.28.0-2563eb?style=for-the-badge" alt="Version"></a>
  <img src="https://img.shields.io/badge/tools-143_(107+36_MCP)-10b981?style=for-the-badge" alt="Tools">
  <img src="https://img.shields.io/badge/skills-7-10b981?style=for-the-badge" alt="Skills">
  <img src="https://img.shields.io/badge/scanners-27-10b981?style=for-the-badge" alt="Scanners">
  <img src="https://img.shields.io/badge/tests-3338-10b981?style=for-the-badge" alt="Tests">
  <img src="https://img.shields.io/badge/eval_suites-16_(192_scenarios)-10b981?style=for-the-badge" alt="Eval Suites">
  <img src="https://img.shields.io/badge/release_gate-97.5%25-10b981?style=for-the-badge" alt="Release Gate">
  <img src="https://img.shields.io/badge/PromQL%20recipes-83-10b981?style=for-the-badge" alt="PromQL Recipes">
  <img src="https://img.shields.io/badge/license-MIT-6366f1?style=for-the-badge" alt="License">
</p>

AI-powered OpenShift SRE and Security Agent built on Claude. Pulse Agent connects to your cluster via the Kubernetes API and uses Claude Opus for diagnostics, incident triage, security audits, and automated remediation -- all through natural language. It pairs with the [OpenShift Pulse](https://github.com/PulseSRE/pulse-ui) UI for rich incident management, or runs standalone as a CLI. Both are deployed together via the [pulse-operator](https://github.com/PulseSRE/pulse-operator) — see [Deploy to OpenShift](#deploy-to-openshift) below.

> **Runs on OpenShift, not vanilla Kubernetes.** The deployed product uses
> `route.openshift.io` for ingress, `oauth.openshift.io` for single sign-on, and
> `config.openshift.io` to discover the cluster's application domain. There is no
> Ingress fallback and no capability detection, so on a cluster without those APIs
> the install fails rather than degrades. OpenShift 4.12+ is required — see the
> [operator's prerequisites](https://github.com/PulseSRE/pulse-operator#prerequisites).
> The CLI below is a developer workflow and will talk to any cluster your
> kubeconfig points at; only the deployed product is OpenShift-bound.

**Docs:** [Operator (install)](https://github.com/PulseSRE/pulse-operator) | [API Contract](API_CONTRACT.md) | [Architecture](docs/ARCHITECTURE.md) | [Database](DATABASE.md) | [Security](SECURITY.md) | [Design Principles](DESIGN_PRINCIPLES.md) | [Testing & Evals](TESTING.md) | [Skill Developer Guide](docs/SKILL_DEVELOPER_GUIDE.md) | [Contributing](CONTRIBUTING.md) | [Changelog](CHANGELOG.md)

## Agent Intelligence (ORCA)

Pulse Agent uses the ORCA (Orchestrated Routing & Classification Architecture) system to route every user query to the right skill with the right tools. This replaces keyword-only routing with multi-signal intelligence.

### Skill Selector (6 channels)

Every incoming query is scored by 6 independent channels. Scores are fused with learned weights and re-ranked:

| Channel | Signal | Weight |
|---------|--------|--------|
| **Keyword** | Skill keyword index (longest-match-first) | 0.30 |
| **Component** | K8s resource types extracted from query (Pod, Deployment, Service, etc.) matched to skill categories | 0.20 |
| **Historical** | Token co-occurrence from past successful skill usages (from `skill_usage` table) | 0.20 |
| **Semantic** | TF-IDF cosine similarity between query and skill descriptions/keywords | 0.15 |
| **Taxonomy** | Alert name prefixes and scanner category matching | 0.10 |
| **Temporal** | Recent-change keywords ("just deployed", "after upgrade") boost operations skills | 0.05 |

Weights are not static -- they are **recomputed from outcomes** via `selector_learning.py`. The system analyzes `skill_selection_log` entries (correct selections vs. overrides) and adjusts channel weights to optimize routing accuracy. Learned weights persist to the database.

### Phased Plan Execution

Complex incidents are resolved through multi-phase plans that progress through stages:

**Triage** -- Identify the problem scope and severity.
**Diagnose** -- Investigate root cause with evidence gathering.
**Remediate** -- Apply fixes (with confirmation gates for write operations).
**Verify** -- Confirm the fix resolved the issue.
**Postmortem** -- Auto-generate a structured incident report.

### Plan Templates

10 built-in plan templates cover the most common incident types:

| Template | Scanner Category | Phases |
|----------|-----------------|--------|
| `crashloop-resolution` | `crashloop` | triage, diagnose, remediate, verify |
| `oom-investigation` | `oom` | triage, diagnose, remediate, verify |
| `node-pressure` | `nodes` | triage, node_diagnostics, drain_cordon, verify |
| `deployment-failure` | `workloads` | triage, change_analysis, rollback_decision, verify |
| `image-pull-error` | `image_pull` | triage, diagnose, remediate, verify |
| `scheduling-failure` | `scheduling` | triage, diagnose, remediate, verify |
| `cert-expiry` | `cert_expiry` | triage, diagnose, verify |
| `operator-degraded` | `operators` | triage, diagnose, verify |
| `security-incident` | `security` | triage, diagnose, remediate, verify |
| `latency-degradation` | `latency` | triage, diagnose, remediate, verify |

When no template matches, the plan builder skill dynamically constructs a plan from the query context.

### Supporting Systems

- **Dependency Graph** -- Live in-memory graph of K8s resources (Pods, Deployments, Services, PVCs, ConfigMaps) connected by ownerReferences, selectors, and volume mounts. Used for blast radius calculation and topology-aware routing.
- **SLO Registry** -- Per-service SLO/SLI tracking with error budget calculation and burn rate alerting. The monitor includes a dedicated SLO burn rate scanner. SLO alerts feed into the skill selector.
- **Change Risk Scoring** -- Pre-deploy risk assessment that analyzes image changes, resource modifications, historical failure rates, time-of-day risk, and blast radius from the dependency graph. Returns a 0-100 risk score with human-readable factors.
- **Auto-Postmortem** -- After plan execution completes, the postmortem skill auto-generates a structured report: timeline, root cause, contributing factors, blast radius, actions taken, and prevention recommendations.
- **Skill Scaffolding** -- When a novel incident (no matching template) is resolved, the system auto-drafts a new `skill.md` with trigger patterns, tool sequences, and investigation framework extracted from the resolution. Stored as `generated_by="auto", reviewed=false` and surfaced in the Toolbox UI for review.

## Skills

7 skills loaded at startup from `sre_agent/skills/`. Each skill is a self-contained directory with `skill.md` (prompt + frontmatter), `evals.yaml` (test scenarios), and optional `components.yaml` or `mcp.yaml`.

| Skill | Description | Categories | Write Access |
|-------|-------------|------------|:------------:|
| **sre** | Cluster diagnostics, incident triage, resource management | diagnostics, workloads, networking, storage, monitoring, operations, gitops | Yes |
| **security** | Security scanning, RBAC analysis, compliance checks | security, networking | No |
| **view_designer** | Dashboard creation and component design | (all tools) | No |
| **capacity_planner** | Capacity analysis, resource forecasting, scaling recommendations | diagnostics, monitoring, workloads | No |
| **plan_builder** | Investigation plans and custom skill creation | diagnostics, workloads, monitoring, operations | Yes |
| **postmortem** | Auto-generates structured postmortem reports from incident data | diagnostics | No |
| **slo_management** | SLO/SLI tracking, error budget analysis, burn rate alerting | monitoring, diagnostics | No |

Skills support handoff: the SRE skill hands off to `security` when it detects scan/RBAC keywords, and to `view_designer` for dashboard requests. User-created skills can be added at runtime without restarting the agent.

See [docs/SKILL_DEVELOPER_GUIDE.md](docs/SKILL_DEVELOPER_GUIDE.md) for creating new skills.

## Features

### SRE Agent
- **Cluster Diagnostics** -- Investigate pod crashes, OOM kills, image pull errors, scheduling problems, and operator degradation
- **Incident Triage** -- Correlate events, pod status, logs, and Prometheus metrics to identify root causes
- **Resource Management** -- Analyze quotas, capacity, utilization, and HPA status across nodes
- **Runbook Execution** -- 10 built-in runbooks. Scale deployments, restart pods, cordon/drain nodes, apply YAML (with confirmation gates)
- **PromQL** -- 83 production-tested recipes across 16 categories, metric discovery, query verification against live clusters
- **Right-Sizing** -- `get_resource_recommendations` compares actual CPU/memory usage to requests/limits via Prometheus

### Security Scanner
- **Pod Security** -- Detect privileged containers, root execution, missing security contexts, dangerous capabilities
- **RBAC Analysis** -- Find overly permissive roles, non-system cluster-admin bindings, wildcard permissions
- **Network Policies** -- Identify namespaces with unrestricted traffic, create deny-all policies
- **Image Security** -- Flag `:latest` tags, missing digest pins, untrusted registries
- **SCC Analysis** -- Review Security Context Constraints and pod assignments (OpenShift)
- **Secret Hygiene** -- Find old unrotated secrets, env-exposed secrets, unused secrets

### Autonomous Monitor
- **27 Scanners** -- 5 availability (crashlooping pods, pending pods, failed workloads, image pull errors, DaemonSet gaps) + 5 audit (config, RBAC, deployments, warning events, auth) + 5 predictive trend (memory/disk pressure forecast, HPA exhaustion, error rate acceleration, operator degradation) + 4 liveness (stuck, hot loop, control plane, degraded) + 2 each for infrastructure, security, monitoring and resources
- **Auto-Fix** -- Trust level 3 auto-fixes safe categories (crashloop pod deletion, deployment restarts). Trust level 4 fixes everything automatically. Rate-limited to 3 fixes per scan with a per-resource attempt cap, and a database-backed kill switch (`POST /monitor/pause`) that survives pod restarts
- **Confidence Scores** -- Every finding, investigation, and action includes a 0-100% confidence score
- **Noise Learning** -- Tracks transient findings and assigns noise scores to suppress flaky alerts
- **Simulation Preview** -- Predict impact, risk, and duration before executing a fix

### Durable Plan Execution (Temporal)
Optional and inert until configured — see [docs/TEMPORAL.md](docs/TEMPORAL.md). Not to be confused with ORCA's *temporal channel* above, which scores recent-change keywords.

- **Executions survive the pod** -- The in-process engine ran a plan as an asyncio task and wrote its record only at the end, so a rollout mid-plan lost the run entirely, with no record and no resume. Definitions became durable with migration 036; this makes executions durable too
- **Approvals can actually wait** -- `approval_required` phases were marked `needs_escalation` and skipped, because an in-process engine cannot block for hours. On Temporal the workflow waits for a signal, so human-in-the-loop plans are possible rather than structurally impossible
- **One interpreter workflow** -- `PulsePlanWorkflow` executes *any* plan definition by walking its phase graph, including plans authored in the UI at runtime. A new plan is data, not a deploy. Sequencing decisions are pure functions (`temporal/sequencing.py`), testable without a Temporal server and incapable of IO; all IO lives in activities, and phase execution still goes through `PlanRuntime._execute_phase` with the same contract check and retry-with-gap
- **Configure with** `PULSE_AGENT_TEMPORAL_HOST` (plus `_NAMESPACE`, `_TASK_QUEUE`, `_APPROVAL_TIMEOUT`). The [operator](https://github.com/PulseSRE/pulse-operator) provisions a server with `spec.temporal.enabled` and injects the host

### Verified Action
Nothing is reported as fixed because a symptom stopped appearing. Every mutating path is a contract: check first, capture an undo, then prove the outcome by reading the cluster.

- **Verification Contracts** (`tool_contracts.py`) -- The five most-used write tools (`restart_deployment`, `scale_deployment`, `delete_pod`, `rollback_deployment`, `cordon_node`) run as precondition read -> snapshot -> action -> postcondition probe. A missing target or permission gap refuses the write *before* anything changes, under the caller's own token
- **Tool-Specific Postconditions** -- A scale-to-0 verifies as 0 ready replicas; a rollback verifies the revision actually moved; a deleted pod verifies through its owning controller. Probes run on the monitor's verification pipeline with a grace window, because a rollout in progress is not a failed rollout
- **Affirmative Health Gate** (`monitor/health_gate.py`) -- Post-fix verification reads the live object and requires it to look healthy. A gate that cannot get a clear answer returns UNVERIFIABLE, which is never treated as success
- **Restorable Snapshots** (`snapshot.py`) -- A copy of the resource's own spec captured immediately before the write, so `POST /fix-history/{id}/rollback` can put it back rather than describing what it used to look like
- **Deny Policy** (`policy.py`) -- Deterministic, config-backed, no model in the loop. Protected namespaces (`PULSE_AGENT_PROTECTED_NAMESPACES`, default `production,openshift-*,kube-system`) and node operations are denied on **both** the chat path and the unattended auto-fix path, which records a `blocked` outcome instead of acting. A denial names the policy and the sanctioned path rather than just refusing
- **Verified-Trajectory Learning** (`trajectory.py`) -- A diagnosis becomes a reusable skill only after its fix is confirmed resolved. A fix that did not hold drops its candidate unlearned

### MCP Integration
- **36 MCP Tools** from the OpenShift MCP server (sidecar pod) across 11 toolsets: core, config, helm, observability, openshift, ossm, netedge, tekton, kiali, kubevirt, kcp
- **Auto-Discovery** -- MCP tools registered alongside native tools at startup
- **Toggle from UI** -- Enable/disable individual toolsets from the Toolbox page

### Cost Observability
- **Prometheus `/metrics`** -- Token usage, estimated USD cost, investigation budget, scanner runs, autofix outcomes exposed as Prometheus counters/gauges for alerting via cluster monitoring stack
- **Budget API** -- `GET /analytics/budget` returns real-time investigation budget (used/remaining) and optional cost budget status
- **Cost Forecast** -- 30-day projected spend based on last 7 days of daily token totals
- **Cost Budget** -- Optional daily dollar-amount cap (`PULSE_AGENT_COST_BUDGET_USD`) pauses investigations when exceeded
- **ServiceMonitor** -- deployed automatically by the [pulse-operator](https://github.com/PulseSRE/pulse-operator) for Prometheus Operator scraping (`spec.monitoring.enabled`, default `true`)

### Self-Improving Agent
- **Incident Memory** -- Every interaction stored with query, tool sequence, resolution, and outcome
- **Learned Runbooks** -- Confirmed resolutions are extracted as reusable runbooks
- **Pattern Detection** -- Identifies recurring issues and time-based patterns
- **Intelligence Loop** -- `intelligence.py` feeds query reliability, error hotspots, dashboard patterns, and token efficiency back into the system prompt
- **Durable Runtime Artifacts** (`artifact_store.py`, migration 036) -- Skills the agent writes at runtime, plan templates, scaffolded eval scenarios and replay fixtures, and their `.versions/` history are written through to PostgreSQL and replayed onto disk at startup. Before this, everything the agent learned lived on the container's overlay filesystem and was erased by the next restart or rollout
- **Authored Plans** -- `POST /plan-templates` creates an investigation plan (not just edits one), validating phase ids and skill names and carrying each phase's `produces` contract. Every write archives the prior body, so `GET /plan-templates/{type}/versions` makes a plan edit reversible
- **Contract-Aware Plan Phases** -- `phase_judge` names which declared `produces` fields a phase failed to return; `contract_missing` is persisted per phase and aggregated by `/analytics/plans`, so "3 partial" becomes "partial because diagnose never produced root_cause"

## Getting Started

### Prerequisites

- **Python 3.12+**
- **Access to a Kubernetes or OpenShift cluster** (`oc login` or valid `~/.kube/config`) — the CLI itself is not OpenShift-bound; the deployed product is
- **Claude API access** via Anthropic API key or Google Vertex AI project
- **PostgreSQL 14+** for data persistence (optional for basic CLI use, required for memory/monitor/views)

### Install

```bash
git clone https://github.com/PulseSRE/pulse-agent.git
cd pulse-agent
pip install -e .
```

### Configure API Access

Pick one:

```bash
# Option A: Vertex AI
export ANTHROPIC_VERTEX_PROJECT_ID=your-gcp-project
export CLOUD_ML_REGION=us-east5
gcloud auth application-default login

# Option B: Anthropic API
export ANTHROPIC_API_KEY=sk-ant-...
```

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `ANTHROPIC_VERTEX_PROJECT_ID` | GCP project for Vertex AI | required* |
| `CLOUD_ML_REGION` | GCP region | required* |
| `ANTHROPIC_API_KEY` | Direct Anthropic API key | required* |
| `PULSE_AGENT_MODEL` | Claude model | `claude-opus-4-6` |
| `PULSE_AGENT_DATABASE_URL` | PostgreSQL connection URL | required for full features |
| `PULSE_AGENT_MEMORY` | Enable self-improving memory | `1` (enabled) |
| `PULSE_AGENT_AUTOFIX_ENABLED` | Enable monitor auto-fix | `true` |
| `PULSE_AGENT_MAX_TRUST_LEVEL` | Server-side max trust level (0-4). Also accepts `PULSE_AGENT_TRUST_LEVEL`, the name the operator injects from `spec.agent.trustLevel` | `2` (ask first) |
| `PULSE_AGENT_PROTECTED_NAMESPACES` | Namespaces where destructive actions are denied on both the chat and auto-fix paths (comma list, `*` wildcards) | `production,openshift-*,kube-system` |
| `PULSE_AGENT_ALLOW_NODE_OPS` | Allow node-level operations (cordon/drain) through chat confirmation | `false` |
| `PULSE_AGENT_RECURRENCE_WINDOW` | Seconds after a verified fix within which the same problem returning is recorded as a recurrence | `1800` |
| `PULSE_AGENT_SCAN_INTERVAL` | Monitor scan interval (seconds) | `60` |
| `PULSE_AGENT_WS_TOKEN` | WebSocket auth token | auto-generated |
| `PULSE_AGENT_HARNESS` | Enable tool selection optimizations | `1` (enabled) |

*One of Vertex AI or Anthropic API key is required.

### Run

```bash
# SRE agent (CLI)
python -m sre_agent.main

# Security scanner (CLI)
python -m sre_agent.main security

# API server (WebSocket + REST, port 8080)
pulse-agent-api
```

### PostgreSQL Setup (Local Development)

For full features (memory, views, tool analytics, SLOs), you need a PostgreSQL instance. The simplest local setup:

```bash
podman run -d --name pulse-pg \
  -p 5433:5432 \
  -e POSTGRES_USER=pulse \
  -e POSTGRES_PASSWORD=pulse \
  -e POSTGRES_DB=pulse_test \
  postgres:16-alpine

export PULSE_AGENT_DATABASE_URL=postgresql://pulse:pulse@localhost:5433/pulse_test
```

Schema migrations are applied automatically on startup.

The test suite (`make test`) uses `PULSE_AGENT_TEST_DATABASE_URL`, defaulting to the
same URL. Create that database as **UTF-8** — a `SQL_ASCII` cluster (the default for
some standalone macOS builds) fails on fixtures containing an em dash, and the error
surfaces as an unrelated-looking empty result rather than an encoding error.

## Deploy to OpenShift

The [pulse-operator README](https://github.com/PulseSRE/pulse-operator#install-via-olm) is the canonical install guide — it is the only place the steps are maintained, so anything here that contradicts it is wrong.

**Install via the [pulse-operator](https://github.com/PulseSRE/pulse-operator)** — an OLM-managed Kubernetes Operator that deploys this agent alongside the [OpenShift Pulse](https://github.com/PulseSRE/pulse-ui) UI and PostgreSQL from a single `OpenShiftPulse` custom resource. See the operator's [README](https://github.com/PulseSRE/pulse-operator#install-via-olm) for the full CatalogSource → Subscription → CR walkthrough.

Key CR fields relevant to this agent (set on the `OpenShiftPulse` resource, not via Helm values):

| Field | Description | Default |
|-------|-------------|---------|
| `spec.vertexAI.projectId` | GCP project (required if using Vertex AI) | -- |
| `spec.anthropicApiKey.existingSecret` | K8s Secret with Anthropic API key | -- |
| `spec.agent.allowWriteOperations` | Enable scale, restart, cordon, delete, apply | `false` |
| `spec.agent.allowSecretAccess` | Enable secret scanning | `false` |
| `spec.agent.mcp.enabled` | Deploy OpenShift MCP server sidecar | `false` |
| `spec.agent.trustLevel` | Autonomy level: 0=observe, 1=suggest, 2=confirm, 3=batch, 4=autonomous | `2` |

`spec.vertexAI` and `spec.anthropicApiKey` are mutually exclusive — the CRD rejects a CR with both set via a validation rule. Neither is required at the CRD level, but the agent has no AI backend without one (the operator emits a `NoAIBackendConfigured` warning event if you omit both). The WebSocket auth token and PostgreSQL credentials are auto-generated once and reused across every reconcile — no manual token management, and no re-generation that would invalidate an existing connection.

### Container Security

- Non-root user (UID 1001) on RHEL UBI9 base image
- `runAsNonRoot`, `readOnlyRootFilesystem`, drops all capabilities
- NetworkPolicy restricts egress to DNS + HTTPS only
- Liveness/readiness probes via `/healthz`

## UI (OpenShift Pulse)

The [OpenShift Pulse](https://github.com/PulseSRE/pulse-ui) frontend is a React/TypeScript application that connects to the agent via WebSocket. Key surfaces:

### Incident Center
6 tabs for full incident lifecycle management:

| Tab | What it shows |
|-----|---------------|
| **Active** | Live findings from the monitor with severity, confidence, and auto-fix controls |
| **Timeline** | Chronological event stream across all scanners |
| **Review Queue** | Proposed actions awaiting human approval (trust level 2) |
| **Postmortems** | Auto-generated postmortem reports from resolved incidents |
| **History** | All past findings and actions with rollback support |
| **Alerts** | Prometheus firing alerts with investigation links |

### Impact Analysis (`/topology`)
Live dependency graph visualization showing resource relationships, blast radius overlays, and change risk scores.

### Toolbox
Consolidated management page with 8 tabs:

| Tab | Purpose |
|-----|---------|
| **Catalog** | All 154 tools organized by agent and category |
| **Skills** | 7 loaded skills with status, keywords, and handoff configuration |
| **Plans** | Plan templates and active plan executions |
| **SLOs** | SLO registry, error budgets, and burn rate status |
| **Connections** | MCP server connections and toolset toggles |
| **Components** | Component type catalog with rendering examples |
| **Usage** | Paginated tool invocation audit log |
| **Analytics** | Top tools, chain patterns, routing accuracy, token efficiency |

### Other Surfaces
- **Mission Control** -- Real-time cluster overview with trust level slider
- **Custom Dashboards** -- User-scoped generative dashboards with share/clone support

## Testing

```bash
pip install -e '.[test]'
python3 -m pytest tests/ -v           # Full test suite
python3 -m pytest tests/test_foo.py   # Single file
make verify                           # Lint + type-check + tests
```

All tests run without a live cluster or API key (fully mocked). See [TESTING.md](TESTING.md) for test conventions, fixtures, and coverage targets.

### Eval Framework

16 eval suites with 192 scenarios for release gating and regression detection:

| Suite | Scenarios | Purpose |
|-------|:---------:|---------|
| `release` | 19 | Primary CI gate (must pass) |
| `selector` | 59 | Skill routing accuracy |
| `sysadmin` | 20 | Real-world sysadmin queries |
| `integration` | 23 | Reliability and failure modes |
| `view_designer` | 11 | Dashboard generation quality |
| `fleet` | 11 | Multi-cluster operations |
| `core` | 6 | Mixed baseline coverage |
| `capacity_planner` | 5 | Capacity analysis accuracy |
| `plan_builder` | 5 | Investigation plan quality |
| `postmortem` | 5 | Auto-postmortem generation |
| `slo_management` | 5 | SLO burn rate and alerting |
| `adversarial` | 5 | Prompt injection and edge cases |
| `errors` | 5 | Error handling and recovery |
| `autofix` | 7 | Auto-fix decision accuracy |
| `safety` | 5 | Safety and compliance checks |
| `scaffolded` | 1 | Auto-generated skill scenarios |

```bash
python3 -m sre_agent.evals.cli --suite release --fail-on-gate   # CI gate
python3 -m sre_agent.evals.cli --suite core --save-baseline     # Save baseline
python3 -m sre_agent.evals.cli --suite core --compare-baseline  # Regression check
```

Current release gate average: **99.6%**.

## Architecture

Simplified overview. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for full details.

```
sre_agent/
  main.py              CLI entry point (Rich UI, streaming)
  serve.py + api/      FastAPI server (WebSocket + REST, 12 modules)
  agent.py             Shared agent loop (Claude API, tool execution, confirmation gates)
  config.py            Pydantic v2 Settings (PULSE_AGENT_ env prefix)

  # ORCA routing
  skill_loader.py      Skill loading, tool selection, query routing
  skill_selector.py    6-channel multi-signal selector
  selector_learning.py Batch weight recomputation from outcomes
  skill_scaffolder.py  Auto-scaffold skills from novel resolutions
  skill_plan.py        Phased plan data structures
  plan_templates/      10 YAML plan templates (crashloop, OOM, nodes, workloads, image_pull, ...)
  postmortem.py        Auto-postmortem from plan outputs
  slo_registry.py      SLO/SLI registry with burn rates
  dependency_graph.py  Live K8s resource dependency graph
  change_risk.py       Pre-deploy risk scoring

  # Tools
  k8s_tools/           41 K8s tools across 11 submodules
  security_tools.py    9 security scanning tools
  fleet_tools.py       5 multi-cluster tools
  gitops_tools.py      6 ArgoCD tools
  predict_tools.py     3 predictive analytics tools
  timeline_tools.py    Incident correlation
  view_tools.py        Dashboard creation + namespace summary
  self_tools.py        Self-description + skill management + K8s API introspection
  handoff_tools.py     Agent-to-agent handoff
  tool_registry.py     Central registry (all tools register at import)

  # Monitor
  monitor/             11 modules: session, scanners, investigations, auto-fix, ...

  # Intelligence
  intelligence.py      Analytics feedback loop into system prompt
  tool_predictor.py    TF-IDF + LLM fallback + co-occurrence tool selection
  tool_chains.py       Bigram tool chain discovery
  tool_usage.py        Audit log (PostgreSQL)
  promql_recipes.py    83 PromQL recipes

  # Infrastructure
  db.py                PostgreSQL abstraction + migrations (v022)
  memory/              Self-improving agent (incidents, runbooks, patterns)
  mcp_client.py        MCP server connections (SSE transport)
  orchestrator.py      Typo correction (~130 K8s misspellings)
  context_bus.py       Cross-agent shared context

```

### WebSocket Endpoints

| Endpoint | Description |
|----------|-------------|
| `WS /ws/agent` | Auto-routing orchestrated agent (ORCA classifies each message) |
| `WS /ws/monitor` | Autonomous monitor (27 scanners, auto-fix, predictions) |

All WebSocket endpoints require `?token=...` query parameter (constant-time comparison). Protocol v2.

---

<p align="center">
  <strong>104 native tools + MCP</strong> &bull; <strong>7 skills</strong> &bull; <strong>27 scanners</strong> &bull; <strong>13 runbooks</strong> &bull; <strong>83 PromQL recipes</strong> &bull; <strong>16 eval suites (192 scenarios)</strong> &bull; <strong>2,848 tests</strong> &bull; <strong>Migration v031</strong> &bull; <strong>Protocol v2</strong>
</p>

<p align="center">
  <a href="https://github.com/PulseSRE/pulse-agent/releases">Releases</a> &bull;
  <a href="https://github.com/PulseSRE/pulse-ui">Pulse UI</a> &bull;
  <a href="https://github.com/PulseSRE/pulse-agent/issues">Issues</a>
</p>

<p align="center">MIT License</p>
