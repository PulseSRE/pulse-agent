# Pulse Agent Review (2026-03-24, Post-Security Hardening)

## Project Structure
- 6 Python source files: main.py, agent.py, security_agent.py, k8s_client.py, k8s_tools.py, security_tools.py
- Helm chart with 7 templates (including _helpers.tpl)
- No tests, no CI, no linting config
- 20 SRE tools, 9 security tools
- Uses @beta_tool decorator from anthropic SDK
- Dual API support: Vertex AI + direct Anthropic API
- Interactive REPL with Rich console

## Security Hardening Fixes (commit 3a47c81)
1. Confirmation gate now programmatic (WRITE_TOOLS set + agent loop enforcement)
2. Dockerfile: UBI9 with specific tag, USER 1001, no-cache pip
3. Pod security context: runAsNonRoot, readOnlyRootFilesystem, drop ALL caps, seccomp RuntimeDefault
4. RBAC: read-only default, write ops and secret access opt-in via values.yaml flags
5. NetworkPolicy: egress-only to DNS + HTTPS
6. Secret template intentionally empty (external creation required)
7. Audit logging: structured JSON with tool name, input, result length, timestamp
8. Input validation: MAX_TAIL_LINES=1000, MAX_REPLICAS=100, MAX_RESULTS=200
9. MAX_ITERATIONS=25 agent loop guard
10. Shared k8s_client.py eliminates code duplication
11. Security agent explicitly read-only (write_tools=set(), on_confirm not passed)

## Remaining Issues
1. ZERO tests - not production-ready
2. Audit log writes to CWD (pulse_agent_audit.log) - crashes with readOnlyRootFilesystem
3. No liveness/readiness probes in deployment
4. Error messages show only exception class name, not message
5. No rate limiting or cost controls
6. No retry logic for transient failures
7. Conversation history grows unbounded (no pruning/budget)
8. NetworkPolicy egress too permissive (any HTTPS destination)
9. No Anthropic API key support in Helm chart templates
10. PULSE_AGENT_MAX_TOKENS not validated (int() can throw)
11. No multi-cluster support
12. No metrics API integration (only capacity, not usage)
13. Missing StatefulSet/DaemonSet tools despite RBAC granting access
14. REPL-only, no API/webhook mode

## Strengths
- Clean tool definitions with @beta_tool
- Defense-in-depth on write operations (RBAC + confirmation gate + input bounds)
- Security scanner tool coverage is comprehensive (pod security, RBAC, netpol, SCC, secrets, images)
- Mode switching between SRE/security
- Streaming support with callbacks
- GCP auth flexibility (Workload Identity, existing secret)
- OpenShift-aware (SCCs, ClusterOperators, ClusterVersion)
- README is comprehensive and accurate
- Shared agent loop eliminates duplication
