# Pulse Agent

<p>
  <a href="https://github.com/alimobrem/pulse-agent/releases/tag/v2.3.0"><img src="https://img.shields.io/badge/release-v2.3.0-2563eb?style=for-the-badge" alt="Version"></a>
  <img src="https://img.shields.io/badge/tools-122_(86+36_MCP)-10b981?style=for-the-badge" alt="Tools">
  <img src="https://img.shields.io/badge/skills-7-10b981?style=for-the-badge" alt="Skills">
  <img src="https://img.shields.io/badge/scanners-18-10b981?style=for-the-badge" alt="Scanners">
  <img src="https://img.shields.io/badge/tests-1676-10b981?style=for-the-badge" alt="Tests">
  <img src="https://img.shields.io/badge/eval_suites-11_(98_scenarios)-10b981?style=for-the-badge" alt="Eval Suites">
  <img src="https://img.shields.io/badge/release_gate-98.1%25-10b981?style=for-the-badge" alt="Release Gate">
  <img src="https://img.shields.io/badge/PromQL%20recipes-73-10b981?style=for-the-badge" alt="PromQL Recipes">
  <img src="https://img.shields.io/badge/license-MIT-6366f1?style=for-the-badge" alt="License">
</p>

AI-powered OpenShift/Kubernetes SRE and Security Agent built on Claude. Pulse Agent connects to your cluster via the Kubernetes API and uses Claude Opus for diagnostics, incident triage, security audits, and automated remediation -- all through natural language. It pairs with the [OpenShift Pulse](https://github.com/alimobrem/OpenshiftPulse) UI for rich incident management, or runs standalone as a CLI.

**Docs:** [API Contract](API_CONTRACT.md) | [Architecture](docs/ARCHITECTURE.md) | [Security](SECURITY.md) | [Design Principles](DESIGN_PRINCIPLES.md) | [Testing & Evals](TESTING.md) | [Skill Developer Guide](docs/SKILL_DEVELOPER_GUIDE.md) | [Contributing](CONTRIBUTING.md) | [Changelog](CHANGELOG.md)

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

6 built-in plan templates cover the most common incident types:

| Template | Incident Type | Phases |
|----------|---------------|--------|
| `crashloop-resolution` | CrashLoopBackOff pods | triage, diagnose, remediate, verify |
| `oom-investigation` | OOM-killed containers | triage, diagnose, remediate, verify |
| `node-pressure` | Node CPU/memory/disk pressure | triage, diagnose, remediate, verify |
| `deployment-failure` | Failed rollouts | triage, diagnose, remediate, verify |
| `security-incident` | Security posture issues | triage, diagnose, remediate, verify |
| `latency-degradation` | Service latency spikes | triage, diagnose, remediate, verify |

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
- **PromQL** -- 73 production-tested recipes across 16 categories, metric discovery, query verification against live clusters
- **Right-Sizing** -- `get_resource_recommendations` compares actual CPU/memory usage to requests/limits via Prometheus

### Security Scanner
- **Pod Security** -- Detect privileged containers, root execution, missing security contexts, dangerous capabilities
- **RBAC Analysis** -- Find overly permissive roles, non-system cluster-admin bindings, wildcard permissions
- **Network Policies** -- Identify namespaces with unrestricted traffic, create deny-all policies
- **Image Security** -- Flag `:latest` tags, missing digest pins, untrusted registries
- **SCC Analysis** -- Review Security Context Constraints and pod assignments (OpenShift)
- **Secret Hygiene** -- Find old unrotated secrets, env-exposed secrets, unused secrets

### Autonomous Monitor
- **18 Scanners** -- 17 infrastructure scanners (crashlooping pods, pending pods, failed deployments, node pressure, certificate expiry, firing alerts, OOM-killed pods, image pull errors, degraded operators, DaemonSet gaps, HPA saturation, security posture, plus 5 audit scanners) + 1 SLO burn rate scanner
- **Auto-Fix** -- Trust level 3 auto-fixes safe categories (crashloop pod deletion, deployment restarts). Trust level 4 fixes everything automatically with rollback snapshots
- **Confidence Scores** -- Every finding, investigation, and action includes a 0-100% confidence score
- **Noise Learning** -- Tracks transient findings and assigns noise scores to suppress flaky alerts
- **Simulation Preview** -- `POST /simulate` predicts impact, risk, and duration before execution

### MCP Integration
- **36 MCP Tools** from the OpenShift MCP server (sidecar pod) across 11 toolsets: core, config, helm, observability, openshift, ossm, netedge, tekton, kiali, kubevirt, kcp
- **Auto-Discovery** -- MCP tools registered alongside native tools at startup
- **Toggle from UI** -- Enable/disable individual toolsets from the Toolbox page

### Self-Improving Agent
- **Incident Memory** -- Every interaction stored with query, tool sequence, resolution, and outcome
- **Learned Runbooks** -- Confirmed resolutions are extracted as reusable runbooks
- **Pattern Detection** -- Identifies recurring issues and time-based patterns
- **Intelligence Loop** -- `intelligence.py` feeds query reliability, error hotspots, dashboard patterns, and token efficiency back into the system prompt

## Getting Started

### Prerequisites

- **Python 3.12+**
- **Access to a Kubernetes or OpenShift cluster** (`oc login` or valid `~/.kube/config`)
- **Claude API access** via Anthropic API key or Google Vertex AI project
- **PostgreSQL 14+** for data persistence (optional for basic CLI use, required for memory/monitor/views)

### Install

```bash
git clone https://github.com/alimobrem/pulse-agent.git
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
| `PULSE_AGENT_MAX_TRUST_LEVEL` | Server-side max trust level (0-4) | `3` |
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

Schema migrations (currently at v016) are applied automatically on startup.

## Deploy to OpenShift

### Recommended: Unified Deploy

The deploy script in the UI repo builds both images, pushes to your container registry, and runs Helm upgrade:

```bash
# Prerequisites
oc login https://api.your-cluster:6443
podman login quay.io  # or your registry

# Clone both repos
git clone https://github.com/alimobrem/pulse-agent.git
git clone https://github.com/alimobrem/OpenshiftPulse.git

# Deploy everything (UI + Agent)
cd OpenshiftPulse
./deploy/deploy.sh
```

What `deploy.sh` does:
1. Builds the React/TypeScript UI with rspack
2. Builds Agent and UI container images in parallel with Podman
3. Pushes both images to Quay.io (or your configured registry)
4. Runs `helm upgrade` with the umbrella chart (agent deploys first, UI reads the auto-generated WS token)

Useful flags:
```bash
./deploy/deploy.sh --dry-run           # Preview without applying
./deploy/deploy.sh --skip-build        # Redeploy with existing images
./deploy/deploy.sh --set agent.mcp.enabled=true  # Deploy with MCP enabled
```

### Helm Values

Key values to configure:

| Value | Description | Default |
|-------|-------------|---------|
| `vertexAI.projectId` | GCP project (required if using Vertex AI) | -- |
| `anthropicApiKey.existingSecret` | K8s Secret with Anthropic API key | -- |
| `rbac.allowWriteOperations` | Enable scale, restart, cordon, delete, apply | `false` |
| `rbac.allowSecretAccess` | Enable secret scanning | `false` |
| `mcp.enabled` | Deploy OpenShift MCP server sidecar | `true` |
| `memory.enabled` | Enable self-improving agent memory | `true` |

The chart requires either `vertexAI.projectId` or `anthropicApiKey.existingSecret`. Install will fail with a clear error if neither is set. The WebSocket auth token is auto-generated as a Kubernetes Secret on first install.

### Container Security

- Non-root user (UID 1001) on RHEL UBI9 base image
- `runAsNonRoot`, `readOnlyRootFilesystem`, drops all capabilities
- NetworkPolicy restricts egress to DNS + HTTPS only
- Liveness/readiness probes via `/healthz`

## UI (OpenShift Pulse)

The [OpenShift Pulse](https://github.com/alimobrem/OpenshiftPulse) frontend is a React/TypeScript application that connects to the agent via WebSocket. Key surfaces:

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
| **Catalog** | All 122 tools organized by agent and category |
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
python3 -m pytest tests/ -v           # All 1,676 tests
python3 -m pytest tests/test_foo.py   # Single file
make verify                           # Lint + type-check + tests
```

All tests run without a live cluster or API key (fully mocked). See [TESTING.md](TESTING.md) for test conventions, fixtures, and coverage targets.

### Eval Framework

11 eval suites with 98 scenarios for release gating and regression detection:

| Suite | Scenarios | Purpose |
|-------|:---------:|---------|
| `release` | 12 | Primary CI gate (must pass) |
| `selector` | 23 | Skill routing accuracy |
| `sysadmin` | 20 | Real-world sysadmin queries |
| `view_designer` | 7 | Dashboard generation quality |
| `core` | 6 | Mixed baseline coverage |
| `integration` | 7 | Reliability and failure modes |
| `adversarial` | 5 | Prompt injection and edge cases |
| `errors` | 5 | Error handling and recovery |
| `fleet` | 5 | Multi-cluster operations |
| `autofix` | 5 | Auto-fix decision accuracy |
| `safety` | 3 | Safety and compliance checks |

```bash
python -m sre_agent.evals.cli --suite release --fail-on-gate   # CI gate
python -m sre_agent.evals.cli --suite core --save-baseline     # Save baseline
python -m sre_agent.evals.cli --suite core --compare-baseline  # Regression check
```

Current release gate average: **98.1%**.

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
  plan_templates/      6 YAML plan templates (crashloop, OOM, node-pressure, ...)
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
  monitor/             10 modules: session, scanners, investigations, auto-fix, ...

  # Intelligence
  intelligence.py      Analytics feedback loop into system prompt
  tool_predictor.py    TF-IDF + LLM fallback + co-occurrence tool selection
  tool_chains.py       Bigram tool chain discovery
  tool_usage.py        Audit log (PostgreSQL)
  promql_recipes.py    73 PromQL recipes

  # Infrastructure
  db.py                PostgreSQL abstraction + migrations (v016)
  memory/              Self-improving agent (incidents, runbooks, patterns)
  mcp_client.py        MCP server connections (SSE transport)
  orchestrator.py      Typo correction (~130 K8s misspellings)
  context_bus.py       Cross-agent shared context

chart/                 Helm chart (deployment, RBAC, PostgreSQL StatefulSet, NetworkPolicy)
```

### WebSocket Endpoints

| Endpoint | Description |
|----------|-------------|
| `WS /ws/agent` | Auto-routing orchestrated agent (ORCA classifies each message) |
| `WS /ws/sre` | Direct SRE agent |
| `WS /ws/security` | Direct security scanner |
| `WS /ws/monitor` | Autonomous monitor (18 scanners, auto-fix, predictions) |

All WebSocket endpoints require `?token=...` query parameter (constant-time comparison). Protocol v2.

---

<p align="center">
  <strong>122 tools (86 native + 36 MCP)</strong> &bull; <strong>7 skills</strong> &bull; <strong>18 scanners</strong> &bull; <strong>10 runbooks</strong> &bull; <strong>73 PromQL recipes</strong> &bull; <strong>11 eval suites (98 scenarios)</strong> &bull; <strong>1,676 tests</strong> &bull; <strong>Migration v016</strong> &bull; <strong>Protocol v2</strong>
</p>

<p align="center">
  <a href="https://github.com/alimobrem/pulse-agent/releases">Releases</a> &bull;
  <a href="https://github.com/alimobrem/OpenshiftPulse">Pulse UI</a> &bull;
  <a href="https://github.com/alimobrem/pulse-agent/issues">Issues</a>
</p>

<p align="center">MIT License</p>
