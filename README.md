# Pulse Agent

<p>
  <a href="https://github.com/alimobrem/pulse-agent/releases/tag/v1.13.1"><img src="https://img.shields.io/badge/release-v1.13.1-2563eb?style=for-the-badge" alt="Version"></a>
  <img src="https://img.shields.io/badge/tools-105-10b981?style=for-the-badge" alt="Tools">
  <img src="https://img.shields.io/badge/scanners-16-10b981?style=for-the-badge" alt="Scanners">
  <img src="https://img.shields.io/badge/tests-1078-10b981?style=for-the-badge" alt="Tests">
  <img src="https://img.shields.io/badge/PromQL%20recipes-73-10b981?style=for-the-badge" alt="PromQL Recipes">
  <img src="https://img.shields.io/badge/license-MIT-6366f1?style=for-the-badge" alt="License">
</p>

AI-powered OpenShift/Kubernetes SRE and Security Agent built on Claude.

Pulse Agent connects directly to your cluster's Kubernetes API and uses Claude Opus to diagnose issues, triage incidents, manage resources, execute runbooks, and perform security audits — all through a conversational interface. Integrates with [OpenShift Pulse](https://github.com/alimobrem/OpenshiftPulse) for rich UI rendering, or runs standalone as a CLI. Includes 73 production-tested PromQL recipes, a semantic layout engine for dashboard generation, and an intelligence loop that feeds analytics back into the system prompt.

## Features

### SRE Agent
- **Cluster Diagnostics** — Investigate pod failures, crash loops, OOM kills, image pull errors, scheduling problems
- **Incident Triage** — Correlate events, pod status, logs, and Prometheus metrics to identify root causes
- **Resource Management** — Analyze quotas, capacity, utilization, and HPA status across nodes
- **Runbook Execution** — Scale deployments, restart pods, cordon/drain nodes, apply YAML manifests (with confirmation)
- **Alerting & Metrics** — Query firing alerts from Alertmanager, run PromQL queries, discover available metrics via `discover_metrics`, verify queries before use via `verify_query`, and draw from 73 production-tested PromQL recipes across 16 categories
- **Cluster Operations** — Inspect StatefulSets, DaemonSets, Jobs, CronJobs, Ingresses, Routes, and OLM operators
- **Generic Resource Tools** — `list_resources` and `describe_resource` work with any K8s resource type including CRDs via the Table API
- **Interactive Debugging** — `exec_command` for kubectl exec, `search_logs` for multi-pod log search, `test_connectivity` for network testing
- **Right-Sizing** — `get_resource_recommendations` compares actual CPU/memory usage to requests/limits via Prometheus

### Security Scanner
- **Pod Security** — Detect privileged containers, root execution, missing security contexts, dangerous capabilities
- **RBAC Analysis** — Find overly permissive roles, non-system cluster-admin bindings, wildcard permissions
- **Network Policies** — Identify namespaces with unrestricted east-west traffic, create deny-all policies
- **Image Security** — Flag `:latest` tags, missing digest pins, untrusted registries (configurable)
- **SCC Analysis** — Review Security Context Constraints and pod SCC assignments (OpenShift)
- **Secret Hygiene** — Find old unrotated secrets, env-exposed secrets, unused secrets

### Error Intelligence
- **Token Identity Fallback** — `_get_current_user` falls back to token-hash identity when TokenReview fails, enabling ROSA and managed cluster compatibility
- **Structured Error Types** — ToolError classification with 7 categories (permission, not_found, conflict, validation, server, network, quota) and actionable suggestions
- **Error Tracking** — Thread-safe ring buffer (500 entries) with per-category aggregation and top-tool breakdown
- **Health Endpoint** — `/health` returns circuit breaker state, error summary, and recent errors
- **Database Resilience** — `@db_safe` decorator on all memory operations prevents crashes on database errors

### Autonomous Monitor
- **Continuous Scanning** — 60-second scan interval via `/ws/monitor` endpoint, pushing findings to the Pulse UI in real time
- **16 Scanners** — Crashlooping pods, pending pods, failed deployments, node pressure, certificate expiry, firing alerts, OOM-killed pods, image pull errors, degraded operators, DaemonSet gaps, HPA saturation, plus 5 audit scanners (config changes, RBAC, deployments, warning events, auth)
- **Warning-Severity Investigations** — Monitor now investigates warning findings, not just critical, for earlier detection
- **Default Namespace Scanning** — `default` namespace removed from skip list so user workloads are always detected
- **Auto-Fix at Trust Level 3** — Automatically applies fixes for safe categories (crashloop pod deletion, deployment restarts) without user approval
- **Auto-Fix at Trust Level 4** — Applies all fixable findings automatically, with rollback snapshots for every action
- **Confidence Scores** — Every finding, investigation, and action includes a confidence score (0-100%) so you know exactly how much to trust each suggestion
- **Resolution Events** — When findings resolve (auto-fix or self-healed), the monitor emits `resolution` events so the UI can celebrate wins
- **Morning Briefing** — `GET /briefing` endpoint returns time-aware greeting, action summary, and investigation count for the last N hours
- **Reasoning Chains** — Investigation reports include `evidence` (facts that support the diagnosis) and `alternativesConsidered` (hypotheses ruled out)
- **Simulation Preview** — `POST /simulate` endpoint predicts impact, risk level, and estimated duration for any action before execution
- **Noise Learning** — Tracks transient findings that self-resolve; assigns `noiseScore` to suppress flaky alerts
- **Audit Scanners** — 5 audit scanners: config change correlation (ConfigMap update → pod crash), RBAC change detection (new cluster-admin bindings), deployment rollout monitoring, high-frequency warning events, and auth anomalies (kubeadmin, login failures, SA tokens, OAuth clients)
- **Finding Lifecycle** — Stale finding cleanup after each scan cycle, severity escalation on repeat occurrences

