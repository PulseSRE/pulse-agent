"""Agent-to-agent handoff tools.

Allow the SRE and Security agents to request investigations from each other
by publishing handoff requests to the shared context bus.
"""

from __future__ import annotations

from .decorators import beta_tool


@beta_tool
def request_security_scan(namespace: str, context: str = "") -> str:
    """Request a security scan of a namespace. Use this when you find issues that may have security implications (RBAC problems, missing network policies, suspicious pod configurations).

    Args:
        namespace: The namespace to scan.
        context: Brief description of why you're requesting the scan.
    """
    from .context_bus import ContextEntry, get_context_bus

    bus = get_context_bus()
    bus.publish(
        ContextEntry(
            source="sre_agent",
            category="handoff_request",
            summary=f"SRE agent requests security scan of namespace '{namespace}': {context}",
            details={"target": "security_agent", "namespace": namespace, "context": context},
            namespace=namespace,
        )
    )
    return f"Security scan requested for namespace '{namespace}'. The monitor will pick this up on the next scan cycle and run a security followup."


@beta_tool
def request_sre_investigation(
    namespace: str, resource_kind: str = "", resource_name: str = "", context: str = ""
) -> str:
    """Request an SRE investigation of a specific resource or namespace. Use this when you find security issues that need operational diagnosis (why is this pod running as root? why is there no network policy?).

    Args:
        namespace: The namespace to investigate.
        resource_kind: Kind of resource to investigate (e.g., 'Deployment', 'Pod').
        resource_name: Name of the specific resource.
        context: Brief description of what to investigate.
    """
    from .context_bus import ContextEntry, get_context_bus

    bus = get_context_bus()
    bus.publish(
        ContextEntry(
            source="security_agent",
            category="handoff_request",
            summary=f"Security agent requests SRE investigation: {resource_kind}/{resource_name} in '{namespace}': {context}",
            details={
                "target": "sre_agent",
                "namespace": namespace,
                "kind": resource_kind,
                "name": resource_name,
                "context": context,
            },
            namespace=namespace,
        )
    )
    return f"SRE investigation requested for {resource_kind}/{resource_name} in namespace '{namespace}'. The monitor will investigate on the next scan cycle."


# Register handoff tools in the central registry (read-only — they publish messages, not mutate cluster)
from .tool_registry import register_tool

register_tool(request_security_scan, is_write=False)
register_tool(request_sre_investigation, is_write=False)
