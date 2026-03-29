# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Pulse Agent — AI-powered OpenShift/Kubernetes SRE and Security agent built on Claude. Connects to live clusters via the K8s API and uses Claude Opus for diagnostics, incident triage, and automated remediation. v1.7.0, Protocol v2, 109 tools, 11 scanners, 393 tests. Auto-routing orchestrator classifies queries and routes to SRE or Security agent.

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
python3 -m pytest tests/ -v           # all tests (~393 tests)
python3 -m pytest tests/test_k8s_tools.py -v  # single file
make verify                                    # lint + type-check + test

# Release
make release VERSION=1.6.0            # bump version everywhere, commit, tag
# then: git push && git push --tags   # triggers build-push.yml

# Deploy (OpenShift)
./deploy/quick-deploy.sh openshiftpulse        # fast Podman build + push to internal registry
make helm-lint                                 # validate chart locally (tests PostgreSQL StatefulSet)
```

## Architecture

### Entry Points
- `sre_agent/main.py` — Interactive CLI with Rich UI
- `sre_agent/serve.py` → `sre_agent/api.py` — FastAPI WebSocket server

### Agent Loop
- `agent.py` — shared `run_agent_streaming()` loop used by both SRE and Security agents
- Circuit breaker: `CircuitBreaker` class with CLOSED→OPEN→HALF_OPEN states
- Tool execution: parallel for reads, sequential with confirmation gate for writes
- Confirmation: `confirm_request` → `confirm_response` with JIT nonce for replay prevention

### WebSocket API (Protocol v2)
- `/ws/sre` — SRE agent chat
- `/ws/security` — Security scanner chat
- `/ws/monitor` — Autonomous cluster monitoring (push-based findings, predictions, actions)
- `/ws/agent` — Auto-routing orchestrated agent (classifies intent per message, routes to SRE or Security)
- Auth: `PULSE_AGENT_WS_TOKEN` via query param, constant-time comparison

### Monitor System (`monitor.py`)
- `MonitorSession` — periodic cluster scanning (default 60s interval)
- 11 scanners: crashlooping pods, pending pods, failed deployments, node pressure, expiring certs, firing alerts, OOM-killed pods, image pull errors, degraded operators, DaemonSet gaps, HPA saturation
- Auto-fix at trust level 3+: deletes crashlooping pods, restarts failed deployments
- Confidence scores on all findings, investigations, and action proposals
- `resolution` events emitted when findings resolve (auto-fix or self-healed)
- `findings_snapshot` event for stale finding cleanup
- Fix history persisted to the database (`PULSE_AGENT_DATABASE_URL`)
- `_sanitize_for_prompt()` on all cluster data in investigation prompts with `--- BEGIN/END CLUSTER DATA ---` delimiters

### Orchestrator (`orchestrator.py`)
- `classify_intent()` — keyword-based SRE/Security/Both classification
- `build_orchestrated_config()` — returns system_prompt, tool_defs, tool_map, write_tools for the classified mode
- Used by `/ws/agent` endpoint for auto-routing

### Tools
- `k8s_tools.py` — 35 K8s tools (`@beta_tool` decorated). Write tools in `WRITE_TOOLS` set require confirmation.
- `security_tools.py` — 9 security scanning tools (read-only)
- `fleet_tools.py` — 5 multi-cluster tools
- `gitops_tools.py` — 6 ArgoCD tools
- `predict_tools.py` — 3 predictive analytics tools
- `timeline_tools.py` — 1 incident correlation tool
- `git_tools.py` — 1 Git PR proposal tool
- `handoff_tools.py` — 2 agent-to-agent handoff tools (`request_security_scan`, `request_sre_investigation`)
- `tool_registry.py` — central registry; all tool modules call `register_tool()` at import time

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
- `.env` file support, type validation at startup
- All config accessed via settings instance, not raw `os.environ`

### Harness (`harness.py`)
- Dynamic tool selection: 8 categories, loads 15-25 of 109 tools per query
- Prompt caching: `cache_control: ephemeral` on system prompt
- Cluster context injection: pre-fetches node count, namespaces, OCP version

### Security
- Non-root container (UID 1001) on UBI9
- RBAC: read-only by default, write ops opt-in via `rbac.allowWriteOperations`
- Confirmation gate enforced in code (not just prompt)
- Prompt injection defense in system prompt
- Input validation: replicas 0-100, log lines 1-1000, grace period 1-300s

### Helm Chart (`chart/`)
- `values.yaml` — requires `vertexAI.projectId` or `anthropicApiKey.existingSecret`
- WS token auto-generated as K8s Secret (`helm.sh/resource-policy: keep`)
- Recreate strategy (avoids 2x resource usage during rollouts on quota-constrained namespaces)
- `chart/templates/deployment.yaml` — validates credentials at install time via `_helpers.tpl`
- `chart/templates/postgresql.yaml` — PostgreSQL deployment (RHEL 9, runAsNonRoot, NetworkPolicy)

### Key Files
- `config.py` — Pydantic v2 Settings (`PulseAgentSettings` with `PULSE_AGENT_` prefix)
- `errors.py` — `ToolError` classification (7 categories + suggestions)
- `error_tracker.py` — thread-safe ring buffer for error aggregation
- `runbooks.py` — 10 built-in SRE runbooks injected into system prompt
- `memory/` — self-improving agent (PostgreSQL, pattern detection, learned runbooks)
- `k8s_client.py` — lazy-initialized K8s client with `safe()` wrapper
- `context_bus.py` — shared context bus for cross-agent communication
- `orchestrator.py` — intent classification + agent routing for `/ws/agent`

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
| `PULSE_AGENT_WS_TOKEN` | WebSocket auth token | auto-generated |
| `PULSE_AGENT_SCAN_INTERVAL` | Monitor scan interval (seconds) | `60` |
| `PULSE_AGENT_HARNESS` | Enable harness optimizations | `1` |
| `PULSE_AGENT_MEMORY` | Enable self-improving memory | `1` (enabled) |
| `PULSE_AGENT_DATABASE_URL` | Database URL (PostgreSQL) | required |
| `PULSE_AGENT_AUTOFIX_ENABLED` | Enable monitor auto-fix | `true` |
| `PULSE_AGENT_MAX_TRUST_LEVEL` | Server-side max trust level (0-4) | `3` |
| `PULSE_AGENT_CB_THRESHOLD` | Circuit breaker failure threshold | `3` |
| `PULSE_AGENT_CB_TIMEOUT` | Circuit breaker recovery (seconds) | `60` |

*One of Vertex AI or Anthropic API key is required.
