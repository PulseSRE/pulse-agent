"""Security scanning tools for the OpenShift SRE agent.

Focused on cluster security posture: pod security, RBAC, network policies,
SCCs, image policies, and secret hygiene.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from anthropic import beta_tool
from kubernetes.client.rest import ApiException

from .k8s_client import (
    get_core_client,
    get_apps_client,
    get_custom_client,
    get_networking_client,
    get_rbac_client,
    safe,
)


# ---------------------------------------------------------------------------
# Pod Security Scanning
# ---------------------------------------------------------------------------


@beta_tool
def scan_pod_security(namespace: str = "ALL") -> str:
    """Scan pods for security issues: privileged containers, running as root, hostNetwork/hostPID, missing security contexts, and dangerous capabilities.

    Args:
        namespace: Namespace to scan. Use 'ALL' for cluster-wide scan.
    """
    if namespace.upper() == "ALL":
        result = safe(lambda: get_core_client().list_pod_for_all_namespaces())
    else:
        result = safe(lambda: get_core_client().list_namespaced_pod(namespace))
    if isinstance(result, str):
        return result

    findings = []
    for pod in result.items:
        ns = pod.metadata.namespace
        name = pod.metadata.name
        pod_findings = []

        # Pod-level security
        spec = pod.spec
        if spec.host_network:
            pod_findings.append("hostNetwork=true")
        if spec.host_pid:
            pod_findings.append("hostPID=true")
        if spec.host_ipc:
            pod_findings.append("hostIPC=true")

        all_containers = list(spec.containers or []) + list(spec.init_containers or [])
        for c in all_containers:
            sc = c.security_context
            prefix = f"container/{c.name}"

            if sc is None:
                pod_findings.append(f"{prefix}: NO security context defined")
                continue

            if sc.privileged:
                pod_findings.append(f"{prefix}: PRIVILEGED")
            if sc.run_as_user == 0 or (sc.run_as_non_root is not True and sc.run_as_user is None):
                if sc.run_as_non_root is not True:
                    pod_findings.append(f"{prefix}: may run as root (runAsNonRoot not set)")
            if sc.allow_privilege_escalation is not False:
                pod_findings.append(f"{prefix}: allowPrivilegeEscalation not explicitly false")
            if sc.read_only_root_filesystem is not True:
                pod_findings.append(f"{prefix}: root filesystem is writable")

            # Dangerous capabilities
            if sc.capabilities and sc.capabilities.add:
                dangerous = {"SYS_ADMIN", "NET_ADMIN", "ALL", "SYS_PTRACE", "NET_RAW", "SYS_RAWIO"}
                added = set(sc.capabilities.add)
                bad = added & dangerous
                if bad:
                    pod_findings.append(f"{prefix}: dangerous capabilities: {', '.join(bad)}")

            if sc.capabilities is None or sc.capabilities.drop is None or "ALL" not in (sc.capabilities.drop or []):
                pod_findings.append(f"{prefix}: does not drop ALL capabilities")

        if pod_findings:
            findings.append(f"\n{ns}/{name}:\n  " + "\n  ".join(pod_findings))

    if not findings:
        return "No pod security issues found."
    return f"Found security issues in {len(findings)} pods:{''.join(findings)}"


@beta_tool
def scan_images(namespace: str = "ALL") -> str:
    """Scan running container images for policy violations: latest tags, no digest pinning, non-trusted registries.

    Args:
        namespace: Namespace to scan. Use 'ALL' for cluster-wide scan.
    """
    if namespace.upper() == "ALL":
        result = safe(lambda: get_core_client().list_pod_for_all_namespaces())
    else:
        result = safe(lambda: get_core_client().list_namespaced_pod(namespace))
    if isinstance(result, str):
        return result

    default_trusted = "registry.redhat.io/,registry.access.redhat.com/,quay.io/,image-registry.openshift-image-registry.svc:"
    trusted_prefixes = [
        p.strip() for p in
        os.environ.get("PULSE_AGENT_TRUSTED_REGISTRIES", default_trusted).split(",")
        if p.strip()
    ]

    findings = []
    seen_images = set()
    for pod in result.items:
        ns = pod.metadata.namespace
        name = pod.metadata.name
        all_containers = list(pod.spec.containers or []) + list(pod.spec.init_containers or [])
        for c in all_containers:
            image = c.image or ""
            if image in seen_images:
                continue
            seen_images.add(image)

            issues = []
            if ":latest" in image or ":" not in image.split("/")[-1]:
                issues.append("uses :latest or no tag")
            if "@sha256:" not in image:
                issues.append("not pinned by digest")
            if not any(image.startswith(p) for p in trusted_prefixes):
                issues.append("untrusted registry")

            pull_policy = c.image_pull_policy or ""
            if pull_policy == "Always" and "@sha256:" not in image:
                issues.append("pullPolicy=Always without digest pin")

            if issues:
                findings.append(f"{ns}/{name} [{c.name}]: {image}\n    " + ", ".join(issues))

    if not findings:
        return "No image policy violations found."
    return f"Image policy issues ({len(findings)}):\n" + "\n".join(findings)


# ---------------------------------------------------------------------------
# RBAC Analysis
# ---------------------------------------------------------------------------


@beta_tool
def scan_rbac_risks() -> str:
    """Scan for RBAC security risks: cluster-admin bindings, wildcard permissions, overly broad roles, and risky verb grants (escalate, bind, impersonate)."""
    findings = []

    # Check ClusterRoleBindings for cluster-admin
    crbs = safe(lambda: get_rbac_client().list_cluster_role_binding())
    if isinstance(crbs, str):
        return crbs

    for crb in crbs.items:
        if crb.role_ref.name == "cluster-admin":
            subjects = crb.subjects or []
            for s in subjects:
                # Skip system accounts that legitimately need cluster-admin
                if s.name and s.name.startswith("system:"):
                    continue
                findings.append(
                    f"CRITICAL: cluster-admin bound to {s.kind}/{s.name} "
                    f"(namespace={s.namespace or 'cluster'}) via ClusterRoleBinding/{crb.metadata.name}"
                )

    # Check ClusterRoles for wildcard and dangerous permissions
    crs = safe(lambda: get_rbac_client().list_cluster_role())
    if isinstance(crs, str):
        return crs

    dangerous_verbs = {"escalate", "bind", "impersonate"}
    sensitive_resources = {"secrets", "configmaps", "pods/exec", "serviceaccounts/token"}

    for cr in crs.items:
        name = cr.metadata.name
        if name.startswith("system:"):
            continue

        for rule in cr.rules or []:
            verbs = set(rule.verbs or [])
            resources = set(rule.resources or [])
            api_groups = set(rule.api_groups or [])

            if "*" in verbs and "*" in resources:
                findings.append(f"HIGH: ClusterRole/{name} has wildcard verbs AND resources (*/*)")
            elif "*" in verbs:
                findings.append(f"MEDIUM: ClusterRole/{name} has wildcard verbs on {resources}")
            elif "*" in resources:
                findings.append(f"MEDIUM: ClusterRole/{name} has wildcard resources with verbs {verbs}")

            bad_verbs = verbs & dangerous_verbs
            if bad_verbs:
                findings.append(f"HIGH: ClusterRole/{name} grants {bad_verbs} on {resources}")

            if resources & sensitive_resources and ("get" in verbs or "list" in verbs or "*" in verbs):
                findings.append(f"MEDIUM: ClusterRole/{name} allows read on sensitive resources: {resources & sensitive_resources}")

    if not findings:
        return "No significant RBAC risks found."
    return f"RBAC risks ({len(findings)}):\n" + "\n".join(f"  - {f}" for f in findings)


@beta_tool
def list_service_account_secrets(namespace: str = "default") -> str:
    """List service accounts and their auto-mounted token status in a namespace.

    Args:
        namespace: Namespace to inspect. Use 'ALL' for all namespaces.
    """
    if namespace.upper() == "ALL":
        result = safe(lambda: get_core_client().list_service_account_for_all_namespaces())
    else:
        result = safe(lambda: get_core_client().list_namespaced_service_account(namespace))
    if isinstance(result, str):
        return result

    lines = []
    for sa in result.items:
        auto_mount = sa.automount_service_account_token
        auto_str = "default(true)" if auto_mount is None else str(auto_mount)
        secret_count = len(sa.secrets or [])
        lines.append(
            f"{sa.metadata.namespace}/{sa.metadata.name}  "
            f"automountToken={auto_str}  secrets={secret_count}"
        )
    return "\n".join(lines) or "No service accounts found."


# ---------------------------------------------------------------------------
# Network Policy Analysis
# ---------------------------------------------------------------------------


@beta_tool
def scan_network_policies(namespace: str = "ALL") -> str:
    """Find namespaces with no network policies (wide-open network access) and analyze existing policies.

    Args:
        namespace: Namespace to scan. Use 'ALL' for cluster-wide.
    """
    # Get all namespaces
    ns_list = safe(lambda: get_core_client().list_namespace())
    if isinstance(ns_list, str):
        return ns_list

    skip_prefixes = ("openshift-", "kube-", "default")

    if namespace.upper() == "ALL":
        netpols = safe(lambda: get_networking_client().list_network_policy_for_all_namespaces())
    else:
        netpols = safe(lambda: get_networking_client().list_namespaced_network_policy(namespace))
    if isinstance(netpols, str):
        return netpols

    # Map namespace -> list of policies
    ns_policies: dict[str, list] = {}
    for np in netpols.items:
        ns_policies.setdefault(np.metadata.namespace, []).append(np.metadata.name)

    findings = []

    # Check which non-system namespaces lack network policies
    unprotected = []
    for ns in ns_list.items:
        name = ns.metadata.name
        if any(name.startswith(p) for p in skip_prefixes):
            continue
        if namespace.upper() != "ALL" and name != namespace:
            continue
        if name not in ns_policies:
            unprotected.append(name)

    if unprotected:
        findings.append(f"Namespaces with NO network policies ({len(unprotected)}):\n  " + "\n  ".join(unprotected))

    # Summarize existing policies
    for ns_name, policies in sorted(ns_policies.items()):
        if namespace.upper() != "ALL" and ns_name != namespace:
            continue
        findings.append(f"{ns_name}: {len(policies)} policies — {', '.join(policies)}")

    return "\n".join(findings) or "No network policy data found."


# ---------------------------------------------------------------------------
# OpenShift Security Context Constraints (SCCs)
# ---------------------------------------------------------------------------


@beta_tool
def scan_sccs() -> str:
    """List all Security Context Constraints (SCCs) and highlight risky ones that allow privileged, root, or host access. OpenShift only."""
    try:
        result = get_custom_client().list_cluster_custom_object(
            "security.openshift.io", "v1", "securitycontextconstraints"
        )
    except ApiException as e:
        return f"Error ({e.status}): {e.reason}. Is this an OpenShift cluster?"

    lines = []
    for scc in result.get("items", []):
        name = scc["metadata"]["name"]
        risks = []

        if scc.get("allowPrivilegedContainer"):
            risks.append("PRIVILEGED")
        if scc.get("allowHostNetwork"):
            risks.append("hostNetwork")
        if scc.get("allowHostPID"):
            risks.append("hostPID")
        if scc.get("allowHostIPC"):
            risks.append("hostIPC")
        if scc.get("allowHostPorts"):
            risks.append("hostPorts")
        if scc.get("allowHostDirVolumePlugin"):
            risks.append("hostPath")

        run_as = scc.get("runAsUser", {}).get("type", "?")
        se_linux = scc.get("seLinuxContext", {}).get("type", "?")
        volumes = scc.get("volumes", [])

        risk_level = "HIGH" if risks else "LOW"
        risk_str = ", ".join(risks) if risks else "none"

        users = scc.get("users", [])
        groups = scc.get("groups", [])

        lines.append(
            f"[{risk_level}] {name}\n"
            f"  Risks: {risk_str}\n"
            f"  runAsUser: {run_as}, seLinux: {se_linux}\n"
            f"  Volumes: {', '.join(volumes) if volumes else 'none'}\n"
            f"  Users: {', '.join(users[:5]) if users else 'none'}"
            f"{'...' if len(users) > 5 else ''}\n"
            f"  Groups: {', '.join(groups[:5]) if groups else 'none'}"
            f"{'...' if len(groups) > 5 else ''}"
        )

    return "\n\n".join(lines) or "No SCCs found."


@beta_tool
def scan_scc_usage(namespace: str = "ALL") -> str:
    """Show which SCC each running pod is using. Helps find pods using overly permissive SCCs. OpenShift only.

    Args:
        namespace: Namespace to scan. Use 'ALL' for cluster-wide.
    """
    if namespace.upper() == "ALL":
        result = safe(lambda: get_core_client().list_pod_for_all_namespaces())
    else:
        result = safe(lambda: get_core_client().list_namespaced_pod(namespace))
    if isinstance(result, str):
        return result

    risky_sccs = {"privileged", "anyuid", "hostaccess", "hostmount-anyuid", "hostnetwork"}
    findings = []
    scc_counts: dict[str, int] = {}

    for pod in result.items:
        annotations = pod.metadata.annotations or {}
        scc = annotations.get("openshift.io/scc", "unknown")
        scc_counts[scc] = scc_counts.get(scc, 0) + 1

        if scc in risky_sccs:
            findings.append(
                f"  {pod.metadata.namespace}/{pod.metadata.name} → SCC={scc}"
            )

    summary = "SCC usage summary:\n" + "\n".join(
        f"  {scc}: {count} pods" for scc, count in sorted(scc_counts.items(), key=lambda x: -x[1])
    )

    if findings:
        summary += f"\n\nPods using risky SCCs ({len(findings)}):\n" + "\n".join(findings[:50])
        if len(findings) > 50:
            summary += f"\n  ... and {len(findings) - 50} more"

    return summary


# ---------------------------------------------------------------------------
# Secret & Sensitive Data Scanning
# ---------------------------------------------------------------------------


@beta_tool
def scan_secrets(namespace: str = "ALL") -> str:
    """Scan for secret hygiene issues: unused secrets, secrets mounted in pods, secrets in environment variables, and very old secrets that may need rotation.

    Args:
        namespace: Namespace to scan. Use 'ALL' for cluster-wide.
    """
    if namespace.upper() == "ALL":
        secrets = safe(lambda: get_core_client().list_secret_for_all_namespaces())
        pods = safe(lambda: get_core_client().list_pod_for_all_namespaces())
    else:
        secrets = safe(lambda: get_core_client().list_namespaced_secret(namespace))
        pods = safe(lambda: get_core_client().list_namespaced_pod(namespace))
    if isinstance(secrets, str):
        return secrets
    if isinstance(pods, str):
        return pods

    # Track which secrets are referenced by pods
    referenced_secrets: set[str] = set()
    env_secrets: list[str] = []

    for pod in pods.items:
        ns = pod.metadata.namespace
        all_containers = list(pod.spec.containers or []) + list(pod.spec.init_containers or [])

        # Volume-mounted secrets
        for vol in pod.spec.volumes or []:
            if vol.secret:
                referenced_secrets.add(f"{ns}/{vol.secret.secret_name}")

        # Env var secrets
        for c in all_containers:
            for env in c.env or []:
                if env.value_from and env.value_from.secret_key_ref:
                    ref = env.value_from.secret_key_ref
                    key_name = f"{ns}/{ref.name}"
                    referenced_secrets.add(key_name)
                    env_secrets.append(
                        f"{ns}/{pod.metadata.name} [{c.name}]: env {env.name} → secret/{ref.name}.{ref.key}"
                    )

    findings = []

    # Old secrets (> 90 days)
    now = datetime.now(timezone.utc)
    old_secrets = []
    for s in secrets.items:
        if s.type in ("kubernetes.io/service-account-token", "kubernetes.io/dockercfg", "kubernetes.io/dockerconfigjson"):
            continue
        ts = s.metadata.creation_timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_days = (now - ts.astimezone(timezone.utc)).days
        if age_days > 90:
            old_secrets.append(f"  {s.metadata.namespace}/{s.metadata.name} ({age_days} days old, type={s.type})")

    if old_secrets:
        findings.append(f"Secrets older than 90 days ({len(old_secrets)}):\n" + "\n".join(old_secrets[:20]))

    if env_secrets:
        findings.append(f"Secrets exposed as env vars ({len(env_secrets)}):\n  " + "\n  ".join(env_secrets[:20]))

    # Count unreferenced secrets (excluding system types)
    all_secret_keys = set()
    for s in secrets.items:
        if s.type in ("kubernetes.io/service-account-token", "kubernetes.io/dockercfg", "kubernetes.io/dockerconfigjson"):
            continue
        all_secret_keys.add(f"{s.metadata.namespace}/{s.metadata.name}")
    unreferenced = all_secret_keys - referenced_secrets
    if unreferenced:
        findings.append(f"Potentially unused secrets ({len(unreferenced)}):\n  " + "\n  ".join(sorted(unreferenced)[:20]))

    if not findings:
        return "No secret hygiene issues found."
    return "\n\n".join(findings)


# ---------------------------------------------------------------------------
# Compliance Summary
# ---------------------------------------------------------------------------


@beta_tool
def get_security_summary() -> str:
    """Get a high-level cluster security posture summary: counts of issues by category."""
    summary = {}

    # Count pods with no security context
    pods = safe(lambda: get_core_client().list_pod_for_all_namespaces())
    if not isinstance(pods, str):
        no_sc = 0
        privileged = 0
        for pod in pods.items:
            for c in pod.spec.containers or []:
                if c.security_context is None:
                    no_sc += 1
                elif c.security_context.privileged:
                    privileged += 1
        summary["containers_no_security_context"] = no_sc
        summary["privileged_containers"] = privileged
        summary["total_pods"] = len(pods.items)

    # Count namespaces without network policies
    ns_list = safe(lambda: get_core_client().list_namespace())
    netpols = safe(lambda: get_networking_client().list_network_policy_for_all_namespaces())
    if not isinstance(ns_list, str) and not isinstance(netpols, str):
        ns_with_policies = {np.metadata.namespace for np in netpols.items}
        user_ns = [
            ns.metadata.name for ns in ns_list.items
            if not ns.metadata.name.startswith(("openshift-", "kube-", "default"))
        ]
        summary["user_namespaces"] = len(user_ns)
        summary["namespaces_without_network_policy"] = len([n for n in user_ns if n not in ns_with_policies])

    # Count cluster-admin bindings (non-system)
    crbs = safe(lambda: get_rbac_client().list_cluster_role_binding())
    if not isinstance(crbs, str):
        admin_bindings = 0
        for crb in crbs.items:
            if crb.role_ref.name == "cluster-admin":
                for s in crb.subjects or []:
                    if s.name and not s.name.startswith("system:"):
                        admin_bindings += 1
        summary["non_system_cluster_admin_bindings"] = admin_bindings

    return json.dumps(summary, indent=2)


ALL_SECURITY_TOOLS = [
    scan_pod_security,
    scan_images,
    scan_rbac_risks,
    list_service_account_secrets,
    scan_network_policies,
    scan_sccs,
    scan_scc_usage,
    scan_secrets,
    get_security_summary,
]
