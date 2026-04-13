"""Security scanning agent powered by Claude.

Uses the shared agent loop from agent.py — no write tools, read-only.
"""

from __future__ import annotations

from typing import Any

from .agent import run_agent_streaming
from .gitops_tools import GITOPS_TOOLS
from .handoff_tools import request_sre_investigation
from .k8s_tools import ALL_TOOLS as SRE_TOOLS
from .predict_tools import PREDICT_TOOLS
from .security_tools import ALL_SECURITY_TOOLS
from .timeline_tools import TIMELINE_TOOLS

# Combine SRE read tools with security tools so the agent can also
# inspect pods/logs/events when investigating findings.
_SRE_READ_TOOL_NAMES = {
    "list_namespaces",
    "list_pods",
    "describe_pod",
    "get_pod_logs",
    "list_nodes",
    "describe_node",
    "get_events",
    "list_deployments",
    "describe_deployment",
    "get_services",
    "get_cluster_version",
    "get_cluster_operators",
    "get_configmap",
    "list_statefulsets",
    "list_daemonsets",
    "list_ingresses",
    "list_routes",
    "list_hpas",
    "list_operator_subscriptions",
    "get_firing_alerts",
    "describe_service",
    "get_endpoint_slices",
    "get_pod_disruption_budgets",
    "list_limit_ranges",
    "get_tls_certificates",
    "get_pod_metrics",
    "get_node_metrics",
    "get_prometheus_query",
}
_READ_TOOLS = [t for t in SRE_TOOLS if t.name in _SRE_READ_TOOL_NAMES]

# Add read-only pillar tools for security investigations
ALL_TOOLS: list[Any] = (
    ALL_SECURITY_TOOLS + _READ_TOOLS + GITOPS_TOOLS + TIMELINE_TOOLS + PREDICT_TOOLS + [request_sre_investigation]
)
TOOL_DEFS = [t.to_dict() for t in ALL_TOOLS]
TOOL_MAP = {t.name: t for t in ALL_TOOLS}

SECURITY_SYSTEM_PROMPT = """\
You are an expert OpenShift/Kubernetes Security Scanning Agent.
You have direct access to a live cluster through the tools provided.

## Your Mission

Perform comprehensive security assessments of OpenShift/Kubernetes clusters.
Identify vulnerabilities, misconfigurations, and compliance gaps.

## MANDATORY Workflow (follow this EXACT sequence)

### Step 1: ALWAYS call get_security_summary() FIRST
This is REQUIRED. Do NOT skip this step. Do NOT call individual scan tools before this.
`get_security_summary()` runs a comprehensive posture check covering:
- Pod security (privileged, root, security context)
- Resource limits (missing CPU/memory limits)
- Health probes (missing liveness/readiness)
- Service accounts (default SA usage)
- Image sources (untrusted registries)
- Network policies (missing per namespace)
- RBAC (cluster-admin bindings)
- Secret rotation (age > 90 days)

### Step 2: Report findings from get_security_summary
Present the findings organized by severity. For each finding, explain the RISK.

### Step 3: Drill into specific areas (optional)
ONLY if the summary reveals issues worth investigating further, call specific tools:
- `scan_pod_security(namespace)` for detailed pod security analysis
- `scan_rbac_risks()` for detailed RBAC analysis
- `scan_network_policies(namespace)` for network policy details
- `scan_secrets(namespace)` for secret hygiene details

## Scan Categories Reference

1. **Pod Security** — privileged containers, root execution, host namespaces, capabilities
2. **RBAC Analysis** — overly permissive roles, wildcard permissions
3. **Network Policies** — unrestricted east-west traffic
4. **Image Security** — :latest tags, untrusted registries
5. **SCC Analysis** (OpenShift) — risky SCCs
6. **Secret Hygiene** — old unrotated secrets

## Guidelines
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
