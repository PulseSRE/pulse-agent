# Security Policy

## Reporting Vulnerabilities

Report security issues to [GitHub Issues](https://github.com/alimobrem/pulse-agent/issues) with the `security` label.

## Security Model

### Authentication
- WebSocket API requires `PULSE_AGENT_WS_TOKEN` (constant-time comparison via `hmac.compare_digest`)
- In production, deploy behind OpenShift OAuth proxy — the agent service should not be exposed directly
- Rate limited to 10 messages/minute per WebSocket connection

### Authorization
- Agent uses the pod's ServiceAccount for K8s API calls (not user impersonation)
- RBAC is least-privilege: the Helm chart's ClusterRole grants only `get`, `list`, `watch` verbs by default. Write verbs (`patch`, `delete`, `create`, `update`) are not included in the ClusterRole unless the chart is deployed with `rbac.allowWriteOperations=true`.
- Secret access (`get`, `list` on secrets) is not granted unless `rbac.allowSecretAccess=true`
- No wildcard RBAC rules

### RBAC Levels
- **Default:** `get`, `list`, `watch` on pods, nodes, events, services, namespaces, configmaps, deployments, statefulsets, daemonsets, jobs, cronjobs, HPAs, metrics, RBAC objects, network policies, ingresses, routes, SCCs, OLM resources (read-only), and ArgoCD resources (read-only)
- **`allowWriteOperations=true`:** Adds `delete` pods, `create` pods/eviction, `patch` nodes, `patch`/`update` deployments/scale, `create` namespaces, `create` network policies, `create`/`update`/`patch` configmaps, `patch`/`create` on workload resources (deployments, statefulsets, daemonsets, jobs, cronjobs, HPAs), `create` on OLM resources (subscriptions, operatorgroups, catalogsources), `create`/`patch`/`update` on ArgoCD resources (applications, appprojects, applicationsets)
- **`allowSecretAccess=true`:** Adds `get`, `list` on secrets

### Trust Levels
The agent supports trust levels 0-4 when connected via the Monitor endpoint:
- **Level 0 (Monitor only):** Observe and report — no action taken
- **Level 1 (Suggest):** Propose remediations but take no action
- **Level 2 (Ask):** Propose fixes and prompt the user for approval
- **Level 3 (Auto-fix safe):** Auto-apply fixes for safe categories; prompt for others
- **Level 4 (Full autonomous):** Apply all fixes automatically

### Confirmation Gate
There are two execution paths with different confirmation behavior:

- **Interactive agent (`/ws/sre`):** All write operations require programmatic user approval regardless of trust level. The confirmation gate is enforced in code — the agent cannot bypass it. Every write Kubernetes API call requires a `confirm_request`/`confirm_response` round-trip before execution.
- **Monitor auto-fix (`/ws/monitor` at trust level 3+):** Fixes execute WITHOUT the confirmation gate. This is by design for autonomous remediation. Auto-fix actions (pod deletion, rolling restart) cannot be rolled back — the Kubernetes controller recreates pods automatically. Safety guardrails are enforced instead: rate limiting (max 3 auto-fixes per scan cycle), cooldown (5-minute per-resource cooldown to prevent fix loops), bare pod protection (pods without ownerReferences are never deleted, as they would not be recreated by a controller), and an emergency kill switch (`POST /monitor/pause` or `PULSE_AGENT_AUTOFIX_ENABLED=false`).

### Input Sanitization
- Context fields (kind, namespace, name) validated against `^[a-zA-Z0-9\-._/: ]{0,253}$`
- Non-matching values rejected entirely (strict mode)
- WebSocket messages capped at 1MB
- Tool inputs bounds-checked (replicas 0-100, log lines 1-1000, grace period 1-300s)

### Prompt Injection Defense
- System prompt includes explicit security rules against tool-result injection
- Confirmation gates enforced in code (not just prompt instructions)
- Write tools require programmatic user approval before execution

### Container Security
- Non-root user (UID 1001) on RHEL UBI9 base
- `runAsNonRoot: true`, `readOnlyRootFilesystem: true`, `capabilities.drop: ["ALL"]`
- `seccompProfile: RuntimeDefault`
- Liveness/readiness probes via `/healthz`

### Audit Trail
- All tool executions logged to structured JSON (`pulse_agent_audit.log`)
- Cluster-side audit via `record_audit_entry` tool (writes to ConfigMap)
- Retry-on-409 for concurrent audit writes

### Network
- Egress restricted to DNS (port 53) and HTTPS (443/6443)
- Ingress limited to port 8080 (WebSocket/HTTP)
