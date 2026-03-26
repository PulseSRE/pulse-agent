# Pulse Agent

<p>
  <a href="https://github.com/alimobrem/pulse-agent/releases/tag/v1.1.0"><img src="https://img.shields.io/badge/release-v1.1.0-2563eb?style=for-the-badge" alt="Version"></a>
  <img src="https://img.shields.io/badge/tools-62-10b981?style=for-the-badge" alt="Tools">
  <img src="https://img.shields.io/badge/tests-231-10b981?style=for-the-badge" alt="Tests">
  <img src="https://img.shields.io/badge/license-MIT-6366f1?style=for-the-badge" alt="License">
</p>

AI-powered OpenShift/Kubernetes SRE and Security Agent built on Claude.

Pulse Agent connects directly to your cluster's Kubernetes API and uses Claude Opus to diagnose issues, triage incidents, manage resources, execute runbooks, and perform security audits — all through a conversational interface. Integrates with [OpenShift Pulse](https://github.com/alimobrem/OpenshiftPulse) for rich UI rendering, or runs standalone as a CLI.

## Features

### SRE Agent
- **Cluster Diagnostics** — Investigate pod failures, crash loops, OOM kills, image pull errors, scheduling problems
- **Incident Triage** — Correlate events, pod status, logs, and Prometheus metrics to identify root causes
- **Resource Management** — Analyze quotas, capacity, utilization, and HPA status across nodes
- **Runbook Execution** — Scale deployments, restart pods, cordon/drain nodes, apply YAML manifests (with confirmation)
- **Alerting** — Query firing alerts from Alertmanager and run PromQL queries
- **Cluster Operations** — Inspect StatefulSets, DaemonSets, Jobs, CronJobs, Ingresses, Routes, and OLM operators

### Security Scanner
- **Pod Security** — Detect privileged containers, root execution, missing security contexts, dangerous capabilities
- **RBAC Analysis** — Find overly permissive roles, non-system cluster-admin bindings, wildcard permissions
- **Network Policies** — Identify namespaces with unrestricted east-west traffic, create deny-all policies
- **Image Security** — Flag `:latest` tags, missing digest pins, untrusted registries (configurable)
- **SCC Analysis** — Review Security Context Constraints and pod SCC assignments (OpenShift)
- **Secret Hygiene** — Find old unrotated secrets, env-exposed secrets, unused secrets

### Error Intelligence
- **Structured Error Types** — ToolError classification with 7 categories (permission, not_found, conflict, validation, server, network, quota) and actionable suggestions
- **Error Tracking** — Thread-safe ring buffer (500 entries) with per-category aggregation and top-tool breakdown
- **Health Endpoint** — `/health` returns circuit breaker state, error summary, and recent errors
- **SQLite Resilience** — `@db_safe` decorator on all memory operations prevents crashes on database errors

### Self-Improving Agent
- **Incident Memory** — Stores every interaction with query, tool sequence, resolution, and outcome in SQLite
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
| `PULSE_AGENT_MEMORY` | Enable self-improving memory (`1`/`true`) | disabled |
| `PULSE_AGENT_MEMORY_PATH` | SQLite database path | `~/.pulse_agent/memory.db` |
| `PULSE_AGENT_TRUSTED_REGISTRIES` | Comma-separated trusted image registry prefixes | Red Hat, Quay, OpenShift |
| `PULSE_AGENT_HARNESS` | Enable Claude harness optimizations | `1` (enabled) |
| `PULSE_AGENT_WS_TOKEN` | WebSocket authentication token (required for API mode) | |
| `PULSE_AGENT_CB_THRESHOLD` | Circuit breaker failure threshold | `3` |
| `PULSE_AGENT_CB_TIMEOUT` | Circuit breaker recovery timeout (seconds) | `60` |

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

- **Confirmation gate** — All write operations require explicit `y/N` user confirmation. Enforced programmatically, not just via prompt.
- **Input validation** — Bounds-checked: replicas (0-100), log tail lines (1-1000), grace period (1-300s). Lists truncated at 200 items.
- **Max iteration guard** — Tool loop capped at 25 iterations.
- **Audit logging** — Every tool invocation logged to `/tmp/pulse_agent_audit.log` in structured JSON. Cluster audit trail via `record_audit_entry` tool writes to a ConfigMap.
- **Read-only by default** — Security scanner has no write tools. SRE write tools require RBAC opt-in.

### Container Security

- Non-root user (UID 1001) on RHEL UBI9 base image
- Pod security context: `runAsNonRoot`, `readOnlyRootFilesystem`, drops all capabilities
- NetworkPolicy restricts egress to DNS + HTTPS only
- Liveness/readiness probes via `/healthz`

### RBAC

| Flag | Default | Grants |
|------|---------|--------|
| `rbac.allowWriteOperations` | `false` | Scale, restart, cordon, delete, apply YAML, create NetworkPolicy |
| `rbac.allowSecretAccess` | `false` | List/read secrets (for security scanning) |

