"""Built-in runbooks for common Kubernetes failure patterns.

These are injected into the agent's system prompt so it can follow
structured diagnostic procedures when encountering known issues.

RUNBOOK_CHUNKS is a dict keyed by pattern name for selective injection.
select_runbooks() picks the most relevant runbooks based on user query.
RUNBOOKS is the full concatenation kept for backward compatibility.
"""

RUNBOOK_CHUNKS: dict[str, str] = {
    "crashloop": (
        "### CrashLoopBackOff\n"
        "1. `describe_pod` — check container states, exit codes, and OOM indicators\n"
        "2. `get_pod_logs(previous=True)` — get logs from the crashed container\n"
        "3. `get_events` for the pod — look for resource limit, liveness probe, or image issues\n"
        "4. Common causes:\n"
        "   - Exit code 137 → OOMKilled — check memory limits vs actual usage\n"
        "   - Exit code 1 → Application error — check logs for stack trace\n"
        "   - Exit code 127 → Command not found — check image and entrypoint\n"
        "   - Liveness probe failing — check probe config and startup time\n"
        "5. Suggest: increase memory limits, fix application error, adjust probe timing"
    ),
    "imagepull": (
        "### ImagePullBackOff\n"
        "1. `describe_pod` — check container image name and pull policy\n"
        '2. `get_events` for the pod — look for "ImagePullBackOff" or "ErrImagePull"\n'
        "3. Check if image exists: verify tag, registry URL, digest\n"
        "4. Check pull secrets: `get_services` and service account secrets\n"
        "5. Common causes:\n"
        "   - Typo in image name or tag\n"
        "   - Image deleted from registry\n"
        "   - Missing or expired imagePullSecret\n"
        "   - Private registry without authentication\n"
        "   - Network policy blocking egress to registry"
    ),
    "oomkilled": (
        "### OOMKilled\n"
        "1. `describe_pod` — check container exit code 137, OOMKilled reason\n"
        "2. `get_pod_metrics` — check current memory usage vs limits\n"
        "3. `get_pod_logs(previous=True)` — check what was happening before OOM\n"
        "4. `get_resource_quotas` — check namespace memory quotas\n"
        "5. Suggest: increase memory limits, investigate memory leak, add memory profiling"
    ),
    "node_notready": (
        "### Node NotReady\n"
        "1. `describe_node` — check conditions (Ready, MemoryPressure, DiskPressure, PIDPressure)\n"
        "2. `get_events` for the node — look for kubelet, docker/cri-o errors\n"
        "3. `list_pods(field_selector='spec.nodeName=<node>')` — check pods on the node\n"
        "4. Common causes:\n"
        "   - DiskPressure → disk full, clean up images/logs\n"
        "   - MemoryPressure → too many pods, eviction needed\n"
        "   - NetworkUnavailable → CNI plugin issue\n"
        "   - Kubelet stopped → node-level issue, may need restart"
    ),
    "pvc_pending": (
        "### PVC Pending\n"
        "1. `get_persistent_volume_claims` — check PVC status and storage class\n"
        "2. `get_events` for the PVC — look for provisioning errors\n"
        "3. Common causes:\n"
        "   - No matching PV available (static provisioning)\n"
        "   - StorageClass misconfigured or not found\n"
        "   - Cloud provider quota reached\n"
        "   - Zone mismatch between PVC and available storage"
    ),
    "dns": (
        "### DNS Resolution Failures\n"
        "1. `list_pods(namespace='openshift-dns')` or "
        "`list_pods(namespace='kube-system', label_selector='k8s-app=kube-dns')` — check DNS pods\n"
        "2. `get_events(namespace='openshift-dns')` — check for DNS pod issues\n"
        "3. `get_services(namespace='openshift-dns')` — verify DNS service exists\n"
        "4. Check if CoreDNS/dns-default pods are running and ready\n"
        "5. Look for NetworkPolicy blocking DNS (UDP/TCP port 53)"
    ),
    "high_restarts": (
        "### High Pod Restart Count\n"
        "1. `list_pods` — find pods with high restart counts\n"
        "2. For each: `describe_pod` → check container states and last termination reason\n"
        "3. `get_pod_logs(previous=True)` for the most restarting containers\n"
        "4. Common patterns: OOM, liveness probe timeout, dependency not ready"
    ),
    "deployment_stuck": (
        "### Deployment Not Progressing\n"
        '1. `describe_deployment` — check conditions, especially "Progressing"\n'
        "2. `list_pods(label_selector='app=<name>')` — check pod status\n"
        "3. `get_events` for the deployment — look for quota, scheduling, or image errors\n"
        "4. Common causes:\n"
        "   - Insufficient resources (CPU/memory quota exhausted)\n"
        "   - Image pull failure\n"
        "   - Pod security admission blocking pods\n"
        "   - Node selector/affinity not matching any nodes"
    ),
    "operator_degraded": (
        "### Operator Degraded\n"
        "1. `get_cluster_operators` — identify which operators are Degraded\n"
        "2. `get_events(namespace='openshift-*')` — check operator namespace events\n"
        "3. `list_pods` in the operator's namespace — check for crash loops\n"
        "4. `get_pod_logs` for the operator pod — look for error messages\n"
        "5. Common: cert expiry, etcd issues, API server connectivity"
    ),
    "quota": (
        "### Quota / LimitRange Issues\n"
        "1. `get_resource_quotas` — check quota usage vs limits\n"
        '2. `get_events` — look for "forbidden: exceeded quota" messages\n'
        "3. Check if pods have resource requests/limits set\n"
        "4. Suggest: increase quota, add resource requests to pods, or clean up unused resources"
    ),
}

_RUNBOOK_KEYWORDS: dict[str, list[str]] = {
    "crashloop": ["crash", "crashloop", "restart", "backoff", "exit code"],
    "imagepull": ["image", "pull", "imagepull", "registry", "container image"],
    "oomkilled": ["oom", "killed", "memory", "out of memory", "137"],
    "node_notready": ["node", "notready", "not ready", "cordon", "drain"],
    "pvc_pending": ["pvc", "volume", "storage", "persistent", "pending"],
    "dns": ["dns", "resolution", "coredns", "nslookup"],
    "high_restarts": ["restart", "high restart", "container restart"],
    "deployment_stuck": ["deployment", "progressing", "rollout", "stuck", "not progressing"],
    "operator_degraded": ["operator", "degraded", "clusteroperator"],
    "quota": ["quota", "limit", "limitrange", "resourcequota", "exceeded"],
}


def select_runbooks(query: str, max_runbooks: int = 3) -> str:
    """Select relevant runbooks based on query keywords. Returns formatted text."""
    q = query.lower()
    scored: list[tuple[int, str]] = []
    for name, keywords in _RUNBOOK_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in q)
        if score > 0:
            scored.append((score, name))

    scored.sort(reverse=True)
    selected = [RUNBOOK_CHUNKS[name] for _, name in scored[:max_runbooks]]

    if not selected:
        # No match — include the 2 most common (crashloop, deployment_stuck)
        selected = [RUNBOOK_CHUNKS["crashloop"], RUNBOOK_CHUNKS["deployment_stuck"]]

    return "## Relevant Runbooks\n\n" + "\n\n".join(selected)


# Full concatenation for backward compatibility
RUNBOOKS = (
    "\n## Runbooks — Structured Diagnostic Procedures\n\n"
    "When you encounter these patterns, follow the steps systematically.\n\n"
    + "\n\n".join(RUNBOOK_CHUNKS.values())
    + "\n"
)

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
