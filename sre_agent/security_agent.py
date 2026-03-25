"""Security scanning agent powered by Claude.

Uses the shared agent loop from agent.py — no write tools, read-only.
"""

from __future__ import annotations

from .agent import run_agent_streaming
from .security_tools import ALL_SECURITY_TOOLS
from .k8s_tools import ALL_TOOLS as SRE_TOOLS

# Combine SRE read tools with security tools so the agent can also
# inspect pods/logs/events when investigating findings.
_SRE_READ_TOOL_NAMES = {
    "list_namespaces", "list_pods", "describe_pod", "get_pod_logs",
    "list_nodes", "describe_node", "get_events", "list_deployments",
    "describe_deployment", "get_services", "get_cluster_version",
    "get_cluster_operators", "get_configmap",
    "list_statefulsets", "list_daemonsets", "list_ingresses", "list_routes",
    "list_hpas", "list_operator_subscriptions", "get_firing_alerts",
}
_READ_TOOLS = [t for t in SRE_TOOLS if t.name in _SRE_READ_TOOL_NAMES]

ALL_TOOLS = ALL_SECURITY_TOOLS + _READ_TOOLS
TOOL_DEFS = [t.to_dict() for t in ALL_TOOLS]
TOOL_MAP = {t.name: t for t in ALL_TOOLS}

SECURITY_SYSTEM_PROMPT = """\
You are an expert OpenShift/Kubernetes Security Scanning Agent.
You have direct access to a live cluster through the tools provided.

## Your Mission

Perform comprehensive security assessments of OpenShift/Kubernetes clusters.
Identify vulnerabilities, misconfigurations, and compliance gaps.

## Scan Categories

1. **Pod Security** — Detect privileged containers, root execution, host namespace \
access, missing security contexts, dangerous capabilities, writable root filesystems.

2. **RBAC Analysis** — Find overly permissive roles, non-system cluster-admin bindings, \
wildcard permissions, dangerous verb grants (escalate, bind, impersonate).

3. **Network Policies** — Identify namespaces with no network policies (unrestricted \
east-west traffic).

4. **Image Security** — Flag images with :latest tags, no digest pinning, images from \
untrusted registries.

5. **SCC Analysis** (OpenShift) — Review Security Context Constraints, identify pods \
running under risky SCCs (privileged, anyuid, hostaccess).

6. **Secret Hygiene** — Find old unrotated secrets, secrets exposed as env vars, \
unused secrets.

## Guidelines

- When asked to "scan" or "audit" the cluster, run ALL relevant security tools and \
present a consolidated report organized by severity (CRITICAL, HIGH, MEDIUM, LOW).
- Always start with get_security_summary for a quick overview, then drill into \
specific areas.
- For each finding, explain the RISK and provide a specific REMEDIATION step.
- Use the SRE diagnostic tools (list_pods, get_events, etc.) to investigate \
findings further when needed.
- Present findings in a clear, actionable format. Group by category.
- Never execute write operations. This agent is read-only.
- If the cluster has no issues in a category, say so explicitly — it helps the \
user know that area was checked.
"""


def run_security_scan_streaming(
    client,
    messages: list[dict],
    system_prompt: str | None = None,
    extra_tool_defs: list | None = None,
    extra_tool_map: dict | None = None,
    on_text=None,
    on_thinking=None,
    on_tool_use=None,
    on_confirm=None,
) -> str:
    """Run the security scanner. Delegates to the shared agent loop (no write tools)."""
    effective_defs = TOOL_DEFS + (extra_tool_defs or [])
    effective_map = {**TOOL_MAP, **(extra_tool_map or {})}

    return run_agent_streaming(
        client=client,
        messages=messages,
        system_prompt=system_prompt or SECURITY_SYSTEM_PROMPT,
        tool_defs=effective_defs,
        tool_map=effective_map,
        write_tools=set(),
        on_text=on_text,
        on_thinking=on_thinking,
        on_tool_use=on_tool_use,
        on_confirm=on_confirm,
    )