## Tools

### SRE Tools (35)

| Category | Tools |
|----------|-------|
| **Core diagnostics** | `list_namespaces`, `list_pods`, `describe_pod`, `get_pod_logs`, `list_nodes`, `describe_node`, `get_events` |
| **Workloads** | `list_deployments`, `describe_deployment`, `list_statefulsets`, `list_daemonsets`, `list_jobs`, `list_cronjobs` |
| **Networking** | `get_services`, `list_ingresses`, `list_routes` |
| **Storage & resources** | `get_persistent_volume_claims`, `get_resource_quotas`, `get_configmap` |
| **Metrics** | `get_node_metrics`, `get_pod_metrics`, `list_hpas`, `get_prometheus_query` |
| **Cluster info** | `get_cluster_version`, `get_cluster_operators`, `list_operator_subscriptions`, `get_firing_alerts` |
| **Write operations** | `scale_deployment`, `restart_deployment`, `cordon_node`, `uncordon_node`, `delete_pod`, `apply_yaml`, `create_network_policy` |
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

1. **Before each turn** — retrieves similar past incidents, matching runbooks, and patterns from SQLite. Injects into the system prompt (capped at 1500 chars to prevent context bloat).
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
| **Dynamic Tool Selection** | Categorizes 54 tools into 8 groups, loads only relevant ones per query | 54→15-25 tools, faster + cheaper |
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
| `GET /healthz` | Liveness probe |
| `GET /health` | Full health: circuit breaker state, error summary, recent errors |
| `GET /version` | Protocol version, tool count, features |
| `GET /tools` | List all available tools |
| `WS /ws/sre?token=...` | SRE agent WebSocket |
| `WS /ws/security?token=...` | Security scanner WebSocket |

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

Supported: `data_table`, `info_card_grid`, `badge_list`, `status_list`, `key_value`, `chart`.

## Compatibility

| Pulse Agent | OpenShift Pulse UI | Protocol |
|------------|-------------------|----------|
| v1.1.0 | v5.5.0+ | 1 |
| v1.0.0 | v5.3.0+ | 1 |

The `/version` endpoint returns the protocol version. The UI checks this on connect and warns on mismatch.

## Deploy to Cluster

### Build Image

```bash
docker build -t your-registry/pulse-agent:0.2.0 .
docker push your-registry/pulse-agent:0.2.0
```

### Helm Install

```bash
kubectl create secret generic gcp-sa-key \
  --from-file=key.json=./sa-key.json \
  -n pulse-agent

helm install pulse-agent ./chart \
  -n pulse-agent --create-namespace \
  --set image.repository=your-registry/pulse-agent \
  --set vertexAI.projectId=your-gcp-project \
  --set vertexAI.region=us-east5 \
  --set vertexAI.existingSecret=gcp-sa-key
```

Enable write operations, security scanning, and memory:
```bash
helm install pulse-agent ./chart \
  -n pulse-agent --create-namespace \
  --set rbac.allowWriteOperations=true \
  --set rbac.allowSecretAccess=true \
  --set memory.enabled=true
```

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
├── security_agent.py    # Security scanner (read-only, delegates to shared loop)
├── k8s_client.py        # Shared Kubernetes client with lazy initialization
├── k8s_tools.py         # 35+ Kubernetes/OpenShift tools (@beta_tool)
├── security_tools.py    # 9 security scanning tools (@beta_tool)
├── harness.py           # Claude harness: tool selection, prompt caching, cluster context
├── units.py             # Kubernetes resource unit parsing (CPU, memory)
├── runbooks.py          # Built-in SRE runbooks and alert triage context
└── memory/              # Self-improving agent layer
    ├── __init__.py      # MemoryManager orchestrator
    ├── store.py         # SQLite persistence (incidents, runbooks, patterns, metrics)
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
    └── networkpolicy.yaml       # Ingress on 8080, egress DNS + HTTPS
```

## Testing

```bash
pip install -e '.[test]'
python -m pytest tests/ -v
```

231 tests covering all tools, agent loop safety mechanisms, error classification, error tracking, unit parsing, and the memory system. All tests run without a cluster or API key (fully mocked).

---

<p align="center">
  <strong>62 tools</strong> &bull; <strong>10 runbooks</strong> &bull; <strong>8 tool categories</strong> &bull; <strong>231 tests</strong> &bull; <strong>Protocol v1</strong>
</p>

<p align="center">
  <a href="https://github.com/alimobrem/pulse-agent/releases">Releases</a> &bull;
  <a href="https://github.com/alimobrem/OpenshiftPulse">Pulse UI</a> &bull;
  <a href="https://github.com/alimobrem/pulse-agent/issues">Issues</a>
</p>

<p align="center">MIT License</p>
