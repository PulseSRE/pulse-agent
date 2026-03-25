"""Built-in runbooks for common Kubernetes failure patterns.

These are injected into the agent's system prompt so it can follow
structured diagnostic procedures when encountering known issues.
"""

RUNBOOKS = """
## Runbooks — Structured Diagnostic Procedures

When you encounter these patterns, follow the steps systematically.

### CrashLoopBackOff
1. `describe_pod` — check container states, exit codes, and OOM indicators
2. `get_pod_logs(previous=True)` — get logs from the crashed container
3. `get_events` for the pod — look for resource limit, liveness probe, or image issues
4. Common causes:
   - Exit code 137 → OOMKilled — check memory limits vs actual usage
   - Exit code 1 → Application error — check logs for stack trace
   - Exit code 127 → Command not found — check image and entrypoint
   - Liveness probe failing — check probe config and startup time
5. Suggest: increase memory limits, fix application error, adjust probe timing

### ImagePullBackOff
1. `describe_pod` — check container image name and pull policy
2. `get_events` for the pod — look for "ImagePullBackOff" or "ErrImagePull"
3. Check if image exists: verify tag, registry URL, digest
4. Check pull secrets: `get_services` and service account secrets
5. Common causes:
   - Typo in image name or tag
   - Image deleted from registry
   - Missing or expired imagePullSecret
   - Private registry without authentication
   - Network policy blocking egress to registry

### OOMKilled
1. `describe_pod` — check container exit code 137, OOMKilled reason
2. `get_pod_metrics` — check current memory usage vs limits
3. `get_pod_logs(previous=True)` — check what was happening before OOM
4. `get_resource_quotas` — check namespace memory quotas
5. Suggest: increase memory limits, investigate memory leak, add memory profiling

### Node NotReady
1. `describe_node` — check conditions (Ready, MemoryPressure, DiskPressure, PIDPressure)
2. `get_events` for the node — look for kubelet, docker/cri-o errors
3. `list_pods(field_selector='spec.nodeName=<node>')` — check pods on the node
4. Common causes:
   - DiskPressure → disk full, clean up images/logs
   - MemoryPressure → too many pods, eviction needed
   - NetworkUnavailable → CNI plugin issue
   - Kubelet stopped → node-level issue, may need restart

### PVC Pending
1. `get_persistent_volume_claims` — check PVC status and storage class
2. `get_events` for the PVC — look for provisioning errors
3. Common causes:
   - No matching PV available (static provisioning)
   - StorageClass misconfigured or not found
   - Cloud provider quota reached
   - Zone mismatch between PVC and available storage

### DNS Resolution Failures
1. `list_pods(namespace='openshift-dns')` or `list_pods(namespace='kube-system', label_selector='k8s-app=kube-dns')` — check DNS pods
2. `get_events(namespace='openshift-dns')` — check for DNS pod issues
3. `get_services(namespace='openshift-dns')` — verify DNS service exists
4. Check if CoreDNS/dns-default pods are running and ready
5. Look for NetworkPolicy blocking DNS (UDP/TCP port 53)

### High Pod Restart Count
1. `list_pods` — find pods with high restart counts
2. For each: `describe_pod` → check container states and last termination reason
3. `get_pod_logs(previous=True)` for the most restarting containers
4. Common patterns: OOM, liveness probe timeout, dependency not ready

### Deployment Not Progressing
1. `describe_deployment` — check conditions, especially "Progressing"
2. `list_pods(label_selector='app=<name>')` — check pod status
3. `get_events` for the deployment — look for quota, scheduling, or image errors
4. Common causes:
   - Insufficient resources (CPU/memory quota exhausted)
   - Image pull failure
   - Pod security admission blocking pods
   - Node selector/affinity not matching any nodes

### Operator Degraded
1. `get_cluster_operators` — identify which operators are Degraded
2. `get_events(namespace='openshift-*')` — check operator namespace events
3. `list_pods` in the operator's namespace — check for crash loops
4. `get_pod_logs` for the operator pod — look for error messages
5. Common: cert expiry, etcd issues, API server connectivity

### Quota / LimitRange Issues
1. `get_resource_quotas` — check quota usage vs limits
2. `get_events` — look for "forbidden: exceeded quota" messages
3. Check if pods have resource requests/limits set
4. Suggest: increase quota, add resource requests to pods, or clean up unused resources
"""

ALERT_TRIAGE_CONTEXT = """
## Alert Triage Procedure

When asked about alerts or when an alert fires:
1. Use `get_firing_alerts` to get all currently firing alerts
2. For each critical/warning alert:
   a. Identify the affected resource (pod, node, namespace)
   b. Use the appropriate diagnostic tools to gather context
   c. Follow the relevant runbook if the pattern matches
3. Present findings grouped by severity (CRITICAL → WARNING → INFO)
4. For each finding, provide:
   - What is happening (symptom)
   - Why it is happening (root cause analysis)
   - How to fix it (remediation steps)
   - Impact if not fixed (risk assessment)
"""