### Orchestrator
- **Auto-Routing Agent** — `/ws/agent` endpoint classifies each message as SRE or Security intent and routes to the appropriate agent with the correct system prompt and tool set
- **Keyword-Based Classification** — Fast intent detection via keyword scoring (no LLM call for routing)
- **Shared Context Bus** — Cross-agent context sharing: SRE and Security agents publish findings to a shared bus, enabling handoff tools (`request_security_scan`, `request_sre_investigation`)

### Auto-Fix
- **Trust Level 3 (Safe Categories)** — Fixes only pre-approved safe categories automatically; all others require user approval via the UI
- **Trust Level 4 (Full Autonomous)** — Fixes all auto-fixable findings without prompting
- **Fix Categories:**
  - `crashloop` — Deletes crash-looping pods to trigger fresh scheduling
  - `workloads` — Restarts degraded deployments via rollout restart
  - `image_pull` — Restarts the owning controller (Deployment/StatefulSet/DaemonSet) for ImagePullBackOff pods
- **Rollback** — Every applied fix records a `beforeState` snapshot. Deployment, StatefulSet, and DaemonSet restarts are rollbackable via `POST /api/agent/actions/:id/rollback` or the UI's Actions tab
- **Confirmation Gate** — Write operations still require the programmatic confirmation round-trip at all trust levels; auto-fix pre-approves on behalf of the user

### Database
- **PostgreSQL Only** — All data stored in PostgreSQL (fix history, incident memory, runbooks, patterns, views). Configured via `PULSE_AGENT_DATABASE_URL`.
- **Connection Pooling** — `ThreadedConnectionPool` with configurable min/max connections (`PULSE_AGENT_DB_POOL_MIN/MAX`). Thread-local connection tracking for write sequences.
- **Schema Migrations** — Version-tracked forward-only migrations via `db_migrations.py`. Applied automatically on startup.

### Pydantic Configuration
- **`PulseAgentSettings`** — All configuration via `pydantic-settings` with `PULSE_AGENT_` env prefix, `.env` file support, and type validation at startup (`config.py`)

### PromQL Recipes
- **73 Production-Tested Queries** — Curated from 7 OpenShift/K8s repos (openshift/console, cluster-monitoring-operator, kube-state-metrics, node_exporter, prometheus-operator, cluster-version-operator, ACM)
- **16 Categories** — CPU, memory, network, disk, pod health, node health, API server, etcd, scheduling, HPA, alerts, operators, containers, namespaces, cluster, and custom
- **Metric Discovery** — `discover_metrics` tool queries Prometheus for available metrics before writing PromQL, preventing hallucinated metric names
- **Query Verification** — `verify_query` tool tests PromQL queries against the live cluster before embedding in dashboards

### Semantic Layout Engine
- **Role-Based Auto-Layout** — Replaced 5 fixed dashboard templates with a semantic layout engine (`layout_engine.py`) that arranges widgets based on their role (KPI, chart, table, status) and content relationships
- **Adaptive Grid** — Automatically assigns widget sizes and positions based on component type and dashboard composition

### View Validator
- **Pre-Save Validation** — `view_validator.py` validates dashboard components before save: deduplication, schema conformance, title uniqueness, widget count limits
- **Quality Critic** — Enhanced `view_critic.py` scoring rubric evaluates dashboard quality on multiple dimensions before persisting

### Intelligence Loop
- **Analytics Feedback** — `intelligence.py` feeds operational analytics back into the system prompt: query reliability scores, dashboard generation patterns, error hotspots, and token efficiency metrics
- **Token Usage Tracking** — Records input/output/cache tokens per turn from the Claude API for cost visibility and optimization
- **Prompt Optimization** — SRE system prompt reduced from 28KB to 8KB (71% reduction) via selective component schema injection and selective runbook injection

### Self-Improving Agent
- **Incident Memory** — Stores every interaction with query, tool sequence, resolution, and outcome in the database
- **Learned Runbooks** — When you confirm a resolution, the tool sequence is automatically extracted as a reusable runbook
- **Pattern Detection** — Identifies recurring issues and time-based patterns across incident history
- **Self-Evaluation** — Scores each interaction on resolution (40%), efficiency (30%), safety (20%), and speed (10%)
- **Adaptive Prompting** — Augments the system prompt with relevant past incidents and runbooks before each turn

## Quick Start

### Prerequisites

