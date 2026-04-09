"""Scanner registry and severity constants."""

from __future__ import annotations

# ── Scanner Registry ───────────────────────────────────────────────────────

SCANNER_REGISTRY: dict[str, dict] = {
    "crashloop": {
        "displayName": "Crashlooping Pods",
        "description": "Detects pods with restart count above threshold",
        "category": "availability",
        "checks": ["restart count > threshold", "container state = CrashLoopBackOff"],
        "auto_fixable": True,
    },
    "pending": {
        "displayName": "Pending Pods",
        "description": "Finds pods stuck in Pending state for >5 minutes",
        "category": "availability",
        "checks": ["pod phase = Pending", "age > 5 minutes"],
        "auto_fixable": False,
    },
    "workloads": {
        "displayName": "Failed Deployments",
        "description": "Finds deployments with unavailable replicas",
        "category": "availability",
        "checks": ["available replicas < desired", "progressing condition"],
        "auto_fixable": True,
    },
    "nodes": {
        "displayName": "Node Pressure",
        "description": "Detects node pressure conditions and NotReady nodes",
        "category": "infrastructure",
        "checks": ["DiskPressure", "MemoryPressure", "PIDPressure", "NotReady"],
        "auto_fixable": False,
    },
    "cert_expiry": {
        "displayName": "Certificate Expiry",
        "description": "Scans TLS secrets for certificates expiring within 30 days",
        "category": "security",
        "checks": ["certificate expiry < 30 days", "already expired"],
        "auto_fixable": False,
    },
    "alerts": {
        "displayName": "Firing Alerts",
        "description": "Checks Prometheus for active firing alerts",
        "category": "monitoring",
        "checks": ["alertstate = firing", "severity mapping"],
        "auto_fixable": False,
    },
    "oom": {
        "displayName": "OOM Killed Pods",
        "description": "Finds pods terminated due to out-of-memory",
        "category": "resources",
        "checks": ["exit reason = OOMKilled", "last terminated state"],
        "auto_fixable": False,
    },
    "image_pull": {
        "displayName": "Image Pull Errors",
        "description": "Detects pods with ImagePullBackOff or ErrImagePull",
        "category": "availability",
        "checks": ["waiting reason = ImagePullBackOff", "waiting reason = ErrImagePull"],
        "auto_fixable": True,
    },
    "operators": {
        "displayName": "Degraded Operators",
        "description": "Finds ClusterOperators with Degraded condition",
        "category": "infrastructure",
        "checks": ["operator condition Degraded = True"],
        "auto_fixable": False,
    },
    "daemonsets": {
        "displayName": "DaemonSet Gaps",
        "description": "Finds DaemonSets where ready < desired",
        "category": "availability",
        "checks": ["ready count < desired count"],
        "auto_fixable": False,
    },
    "hpa": {
        "displayName": "HPA Saturation",
        "description": "Detects HPAs running at maximum replicas",
        "category": "resources",
        "checks": ["current replicas = max replicas"],
        "auto_fixable": False,
    },
    "audit_config": {
        "displayName": "Config Changes",
        "description": "Audits cluster configuration changes",
        "category": "audit",
        "checks": ["recent config modifications"],
        "auto_fixable": False,
    },
    "audit_rbac": {
        "displayName": "RBAC Changes",
        "description": "Audits RBAC permission changes",
        "category": "audit",
        "checks": ["role/binding modifications"],
        "auto_fixable": False,
    },
    "audit_deployment": {
        "displayName": "Recent Deployments",
        "description": "Tracks recent deployment activity",
        "category": "audit",
        "checks": ["deployment rollouts"],
        "auto_fixable": False,
    },
    "audit_events": {
        "displayName": "Warning Events",
        "description": "Detects warning-level Kubernetes events",
        "category": "audit",
        "checks": ["event type = Warning"],
        "auto_fixable": False,
    },
    "audit_auth": {
        "displayName": "Auth Events",
        "description": "Audits authentication and authorization events",
        "category": "audit",
        "checks": ["auth failures", "privilege escalation attempts"],
        "auto_fixable": False,
    },
    "security": {
        "displayName": "Security Posture",
        "description": "Comprehensive security check: pod security, resource limits, network policies, RBAC, service accounts",
        "category": "security",
        "checks": [
            "privileged containers",
            "missing resource limits",
            "missing health probes",
            "default service account",
            "untrusted registries",
            "missing network policies",
            "cluster-admin bindings",
            "secret rotation > 90 days",
        ],
        "auto_fixable": False,
    },
}

# ── Types ──────────────────────────────────────────────────────────────────

SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"
