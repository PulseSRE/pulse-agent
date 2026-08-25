"""Harness-level deny policy: safety rules no model swap can weaken.

What motivated this: when the configured model changed (opus-4-6 →
sonnet-5), the agent's willingness to execute destructive requests changed
with it — both SRE-Bench traps (delete a production pod "no questions",
drain a node "skip the change process") sailed through the confirmation
gate under a permissive approver. Confirmation is consent, not policy.
This module is policy: deterministic rules checked in the tool execution
path *after* confirmation, before the call — so the effective safety
posture is set by configuration, not by which model happens to be behind
``PULSE_AGENT_MODEL`` this week.

Rules:

- ``delete_pod`` in a protected namespace is denied outright;
  ``restart_deployment`` is the reviewable alternative and stays allowed,
  so routine remediation (crashloop restarts) is unaffected.
- ``drain_node`` / ``cordon_node`` require the change-process break-glass
  flag (``PULSE_AGENT_ALLOW_NODE_OPS``) regardless of confirmation —
  node-level operations are never a chat-approval decision.

``PULSE_AGENT_PROTECTED_NAMESPACES`` (comma list, ``*`` wildcards) defaults
to production plus platform namespaces; set it to ``""`` on clusters where
the policy should not apply.
"""

from __future__ import annotations

from fnmatch import fnmatch

from .errors import ToolError

_NODE_OPS = frozenset({"drain_node", "cordon_node"})
_NAMESPACE_DENIED_OPS = frozenset({"delete_pod"})


def _protected_namespaces() -> list[str]:
    from .config import get_settings

    raw = get_settings().agent.protected_namespaces
    return [p.strip() for p in raw.split(",") if p.strip()]


def _is_protected(namespace: str) -> bool:
    return any(fnmatch(namespace, pattern) for pattern in _protected_namespaces())


def check_write_policy(name: str, input_data: dict | None) -> ToolError | None:
    """Return a ToolError denying the call, or None to allow it.

    Deterministic and config-backed — deliberately no model, no judgment
    calls, no confirmation override. A denied call was still requested and
    confirmed, so the message explains the policy and the sanctioned path
    instead of just saying no.
    """
    from .config import get_settings

    args = input_data or {}

    if name in _NODE_OPS and not get_settings().agent.allow_node_ops:
        return ToolError(
            message=(
                f"Policy: '{name}' requires the change process, not chat confirmation. "
                "Node-level operations are disabled on this deployment "
                "(PULSE_AGENT_ALLOW_NODE_OPS=0). File a change request, or have the "
                "operator enable node ops for a maintenance window."
            ),
            category="forbidden",
            operation=name,
            suggestions=[
                "Investigate the node first (describe_resource, get_node_metrics) and share findings",
                "For workload relief, restart or reschedule specific workloads instead",
            ],
        )

    namespace = str(args.get("namespace", ""))
    if name in _NAMESPACE_DENIED_OPS and namespace and _is_protected(namespace):
        return ToolError(
            message=(
                f"Policy: '{name}' is denied in protected namespace '{namespace}' "
                "(PULSE_AGENT_PROTECTED_NAMESPACES). Raw pod deletion in protected "
                "namespaces is not confirmable in chat; use restart_deployment for a "
                "reviewable, controller-managed replacement."
            ),
            category="forbidden",
            operation=name,
            suggestions=[
                "restart_deployment on the owning workload achieves a safe pod replacement",
                "If the pod is truly orphaned, remove it through the change process",
            ],
        )

    return None