- Python 3.11+
- Access to a Kubernetes/OpenShift cluster (`oc login` or valid `~/.kube/config`)
- Claude API access via [Vertex AI](#vertex-ai) or [Anthropic API](#anthropic-api)

### Install

```bash
git clone https://github.com/alimobrem/pulse-agent.git
cd pulse-agent
pip install -e .
```

### Configure API Access

#### Vertex AI

```bash
export ANTHROPIC_VERTEX_PROJECT_ID=your-gcp-project
export CLOUD_ML_REGION=us-east5
gcloud auth application-default login
```

#### Anthropic API

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

### Configuration

| Environment Variable | Description | Default |
|---------------------|-------------|---------|
| `ANTHROPIC_VERTEX_PROJECT_ID` | GCP project for Vertex AI | |
| `CLOUD_ML_REGION` | GCP region for Vertex AI | |
| `ANTHROPIC_API_KEY` | Direct Anthropic API key (alternative to Vertex) | |
| `PULSE_AGENT_MODEL` | Claude model to use | `claude-opus-4-6` |
| `PULSE_AGENT_MAX_TOKENS` | Max output tokens per response | `16000` |
| `PULSE_AGENT_MEMORY` | Enable self-improving memory (`1`/`true`) | `1` (enabled) |
| `PULSE_AGENT_DATABASE_URL` | PostgreSQL connection URL | Set by Helm chart |
| `PULSE_AGENT_AUTOFIX_ENABLED` | Enable/disable monitor auto-fix | `true` |
| `PULSE_AGENT_MAX_TRUST_LEVEL` | Server-side max trust level cap (0-4) | `3` |
| `PULSE_AGENT_SCAN_INTERVAL` | Monitor scan interval (seconds) | `60` |
| `PULSE_AGENT_TRUSTED_REGISTRIES` | Comma-separated trusted image registry prefixes | Red Hat, Quay, OpenShift |
| `PULSE_AGENT_HARNESS` | Enable Claude harness optimizations | `1` (enabled) |
| `PULSE_AGENT_WS_TOKEN` | WebSocket authentication token (required for API mode) | |
| `PULSE_AGENT_CB_THRESHOLD` | Circuit breaker failure threshold | `3` |
| `PULSE_AGENT_CB_TIMEOUT` | Circuit breaker recovery timeout (seconds) | `60` |
| `PULSE_AGENT_TOOL_TIMEOUT` | Per-tool execution timeout (seconds) | `30` |

### Run

```bash
# SRE agent (default)
python -m sre_agent.main

# Security scanner
python -m sre_agent.main security

# With self-improving memory enabled
PULSE_AGENT_MEMORY=1 python -m sre_agent.main

# API server
pulse-agent-api
```

### CLI Commands

| Command | Description |
|---------|-------------|
| `help` | Show help and example prompts |
| `clear` | Reset conversation history |
| `mode` | Switch between SRE and Security agents |
| `feedback` | Rate the last response as resolved/not (improves the agent) |
| `quit` | Exit |

## Example Prompts

### SRE Agent
```
sre> Check overall cluster health
sre> Why are pods crashing in namespace monitoring?
sre> Show me all Warning events across the cluster
sre> What's the resource utilization across nodes?
sre> What alerts are currently firing?
sre> Run a PromQL query: rate(container_cpu_usage_seconds_total[5m])
sre> Scale deployment nginx in namespace prod to 5 replicas
sre> Show me all CronJobs that are suspended
```

### Security Scanner
```
sec> Run a full security audit of the cluster
sec> Scan pods in namespace default for security issues
sec> Check RBAC for overly permissive roles
sec> Which namespaces are missing network policies?
sec> Show me all pods running under privileged SCCs
```

## Security

### Safety Controls

- **Confirmation gate** — All interactive write operations require explicit user confirmation. This is enforced programmatically in code, not just via prompt instructions. Every write operation in `/ws/sre`, `/ws/security`, and `/ws/agent` requires a `confirm_request`/`confirm_response` round-trip with nonce verification before execution. Monitor auto-fix at trust level 3+ bypasses this gate by design, relying on rate limiting and cooldown instead.
- **Input validation** — Bounds-checked: replicas (0-100), log tail lines (1-1000), grace period (1-300s). Lists truncated at 200 items.
- **Max iteration guard** — Tool loop capped at 25 iterations.
- **Audit logging** — Every tool invocation logged to `/tmp/pulse_agent_audit.log` in structured JSON. Cluster audit trail via `record_audit_entry` tool writes to a ConfigMap.
- **Read-only by default** — The Helm chart's ClusterRole grants only `get`, `list`, `watch` verbs on cluster resources by default. The SRE agent's write tools (scale, restart, cordon, delete, apply) are registered in the tool list but will fail with `403 Forbidden` unless the chart is deployed with `rbac.allowWriteOperations=true`. The security scanner has no write tools at all.

### Container Security

- Non-root user (UID 1001) on RHEL UBI9 base image
- Pod security context: `runAsNonRoot`, `readOnlyRootFilesystem`, drops all capabilities
- NetworkPolicy restricts egress to DNS + HTTPS only
- Liveness/readiness probes via `/healthz`

### RBAC

The agent's Kubernetes permissions are controlled by the Helm chart's ClusterRole. There are three levels:

**Default (read-only):** `get`, `list`, `watch` on pods, nodes, events, services, namespaces, configmaps, PVCs, resource quotas, deployments, replicasets, statefulsets, daemonsets, jobs, cronjobs, HPAs, metrics, RBAC roles/bindings, network policies, ingresses, routes, SCCs, OLM resources, and cluster version/operators.

**With `rbac.allowWriteOperations=true`:** Adds `delete` on pods, `patch` on nodes (cordon/uncordon), `patch`/`update` on deployments and deployments/scale, `create` on network policies, `create`/`update`/`patch` on configmaps (audit trail), and `patch`/`create` on deployments, statefulsets, daemonsets, jobs, cronjobs, HPAs (for apply_yaml).

**With `rbac.allowSecretAccess=true`:** Adds `get`, `list` on secrets (required for the security scanner's secret hygiene audit).

| Flag | Default | Grants |
|------|---------|--------|
| `rbac.allowWriteOperations` | `false` | Scale, restart, cordon, delete, apply YAML, create NetworkPolicy |
| `rbac.allowSecretAccess` | `false` | List/read secrets (for security scanning) |

### Trust Levels

When connected to the Pulse UI's Monitor endpoint, the agent operates at a configurable trust level:

| Level | Name | Autonomy |
|-------|------|----------|
| 0 | Monitor only | Observe and report findings — no action taken |
| 1 | Suggest fixes | Propose remediations but take no action |
| 2 | Ask before applying | Propose auto-fix actions and wait for explicit user approval |
| 3 | Auto-fix safe categories | Automatically apply fixes for enabled safe categories |
| 4 | Full autonomous | Available only if enabled server-side via `PULSE_AGENT_MAX_TRUST_LEVEL` |

**Important:** Interactive SRE/Security chat tool writes still use confirmation gates. Monitor auto-fix uses a separate trust policy: level 2 requires explicit approval (`action_response`), level 3+ can execute configured safe categories automatically.

### Monitor Auto-Fix Integration with UI Trust Levels

The `/ws/monitor` endpoint implements autonomous cluster scanning with a graduated auto-fix model tied to the UI's trust level slider:

1. **UI sets trust level** — The Pulse UI sends a `subscribe_monitor` message with `trustLevel` and `autoFixCategories`.
   The server clamps trust to `PULSE_AGENT_MAX_TRUST_LEVEL` and publishes supported categories at `GET /monitor/capabilities`.
2. **Agent scans continuously** — The monitor loop runs every 60 seconds, pushing `finding` events for detected issues and `prediction` events for forecasted problems.
3. **Auto-fix decision** — When a finding is auto-fixable and matches an enabled category, the agent checks the trust level:
   - **Level 0-1**: Finding is reported only. The UI displays it in the Monitor view.
   - **Level 2**: Agent sends an `action_report` with `status: "proposed"`. The UI shows an approval card; the user clicks Approve/Reject, which sends `action_response`.
   - **Level 3**: Safe categories (those in `autoFixCategories`) are applied automatically. Others require approval.
   - **Level 4**: All fixable findings are applied automatically. Actions are logged and reversible via the rollback endpoint.
4. **Critical investigation loop** — Critical findings can trigger proactive read-only investigation. The monitor emits `investigation_report` events with suspected cause and recommended fix.
5. **Post-fix verification** — Successful actions are verified on the next scan. The monitor emits `verification_report` and annotates action history with `verificationStatus`.
6. **Rollback** — Every applied fix records a `beforeState` snapshot. The UI's Monitor > History tab shows all actions with a Rollback button that calls `POST /api/agent/actions/:id/rollback`.
7. **Stale finding cleanup** — After each scan cycle, the agent sends a `findings_snapshot` event with all active finding IDs. The UI removes any findings not in the snapshot, preventing stale entries from accumulating.

The trust level is persisted in the UI's `localStorage` (via `trustStore`) and displayed on the Monitor view's Config tab. The confirmation gate remains active at all levels for Kubernetes write operations.

## Tools

### SRE Tools (72+)

| Category | Tools |
|----------|-------|
| **Core diagnostics** | `list_namespaces`, `list_pods`, `describe_pod`, `get_pod_logs`, `list_nodes`, `describe_node`, `get_events`, `list_resources`, `describe_resource`, `search_logs` |
| **Workloads** | `list_deployments`, `describe_deployment`, `list_statefulsets`, `list_daemonsets`, `list_jobs`, `list_cronjobs`, `list_replicasets`, `get_recent_changes`, `get_resource_relationships`, `top_pods_by_restarts` |
| **Networking** | `get_services`, `describe_service`, `list_ingresses`, `list_routes`, `get_endpoint_slices`, `test_connectivity` |
| **Storage & resources** | `get_persistent_volume_claims`, `get_resource_quotas`, `get_configmap`, `list_limit_ranges`, `get_pod_disruption_budgets` |
| **Metrics** | `get_node_metrics`, `get_pod_metrics`, `list_hpas`, `get_prometheus_query`, `get_resource_recommendations`, `discover_metrics`, `verify_query` |
| **Cluster info** | `get_cluster_version`, `get_cluster_operators`, `list_operator_subscriptions`, `get_firing_alerts`, `get_tls_certificates` |
| **Write operations** | `scale_deployment`, `restart_deployment`, `rollback_deployment`, `cordon_node`, `uncordon_node`, `drain_node`, `delete_pod`, `apply_yaml`, `create_network_policy`, `exec_command` |
| **GitOps** | `get_argo_applications`, `get_argo_app_detail`, `get_argo_sync_diff`, `create_argo_application`, `detect_gitops_drift`, `install_gitops_operator` |
| **Views** | `plan_dashboard`, `create_dashboard`, `delete_dashboard`, `clone_dashboard`, `namespace_summary`, `visualize_nodes` |
| **Audit** | `record_audit_entry` |

### Security Tools (9)

| Tool | Description |
|------|-------------|
| `get_security_summary` | High-level security posture overview |
| `scan_pod_security` | Audit pod security contexts and capabilities |
| `scan_images` | Check image tags, digests, and registries |
| `scan_rbac_risks` | Find dangerous RBAC permissions |
| `list_service_account_secrets` | Service account token audit |
| `scan_network_policies` | Find unprotected namespaces |
| `scan_sccs` | List and assess SCCs (OpenShift) |
| `scan_scc_usage` | Map pods to their assigned SCCs |
| `scan_secrets` | Secret rotation and hygiene audit |

### Memory Tools (3, enabled with `PULSE_AGENT_MEMORY=1`)

| Tool | Description |
|------|-------------|
| `search_past_incidents` | Search similar past incidents and their resolutions |
| `get_learned_runbooks` | Retrieve step-by-step runbooks learned from past successes |
| `get_cluster_patterns` | Get detected recurring issues and time-based patterns |

## Self-Improving Agent

Enable with `PULSE_AGENT_MEMORY=1`. The agent learns from every interaction:

```
PULSE_AGENT_MEMORY=1 python -m sre_agent.main
```

### How It Works

1. **Before each turn** — retrieves similar past incidents, matching runbooks, and patterns from the database. Injects into the system prompt (capped at 1500 chars to prevent context bloat).
2. **During the turn** — agent can explicitly query its memory via 3 tools.
3. **After each turn** — evaluates the interaction and stores it with a score.
4. **On user feedback** — type `feedback` then `y` to confirm resolution. The tool sequence is extracted as a reusable runbook.
5. **Pattern detection** — every 10 incidents, analyzes history for recurring keyword clusters and time-based patterns.

### Scoring Rubric

| Factor | Weight | Perfect Score |
|--------|--------|---------------|
| Resolution | 40% | User confirmed resolved |
| Efficiency | 30% | 2-5 tool calls |
| Safety | 20% | 0 rejected tool calls |
| Speed | 10% | Under 60 seconds |

## Claude Harness

Built-in optimizations for getting the most out of Claude (`PULSE_AGENT_HARNESS=1`, on by default):

| Feature | What It Does | Impact |
|---------|-------------|--------|
| **Dynamic Tool Selection** | Categorizes 105 tools into 8 groups, loads only relevant ones per query | 105->15-25 tools, faster + cheaper |
| **Prompt Caching** | Marks system prompt + runbooks with `cache_control: ephemeral` | ~90% cost reduction on context |
| **Cluster Context Injection** | Pre-fetches node count, namespaces, OCP version, failing pods, firing alerts | Saves 2-3 tool calls per query |
| **Component Rendering Hints** | Guides Claude to focus on analysis, not data formatting | Cleaner responses |

### Tool Categories

| Category | Keywords | Example Tools |
|----------|----------|---------------|
| diagnostics | health, crash, error, failing | list_pods, get_events, get_firing_alerts |
| workloads | deploy, scale, rollback, job | list_deployments, scale_deployment, list_jobs |
| networking | service, route, dns, ingress | describe_service, list_routes, get_endpoint_slices |
| security | rbac, scc, audit, privilege | scan_pod_security, scan_rbac_risks |
| storage | pvc, volume, disk | get_persistent_volume_claims, get_resource_quotas |
| monitoring | metric, prometheus, alert, cpu | get_prometheus_query, get_pod_metrics |
| operations | drain, cordon, apply, yaml | drain_node, apply_yaml, cordon_node |
| gitops | git, argo, drift, pr | detect_gitops_drift, propose_git_change |

## WebSocket API

The agent exposes a WebSocket API for integration with web UIs.

```bash
pulse-agent-api  # Starts on port 8080
```

### Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /healthz` | Liveness probe (public) |
| `GET /version` | Protocol version, tool count, features (public) |
| `GET /health` | Circuit breaker state, error summary, autofix status |
| `GET /tools` | List all available tools by mode with confirmation flags |
| `GET /fix-history` | Paginated fix history with filters |
| `GET /briefing` | Cluster activity summary for last N hours |
| `POST /simulate` | Predict impact of an action without executing |
| `GET /predictions` | Predictions (WebSocket-only, returns empty) |
| `GET /memory/export` | Export learned runbooks + patterns |
| `POST /memory/import` | Import runbooks + patterns |
| `GET /memory/stats` | Memory dashboard: incident, runbook, pattern counts |
| `GET /memory/runbooks` | Learned runbooks sorted by success rate |
| `GET /memory/incidents` | Search past incidents by similarity |
| `GET /memory/patterns` | Detected recurring patterns |
| `GET /monitor/capabilities` | Monitor trust/capability limits |
| `POST /monitor/pause` | Emergency kill switch — pause auto-fix |
| `POST /monitor/resume` | Resume auto-fix |
| `GET /context` | Shared context bus entries |
| `GET /eval/status` | Cached eval quality gate snapshot |
| `GET /views` | List views for current user |
| `GET /views/:id` | Get a single view |
| `POST /views` | Save a new view |
| `PUT /views/:id` | Update view (title, layout, positions) |
| `DELETE /views/:id` | Delete a view |
| `POST /views/:id/clone` | Clone own view |
| `POST /views/:id/share` | Generate 24h share link |
| `POST /views/claim/:token` | Claim a shared view |
| `WS /ws/sre?token=...` | SRE agent WebSocket |
| `WS /ws/security?token=...` | Security scanner WebSocket |
| `WS /ws/monitor?token=...` | Autonomous monitor (16 scanners, auto-fix) |
| `WS /ws/agent?token=...` | Auto-routing orchestrated agent |

### WebSocket Protocol

**Client → Server:**
- `{"type": "message", "content": "...", "context": {"kind": "Pod", "name": "...", "namespace": "..."}}`
- `{"type": "confirm_response", "approved": true}`
- `{"type": "clear"}`

**Server → Client:**
- `{"type": "text_delta", "text": "..."}` — Streaming text
- `{"type": "thinking_delta", "thinking": "..."}` — Agent reasoning
- `{"type": "tool_use", "tool": "..."}` — Tool invocation
- `{"type": "component", "spec": {...}, "tool": "..."}` — Rich UI component
- `{"type": "confirm_request", "tool": "...", "input": {...}}` — Write op confirmation
- `{"type": "done", "full_response": "..."}` — Turn complete
- `{"type": "error", "message": "...", "category": "...", "suggestions": [...]}` — Structured error

### Component Specs

Tools can return structured UI specs alongside text. The [Pulse UI](https://github.com/alimobrem/OpenshiftPulse) renders these as interactive tables, charts, and cards inline in the chat.

```python
# Tool returns (text_for_claude, component_spec_for_ui)
return (text, {"kind": "data_table", "title": "Pods", "columns": [...], "rows": [...]})
```

Supported: `data_table`, `info_card_grid`, `badge_list`, `status_list`, `key_value`, `chart`, `relationship_tree`, `tabs`, `grid`, `section`.

**Views auto-save:** `create_dashboard` saves views directly to PostgreSQL — no frontend click needed. View deserialization sanitizes NaN/Infinity values. Prometheus chart data filters NaN before rendering.

## Compatibility

| Pulse Agent | OpenShift Pulse UI | Protocol |
|------------|-------------------|----------|
| v1.13.1 | v5.16.2+ | 2 |
| v1.13.0 | v5.16.2+ | 2 |
| v1.12.0 | v5.16.2+ | 2 |
| v1.9.0 | v5.14.0+ | 2 |
| v1.8.0 | v5.14.0+ | 2 |
| v1.7.1 | v5.14.0+ | 2 |
| v1.7.0 | v5.14.0+ | 2 |
| v1.6.1 | v5.13.0+ | 2 |
| v1.5.3 | v5.13.0+ | 2 |
| v1.4.0 | v5.12.0+ | 2 |
| v1.3.0 | v5.6.0+ | 1 |
| v1.2.0 | v5.6.0+ | 1 |
| v1.1.0 | v5.5.0+ | 1 |
| v1.0.0 | v5.3.0+ | 1 |

The `/version` endpoint returns the protocol version. The UI checks this on connect and warns on mismatch.

## Deploy to Cluster

### Quick Deploy (Quay.io)

```bash
# Build locally and push to Quay.io
podman build --platform linux/amd64 -t quay.io/amobrem/pulse-agent:latest -f Dockerfile.full .
podman push quay.io/amobrem/pulse-agent:latest
oc rollout restart deployment/pulse-agent-openshift-sre-agent -n openshiftpulse
```

### Full Deploy (UI + Agent together)

From the [Pulse UI](https://github.com/alimobrem/OpenshiftPulse) repo:
```bash
ANTHROPIC_API_KEY=sk-ant-... ./deploy/deploy.sh --agent-repo ../pulse-agent
```

Builds both images locally with Podman, pushes to Quay.io, deploys via Helm. Agent deploys first (auto-generates WS token), then UI reads and uses that token. Never uses S2I or on-cluster builds.

### Build Image

```bash
podman build --platform linux/amd64 -t quay.io/amobrem/pulse-agent:v1.13.1 -f Dockerfile.full .
podman push quay.io/amobrem/pulse-agent:v1.13.1
```

### Helm Install

**Vertex AI:**
```bash
kubectl create secret generic gcp-sa-key \
  --from-file=key.json=./sa-key.json \
  -n pulse-agent

helm install pulse-agent ./chart \
  -n pulse-agent --create-namespace \
  --set vertexAI.projectId=your-gcp-project \
  --set vertexAI.region=us-east5 \
  --set vertexAI.existingSecret=gcp-sa-key
```

**Anthropic API:**
```bash
kubectl create secret generic anthropic-api-key \
  --from-literal=api-key=sk-ant-... \
  -n pulse-agent

helm install pulse-agent ./chart \
  -n pulse-agent --create-namespace \
  --set anthropicApiKey.existingSecret=anthropic-api-key
```

The chart **requires** either `vertexAI.projectId` or `anthropicApiKey.existingSecret` — `helm install` will fail with a clear error if neither is set.

**Note on PostgreSQL:** The chart now deploys a PostgreSQL StatefulSet by default for production use (persistent fix history, memory, etc.). Set `database.postgresql.enabled=false` if you prefer SQLite-only.

Enable write operations, security scanning, and memory:
```bash
helm upgrade pulse-agent ./chart \
  --set rbac.allowWriteOperations=true \
  --set rbac.allowSecretAccess=true \
  --set memory.enabled=true
```

### WebSocket Token

A WS auth token is **auto-generated** as a Kubernetes Secret on first install (`<release>-ws-token`). Both the agent and the [Pulse UI](https://github.com/alimobrem/OpenshiftPulse) reference this shared secret — no manual token management needed.

To use a pre-created token instead:
```bash
kubectl create secret generic pulse-ws-token --from-literal=token=your-secret
helm install pulse-agent ./chart --set wsAuth.existingSecret=pulse-ws-token
```

### Deployment Strategy

The chart uses `RollingUpdate` strategy with a `PodDisruptionBudget` (PDB) for zero-downtime rollouts. Startup probes give the agent 60 seconds and PostgreSQL 30 seconds to initialize before health checks begin.

### GCP Authentication

1. **Workload Identity** (recommended) — Annotate the ServiceAccount in `values.yaml`
2. **Existing Secret** — Create a secret with your GCP service account key, reference via `vertexAI.existingSecret`

## Architecture

```
sre_agent/
├── main.py              # Interactive CLI with streaming, confirmation gate, memory
├── serve.py             # FastAPI server with WebSocket support
├── api.py               # API routes, /health endpoint
├── agent.py             # Shared agent loop, Claude API client, audit logging
├── errors.py            # ToolError classification, classify_api_error, classify_exception
├── error_tracker.py     # Thread-safe ring buffer for error aggregation
├── config.py            # Pydantic v2 Settings (PulseAgentSettings with PULSE_AGENT_ prefix)
├── orchestrator.py      # Auto-routing: classify_intent() + build_orchestrated_config()
├── tool_registry.py     # Central tool registry — all @beta_tool functions register here
├── security_agent.py    # Security scanner (read-only, delegates to shared loop)
├── k8s_client.py        # Shared Kubernetes client with lazy initialization
├── k8s_tools.py         # 35+ Kubernetes/OpenShift tools (@beta_tool)
├── security_tools.py    # 9 security scanning tools (@beta_tool)
├── handoff_tools.py     # 2 agent-to-agent handoff tools (request_security_scan, request_sre_investigation)
├── harness.py           # Claude harness: tool selection, prompt caching, cluster context
├── db.py                # Database abstraction (PostgreSQL)
├── db_schema.py         # Shared schema definitions
├── context_bus.py       # Shared context bus for cross-agent communication (database-backed)
├── units.py             # Kubernetes resource unit parsing (CPU, memory)
├── runbooks.py          # Built-in SRE runbooks and alert triage context
├── promql_recipes.py    # 73 production-tested PromQL queries across 16 categories
├── prometheus.py        # Shared Prometheus client with SSL CA handling
├── layout_engine.py     # Semantic layout engine — role-based auto-layout for dashboards
├── view_validator.py    # Dashboard validation (dedup, schema, titles, widget count)
├── view_critic.py       # Dashboard quality scoring rubric
├── intelligence.py      # Intelligence loop — analytics feedback into system prompt
├── tool_usage.py        # Tool invocation audit log (PostgreSQL)
├── tool_chains.py       # Tool chain discovery and next-tool hints (bigram analysis)
└── memory/              # Self-improving agent layer
    ├── __init__.py      # MemoryManager orchestrator
    ├── store.py         # Database persistence (incidents, runbooks, patterns, metrics)
    ├── evaluation.py    # Self-evaluation scoring rubric
    ├── retrieval.py     # Context assembly for prompt augmentation
    ├── runbooks.py      # Runbook extraction from resolved incidents
    ├── patterns.py      # Recurring and time-based pattern detection
    └── memory_tools.py  # 3 agent-callable memory tools

chart/                   # Helm chart
├── Chart.yaml
├── values.yaml
└── templates/
    ├── deployment.yaml          # Pod with security context + health probes
    ├── service.yaml             # ClusterIP on port 8080
    ├── serviceaccount.yaml
    ├── clusterrole.yaml         # Least-privilege RBAC (no wildcards)
    ├── clusterrolebinding.yaml
    ├── postgresql.yaml          # PostgreSQL deployment (RHEL 9, hardened)
    ├── ws-token-secret.yaml     # Auto-generated WS auth token
    └── networkpolicy.yaml       # Ingress on 8080, egress DNS + HTTPS

.claude/                 # Claude Code agents & hooks
├── settings.json        # Hook configuration (deploy-validator, tool-auditor, etc.)
├── agents/              # 8 specialized agents
│   ├── tool-writer.md           # Writes new @beta_tool K8s tools
│   ├── runbook-writer.md        # Writes diagnostic runbooks
│   ├── protocol-checker.md      # Validates WebSocket protocol vs API_CONTRACT.md
│   ├── tool-auditor.md          # Audits tools for quality and security
│   ├── memory-auditor.md        # Audits memory system integrity
│   ├── security-hardener.md     # Reviews security across code, containers, Helm
│   ├── test-writer.md           # Writes pytest tests following project patterns
│   └── deploy-validator.md      # Validates deploy config before rollout
└── hooks/               # Hook scripts triggered by Claude Code
    ├── deploy-validator.sh      # PreToolUse: validates deploy/helm commands
    ├── post-edit.sh             # PostToolUse: routes edits to auditor agents
    └── stop-checks.sh           # Stop: suggests tests for changed files
```

## CI/CD

### GitHub Actions

| Workflow | Trigger | What it does |
|----------|---------|-------------|
| `build-push.yml` | `v*` tag push, manual dispatch | Lint, tests, then builds `Dockerfile.full` and pushes to `quay.io/amobrem/pulse-agent` with tag + `latest` |
| `evals.yml` | PR, push to main, daily 6am UTC, manual | Lint, format check, unit tests, version sync check, Helm lint, docs consistency, release eval gate, replay evals, safety/integration suites, outcome regression, weekly digest |

### Image Registry

Images are hosted on **Quay.io** at `quay.io/amobrem/pulse-agent`.

**To release a new version:**
```bash
make release VERSION=1.6.0   # bumps version everywhere, commits, tags
git push && git push --tags   # GitHub Actions builds and pushes automatically
```

**Manual build:**
```bash
podman build --platform linux/amd64 -f Dockerfile.full -t quay.io/amobrem/pulse-agent:v1.13.1 .
podman push quay.io/amobrem/pulse-agent:v1.13.1
```

**Required GitHub Secrets:**
| Secret | Description |
|--------|-------------|
| `QUAY_USERNAME` | Quay.io robot account (e.g., `amobrem+cibot`) |
| `QUAY_PASSWORD` | Quay.io robot account token |

## Testing

```bash
pip install -e '.[test]'
python -m pytest tests/ -v
```

1,078 tests covering all tools, all 16 scanner functions, agent loop safety mechanisms, error classification, error tracking, config validation, unit parsing, orchestrator, context bus, handoff tools, component hint coverage, showcase eval scenarios, PromQL recipes, view validation, layout engine, intelligence loop, token tracking, and the memory system. All tests run without a cluster or API key (fully mocked).

## Evaluation Framework

Pulse Agent includes a deterministic evaluation framework for release gating and regression detection.

```bash
# Run the core eval suite
python -m sre_agent.evals.cli --suite core

# Run release-gating suite (expected to pass)
python -m sre_agent.evals.cli --suite release --fail-on-gate

# Run safety/integration suites (diagnostic, non-gating by default)
python -m sre_agent.evals.cli --suite safety
python -m sre_agent.evals.cli --suite integration

# Generate weekly outcome/regression report from fix history DB
python -m sre_agent.evals.outcomes_cli --current-days 7 --baseline-days 7
python -m sre_agent.evals.outcomes_cli --format json --output artifacts/outcomes.json
python -m sre_agent.evals.outcomes_cli --policy-file sre_agent/evals/policies/outcome_regression_policy.yaml

# Generate weekly markdown digest (gate + outcomes + top failures)
python -m sre_agent.evals.weekly_digest_cli --current-days 7 --baseline-days 7 --output artifacts/weekly-digest.md

# Emit JSON (for CI artifacts)
python -m sre_agent.evals.cli --suite release --format json --output artifacts/release.json

# Fail process if release gate fails
python -m sre_agent.evals.cli --suite release --fail-on-gate
```

Current eval dimensions:
- task success
- safety/compliance
- tool efficiency
- operational quality

### Tool Eval Prompts

84 real-world user prompts mapped to expected tool calls, covering all 82 registered tools. Used for evaluating agent tool selection quality and ensuring every tool is reachable.

See **[docs/EVAL_PROMPTS.md](docs/EVAL_PROMPTS.md)** for the complete prompt-to-tool mapping.

| Mode | Prompts | Example |
|------|---------|---------|
| SRE | 64 | "why are my pods crashing" → `list_pods`, `describe_pod`, `get_pod_logs` |
| Security | 8 | "scan RBAC for overly permissive roles" → `scan_rbac_risks` |
| View Designer | 11 | "create a dashboard for production" → `plan_dashboard`, `create_dashboard` |
| Cross-Agent | 1 | "hand this off to security" → `request_security_scan` |

CI enforced: adding a new tool without an eval prompt fails the test suite.
- reliability

Hard blocker categories:
- `policy_violation`
- `hallucinated_tool`
- `missing_confirmation`

Suites:
- `release` — primary gating suite for CI
- `safety` — adversarial safety checks
- `integration` — reliability/failure-mode checks
- `core` — mixed baseline coverage (includes intentional blocker scenarios)
- `outcomes` — compares current vs baseline action outcomes from fix history telemetry

---

<p align="center">
  <strong>105 tools</strong> &bull; <strong>16 scanners</strong> &bull; <strong>10 runbooks</strong> &bull; <strong>73 PromQL recipes</strong> &bull; <strong>84 eval prompts</strong> &bull; <strong>1,078 tests</strong> &bull; <strong>Protocol v2</strong>
</p>

<p align="center">
  <a href="https://github.com/alimobrem/pulse-agent/releases">Releases</a> &bull;
  <a href="https://github.com/alimobrem/OpenshiftPulse">Pulse UI</a> &bull;
  <a href="https://github.com/alimobrem/pulse-agent/issues">Issues</a>
</p>

<p align="center">MIT License</p>
